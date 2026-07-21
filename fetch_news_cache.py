#!/usr/bin/env python3
"""
fetch_news_cache.py — publishes ForexFactory's weekly calendar as a
same-origin JSON file (news_cache.json) so the browser app can read it
without ever hitting CORS. Runs via GitHub Actions on a schedule.

This script does NOT send any Telegram messages — it only produces
the data file. All alerting/sending logic still lives in the browser
app, so there's no risk of duplicate news alerts firing from two
places.
"""

import json
import requests

NEWS_FEED_URL = 'https://nfs.faireconomy.media/ff_calendar_thisweek.json'
OUTPUT_FILE = 'news_cache.json'

def main():
    try:
        r = requests.get(NEWS_FEED_URL, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"Fetch failed: {e}")
        return  # leave the existing news_cache.json untouched on failure

    with open(OUTPUT_FILE, 'w') as f:
        json.dump(data, f)

    print(f"Saved {len(data)} calendar events to {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
