#!/usr/bin/env python3
"""
HYPE Whale Tracker — приватный мониторинг кошельков через Hyperliquid Info API.

Как это работает:
1. Скрипт опрашивает бесплатный публичный API Hyperliquid (без ключей) для
   каждого адреса из wallets.json.
2. Сравнивает текущее состояние (спот-баланс HYPE и других токенов из твоего
   вотчлиста + открытые перп-позиции) с предыдущим сохранённым снимком.
3. Если изменение превышает порог (threshold_usd) — шлёт сообщение в твой
   ЛИЧНЫЙ Telegram-чат через Bot API. Никаких публичных каналов — сообщения
   идут только тебе, потому что chat_id это твой личный ID.

Настройка (см. README.md рядом):
- Впиши адреса в wallets.json
- Впиши TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID как переменные окружения
  (или в config.json для локального запуска)
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

HL_API_URL = "https://api.hyperliquid.xyz/info"
STATE_FILE = Path(__file__).parent / "last_state.json"
WALLETS_FILE = Path(__file__).parent / "wallets.json"
CONFIG_FILE = Path(__file__).parent / "config.json"


def load_json(path, default):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def hl_post(body):
    req = urllib.request.Request(
        HL_API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_spot_state(address):
    """Спот-балансы кошелька (HYPE и другие токены на Hyperliquid spot)."""
    return hl_post({"type": "spotClearinghouseState", "user": address})


def get_perp_state(address):
    """Открытые перп-позиции кошелька (лонг/шорт, размер, плечо)."""
    return hl_post({"type": "clearinghouseState", "user": address})


def get_ledger_updates(address, start_time_ms):
    """Реальные события кошелька: депозиты, выводы, внутренние переводы.
    В отличие от простого сравнения балансов, здесь видно КУДА именно
    ушли средства (destination address) — можно проверить этот адрес
    на Arkham и узнать, биржа это или чей-то личный кошелёк."""
    try:
        return hl_post({
            "type": "userNonFundingLedgerUpdates",
            "user": address,
            "startTime": start_time_ms,
        })
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"  ledger-запрос не удался для {address}: HTTP {e.code}: {body[:300]}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"  ledger-запрос не удался для {address}: {e}", file=sys.stderr)
        return []


def get_live_prices():
    """Живые mid-цены всех монет — бесплатно, без ключа. Используется вместо
    ручного ввода цен в config.json."""
    try:
        mids = hl_post({"type": "allMids"})
        return {k: float(v) for k, v in mids.items()}
    except Exception as e:
        print(f"Не удалось получить живые цены: {e}", file=sys.stderr)
        return {}


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        print(f"Telegram error: {e.read().decode()}", file=sys.stderr)
        return False


def fmt_usd(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    sign = "-" if x < 0 else ""
    x = abs(x)
    if x >= 1_000_000:
        return f"{sign}${x/1_000_000:.2f}M"
    if x >= 1_000:
        return f"{sign}${x/1_000:.1f}K"
    return f"{sign}${x:.2f}"


def snapshot_wallet(address, coins_of_interest):
    """Собирает текущий снимок: спот-баланс по нужным монетам + перп-позиции."""
    snap = {"spot": {}, "perp": []}

    try:
        spot = get_spot_state(address)
        for bal in spot.get("balances", []):
            coin = bal.get("coin")
            if coins_of_interest and coin not in coins_of_interest:
                continue
            snap["spot"][coin] = {
                "total": float(bal.get("total", 0)),
                "hold": float(bal.get("hold", 0)),
            }
    except Exception as e:
        print(f"  спот-запрос не удался для {address}: {e}", file=sys.stderr)

    try:
        perp = get_perp_state(address)
        for pos in perp.get("assetPositions", []):
            p = pos.get("position", {})
            snap["perp"].append({
                "coin": p.get("coin"),
                "szi": float(p.get("szi", 0)),          # знак = направление
                "notional_usd": float(p.get("positionValue", 0)),
                "unrealized_pnl": float(p.get("unrealizedPnl", 0)),
            })
    except Exception as e:
        print(f"  перп-запрос не удался для {address}: {e}", file=sys.stderr)

    return snap


def fmt_ledger_event(event, threshold_usd):
    """Форматирует одно событие ledger (депозит/вывод/перевод) в читаемое
    сообщение. Возвращает None, если событие ниже порога или неинтересно."""
    delta = event.get("delta", {})
    ev_type = delta.get("type", "unknown")

    RELEVANT_TYPES = {
        "deposit": "💰 ДЕПОЗИТ на Hyperliquid",
        "withdraw": "🏧 ВЫВОД с Hyperliquid",
        "internalTransfer": "↔️ ВНУТРЕННИЙ ПЕРЕВОД",
        "accountClassTransfer": "🔀 ПЕРЕВОД спот↔перп",
        "spotTransfer": "📤 SPOT-ПЕРЕВОД",
    }
    if ev_type not in RELEVANT_TYPES:
        return None

    usd = None
    for key in ("usdc", "usd", "amount"):
        if key in delta:
            try:
                usd = abs(float(delta[key]))
            except (TypeError, ValueError):
                pass
            break

    if usd is not None and usd < threshold_usd:
        return None

    lines = [RELEVANT_TYPES[ev_type]]
    if usd is not None:
        lines.append(f"Сумма: {fmt_usd(usd)}")

    # Адрес назначения — самое ценное поле для этого улучшения:
    # можно вручную проверить на Arkham, биржа это или нет
    destination = delta.get("destination") or delta.get("user")
    if destination:
        lines.append(f"Куда: <code>{destination}</code>")
        lines.append(f'<a href="https://intel.arkm.com/explorer/address/{destination}">Проверить на Arkham</a>')

    token = delta.get("token")
    if token:
        lines.append(f"Токен: {token}")

    return "\n".join(lines)


def check_ledger_activity(label, address, last_seen_ms, threshold_usd, lookback_hours=1, debug=False):
    """Проверяет реальные ledger-события (не просто изменение баланса)
    с момента последней проверки. Возвращает (сообщения, новый_timestamp)."""
    start = last_seen_ms if last_seen_ms else int(time.time() * 1000) - lookback_hours * 3600_000
    events = get_ledger_updates(address, start)

    if debug:
        print(f"  [DEBUG] {label}: тип ответа = {type(events).__name__}")
        raw_preview = json.dumps(events, ensure_ascii=False)[:500]
        print(f"  [DEBUG] {label}: сырой ответ (первые 500 символов) = {raw_preview}")

    if not isinstance(events, list):
        return [], start

    if debug and events:
        print(f"  [DEBUG] {label}: получено {len(events)} событий, первые 5:")
        for e in events[:5]:
            print(f"  [DEBUG]   {json.dumps(e, ensure_ascii=False)}")

    messages = []
    max_time = start
    for event in events:
        ev_time = event.get("time", 0)
        if ev_time <= start:
            continue
        max_time = max(max_time, ev_time)
        formatted = fmt_ledger_event(event, threshold_usd)
        if formatted:
            header = f"<b>{label}</b>\n<code>{address[:6]}...{address[-4:]}</code>\n"
            messages.append(header + formatted)

    return messages, max_time


def diff_and_alert(label, address, prev, curr, threshold_usd, coin_price_hint=None):
    alerts = []

    prev_spot = prev.get("spot", {}) if prev else {}
    curr_spot = curr.get("spot", {})
    for coin, cur_bal in curr_spot.items():
        old_total = prev_spot.get(coin, {}).get("total", 0.0)
        new_total = cur_bal["total"]
        delta = new_total - old_total
        if abs(delta) < 1e-9:
            continue
        # Грубая оценка в USD, если есть подсказка по цене — иначе просто в токенах
        usd_est = None
        if coin_price_hint and coin in coin_price_hint:
            usd_est = abs(delta) * coin_price_hint[coin]
        if usd_est is not None and usd_est < threshold_usd:
            continue
        direction = "🟢 ПРИХОД" if delta > 0 else "🔴 УХОД"
        line = f"{direction} {coin}: {delta:+.4f} (стало {new_total:.4f})"
        if usd_est is not None:
            line += f" ≈ {fmt_usd(usd_est if delta > 0 else -usd_est)}"
        alerts.append(line)

    prev_perp = {p["coin"]: p for p in (prev.get("perp", []) if prev else [])}
    curr_perp = {p["coin"]: p for p in curr.get("perp", [])}
    all_coins = set(prev_perp) | set(curr_perp)
    for coin in all_coins:
        old = prev_perp.get(coin, {"szi": 0.0, "notional_usd": 0.0})
        new = curr_perp.get(coin, {"szi": 0.0, "notional_usd": 0.0})
        old_notional = old.get("notional_usd", 0.0)
        new_notional = new.get("notional_usd", 0.0)
        delta_notional = new_notional - old_notional
        if abs(delta_notional) < threshold_usd:
            continue
        old_dir = "лонг" if old.get("szi", 0) > 0 else ("шорт" if old.get("szi", 0) < 0 else "нет")
        new_dir = "лонг" if new.get("szi", 0) > 0 else ("шорт" if new.get("szi", 0) < 0 else "нет")
        alerts.append(
            f"⚡ ПЕРП {coin}: {old_dir}→{new_dir}, "
            f"объём {fmt_usd(old_notional)}→{fmt_usd(new_notional)} "
            f"({delta_notional:+.0f}$)"
        )

    if not alerts:
        return None

    header = f"<b>{label}</b>\n<code>{address[:6]}...{address[-4:]}</code>\n"
    return header + "\n".join(alerts)


def main():
    wallets = load_json(WALLETS_FILE, [])
    if not wallets:
        print("wallets.json пуст — добавь адреса для отслеживания.")
        return

    config = load_json(CONFIG_FILE, {})
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or config.get("telegram_bot_token")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or config.get("telegram_chat_id")
    threshold_usd = config.get("threshold_usd", 20000)
    coins_of_interest = config.get("coins_of_interest")  # None = все монеты
    debug_ledger = config.get("debug_ledger", False)
    ledger_lookback_hours = config.get("ledger_lookback_hours", 1)
    price_hint = get_live_prices()  # живые цены с Hyperliquid, бесплатно
    if not price_hint:
        price_hint = config.get("price_hint", {})  # запасной вариант из конфига

    if not token or not chat_id:
        print("Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID. См. README.md")
        sys.exit(1)

    state = load_json(STATE_FILE, {})
    new_state = {}
    sent_any = False

    for w in wallets:
        address = w["address"]
        label = w.get("label", address[:8])
        print(f"Проверяю {label} ({address[:10]}...)")

        curr = snapshot_wallet(address, coins_of_interest)
        new_state[address] = curr

        prev = state.get(address)
        if prev is not None:
            msg = diff_and_alert(label, address, prev, curr, threshold_usd, price_hint)
            if msg:
                print("  -> изменение баланса найдено, отправляю в Telegram")
                send_telegram(token, chat_id, msg)
                sent_any = True
        else:
            print("  первый прогон для этого адреса — снимок сохранён, без алерта")

        # Проверяем реальные события (депозит/вывод/перевод) — отдельно
        # от простого сравнения балансов, чтобы видеть КУДА уходят средства
        last_ledger_ms = state.get(f"{address}__ledger_ts")
        ledger_msgs, new_ledger_ts = check_ledger_activity(
            label, address, last_ledger_ms, threshold_usd,
            lookback_hours=ledger_lookback_hours, debug=debug_ledger
        )
        new_state[f"{address}__ledger_ts"] = new_ledger_ts
        for msg in ledger_msgs:
            print("  -> событие перевода найдено, отправляю в Telegram")
            send_telegram(token, chat_id, msg)
            sent_any = True

        time.sleep(0.3)  # вежливая пауза между запросами

    save_json(STATE_FILE, new_state)

    if not sent_any:
        print("Изменений выше порога не найдено.")


if __name__ == "__main__":
    main()
