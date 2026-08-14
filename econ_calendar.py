#!/usr/bin/env python3
"""
Economic Calendar — точечные напоминания в Telegram перед важными
макрособытиями (CPI, FOMC, NFP и т.п.).

Источник: бесплатный публичный JSON-фид Forex Factory, без ключа:
https://nfs.faireconomy.media/ff_calendar_thisweek.json

Как это работает:
Скрипт запускается часто (раз в 10-15 минут, через cron-job.org — как
остальные трекеры). На каждом прогоне смотрит: у какого события до
начала осталось <= reminder_before_minutes? Если такое событие ещё не
напоминалось — шлёт сообщение и запоминает его id, чтобы не продублировать
на следующем прогоне.

Настройка (whale_tracker_data/econ_config.json):
{
  "countries": ["USD"],          // список валют для фильтра, ["*"] = все
  "min_impact": "High",          // "High" или "Medium" (Medium включает High)
  "reminder_before_minutes": 60  // за сколько минут напоминать
}
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
STATE_FILE = Path(__file__).parent / "data" / "econ_state.json"
CONFIG_FILE = Path(__file__).parent / "data" / "econ_config.json"

IMPACT_RANK = {"Low": 0, "Medium": 1, "High": 2}


def load_json(path, default):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_calendar():
    req = urllib.request.Request(
        CALENDAR_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; econ-calendar-bot/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_event_time(raw_date):
    # Формат в фиде — ISO 8601 со смещением, например "2026-08-18T12:30:00-04:00"
    return datetime.fromisoformat(raw_date)


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
            print(f"[TELEGRAM] HTTP {resp.status}")
            return resp.status == 200
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[TELEGRAM] ОШИБКА HTTP {e.code}: {body[:300]}", file=sys.stderr)
        return False


def main():
    config = load_json(CONFIG_FILE, {})
    countries = set(config.get("countries", ["USD"]))
    min_impact = config.get("min_impact", "High")
    reminder_before_minutes = config.get("reminder_before_minutes", 60)

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.")
        sys.exit(1)

    try:
        events = fetch_calendar()
    except Exception as e:
        print(f"Не удалось получить календарь: {e}", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(timezone.utc)
    min_rank = IMPACT_RANK.get(min_impact, 2)

    state = load_json(STATE_FILE, {"reminded_ids": []})
    reminded_ids = set(state.get("reminded_ids", []))

    due = []
    for e in events:
        try:
            ev_time = parse_event_time(e.get("date", ""))
        except Exception:
            continue
        if ev_time.tzinfo is None:
            ev_time = ev_time.replace(tzinfo=timezone.utc)
        ev_time_utc = ev_time.astimezone(timezone.utc)

        minutes_until = (ev_time_utc - now).total_seconds() / 60
        # Событие уже прошло или ещё слишком далеко — не наш случай
        if minutes_until < 0 or minutes_until > reminder_before_minutes:
            continue
        if "*" not in countries and e.get("country") not in countries:
            continue
        if IMPACT_RANK.get(e.get("impact", "Low"), 0) < min_rank:
            continue

        ev_id = f"{e.get('title')}|{e.get('country')}|{e.get('date')}"
        if ev_id in reminded_ids:
            continue  # уже напоминали про это событие

        due.append((minutes_until, ev_time_utc, e, ev_id))

    due.sort(key=lambda x: x[0])

    if not due:
        print("Событий в окне напоминания сейчас нет.")
        return

    new_ids = []
    for minutes_until, ev_time_utc, e, ev_id in due:
        time_str = ev_time_utc.strftime("%d.%m %H:%M UTC")
        impact_icon = "🔴" if e.get("impact") == "High" else "🟠"
        forecast = e.get("forecast") or "—"
        previous = e.get("previous") or "—"
        text = (
            f"🔔 <b>Через ~{int(minutes_until)} мин</b>\n"
            f"{impact_icon} {time_str} [{e.get('country')}] {e.get('title')}\n"
            f"Прогноз: {forecast} | Пред.: {previous}"
        )
        send_telegram(token, chat_id, text)
        new_ids.append(ev_id)
        print(f"[REMINDER] {e.get('title')} через {int(minutes_until)} мин")

    reminded_ids.update(new_ids)
    state["reminded_ids"] = list(reminded_ids)[-500:]  # не даём файлу расти бесконечно
    save_json(STATE_FILE, state)
    print(f"Отправлено напоминаний: {len(new_ids)}")


if __name__ == "__main__":
    main()
