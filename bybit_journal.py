#!/usr/bin/env python3
"""
Bybit Trading Journal — дописывает закрытые сделки прямо в Google-таблицу
"TARS Trading Journal" (лист "📋 Журнал"), через Google Sheets API.

Как это работает:
1. Раз в день дёргает Bybit V5 API (READ-ONLY ключ) — закрытые позиции
   за последние ~lookback_hours часов.
2. Для каждой новой сделки (дедуп по orderId, храним в bybit_state.json
   в приватном whale_tracker_data) находит первую пустую строку в
   "Журнале" и заполняет ТОЛЬКО объективные поля: №, Дата, Время входа,
   Пара, Направление, ТВХ, Объём $, Результат $, Комментарий.
3. Поля СЛ/ТП/ТФ/Паттерн/Эмоции НЕ трогает — это твои ручные поля.
   Формулы R:R/Результат%/Итог в таблице уже стоят заранее на 1000 строк
   вперёд — их тоже не трогаем, они посчитаются сами.

Настройка (whale_tracker_data/bybit_config.json):
{
  "category": "linear",
  "lookback_hours": 26,
  "spreadsheet_id": "1C2KT7txAuaHuHYNRkoJwXBqO-MmDFTEy",
  "sheet_name": "📋 Журнал"
}

Секреты (GitHub):
- BYBIT_API_KEY, BYBIT_API_SECRET — read-only ключ Bybit
- GOOGLE_SERVICE_ACCOUNT_JSON — содержимое JSON-ключа сервисного аккаунта
- DATA_REPO_TOKEN — для чтения/записи bybit_state.json (дедуп)
"""

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

BYBIT_BASE = "https://api.bybit.com"
SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/spreadsheets"

STATE_FILE = Path(__file__).parent / "data" / "bybit_state.json"
CONFIG_FILE = Path(__file__).parent / "data" / "bybit_config.json"

FIXED_UTC_OFFSET = timedelta(hours=2)  # его конвенция "(UTC+2)" во всех прошлых записях


def load_json(path, default):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------- Bybit ----------

def proxy_fetch_json(worker_url, proxy_secret, target_url, headers):
    """Отправляет запрос через Cloudflare Worker-прокси (обход геоблока Bybit
    для IP GitHub Actions). Воркер сам сходит на target_url и вернёт ответ."""
    payload = json.dumps({"url": target_url, "method": "GET", "headers": headers}).encode("utf-8")
    req = urllib.request.Request(worker_url, data=payload, method="POST")
    req.add_header("X-Proxy-Secret", proxy_secret)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def bybit_get_closed_pnl(api_key, api_secret, category, start_ms, end_ms,
                          worker_url, proxy_secret, limit=50):
    params = {
        "category": category,
        "startTime": str(start_ms),
        "endTime": str(end_ms),
        "limit": str(limit),
    }
    query_string = urllib.parse.urlencode(params)
    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"
    sign_payload = timestamp + api_key + recv_window + query_string
    signature = hmac.new(api_secret.encode(), sign_payload.encode(), hashlib.sha256).hexdigest()

    target_url = f"{BYBIT_BASE}/v5/position/closed-pnl?{query_string}"
    headers = {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": signature,
        "X-BAPI-RECV-WINDOW": recv_window,
    }

    data = proxy_fetch_json(worker_url, proxy_secret, target_url, headers)

    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit API ошибка: {data.get('retCode')} {data.get('retMsg')}")

    return data.get("result", {}).get("list", [])


# ---------- Google Sheets (через прямой REST + подпись JWT сервисного аккаунта) ----------

