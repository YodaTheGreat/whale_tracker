#!/usr/bin/env python3
"""
market_signals.py — мониторинг "агрегированных умных денег" через публичные
данные Hyperliquid: funding rate, open interest, движение цены и просадка
ликвидности (через OI как прокси).

Добавлено (v2):
1. price_move_pct за скользящее окно price_window_minutes (напр. 8% за 60 мин)
   — rolling buffer цены в state, не просто 5-минутная дельта.
2. liquidity_drop_pct — просадка OI относительно медианы за то же окно
   (прокси на "исчезновение ликвидности", реальной глубины стакана
   Hyperliquid info-эндпоинт не отдаёт).
3. Ночной режим (window_start–window_end, таймзона Europe/Madrid,
   учитывает CEST/CET автоматически): срочные сигналы (движение цены,
   просадка ликвидности) — как и раньше, сразу в Telegram с 🚨.
   Несрочные (funding extreme, OI 5-мин скачок) — НЕ шлются сразу,
   а дописываются в night_log.json для утренней сводки (digest.py).
   Днём (вне окна) — всё шлётся сразу, как в v1, ничего не меняется.

Источник: Hyperliquid public API (api.hyperliquid.xyz/info), без ключа.
Состояние — в market_state.json, ночной лог — в night_log.json,
оба коммитятся в приватный data-репозиторий через GitHub Actions.

Секреты: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
"""

import json
import os
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

INFO_URL = "https://api.hyperliquid.xyz/info"
TZ = ZoneInfo("Europe/Madrid")

STATE_FILE = Path(__file__).parent / "data" / "market_state.json"
CONFIG_FILE = Path(__file__).parent / "data" / "market_config.json"
NIGHT_LOG_FILE = Path(__file__).parent / "data" / "night_log.json"

DEFAULT_CONFIG = {
    "symbols": ["LINK", "HYPE", "WLD", "NEAR", "ONDO", "TAO", "LIT", "XRP", "ZEC"],
    "funding_extreme_pct": 0.01,
    "oi_change_alert_pct": 3.0,
    "min_oi_usd_to_alert": 5_000_000,
    "majors": ["BTC", "ETH"],
    "price_change_majors_pct": 1.5,
    "price_change_alts_pct": 3.0,
    # --- новое ---
    "price_window_minutes": 60,
    "price_move_tier1_symbols": ["XRP", "HYPE"],  # самые ликвидные/интересные для тебя — порог ниже
    "price_move_tier1_pct": 3.0,
    "price_move_tier2_pct": 5.0,    # LINK, NEAR, ONDO, TAO, LIT, ZEC и всё остальное
    "liquidity_drop_pct": 40.0,     # срочный сигнал: просадка OI относительно медианы за то же окно
    "night_window_start": "22:00",  # локальное время Europe/Madrid
    "night_window_end": "07:00",
}


def load_json(path, default):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def http_post(url, body):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"[WARN] telegram send failed: {e}")


def is_night_window(config, now_dt):
    start_h, start_m = map(int, config["night_window_start"].split(":"))
    end_h, end_m = map(int, config["night_window_end"].split(":"))
    start = now_dt.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = now_dt.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    t = now_dt
    if start <= end:
        return start <= t <= end
    # окно переходит через полночь (22:00 -> 07:00)
    return t >= start or t <= end


def prune_history(history, window_seconds, now_ts):
    return [[ts, val] for ts, val in history if now_ts - ts <= window_seconds]


def median(values):
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def append_night_log(entry_text):
    log = load_json(NIGHT_LOG_FILE, [])
    log.append({"ts": int(time.time()), "text": entry_text})
    save_json(NIGHT_LOG_FILE, log)


