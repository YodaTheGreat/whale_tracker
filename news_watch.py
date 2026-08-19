import json
import os
import time
from pathlib import Path
from datetime import datetime, timezone
import urllib.request
import urllib.parse

CRYPTOPANIC_KEY = os.environ["CRYPTOPANIC_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

DATA_DIR = Path("data")
CONFIG_PATH = DATA_DIR / "news_config.json"
STATE_PATH = DATA_DIR / "news_state.json"

FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CRYPTOPANIC_URL = "https://cryptopanic.com/api/v1/posts/"


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    urllib.request.urlopen(url, data=data, timeout=15)


def fetch_cryptopanic(currencies):
    params = {
        "auth_token": CRYPTOPANIC_KEY,
        "currencies": ",".join(currencies),
        "kind": "news",
    }
    url = CRYPTOPANIC_URL + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read())


def fetch_calendar():
    try:
        with urllib.request.urlopen(FF_CALENDAR_URL, timeout=20) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"calendar fetch failed: {e}")
        return []


def main():
    config = load_json(CONFIG_PATH, {
        "tickers": ["HYPE", "WLD", "NEAR", "ONDO", "TAO", "LIT", "LINK"],
        "min_votes_important": 3,
        "macro_keywords": ["Fed", "FOMC", "CPI", "Interest Rate", "SEC", "CFTC"],
        "macro_impact_min": "High",
    })
    state = load_json(STATE_PATH, {"seen_news_ids": [], "seen_calendar_ids": []})

    seen_news = set(state.get("seen_news_ids", []))
    seen_cal = set(state.get("seen_calendar_ids", []))

    # --- Токен-новости ---
    try:
        news_data = fetch_cryptopanic(config["tickers"])
        for post in news_data.get("results", []):
            post_id = str(post["id"])
            if post_id in seen_news:
                continue
            seen_news.add(post_id)
            votes = post.get("votes", {})
            important = votes.get("important", 0) + votes.get("liked", 0)
            currencies = [c["code"] for c in post.get("currencies", [])]
            title = post.get("title", "")
            source = post.get("source", {}).get("title", "?")
            link = post.get("url", "")
            tag = "🔥" if important >= config["min_votes_important"] else "📰"
            msg = f"{tag} <b>{'/'.join(currencies) or '?'}</b>\n{title}\n{source} | {link}"
            send_telegram(msg)
            time.sleep(1)
    except Exception as e:
        print(f"cryptopanic error: {e}")

    # --- Макрокалендарь ---
    try:
        events = fetch_calendar()
        for ev in events:
            impact = ev.get("impact", "")
            if impact.lower() != config["macro_impact_min"].lower():
                continue
            title = ev.get("title", "")
            if config["macro_keywords"] and not any(
                k.lower() in title.lower() for k in config["macro_keywords"]
            ):
                continue
            ev_id = f"{title}_{ev.get('date')}"
            if ev_id in seen_cal:
                continue
            seen_cal.add(ev_id)
            msg = (
                f"📅 <b>{title}</b>\n"
                f"{ev.get('country','')} | {ev.get('date','')} {ev.get('time','')}\n"
                f"Impact: {impact}"
            )
            send_telegram(msg)
            time.sleep(1)
    except Exception as e:
        print(f"calendar error: {e}")

    state["seen_news_ids"] = list(seen_news)[-500:]
    state["seen_calendar_ids"] = list(seen_cal)[-200:]
    save_json(STATE_PATH, state)


if __name__ == "__main__":
    main()
