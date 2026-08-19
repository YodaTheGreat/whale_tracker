import json
import os
import re
import time
from pathlib import Path
from datetime import datetime, timezone
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

DATA_DIR = Path("data")
CONFIG_PATH = DATA_DIR / "news_config.json"
STATE_PATH = DATA_DIR / "news_state.json"

FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

RSS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://decrypt.co/feed",
    "https://www.theblock.co/rss.xml",
]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NewsWatchBot/1.0)"}

DEFAULT_CONFIG = {
    "ticker_keywords": {
        "HYPE": ["hyperliquid", "hype"],
        "WLD": ["worldcoin", "world chain"],
        "NEAR": ["near protocol"],
        "ONDO": ["ondo finance", "ondo"],
        "TAO": ["bittensor"],
        "LIT": ["lighter dex", "lighter exchange", "lighter protocol", "lighter perp", "$lit", "lit token"],
        "LINK": ["chainlink"],
    },
    "macro_keywords": ["Fed", "FOMC", "CPI", "Interest Rate", "SEC", "CFTC", "White House", "Trump"],
    "macro_impact_min": "High",
}


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
    req = urllib.request.Request(url, data=data, headers=HEADERS)
    urllib.request.urlopen(req, timeout=15)


def fetch_rss(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
    root = ET.fromstring(raw)
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        if title and link:
            items.append({"title": title, "link": link, "desc": desc})
    return items


def fetch_calendar():
    try:
        req = urllib.request.Request(FF_CALENDAR_URL, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"calendar fetch failed: {e}")
        return []


def matches_keywords(text, keywords):
    text_low = text.lower()
    for kw in keywords:
        kw_low = kw.lower()
        if kw_low.startswith("$"):
            if kw_low in text_low:
                return kw
        else:
            if re.search(r"\b" + re.escape(kw_low) + r"\b", text_low):
                return kw
    return None


def main():
    config = load_json(CONFIG_PATH, DEFAULT_CONFIG)
    state = load_json(STATE_PATH, {"seen_links": [], "seen_calendar_ids": []})

    seen_links = set(state.get("seen_links", []))
    seen_cal = set(state.get("seen_calendar_ids", []))

    ticker_keywords = config["ticker_keywords"]
    macro_keywords = config.get("macro_keywords", [])

    # --- Токен-новости и макро через RSS ---
    for feed_url in RSS_FEEDS:
        try:
            items = fetch_rss(feed_url)
        except Exception as e:
            print(f"feed error {feed_url}: {e}")
            continue

        for item in items:
            link = item["link"]
            if link in seen_links:
                continue
            full_text = f"{item['title']} {item['desc']}"

            hit_ticker = None
            hit_kw = None
            for ticker, kws in ticker_keywords.items():
                kw = matches_keywords(full_text, kws)
                if kw:
                    hit_ticker = ticker
                    hit_kw = kw
                    break

            hit_macro = None
            if not hit_ticker:
                hit_macro = matches_keywords(full_text, macro_keywords)

            if not hit_ticker and not hit_macro:
                continue

            seen_links.add(link)
            if hit_ticker:
                msg = f"📰 <b>{hit_ticker}</b> ({hit_kw})\n{item['title']}\n{link}"
            else:
                msg = f"🏛 <b>MACRO</b> ({hit_macro})\n{item['title']}\n{link}"
            send_telegram(msg)
            time.sleep(1)

    # --- Макрокалендарь ---
    try:
        events = fetch_calendar()
        for ev in events:
            impact =
