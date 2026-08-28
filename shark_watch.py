#!/usr/bin/env python3
"""
Shark Tracker — поиск среднесрочных/интрадей крупных трейдеров (в отличие от китов,
которые сидят в позиции неделями и почти не меняют баланс).

Логика (в отличие от whale-трекера, который сравнивает СНИМКИ баланса):
1. Для каждого адреса из sharks_wallets.json запрашиваем userFillsByTime — это реальные
   ИСПОЛНЕННЫЕ сделки (не намерения), с timestamp, стороной, размером, ценой.
2. Восстанавливаем упрощённый журнал позиции по (адрес, монета): следим за бегущим
   размером (running size). Buy = +sz, Sell = -sz.
3. Момент, когда running size переходит через ноль (или уходит в ноль) после того как
   был ненулевым — это ЗАКРЫТИЕ позиции (round-trip). Holding time = время между первым
   fill'ом, который открыл позицию, и fill'ом, который её закрыл.
4. Если holding_hours <= max_holding_hours из конфига — это акулий паттерн, а не китовый.
   Шлём алерт в Telegram.
5. Считаем также, сколько таких round-trip'ов адрес сделал за rolling-окно
   (frequency_window_hours). Несколько подряд = уверенно "акула", а не разовая случайность
   (пришлось закрыть позицию по margin call и т.п.) — тег 🦈 ставится только при
   min_roundtrips_for_shark_tag и выше.
6. Состояние (открытые позиции по каждому адрес+монета, последний обработанный fill)
   хранится в shark_state.json — как и в whale-трекере, коммитится обратно в репозиторий
   через GitHub Actions.

Секреты (те же, что уже настроены для whale-бота): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.

--- ВРЕМЕННАЯ ДИАГНОСТИКА (TARS, 27 авг) ---
Добавлены print() для поиска причины 2-недельной тишины по акулам:
- сколько сырых fill'ов вернул API по каждому адресу
- что реально пришло, если ответ не list (подозрение на смену формата API)
- сколько fill'ов отфильтровано по вотчлисту / min_trade_usd
- сводка round-trip'ов и алертов за прогон
После того как найдём причину — эти print() можно смело убрать обратно.
"""

import json
import os
import time
import urllib.request
from pathlib import Path

API_URL = "https://api.hyperliquid.xyz/info"
STATE_FILE = Path("data/shark_state.json")
CONFIG_FILE = Path("data/sharks_config.json")
WALLETS_FILE = Path("data/sharks_wallets.json")


def load_json(path, default):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def api_post(body):
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_user_fills(address, start_time_ms):
    """Реальные исполненные сделки адреса начиная с start_time_ms."""
    body = {"type": "userFillsByTime", "user": address, "startTime": start_time_ms}
    try:
        result = api_post(body)
    except Exception as e:
        print(f"[WARN] userFills failed for {address}: {e}")
        return []

    if not isinstance(result, list):
        # ВРЕМЕННО: раньше это молча возвращало [] без единого следа в логах.
        # Если сюда попали — вот она, причина тишины: формат ответа API не list.
        print(f"[DEBUG] {address}: ответ userFillsByTime НЕ список! "
              f"type={type(result)} содержимое (обрезано): {str(result)[:300]}")
        return []

    return result


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        print(f"[DEBUG] telegram send OK, status={resp.status}")
    except Exception as e:
        print(f"[WARN] telegram send failed: {e}")


def fmt_duration(seconds):
    h = seconds / 3600
    if h < 1:
        return f"{seconds/60:.0f} мин"
    return f"{h:.1f} ч"


