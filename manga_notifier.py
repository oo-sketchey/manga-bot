#!/usr/bin/env python3
"""
MangaDex -> Discord notifier
Posts to a Discord channel (via webhook) whenever a new ENGLISH chapter
of a tracked manga appears on MangaDex.

Usage:
    python manga_notifier.py          # run forever, checking every POLL_MINUTES
    python manga_notifier.py --once   # check once and exit (for cron / Task Scheduler)
    python manga_notifier.py --test   # post the latest chapter of each series once, to test
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# ======================= CONFIG =======================

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "PASTE_WEBHOOK_URL_HERE")

MANGA = {
    "801513ba-a712-498c-8f57-cae55b38cc92": "Berserk",
    "ab8cbb82-d0ed-45a7-8ffe-4bfa6d43d79c": "JoJo Part 9: The JOJOLands",
    "7f30dfc3-0b80-4dcc-a3b9-0cd746fac005": "Detective Conan",
}

# Optional role ping, e.g. "<@&123456789012345678>". Leave "" for no ping.
ROLE_PING = ""

POLL_MINUTES = 15
STATE_FILE = Path(__file__).with_name("seen_chapters.json")

# MangaDex requires an honest User-Agent that identifies your app.
USER_AGENT = "DiscordMangaNotifier/1.0 (private Discord server)"

# ======================================================

API = "https://api.mangadex.org"
http = requests.Session()
http.headers["User-Agent"] = USER_AGENT
covers = {}


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def chapter_key(ch):
    # Track by chapter number so multiple groups uploading the same chapter only ping once.
    num = ch["attributes"].get("chapter")
    return f"ch:{num}" if num else f"id:{ch['id']}"


def describe(ch):
    a = ch["attributes"]
    parts = []
    if a.get("volume"):
        parts.append(f"Vol. {a['volume']}")
    parts.append(f"Ch. {a['chapter']}" if a.get("chapter") else "Extra")
    return " ".join(parts)


def fetch_english_chapters(manga_id):
    params = {
        "translatedLanguage[]": "en",
        "order[readableAt]": "desc",
        "limit": 20,
        "includes[]": "scanlation_group",
        "contentRating[]": ["safe", "suggestive", "erotica", "pornographic"],
        "includeFutureUpdates": "0",
    }
    r = http.get(f"{API}/manga/{manga_id}/feed", params=params, timeout=30)
    r.raise_for_status()
    return r.json()["data"]


def get_cover(manga_id):
    if manga_id in covers:
        return covers[manga_id]
    try:
        r = http.get(f"{API}/manga/{manga_id}", params={"includes[]": "cover_art"}, timeout=30)
        r.raise_for_status()
        for rel in r.json()["data"]["relationships"]:
            if rel["type"] == "cover_art" and rel.get("attributes"):
                url = f"https://uploads.mangadex.org/covers/{manga_id}/{rel['attributes']['fileName']}.256.jpg"
                covers[manga_id] = url
                return url
    except (requests.RequestException, ValueError, KeyError):
        pass
    return None


def post_to_discord(manga_id, name, ch):
    a = ch["attributes"]
    groups = [
        rel["attributes"]["name"]
        for rel in ch.get("relationships", [])
        if rel["type"] == "scanlation_group" and rel.get("attributes")
    ]
    embed = {
        "title": f"{name} — {describe(ch)}",
        "url": a.get("externalUrl") or f"https://mangadex.org/chapter/{ch['id']}",
        "color": 0xFF6740,
        "footer": {"text": "New English chapter on MangaDex"},
    }
    if a.get("title"):
        embed["description"] = a["title"]
    if groups:
        embed["fields"] = [{"name": "Translated by", "value": ", ".join(groups), "inline": True}]
    if a.get("readableAt"):
        embed["timestamp"] = a["readableAt"]
    cover = get_cover(manga_id)
    if cover:
        embed["thumbnail"] = {"url": cover}

    payload = {"embeds": [embed]}
    if ROLE_PING:
        payload["content"] = ROLE_PING

    for _ in range(3):
        r = http.post(WEBHOOK_URL, json=payload, timeout=30)
        if r.status_code == 429:  # Discord rate limit: wait and retry
            time.sleep(float(r.json().get("retry_after", 2)))
            continue
        if r.ok:
            return True
        log(f"Discord error {r.status_code}: {r.text[:200]}")
        return False
    return False


def check_once(state):
    for manga_id, name in MANGA.items():
        try:
            chapters = fetch_english_chapters(manga_id)
        except (requests.RequestException, ValueError) as e:
            log(f"Couldn't check {name}: {e}")
            continue

        if manga_id not in state:
            # First time seeing this series: remember what's already out, don't post it.
            state[manga_id] = sorted({chapter_key(c) for c in chapters})
            save_state(state)
            if chapters:
                log(f"Now tracking {name}. Latest English chapter: {describe(chapters[0])}")
            else:
                log(f"Now tracking {name}, but found no English chapters. Double-check the ID.")
            continue

        seen = set(state[manga_id])
        for ch in reversed(chapters):  # oldest first, so posts arrive in order
            key = chapter_key(ch)
            if key in seen:
                continue
            if post_to_discord(manga_id, name, ch):
                log(f"Posted {name} {describe(ch)}")
                seen.add(key)
                state[manga_id] = sorted(seen)
                save_state(state)
                time.sleep(1)
        time.sleep(0.5)  # stay well under MangaDex's rate limit


def send_test():
    for manga_id, name in MANGA.items():
        try:
            chapters = fetch_english_chapters(manga_id)
        except (requests.RequestException, ValueError) as e:
            log(f"Couldn't check {name}: {e}")
            continue
        if chapters and post_to_discord(manga_id, name, chapters[0]):
            log(f"Test post sent for {name} {describe(chapters[0])}")
        else:
            log(f"No test post for {name} (no English chapters found or Discord error).")
        time.sleep(1)


def main():
    if "PASTE" in WEBHOOK_URL:
        sys.exit("Set WEBHOOK_URL (or the DISCORD_WEBHOOK_URL environment variable) first.")
    if "--test" in sys.argv:
        send_test()
        return
    state = load_state()
    if "--once" in sys.argv:
        check_once(state)
        return
    log(f"Watching {len(MANGA)} series, checking every {POLL_MINUTES} minutes.")
    while True:
        try:
            check_once(state)
        except Exception as e:
            log(f"Unexpected error: {e}")
        time.sleep(POLL_MINUTES * 60)


if __name__ == "__main__":
    main()