def main():
    config = load_json(CONFIG_FILE, DEFAULT_CONFIG)
    # добираем недостающие ключи, если конфиг старый
    for k, v in DEFAULT_CONFIG.items():
        config.setdefault(k, v)

    state = load_json(STATE_FILE, {})

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы")

    now_dt = datetime.now(TZ)
    now_ts = int(time.time())
    night = is_night_window(config, now_dt)
    window_seconds = config["price_window_minutes"] * 60

    try:
        meta, asset_ctxs = http_post(INFO_URL, {"type": "metaAndAssetCtxs"})
    except Exception as e:
        print(f"[WARN] не удалось получить metaAndAssetCtxs: {e}")
        return

    universe = meta.get("universe", [])
    name_to_index = {a["name"]: i for i, a in enumerate(universe)}

    for symbol in config["symbols"]:
        print(f"Проверяю {symbol}...")

        idx = name_to_index.get(symbol)
        if idx is None:
            print(f"  [WARN] монета {symbol} не найдена в Hyperliquid universe")
            continue

        ctx = asset_ctxs[idx]
        funding_rate_pct = float(ctx.get("funding", 0)) * 100
        mark_price = float(ctx.get("markPx", 0))
        open_interest = float(ctx.get("openInterest", 0))
        oi_usd = open_interest * mark_price

        prev = state.get(symbol, {})
        prev_oi_usd = prev.get("oi_usd")
        prev_mark_price = prev.get("mark_price")

        # rolling история цены и OI
        price_history = prune_history(prev.get("price_history", []), window_seconds, now_ts)
        oi_history = prune_history(prev.get("oi_history", []), window_seconds, now_ts)

        urgent_messages = []
        background_messages = []

        # --- Сигнал 1 (фоновый): экстремальный funding rate ---
        if abs(funding_rate_pct) >= config["funding_extreme_pct"]:
            direction = "толпа в ШОРТАХ (funding отрицательный)" if funding_rate_pct < 0 else "толпа в ЛОНГАХ (funding положительный)"
            background_messages.append(
                f"⚡ <b>Экстремальный funding rate</b>\n"
                f"Токен: {symbol}\nFunding: {funding_rate_pct:+.4f}%\n{direction}\nЦена: ${mark_price:,.4f}"
            )

        # --- Сигнал 2 (фоновый): резкий скачок OI за 5 мин ---
        if prev_oi_usd and prev_oi_usd > 0 and oi_usd >= config["min_oi_usd_to_alert"]:
            oi_change_pct = (oi_usd - prev_oi_usd) / prev_oi_usd * 100
            if abs(oi_change_pct) >= config["oi_change_alert_pct"]:
                direction = "OI РАСТЁТ" if oi_change_pct > 0 else "OI ПАДАЕТ"
                background_messages.append(
                    f"📊 <b>Резкое изменение Open Interest</b>\n"
                    f"Токен: {symbol}\n{direction}: {oi_change_pct:+.2f}% за 5 мин\n"
                    f"OI сейчас: ${oi_usd:,.0f}\nБыло: ${prev_oi_usd:,.0f}"
                )

        # --- Сигнал 3 (фоновый): резкое движение цены за 5 мин (старая логика) ---
        if prev_mark_price and prev_mark_price > 0 and mark_price > 0:
            price_change_5m_pct = (mark_price - prev_mark_price) / prev_mark_price * 100
            is_major = symbol in config.get("majors", [])
            threshold = config["price_change_majors_pct"] if is_major else config["price_change_alts_pct"]
            if abs(price_change_5m_pct) >= threshold:
                direction = "РОСТ ЦЕНЫ 📈" if price_change_5m_pct > 0 else "ПАДЕНИЕ ЦЕНЫ 📉"
                background_messages.append(
                    f"🔹 <b>Движение цены за 5 мин</b>\n"
                    f"Токен: {symbol}\n{direction}: {price_change_5m_pct:+.2f}% за 5 мин\n"
                    f"Цена сейчас: ${mark_price:,.4f}\nБыло: ${prev_mark_price:,.4f}"
                )

        # --- Сигнал 4 (СРОЧНЫЙ): движение цены за price_window_minutes ---
        if price_history and mark_price > 0:
            oldest_ts, oldest_price = price_history[0]
            if oldest_price > 0:
                window_change_pct = (mark_price - oldest_price) / oldest_price * 100
                is_tier1 = symbol in config.get("price_move_tier1_symbols", [])
                move_threshold = config["price_move_tier1_pct"] if is_tier1 else config["price_move_tier2_pct"]
                if abs(window_change_pct) >= move_threshold:
                    direction = "ПАМП 🚀" if window_change_pct > 0 else "ДАМП 💥"
                    urgent_messages.append(
                        f"🚨 <b>{direction} — движение цены за {config['price_window_minutes']} мин</b>\n"
                        f"Токен: {symbol}\nИзменение: {window_change_pct:+.2f}% (порог {move_threshold}%)\n"
                        f"Цена сейчас: ${mark_price:,.4f}\nБыло ~{config['price_window_minutes']}м назад: ${oldest_price:,.4f}"
                    )

        # --- Сигнал 5 (СРОЧНЫЙ): просадка ликвидности (OI vs медиана за окно) ---
        if len(oi_history) >= 3 and oi_usd > 0:
            oi_values = [v for _, v in oi_history]
            med = median(oi_values)
            if med > 0:
                drop_pct = (med - oi_usd) / med * 100
                if drop_pct >= config["liquidity_drop_pct"]:
                    urgent_messages.append(
                        f"🚨 <b>Просадка ликвидности (OI)</b>\n"
                        f"Токен: {symbol}\nOI упал на {drop_pct:.1f}% от медианы за {config['price_window_minutes']} мин\n"
                        f"OI сейчас: ${oi_usd:,.0f}\nМедиана: ${med:,.0f}"
                    )

        # --- Отправка / логирование ---
        for msg in urgent_messages:
            print(f"  -> СРОЧНЫЙ алерт: {msg[:50]}...")
            send_telegram(token, chat_id, msg)

        for msg in background_messages:
            if night:
                print(f"  -> ночь, фоновый сигнал в лог: {msg[:50]}...")
                append_night_log(f"[{symbol}] " + msg.split('\n', 1)[1].replace('\n', ' | '))
            else:
                print(f"  -> день, отправляю как раньше: {msg[:50]}...")
                send_telegram(token, chat_id, msg)

        # обновляем историю
        price_history.append([now_ts, mark_price])
        oi_history.append([now_ts, oi_usd])

        state[symbol] = {
            "oi_usd": oi_usd,
            "funding_rate_pct": funding_rate_pct,
            "mark_price": mark_price,
            "updated_at": now_ts,
            "price_history": price_history,
            "oi_history": oi_history,
        }

    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
