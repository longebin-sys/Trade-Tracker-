#!/usr/bin/env python3
"""
breakout_alert.py — NR4/NR7 breakout alerts for XAUUSD, ported from
Wanpis' TradingView Pine Script indicator ("Wanpis Breakouts + TP").

Pattern detection: NR4/NR7 range on 4-Hour candles — matching the
indicator's "Timeframe" input (set to 4 Hours, not Daily). Once a 4H
bar's range is the narrowest of the last 4 (NR4) or 7 (NR7) bars, that
bar's high/low become the watched range.

Breakout confirmation: checked independently on 30-minute AND 2-hour
candle closes, each with its own arm/disarm memory (mirrors the
close-above-mid / close-below-mid re-arming logic from the Pine
script exactly). A candle must fully CLOSE beyond the range for a
signal to fire — never mid-candle.

Data source: OKX's public candles endpoint (XAUT-USDT as a gold
proxy) — same one the trade tracker's live price bar already uses.
No API key needed.

This script only sends a Telegram alert. It does not touch the trade
tracker or auto-create any trade — that stays a manual decision.
"""

import os
import json
import datetime
import requests

TG_BOT_TOKEN = os.environ.get('TG_BOT_TOKEN', '')
TG_CHAT_ID   = os.environ.get('TG_CHAT_ID', '@wanpis_t_official')
TG_CHAT_ID2  = os.environ.get('TG_CHAT_ID2', '')

OKX_SYMBOL  = 'XAUT-USDT'
STATE_FILE  = 'breakout_state.json'

# Matches Wanpis' actual indicator settings (not the script's placeholder
# defaults): R:R 1/TP1=5, R:R 2/TP2=9, R:R 3/TP3=13
TP_MULTIPLES = [5.0, 9.0, 13.0]

# Pattern detection timeframe — matches the indicator's "Timeframe" input,
# set to 4 Hours (not Daily)
PATTERN_BAR = '4H'


def fetch_candles(bar, limit=30):
    """OKX candles, oldest -> newest. Each candle includes 'confirm'
    (whether it's actually finished, not still forming)."""
    url = 'https://www.okx.com/api/v5/market/candles'
    params = {'instId': OKX_SYMBOL, 'bar': bar, 'limit': str(limit)}
    try:
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        if data.get('code') != '0':
            print(f"OKX error for bar={bar}:", data.get('msg'))
            return []
    except Exception as e:
        print(f"OKX fetch failed for bar={bar}:", e)
        return []

    rows = list(reversed(data['data']))  # API gives newest-first
    candles = []
    for row in rows:
        candles.append({
            'ts':      int(row[0]),
            'open':    float(row[1]),
            'high':    float(row[2]),
            'low':     float(row[3]),
            'close':   float(row[4]),
            'confirm': row[8] == '1'
        })
    return candles


def get_pattern_range():
    """Replicates the NR4/NR7 detection on the pattern timeframe (4H,
    matching the indicator's Timeframe input). Returns the range info
    for the most recently COMPLETED NR4/NR7 bar, or None."""
    candles = fetch_candles(PATTERN_BAR, limit=15)
    closed = [c for c in candles if c['confirm']]
    if len(closed) < 7:
        return None

    ranges = [c['high'] - c['low'] for c in closed]
    last       = closed[-1]
    last_range = ranges[-1]

    is_nr7 = last_range == min(ranges[-7:])
    is_nr4 = (last_range == min(ranges[-4:])) and not is_nr7

    if not (is_nr7 or is_nr4):
        return None

    return {
        'rh':      last['high'],
        'rl':      last['low'],
        'mid':     (last['high'] + last['low']) / 2,
        'ts':      last['ts'],  # unique per 4H bar — several occur per day
        'pattern': 'NR7' if is_nr7 else 'NR4'
    }


def check_breakout(tf_label, bar, state):
    pattern = get_pattern_range()
    if pattern is None:
        return  # no active NR range right now

    tf_state = state.setdefault(tf_label, {})

    # New NR bar detected -> lock in the fresh range, re-arm both directions
    if tf_state.get('nr_ts') != pattern['ts']:
        tf_state.update({
            'nr_ts':      pattern['ts'],
            'rh':         pattern['rh'],
            'rl':         pattern['rl'],
            'mid':        pattern['mid'],
            'pattern':    pattern['pattern'],
            'up_check':   True,
            'down_check': True
        })

    rh, rl, mid = tf_state['rh'], tf_state['rl'], tf_state['mid']

    candles = fetch_candles(bar, limit=5)
    closed  = [c for c in candles if c['confirm']]
    if not closed:
        return
    last_close = closed[-1]['close']

    # Re-arm logic — mirrors the Pine script's close-vs-mid checks exactly
    if last_close > mid and not tf_state.get('down_check', True):
        tf_state['down_check'] = True
    if last_close < mid and not tf_state.get('up_check', True):
        tf_state['up_check'] = True

    if last_close < rl and tf_state.get('down_check'):
        tf_state['down_check'] = False
        send_breakout_alert(tf_label, 'DOWN', rh, rl, last_close, tf_state['pattern'])
    elif last_close > rh and tf_state.get('up_check'):
        tf_state['up_check'] = False
        send_breakout_alert(tf_label, 'UP', rh, rl, last_close, tf_state['pattern'])


def send_breakout_alert(tf_label, direction, rh, rl, price, pattern):
    rng = rh - rl
    if direction == 'UP':
        entry = rh
        tps   = [entry + rng * m for m in TP_MULTIPLES]
        arrow = '▲'
    else:
        entry = rl
        tps   = [entry - rng * m for m in TP_MULTIPLES]
        arrow = '▼'

    msg = (
        f"{arrow} XAUUSD {direction} Breakout ({tf_label})\n\n"
        f"Pattern: {pattern}\n"
        f"Price: {price:.2f}\n"
        f"TP1: {tps[0]:.2f}\n"
        f"TP2: {tps[1]:.2f}\n"
        f"TP3: {tps[2]:.2f}"
    )
    send_telegram(msg)


def send_telegram(msg):
    for chat_id in filter(None, [TG_CHAT_ID, TG_CHAT_ID2]):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": msg},
                timeout=10
            )
            data = r.json()
            if not data.get('ok'):
                print(f"TG error ({chat_id}):", data.get('description'))
        except Exception as e:
            print(f"TG send failed ({chat_id}):", e)


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def main():
    if not TG_BOT_TOKEN:
        print("Missing TG_BOT_TOKEN secret.")
        return

    now   = datetime.datetime.utcnow()
    state = load_state()

    # 30-minute check runs every time this script runs (cron itself is
    # every 30 min)
    check_breakout('30m', '30m', state)

    # 2-hour check only evaluates on the run that lands right at an
    # even UTC hour boundary (00:00, 02:00, 04:00, ...) — every other
    # run this is a no-op, since a new 2H candle hasn't closed yet
    if now.hour % 2 == 0 and now.minute < 15:
        check_breakout('2h', '2H', state)

    save_state(state)
    print(f"Checked at {now.isoformat()} UTC — done.")


if __name__ == '__main__':
    main()
