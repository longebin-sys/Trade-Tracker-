#!/usr/bin/env python3
import json
import math
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import requests


STATE_FILE = Path("data/xau_signal_state.json")
TIMEFRAMES = ["30m", "2h"]
TP_MULTIPLES = [2.0, 3.0, 4.0]

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}


def log(*args):
    print(*args, file=sys.stderr)


def now_ms():
    return int(time.time() * 1000)


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def round2(value):
    try:
        return round(float(value), 2)
    except Exception:
        return None


def load_state():
    default_state = {
        "30m_last_alert_ts": 0,
        "2h_last_alert_ts": 0,
        "last_30m_alert_time": None,
        "last_2h_alert_time": None,
        "last_run": None,
        "last_source": None,
        "last_error": None,
        "last_error_notification": 0
    }

    if STATE_FILE.exists():
        try:
            current = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            default_state.update(current)
        except Exception as e:
            log(f"Could not read state file: {e}")

    return default_state


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def telegram_ready():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = [
        os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        os.getenv("TELEGRAM_CHAT_ID_2", "").strip()
    ]
    chat_ids = [x for x in chat_ids if x]

    return bool(token and chat_ids)


def send_telegram(state, message):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

    chat_ids = [
        os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        os.getenv("TELEGRAM_CHAT_ID_2", "").strip()
    ]

    chat_ids = [x for x in chat_ids if x]

    if not token or not chat_ids:
        log("Telegram credentials missing. Would send:")
        print(message)
        return False

    ok = False

    for chat_id in chat_ids:
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                headers=HEADERS,
                json={
                    "chat_id": chat_id,
                    "text": message,
                    "disable_web_page_preview": True
                },
                timeout=30
            )

            response.raise_for_status()
            ok = True

        except Exception as e:
            log(f"Telegram send failed for {chat_id}: {e}")

    return ok


def send_error(state, message):
    state["last_error"] = message

    last_notification = int(state.get("last_error_notification", 0) or 0)
    six_hours_ms = 6 * 60 * 60 * 1000

    if now_ms() - last_notification < six_hours_ms:
        log("Error notification throttled.")
        return

    sent = send_telegram(state, message)

    if sent or not telegram_ready():
        state["last_error_notification"] = now_ms()


def fetch_okx_candles(timeframe):
    bar = "30m" if timeframe == "30m" else "2H"

    url = (
        "https://www.okx.com/api/v5/market/candles"
        f"?instId=XAUT-USDT&bar={bar}&limit=100"
    )

    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()

    payload = response.json()

    if payload.get("code") != "0":
        raise RuntimeError(f"OKX error: {payload.get('msg')}")

    rows = payload.get("data", [])

    if not rows:
        raise RuntimeError("OKX returned no candles")

    closed_rows = []

    for row in rows:
        if len(row) >= 9 and str(row[8]) == "1":
            closed_rows.append(row)

    if not closed_rows:
        closed_rows = rows[1:] if len(rows) > 1 else []

    if not closed_rows:
        raise RuntimeError("OKX has no closed candles")

    candles = []

    for row in closed_rows:
        candles.append({
            "ts": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4])
        })

    candles.sort(key=lambda x: x["ts"])

    return candles


def fetch_binance_candles(timeframe):
    interval = "30m" if timeframe == "30m" else "2h"

    url = (
        "https://api.binance.com/api/v3/klines"
        f"?symbol=PAXGUSDT&interval={interval}&limit=100"
    )

    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()

    rows = response.json()

    if not isinstance(rows, list) or len(rows) < 20:
        raise RuntimeError("Binance returned not enough candles")

    closed_rows = rows[:-1]

    candles = []

    for row in closed_rows:
        candles.append({
            "ts": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4])
        })

    candles.sort(key=lambda x: x["ts"])

    return candles


def fetch_yahoo_candles(timeframe):
    try:
        import yfinance as yf
        import pandas as pd
    except Exception as e:
        raise RuntimeError(f"yfinance unavailable: {e}")

    df = yf.download(
        "GC=F",
        period="60d",
        interval="30m",
        progress=False,
        auto_adjust=False
    )

    if df is None or df.empty:
        raise RuntimeError("Yahoo returned no data")

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    df = df.sort_index()

    if len(df) < 100:
        raise RuntimeError("Yahoo not enough candles")

    if timeframe == "2h":
        df = df.resample("2h").agg({
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last"
        }).dropna()

    # Drop the currently forming candle
    df = df.iloc[:-1]

    candles = []

    for ts, row in df.iterrows():
        candles.append({
            "ts": int(ts.timestamp() * 1000),
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"])
        })

    candles.sort(key=lambda x: x["ts"])

    return candles


def fetch_candles(timeframe):
    errors = []

    sources = [
        ("OKX", fetch_okx_candles),
        ("Binance", fetch_binance_candles),
        ("Yahoo", fetch_yahoo_candles)
    ]

    for source_name, fetch_function in sources:
        try:
            candles = fetch_function(timeframe)

            if len(candles) < 20:
                errors.append(f"{source_name}: not enough candles")
                continue

            return source_name, candles

        except Exception as e:
            errors.append(f"{source_name}: {e}")

    raise RuntimeError("All data sources failed. " + " | ".join(errors))


def is_lowest_range(ranges, lookback, index):
    if index + 1 < lookback:
        return False

    window = ranges[index - lookback + 1:index + 1]

    if not window:
        return False

    return math.isclose(
        ranges[index],
        min(window),
        rel_tol=0.0,
        abs_tol=1e-9
    )


