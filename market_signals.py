#!/usr/bin/env python3
"""
market_signals.py — мониторинг "агрегированных умных денег" через публичные
данные Binance Futures: funding rate и open interest.

В отличие от whale/shark трекеров (которые следят за конкретными кошельками
на Hyperliquid), этот скрипт смотрит на рынок в целом:

1. Funding rate — стоимость удержания позиции с плечом. Сильно отрицательный
   funding = толпа перегружена шортами и платит лонгам за удержание позиции —
   часто (не всегда) сигнал возможного разворота вверх (шорт-сквиз).
   Сильно положительный — обратная логика, толпа перегружена лонгами.
2. Open Interest (OI) — суммарный объём открытых позиций. Резкий скачок за
   короткий интервал = крупный приток или отток капитала, часто предшествует
   волатильному движению.

Источник: Hyperliquid public API (api.hyperliquid.xyz/info), без ключа,
бесплатно. (Изначально пробовались Binance и Bybit, но обе биржи блокируют
запросы с IP облачных провайдеров вроде GitHub Actions — HTTP 451/403.
Hyperliquid таких ограничений не имеет — тот же источник, что уже успешно
использует whale/shark трекер.)

Состояние (предыдущее значение OI для расчёта % изменения) хранится в
market_state.json — как и у shark/whale трекеров, коммитится в приватный
data-репозиторий через GitHub Actions.

Секреты: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (те же, что у остальных ботов).
"""

import json
import os
import time
import urllib.request
from pathlib import Path

INFO_URL = "https://api.hyperliquid.xyz/info"

STATE_FILE = Path(__file__).parent / "data" / "market_state.json"
CONFIG_FILE = Path(__file__).parent / "data" / "market_config.json"


def load_json(path, default):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def http_get(url, params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    full_url = f"{url}?{query}"
    req = urllib.request.Request(full_url, headers={"User-Agent": "Mozilla/5.0"}, method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
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


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"[WARN] telegram send failed: {e}")


def main():
    config = load_json(CONFIG_FILE, {
        "symbols": ["LINK"],
        "funding_extreme_pct": 0.01,      # Hyperliquid funding обычно расчитывается за 1 час, пороги меньше, чем на CEX за 8ч
        "oi_change_alert_pct": 3.0,
        "min_oi_usd_to_alert": 5_000_000,
        "majors": ["BTC", "ETH"],          # для этих монет используется price_change_majors_pct
        "price_change_majors_pct": 1.5,    # % движение цены за 5 мин для BTC/ETH
        "price_change_alts_pct": 3.0       # % движение цены за 5 мин для остальных монет из symbols
    })
    state = load_json(STATE_FILE, {})

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы")

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

        messages = []

        # --- Сигнал 1: экстремальный funding rate ---
        if abs(funding_rate_pct) >= config["funding_extreme_pct"]:
            direction = "толпа в ШОРТАХ (funding отрицательный)" if funding_rate_pct < 0 else "толпа в ЛОНГАХ (funding положительный)"
            messages.append(
                f"⚡ <b>Экстремальный funding rate</b>\n"
                f"Токен: {symbol}\n"
                f"Funding: {funding_rate_pct:+.4f}%\n"
                f"{direction}\n"
                f"Цена: ${mark_price:,.4f}"
            )

        # --- Сигнал 2: резкий скачок OI ---
        if prev_oi_usd and prev_oi_usd > 0 and oi_usd >= config["min_oi_usd_to_alert"]:
            oi_change_pct = (oi_usd - prev_oi_usd) / prev_oi_usd * 100
            if abs(oi_change_pct) >= config["oi_change_alert_pct"]:
                direction = "OI РАСТЁТ" if oi_change_pct > 0 else "OI ПАДАЕТ"
                messages.append(
                    f"📊 <b>Резкое изменение Open Interest</b>\n"
                    f"Токен: {symbol}\n"
                    f"{direction}: {oi_change_pct:+.2f}% за 5 мин\n"
                    f"OI сейчас: ${oi_usd:,.0f}\n"
                    f"Было: ${prev_oi_usd:,.0f}"
                )

        # --- Сигнал 3: резкое % движение цены за 5 мин ---
        prev_mark_price = prev.get("mark_price")
        if prev_mark_price and prev_mark_price > 0 and mark_price > 0:
            price_change_pct = (mark_price - prev_mark_price) / prev_mark_price * 100
            is_major = symbol in config.get("majors", [])
            threshold = (
                config["price_change_majors_pct"] if is_major
                else config["price_change_alts_pct"]
            )
            if abs(price_change_pct) >= threshold:
                direction = "РОСТ ЦЕНЫ 📈" if price_change_pct > 0 else "ПАДЕНИЕ ЦЕНЫ 📉"
                messages.append(
                    f"🚨 <b>Резкое движение цены</b>\n"
                    f"Токен: {symbol}\n"
                    f"{direction}: {price_change_pct:+.2f}% за 5 мин\n"
                    f"Цена сейчас: ${mark_price:,.4f}\n"
                    f"Было: ${prev_mark_price:,.4f}"
                )

        for msg in messages:
            print(f"  -> отправляю алерт: {msg[:50]}...")
            send_telegram(token, chat_id, msg)

        state[symbol] = {
            "oi_usd": oi_usd,
            "funding_rate_pct": funding_rate_pct,
            "mark_price": mark_price,
            "updated_at": int(time.time()),
        }

    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
