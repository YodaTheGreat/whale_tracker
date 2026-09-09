#!/usr/bin/env python3
"""
digest.py — утренняя сводка ночных несрочных сигналов из night_log.json.
Запускается раз в сутки в 07:00 Europe/Madrid через отдельный workflow
(digest.yml, только workflow_dispatch + внешний cron на cron-job.org).
После отправки — очищает night_log.json.
"""

import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Madrid")
NIGHT_LOG_FILE = Path(__file__).parent / "data" / "night_log.json"


def load_json(path, default):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=15)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы")

    log = load_json(NIGHT_LOG_FILE, [])

    if not log:
        text = "☀️ <b>Утренняя сводка</b>\nЗа ночь ничего примечательного — тишина по вотчлисту."
    else:
        lines = ["☀️ <b>Утренняя сводка</b>", f"За ночь: {len(log)} несрочных сигналов.\n"]
        for entry in log:
            ts = datetime.fromtimestamp(entry["ts"], tz=timezone.utc).astimezone(TZ)
            lines.append(f"{ts.strftime('%H:%M')} — {entry['text']}")
        text = "\n".join(lines)

    send_telegram(token, chat_id, text)
    save_json(NIGHT_LOG_FILE, [])


if __name__ == "__main__":
    main()