def make_signal(direction, candle, pattern_state, timeframe):
    range_high = float(pattern_state["rh"])
    range_low = float(pattern_state["rl"])
    range_size = max(range_high - range_low, 0.0)

    if direction == "Bullish":
        entry = range_high
        tp1 = round2(entry + range_size * TP_MULTIPLES[0])
        tp2 = round2(entry + range_size * TP_MULTIPLES[1])
        tp3 = round2(entry + range_size * TP_MULTIPLES[2])
    else:
        entry = range_low
        tp1 = round2(entry - range_size * TP_MULTIPLES[0])
        tp2 = round2(entry - range_size * TP_MULTIPLES[1])
        tp3 = round2(entry - range_size * TP_MULTIPLES[2])

    return {
        "timeframe": timeframe,
        "direction": direction,
        "signal_ts": int(candle["ts"]),
        "signal_time_utc": datetime.fromtimestamp(
            int(candle["ts"]) / 1000,
            timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC"),
        "pattern": pattern_state["pattern"],
        "close": round2(candle["close"]),
        "range_high": round2(range_high),
        "range_low": round2(range_low),
        "mid": round2(pattern_state["mid"]),
        "entry": round2(entry),
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "source": None
    }


def detect_latest_signal(candles, timeframe):
    if len(candles) < 10:
        return None

    ranges = [
        max(float(c["high"]) - float(c["low"]), 0.0)
        for c in candles
    ]

    buffer = 0.30 if timeframe == "30m" else 0.60

    pattern_state = None
    latest_signal = None

    for i, candle in enumerate(candles):
        nr7 = is_lowest_range(ranges, 7, i)
        nr4 = is_lowest_range(ranges, 4, i) and not nr7

        if nr4 or nr7:
            pattern_state = {
                "pattern": "NR7" if nr7 else "NR4",
                "rh": float(candle["high"]),
                "rl": float(candle["low"]),
                "mid": (float(candle["high"]) + float(candle["low"])) / 2,
                "armed_up": False,
                "armed_down": False,
                "pattern_ts": int(candle["ts"])
            }

            latest_signal = None

        if pattern_state is None:
            continue

        close = float(candle["close"])
        prev_close = float(candles[i - 1]["close"]) if i > 0 else None

        mid = float(pattern_state["mid"])
        range_high = float(pattern_state["rh"])
        range_low = float(pattern_state["rl"])

        if close < mid:
            pattern_state["armed_up"] = True

        if close > mid:
            pattern_state["armed_down"] = True

        if prev_close is not None:
            if (
                pattern_state["armed_up"]
                and prev_close <= range_high
                and close > range_high + buffer
            ):
                latest_signal = make_signal(
                    "Bullish",
                    candle,
                    pattern_state,
                    timeframe
                )

                pattern_state["armed_up"] = False

            if (
                pattern_state["armed_down"]
                and prev_close >= range_low
                and close < range_low - buffer
            ):
                latest_signal = make_signal(
                    "Bearish",
                    candle,
                    pattern_state,
                    timeframe
                )

                pattern_state["armed_down"] = False

    return latest_signal


def format_alert(signal):
    direction_text = (
        "🟢 UP"
        if signal["direction"] == "Bullish"
        else "🔴 DOWN"
    )

    return f"""🥇 XAUUSD {signal['timeframe']} {direction_text} BREAKOUT

Price: {signal['close']}
Pattern: {signal['pattern']}
Range High: {signal['range_high']}
Range Low: {signal['range_low']}
Mid: {signal['mid']}

Entry: {signal['entry']}
TP1: {signal['tp1']}
TP2: {signal['tp2']}
TP3: {signal['tp3']}

Source: {signal['source']}
Time: {signal['signal_time_utc']} UTC
"""


def main():
    state = load_state()
    state["last_run"] = utc_now()

    if os.getenv("TEST_TELEGRAM", "0").strip() == "1":
        test_message = "✅ XAU breakout alert test successful."
        sent = send_telegram(state, test_message)
        log(f"Telegram test sent: {sent}")
        save_state(state)
        return

    data_errors = []
    telegram_errors = []

    for timeframe in TIMEFRAMES:
        try:
            source, candles = fetch_candles(timeframe)
            state["last_source"] = source

            log(f"{timeframe}: fetched {len(candles)} candles from {source}")

            signal = detect_latest_signal(candles, timeframe)

            if not signal:
                log(f"{timeframe}: no signal detected")
                continue

            signal["source"] = source

            last_alert_ts = int(state.get(f"{timeframe}_last_alert_ts", 0) or 0)
            signal_ts = int(signal["signal_ts"])
            age_ms = now_ms() - signal_ts

            max_age_ms = (
                3 * 60 * 60 * 1000
                if timeframe == "30m"
                else 8 * 60 * 60 * 1000
            )

            if signal_ts > last_alert_ts and age_ms <= max_age_ms:
                message = format_alert(signal)
                sent = send_telegram(state, message)

                if sent:
                    state[f"{timeframe}_last_alert_ts"] = signal_ts
                    state[f"last_{timeframe}_alert_time"] = signal["signal_time_utc"]
                    save_state(state)
                    log(f"{timeframe}: alert sent")
                else:
                    telegram_errors.append(
                        f"{timeframe}: Telegram send failed or credentials missing"
                    )
            else:
                log(f"{timeframe}: signal exists but already alerted or too old")

        except Exception as e:
            data_errors.append(f"{timeframe}: {e}")
            log(traceback.format_exc())

    if len(data_errors) == len(TIMEFRAMES):
        send_error(
            state,
            "⚠️ XAUUSD Breakout Alert data error\n" + "\n".join(data_errors)
        )
    elif data_errors:
        state["last_error"] = "\n".join(data_errors)

    if telegram_errors:
        state["last_error"] = "\n".join(telegram_errors)
        log("Telegram errors: " + ", ".join(telegram_errors))

    save_state(state)


if __name__ == "__main__":
    main()