def main():
    config = load_json(CONFIG_FILE, {
        "max_holding_hours": 48,
        "min_trade_usd": 20000,
        "lookback_hours_first_run": 72,
        "frequency_window_hours": 168,
        "min_roundtrips_for_shark_tag": 2,
        "watchlist_coins": ["HYPE", "WLD", "NEAR", "ONDO", "TAO", "LIT", "LINK"],
        "alert_on_open": True,
    })
    watchlist = set(config.get("watchlist_coins", []))
    wallets = load_json(WALLETS_FILE, [])
    state = load_json(STATE_FILE, {})

    print(f"[DEBUG] Кошельков в списке: {len(wallets)}. "
          f"Вотчлист: {sorted(watchlist)}. "
          f"min_trade_usd={config['min_trade_usd']}, max_holding_hours={config['max_holding_hours']}")

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы")

    now_ms = int(time.time() * 1000)
    total_alerts_sent = 0

    for w in wallets:
        address = w["address"]
        label = w.get("label", address[:10])

        wstate = state.setdefault(address, {
            "last_fill_time": now_ms - config["lookback_hours_first_run"] * 3600 * 1000,
            "open_positions": {},  # coin -> {"size": float, "open_time": ms}
            "roundtrips": [],      # [{"coin":, "close_time":, "holding_h":}]
        })

        age_hours = (now_ms - wstate["last_fill_time"]) / 3600000
        print(f"[DEBUG] {label} ({address[:10]}...): "
              f"last_fill_time = {wstate['last_fill_time']} ({age_hours:.1f}ч назад от сейчас)")

        fills = get_user_fills(address, wstate["last_fill_time"])
        print(f"[DEBUG] {label}: сырых fill'ов от API получено = {len(fills)}")

        if not fills:
            print(f"[DEBUG] {label}: fills пустой — last_fill_time НЕ обновится в этом прогоне")
            continue

        fills = sorted(fills, key=lambda f: f.get("time", 0))

        skipped_not_watchlist = 0
        skipped_small = 0
        opened_count = 0
        closed_count = 0

        for fill in fills:
            coin = fill.get("coin")
            side = fill.get("side")  # "B" = buy, "A" = sell
            sz = float(fill.get("sz", 0))
            px = float(fill.get("px", 0))
            fill_time = fill.get("time", now_ms)
            notional = sz * px

            # только монеты из вотчлиста (пропускаем акции типа xyz:PLTR и всё лишнее)
            if watchlist and coin not in watchlist:
                skipped_not_watchlist += 1
                continue

            if notional < config["min_trade_usd"]:
                skipped_small += 1
                continue

            signed_sz = sz if side == "B" else -sz
            pos = wstate["open_positions"].get(coin)

            if pos is None:
                wstate["open_positions"][coin] = {"size": signed_sz, "open_time": fill_time}
                opened_count += 1
                if config.get("alert_on_open", True):
                    direction = "LONG" if signed_sz > 0 else "SHORT"
                    msg = (
                        f"<b>🐋 ОТКРЫТИЕ</b>\n"
                        f"Кошелёк: {label}\n"
                        f"Токен: {coin}\n"
                        f"Направление: {direction}\n"
                        f"Объём: ${notional:,.0f}\n"
                        f"Цена входа: {px}\n"
                        f"https://hypurrscan.io/address/{address}"
                    )
                    send_telegram(token, chat_id, msg)
                    total_alerts_sent += 1
                continue

            new_size = pos["size"] + signed_sz
            closed = (pos["size"] > 0 and new_size <= 0) or (pos["size"] < 0 and new_size >= 0)

            if closed:
                closed_count += 1
                holding_seconds = (fill_time - pos["open_time"]) / 1000
                holding_h = holding_seconds / 3600

                wstate["roundtrips"].append({"coin": coin, "close_time": fill_time, "holding_h": holding_h})
                cutoff = now_ms - config["frequency_window_hours"] * 3600 * 1000
                wstate["roundtrips"] = [r for r in wstate["roundtrips"] if r["close_time"] >= cutoff]

                if holding_h <= config["max_holding_hours"]:
                    recent_count = len([r for r in wstate["roundtrips"] if r["coin"] == coin])
                    tag = "🦈 АКУЛА" if recent_count >= config["min_roundtrips_for_shark_tag"] else "быстрое закрытие"
                    direction = "LONG → flat" if pos["size"] > 0 else "SHORT → flat"
                    msg = (
                        f"<b>{tag}</b>\n"
                        f"Кошелёк: {label}\n"
                        f"Токен: {coin}\n"
                        f"{direction}, holding: {fmt_duration(holding_seconds)}\n"
                        f"Объём закрытия: ${notional:,.0f}\n"
                        f"Round-trip'ов по {coin} за {config['frequency_window_hours']}ч: {recent_count}\n"
                        f"https://hypurrscan.io/address/{address}"
                    )
                    send_telegram(token, chat_id, msg)
                    total_alerts_sent += 1
                else:
                    print(f"[DEBUG] {label}: round-trip по {coin} закрыт, "
                          f"но holding_h={holding_h:.1f} > max_holding_hours={config['max_holding_hours']} — не акула")

                if abs(new_size) > 1e-9:
                    wstate["open_positions"][coin] = {"size": new_size, "open_time": fill_time}
                else:
                    del wstate["open_positions"][coin]
            else:
                wstate["open_positions"][coin]["size"] = new_size

        print(f"[DEBUG] {label}: обработано fill'ов={len(fills)}, "
              f"пропущено (не вотчлист)={skipped_not_watchlist}, "
              f"пропущено (мелкая сумма < min_trade_usd)={skipped_small}, "
              f"открытий={opened_count}, закрытий={closed_count}")

        wstate["last_fill_time"] = fills[-1].get("time", wstate["last_fill_time"]) + 1

    print(f"[DEBUG] Итого алертов отправлено за прогон: {total_alerts_sent}")
    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