def b64url(data: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def get_access_token(service_account_info):
    """Получаем access token через OAuth2 JWT-flow сервисного аккаунта.
    Используем библиотеку cryptography (тянется вместе с google-auth) для RSA-подписи."""
    from google.oauth2 import service_account as gsa
    from google.auth.transport.requests import Request as GRequest

    creds = gsa.Credentials.from_service_account_info(service_account_info, scopes=[SCOPE])
    creds.refresh(GRequest())
    return creds.token


def sheets_get(token, spreadsheet_id, range_a1):
    url = f"{SHEETS_BASE}/{spreadsheet_id}/values/{urllib.parse.quote(range_a1)}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def sheets_batch_update(token, spreadsheet_id, data_ranges):
    """data_ranges: список {"range": "...", "values": [[...]]}"""
    url = f"{SHEETS_BASE}/{spreadsheet_id}/values:batchUpdate"
    payload = json.dumps({
        "valueInputOption": "USER_ENTERED",
        "data": data_ranges,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"  [SHEETS] ОШИБКА HTTP {e.code}: {body[:400]}", file=sys.stderr)
        return None


def find_next_empty_row(token, spreadsheet_id, sheet_name, first_row=3, last_row=1002):
    """Смотрит колонку B (Дата) и ищет первую пустую строку."""
    range_a1 = f"'{sheet_name}'!B{first_row}:B{last_row}"
    result = sheets_get(token, spreadsheet_id, range_a1)
    values = result.get("values", [])
    row = first_row + len(values)
    return row


# ---------- Форматирование под конвенцию журнала ----------

def symbol_to_pair(symbol):
    for quote in ("USDT", "USDC", "USD"):
        if symbol.endswith(quote):
            return f"{symbol[:-len(quote)]}/{quote}"
    return symbol


def fmt_date_no_leading_zeros(dt):
    return f"{dt.day}.{dt.month}.{dt.year}"


def fmt_num(x, digits=4):
    try:
        return round(float(x), digits)
    except (TypeError, ValueError):
        return x


def main():
    api_key = os.environ.get("BYBIT_API_KEY")
    api_secret = os.environ.get("BYBIT_API_SECRET")
    gsa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    worker_url = os.environ.get("CF_WORKER_URL")
    proxy_secret = os.environ.get("CF_PROXY_SECRET")

    if not api_key or not api_secret:
        print("Не заданы BYBIT_API_KEY / BYBIT_API_SECRET.")
        sys.exit(1)
    if not gsa_json:
        print("Не задан GOOGLE_SERVICE_ACCOUNT_JSON.")
        sys.exit(1)
    if not worker_url or not proxy_secret:
        print("Не заданы CF_WORKER_URL / CF_PROXY_SECRET (прокси для обхода геоблока Bybit).")
        sys.exit(1)

    config = load_json(CONFIG_FILE, {})
    category = config.get("category", "linear")
    lookback_hours = config.get("lookback_hours", 26)
    spreadsheet_id = config.get("spreadsheet_id")
    sheet_name = config.get("sheet_name", "📋 Журнал")

    if not spreadsheet_id:
        print("В bybit_config.json не задан spreadsheet_id.")
        sys.exit(1)

    state = load_json(STATE_FILE, {"processed_order_ids": [], "next_number": None})
    processed_ids = set(state.get("processed_order_ids", []))

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - lookback_hours * 3600_000

    try:
        trades = bybit_get_closed_pnl(api_key, api_secret, category, start_ms, now_ms, worker_url, proxy_secret)
    except Exception as e:
        print(f"Не удалось получить сделки с Bybit: {e}", file=sys.stderr)
        sys.exit(1)

    new_trades = [t for t in trades if t.get("orderId") and t.get("orderId") not in processed_ids]
    print(f"Найдено закрытых позиций за окно: {len(trades)}, новых: {len(new_trades)}")

    if not new_trades:
        print("Новых сделок нет — таблицу не трогаем.")
        return

    token = get_access_token(json.loads(gsa_json))

    next_row = find_next_empty_row(token, spreadsheet_id, sheet_name)
    next_number = state.get("next_number") or (next_row - 2)  # № = строка - 2 (данные с row 3 = №1)

    new_ids = []
    for trade in new_trades:
        order_id = trade["orderId"]

        symbol = trade.get("symbol", "")
        pair = symbol_to_pair(symbol)
        side_raw = trade.get("side", "")
        direction = "Long" if side_raw == "Buy" else ("Short" if side_raw == "Sell" else side_raw)

        created_ms = int(trade.get("createdTime", 0))
        updated_ms = int(trade.get("updatedTime", 0))
        entry_dt = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc) + FIXED_UTC_OFFSET
        exit_dt = datetime.fromtimestamp(updated_ms / 1000, tz=timezone.utc) + FIXED_UTC_OFFSET

        entry_price = fmt_num(trade.get("avgEntryPrice"))
        exit_price = fmt_num(trade.get("avgExitPrice"))
        volume_usd = fmt_num(trade.get("cumEntryValue") or (
            float(trade.get("qty", 0)) * float(trade.get("avgEntryPrice", 0))
        ), 2)
        pnl = fmt_num(trade.get("closedPnl"), 2)

        comment = f"Bybit import: выход {exit_price}, close {exit_dt.strftime('%H:%M')} (UTC+2)"

        # A:E — №, Дата, Время входа, Пара, Направление
        row_a_e = [next_number, fmt_date_no_leading_zeros(entry_dt), entry_dt.strftime("%H:%M:%S"), pair, direction]
        # G — ТВХ (цена входа)
        row_g = [entry_price]
        # K:L — Объём $, Результат $
        row_k_l = [volume_usd, pnl]
        # Q — Комментарий
        row_q = [comment]

        data_ranges = [
            {"range": f"'{sheet_name}'!A{next_row}:E{next_row}", "values": [row_a_e]},
            {"range": f"'{sheet_name}'!G{next_row}", "values": [row_g]},
            {"range": f"'{sheet_name}'!K{next_row}:L{next_row}", "values": [row_k_l]},
            {"range": f"'{sheet_name}'!Q{next_row}", "values": [row_q]},
        ]

        result = sheets_batch_update(token, spreadsheet_id, data_ranges)
        if result:
            print(f"  -> строка {next_row} (№{next_number}) записана: {pair} {direction} {pnl}")
            new_ids.append(order_id)
            next_row += 1
            next_number += 1
        else:
            print(f"  -> НЕ записано (order_id={order_id}), попробуем в следующий раз")

    processed_ids.update(new_ids)
    state["processed_order_ids"] = list(processed_ids)[-1000:]
    state["next_number"] = next_number
    save_json(STATE_FILE, state)

    print(f"Готово. Записано новых сделок: {len(new_ids)}")


if __name__ == "__main__":
    main()
