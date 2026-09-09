#!/usr/bin/env python3
"""
market_signals.py — мониторинг "агрегированных умных денег" через публичные
данные Hyperliquid: funding rate, open interest, движение цены и просадка
ликвидности (через OI как прокси).

Добавлено (v3):
1. price_move_pct за скользящее окно price_window_minutes — теперь считается
   как ДИАПАЗОН (high/low внутри окна), а не разница "было/стало". Ловит
   спайки, которые уже частично откатились к моменту проверки.
2. liquidity_drop_pct — просадка OI относительно медианы за то же окно.
3. Ночной режим: срочные сигналы (движение цены, ликвидность) — сразу.
   Несрочные — в night_log.json для утренней сводки (digest.py).
4. Сканер ВСЕГО рынка Hyperliquid (не только твоего вотчлиста, config["symbols"]) —
   ловит резкие 5-минутные движения по любой монете, которой у тебя нет
   в списке (например DOT). Порог выше (market_wide_move_pct), т.к. без
   этого будет шуметь на мусорных монетах с низкой ликвидностью.
5. Cooldown (alert_cooldown_minutes) на срочные алерты — чтобы одно и то же
   движение не долбило в Telegram каждые 5 минут, пока диапазон остаётся
   широким.

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
    "alert_cooldown_minutes": 30,   # не повторять один и тот же срочный алерт чаще этого
    "market_wide_move_pct": 10.0,   # порог для монет ВНЕ твоего вотчлиста (весь Hyperliquid)
    "market_wide_min_oi_usd": 5_000_000,  # отсекаем низколиквидные монеты (OI как прокси капы)
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
        last_price_alert_ts = prev.get("last_price_alert_ts", 0)
        last_liquidity_alert_ts = prev.get("last_liquidity_alert_ts", 0)

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

        # --- Сигнал 4 (СРОЧНЫЙ): диапазон движения цены за price_window_minutes ---
        # Считаем high/low ВСЕГО окна (включая текущую точку), а не просто
        # "было час назад vs сейчас" — так ловим спайки, которые уже откатились.
        if len(price_history) >= 2:
            window_prices = [p for _, p in price_history] + [mark_price]
            w_high, w_low = max(window_prices), min(window_prices)
            if w_low > 0:
                range_pct = (w_high - w_low) / w_low * 100
                is_tier1 = symbol in config.get("price_move_tier1_symbols", [])
                move_threshold = config["price_move_tier1_pct"] if is_tier1 else config["price_move_tier2_pct"]
                cooldown_ok = now_ts - last_price_alert_ts >= config["alert_cooldown_minutes"] * 60
                if range_pct >= move_threshold and cooldown_ok:
                    direction = "ПАМП 🚀" if mark_price >= (w_high + w_low) / 2 else "ДАМП 💥"
                    urgent_messages.append(
                        f"🚨 <b>{direction} — диапазон за {config['price_window_minutes']} мин</b>\n"
                        f"Токен: {symbol}\nРазмах: {range_pct:+.2f}% (порог {move_threshold}%)\n"
                        f"High: ${w_high:,.4f} / Low: ${w_low:,.4f}\nСейчас: ${mark_price:,.4f}"
                    )
                    last_price_alert_ts = now_ts

        # --- Сигнал 5 (СРОЧНЫЙ): просадка ликвидности (OI vs медиана за окно) ---
        if len(oi_history) >= 3 and oi_usd > 0:
            oi_values = [v for _, v in oi_history]
            med = median(oi_values)
            if med > 0:
                drop_pct = (med - oi_usd) / med * 100
                cooldown_ok = now_ts - last_liquidity_alert_ts >= config["alert_cooldown_minutes"] * 60
                if drop_pct >= config["liquidity_drop_pct"] and cooldown_ok:
                    urgent_messages.append(
                        f"🚨 <b>Просадка ликвидности (OI)</b>\n"
                        f"Токен: {symbol}\nOI упал на {drop_pct:.1f}% от медианы за {config['price_window_minutes']} мин\n"
                        f"OI сейчас: ${oi_usd:,.0f}\nМедиана: ${med:,.0f}"
                    )
                    last_liquidity_alert_ts = now_ts

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
            "last_price_alert_ts": last_price_alert_ts,
            "last_liquidity_alert_ts": last_liquidity_alert_ts,
        }

    # --- Сканер ВСЕГО рынка Hyperliquid: ловим резкие движения по монетам,
    # которых нет в твоём личном вотчлисте (например DOT). Лёгкий — храним
    # только предыдущую цену, не полную часовую историю, чтобы не раздувать
    # state.json на сотни инструментов. Порог выше, чтобы не шуметь на мусоре.
    watchlist_set = set(config["symbols"])
    universe_prev = state.get("_universe_prev", {})
    universe_next = {}
    universe_alert_ts = state.get("_universe_alert_ts", {})

    for asset in universe:
        name = asset["name"]
        if name in watchlist_set:
            continue  # это уже покрыто детальной логикой выше
        idx = name_to_index.get(name)
        if idx is None:
            continue
        ctx = asset_ctxs[idx]
        price = float(ctx.get("markPx", 0))
        if price <= 0:
            continue

        open_interest = float(ctx.get("openInterest", 0))
        oi_usd = open_interest * price
        if oi_usd < config["market_wide_min_oi_usd"]:
            continue  # низкая капа/ликвидность — не интересно, отсекаем шум

        universe_next[name] = price

        prev_price = universe_prev.get(name)
        if not prev_price or prev_price <= 0:
            continue

        change_pct = (price - prev_price) / prev_price * 100
        last_ts = universe_alert_ts.get(name, 0)
        cooldown_ok = now_ts - last_ts >= config["alert_cooldown_minutes"] * 60
        if abs(change_pct) >= config["market_wide_move_pct"] and cooldown_ok:
            direction = "ПАМП 🚀" if change_pct > 0 else "ДАМП 💥"
            msg = (
                f"🚨 <b>{direction} — монета НЕ из вотчлиста</b>\n"
                f"Токен: {name}\nИзменение: {change_pct:+.2f}% за 5 мин\n"
                f"Цена сейчас: ${price:,.4f}\nБыло: ${prev_price:,.4f}"
            )
            print(f"  -> СРОЧНЫЙ алерт (вне вотчлиста): {msg[:50]}...")
            send_telegram(token, chat_id, msg)
            universe_alert_ts[name] = now_ts

    state["_universe_prev"] = universe_next
    state["_universe_alert_ts"] = universe_alert_ts

    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
