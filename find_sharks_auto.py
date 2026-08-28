#!/usr/bin/env python3
"""
find_sharks_auto.py — автоматическая версия find_sharks_v2.py для еженедельного
прогона через GitHub Actions.

В отличие от find_sharks_v2.py (запускался вручную, печатал JSON в консоль для
ручной вставки), этот скрипт:
1. Читает ТЕКУЩИЙ sharks_wallets.json из data-репозитория.
2. Ищет новых кандидатов через leaderboard + userFillsByTime (та же логика, что в v2).
3. Пропускает адреса, которые УЖЕ есть в списке (по адресу, без учёта регистра).
4. ДОБАВЛЯЕТ новых (максимум MAX_NEW_PER_RUN штук за прогон) — существующие записи
   не трогает и не удаляет.
5. Если нашли хотя бы одного нового — шлёт сводку в Telegram и сохраняет файл.
   Если новых нет — просто печатает в лог, файл не трогает, коммита не будет.

Работает внутри workflow, где data-репозиторий уже checkout'нут в data/ (как и в
остальных трекерах) — пути к файлам через data/, не относительно текущей папки.
"""

import json
import os
import time
import urllib.request
from pathlib import Path

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO_URL = "https://api.hyperliquid.xyz/info"

WALLETS_FILE = Path("data/sharks_wallets.json")

MIN_ACCOUNT_VALUE = 100_000
MAX_ACCOUNT_VALUE = 2_000_000
WINDOW = "day"
MIN_RATIO = 15.0
MAX_ROI_FOR_BOT_FILTER = 3.0
MAX_CANDIDATES_TO_CHECK = 80     # сколько верхних по ratio проверять через API за прогон
FILLS_LOOKBACK_DAYS = 5
WATCHLIST_COINS = {"HYPE", "WLD", "NEAR", "ONDO", "TAO", "LIT", "LINK"}
MAX_NEW_PER_RUN = 5               # потолок, чтобы список не раздувался бесконтрольно
SLEEP_BETWEEN_REQUESTS = 0.5      # было 0.1 — ловили HTTP 429 после ~45 запросов подряд


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post(url, body):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_json(path, default):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"[WARN] telegram send failed: {e}")


def get_window_stats(row, window):
    for entry in row.get("windowPerformances", []):
        if isinstance(entry, list) and len(entry) == 2 and entry[0] == window:
            return entry[1]
    return None


def fetch_candidates(existing_addresses):
    print("Забираю leaderboard...")
    data = http_get(LEADERBOARD_URL)
    rows = data.get("leaderboardRows", [])
    print(f"Всего адресов в leaderboard: {len(rows)}")

    candidates = []
    for row in rows:
        address = row.get("ethAddress")
        if not address or address.lower() in existing_addresses:
            continue  # уже в списке — пропускаем сразу, не тратим лимит API на проверку

        try:
            account_value = float(row.get("accountValue", 0))
        except (TypeError, ValueError):
            continue
        if not (MIN_ACCOUNT_VALUE <= account_value <= MAX_ACCOUNT_VALUE):
            continue

        stats = get_window_stats(row, WINDOW)
        if not stats:
            continue
        try:
            volume = float(stats.get("vlm", 0))
            roi = float(stats.get("roi", 0)) * 100
        except (TypeError, ValueError):
            continue
        if account_value <= 0:
            continue

        ratio = volume / account_value
        if ratio < MIN_RATIO:
            continue
        if ratio > 100 and abs(roi) < MAX_ROI_FOR_BOT_FILTER:
            continue

        candidates.append({
            "address": address, "account_value": account_value,
            "volume": volume, "roi_pct": roi, "ratio": ratio,
        })

    candidates.sort(key=lambda c: c["ratio"], reverse=True)
    return candidates[:MAX_CANDIDATES_TO_CHECK]


def address_trades_watchlist(address):
    start_ms = int((time.time() - FILLS_LOOKBACK_DAYS * 86400) * 1000)
    try:
        fills = http_post(INFO_URL, {
            "type": "userFillsByTime", "user": address, "startTime": start_ms
        })
    except Exception as e:
        print(f"  [WARN] userFills failed for {address}: {e}")
        return set()

    if not isinstance(fills, list):
        print(f"  [DEBUG] {address}: ответ userFillsByTime не список, type={type(fills)}")
        return set()

    coins_traded = {f.get("coin") for f in fills if f.get("coin")}
    return coins_traded & WATCHLIST_COINS


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы")

    wallets = load_json(WALLETS_FILE, [])
    existing_addresses = {w["address"].lower() for w in wallets if w.get("address")}
    print(f"Сейчас в списке {len(wallets)} адресов")

    candidates = fetch_candidates(existing_addresses)
    print(f"Новых кандидатов после фильтра по депозиту/ratio: {len(candidates)}")
    print(f"Проверяю через userFillsByTime (вотчлист: {sorted(WATCHLIST_COINS)})...")

    found = []
    for i, c in enumerate(candidates, 1):
        matched_coins = address_trades_watchlist(c["address"])
        status = f"✓ {sorted(matched_coins)}" if matched_coins else "—"
        print(f"[{i}/{len(candidates)}] {c['address']}  "
              f"ratio={c['ratio']:.1f}x roi={c['roi_pct']:.1f}%  {status}")

        if matched_coins:
            c["matched_coins"] = ",".join(sorted(matched_coins))
            found.append(c)

        if len(found) >= MAX_NEW_PER_RUN:
            print(f"Достигнут потолок {MAX_NEW_PER_RUN} новых за прогон — останавливаюсь раньше времени.")
            break

        time.sleep(SLEEP_BETWEEN_REQUESTS)

    if not found:
        print("Новых подходящих акул не найдено в этом прогоне. Файл не трогаю, коммита не будет.")
        return

    today = time.strftime("%Y-%m-%d", time.gmtime())
    new_entries = []
    for f in found:
        new_entries.append({
            "address": f["address"],
            "label": f"Акула (авто, {f['matched_coins']}) [найдена {today}]",
        })

    wallets.extend(new_entries)  # только добавляем, существующие записи не трогаем
    save_json(WALLETS_FILE, wallets)
    print(f"Добавлено {len(new_entries)} новых адресов. Всего в списке теперь: {len(wallets)}")

    lines = "\n".join(
        f"• <code>{e['address']}</code>\n  {e['label']}" for e in new_entries
    )
    msg = (
        f"<b>🔍 Автопоиск акул: найдено {len(new_entries)} новых</b>\n\n"
        f"{lines}\n\n"
        f"Всего адресов в списке: {len(wallets)}"
    )
    send_telegram(token, chat_id, msg)


if __name__ == "__main__":
    main()
