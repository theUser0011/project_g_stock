import json
import os
import time
import asyncio
from datetime import datetime, timedelta, timezone, time as dtime, UTC

import aiohttp
from flask import Flask, jsonify, request
from flask_compress import Compress
from flask_cors import CORS

# =====================================================
# CONFIG
# =====================================================
COMPANY_FILE = "companies_list.json"

GROWW_URL = (
    "https://groww.in/v1/api/charting_service/v2/chart/"
    "delayed/exchange/NSE/segment/CASH"
)

SIGNALS_URL = "https://project-get-entry.vercel.app/api/signals"

INTERVAL_MINUTES = 3
LATEST_WINDOW_MINUTES = 5

MAX_WORKERS = 100
TIMEOUT = 20

TOTAL_BATCHES = 2
BATCH_NO = int(os.getenv("BATCH_NUM", 1))

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

DEFAULT_ENTRY_AFTER = dtime(9, 25)

# =====================================================
# FLASK
# =====================================================
app = Flask(__name__)
Compress(app)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# =====================================================
# TIME HELPERS
# =====================================================
def to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def market_range_for_date(date_str: str):
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=IST)
    start = d.replace(hour=9, minute=0, second=0, microsecond=0)
    end = d.replace(hour=15, minute=30, second=0, microsecond=0)
    return to_ms(start), to_ms(end)


def candle_time_range(candles):
    if not candles:
        return None, None

    return (
        datetime.fromtimestamp(candles[0][0], IST).strftime("%H:%M:%S"),
        datetime.fromtimestamp(candles[-1][0], IST).strftime("%H:%M:%S"),
    )


def parse_entry_after():
    val = request.args.get("entry_after")
    if not val:
        return DEFAULT_ENTRY_AFTER
    try:
        h, m = map(int, val.split(":"))
        return dtime(h, m)
    except Exception:
        return DEFAULT_ENTRY_AFTER


def normalize_ts(ts: int) -> int:
    return ts // 1000 if ts > 1_000_000_000_000 else ts

# =====================================================
# ASYNC FETCH HELPERS
# =====================================================
async def fetch_groww_candles(session, symbol, start_ms, end_ms):
    url = f"{GROWW_URL}/{symbol}"
    params = {
        "intervalInMinutes": INTERVAL_MINUTES,
        "startTimeInMillis": start_ms,
        "endTimeInMillis": end_ms,
    }
    headers = {
        "accept": "application/json, text/plain, */*",
        "x-app-id": "growwWeb",
        "x-platform": "web",
        "x-device-type": "charts",
    }

    try:
        async with session.get(url, params=params, headers=headers, timeout=TIMEOUT) as r:
            if r.status == 200:
                data = await r.json()
                return symbol, data.get("candles", [])
    except Exception:
        pass

    return symbol, []


async def fetch_signals(session):
    try:
        async with session.get(SIGNALS_URL, timeout=TIMEOUT) as r:
            if r.status == 200:
                data = await r.json()
                return data.get("data", [])
    except Exception:
        pass

    return []

# =====================================================
# TRADE ANALYSIS
# =====================================================
def analyze_trade(candles, signal, entry_after_time: dtime):
    entry = signal["entry"]
    target = signal["target"]
    stoploss = signal["stoploss"]
    qty = signal["qty"]

    entered = False
    entry_time = None

    last_close = None
    last_dt = None

    for ts, o, h, l, c, v in candles:
        ts = normalize_ts(ts)
        candle_dt = datetime.fromtimestamp(ts, UTC).astimezone(IST)
        t = candle_dt.strftime("%H:%M:%S")

        last_close = c
        last_dt = candle_dt

        # ENTRY
        if (
            not entered
            and candle_dt.time() >= entry_after_time
            and h >= entry
        ):
            entered = True
            entry_time = t

        if entered:
            if h >= target:
                return {
                    "status": "EXITED_TARGET",
                    "entry_time": entry_time,
                    "exit_time": t,
                    "exit_ltp": target,
                    "pnl": round((target - entry) * qty, 2),
                    "market_closed": False,
                }

            if l <= stoploss:
                return {
                    "status": "EXITED_SL",
                    "entry_time": entry_time,
                    "exit_time": t,
                    "exit_ltp": stoploss,
                    "pnl": round((stoploss - entry) * qty, 2),
                    "market_closed": False,
                }

    # ✅ AUTO EXIT AT MARKET CLOSE
    if entered and last_close is not None:
        return {
            "status": "EXITED_MARKET_CLOSE",
            "entry_time": entry_time,
            "exit_time": MARKET_CLOSE.strftime("%H:%M:%S"),
            "exit_ltp": last_close,
            "pnl": round((last_close - entry) * qty, 2),
            "market_closed": True,
        }

    return {
        "status": "NOT_ENTERED",
        "entry_time": None,
        "exit_time": None,
        "exit_ltp": None,
        "pnl": None,
        "market_closed": False,
    }

# =====================================================
# ROUTES
# =====================================================
@app.route("/")
def home():
    return jsonify({
        "status": "ok",
        "time": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.route("/api/symbols")
def get_symbols():
    with open(COMPANY_FILE) as f:
        companies = json.load(f)

    symbols = sorted({c.split("__")[0].strip() for c in companies if "__" in c})
    return jsonify({"status": "ok", "count": len(symbols), "symbols": symbols})


@app.route("/api/live-candles")
def live_candles():
    latest = request.args.get("latest", "false").lower() == "true"

    now = datetime.now(IST)
    start_ms, end_ms = market_range_for_date(now.strftime("%Y-%m-%d"))

    with open(COMPANY_FILE) as f:
        companies = json.load(f)

    batch_size = max(1, len(companies) // TOTAL_BATCHES)
    start = (BATCH_NO - 1) * batch_size
    end = start + batch_size

    symbols = [c.split("__")[0].strip() for c in companies[start:end]]

    async def runner():
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=MAX_WORKERS)
        ) as session:
            return await asyncio.gather(*[
                fetch_groww_candles(session, s, start_ms, end_ms)
                for s in symbols
            ])

    data = asyncio.run(runner())

    results = {}
    candles_needed = max(1, LATEST_WINDOW_MINUTES // INTERVAL_MINUTES)

    for sym, candles in data:
        results[sym] = candles[-candles_needed:] if latest else candles

    st, et = candle_time_range([c for v in results.values() for c in v])

    return jsonify({
        "mode": "latest" if latest else "full",
        "count": len(results),
        "start_time": st,
        "end_time": et,
        "data": results,
    })


@app.route("/api/analyze-signals")
def analyze_signals():
    start_clock = time.perf_counter()

    breakout_pct = float(request.args.get("breakout", 3))
    profit_pct = float(request.args.get("profit", 3))
    entry_after_time = parse_entry_after()

    async def runner():
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=MAX_WORKERS)
        ) as session:
            signals = await fetch_signals(session)
            signal_map = {s["symbol"]: s for s in signals}

            now = datetime.now(IST)
            market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
            if now < market_open:
                market_open -= timedelta(days=1)

            start_ms = to_ms(market_open)
            end_ms = to_ms(now)

            candles = await asyncio.gather(*[
                fetch_groww_candles(session, s, start_ms, end_ms)
                for s in signal_map
            ])

            return signal_map, candles

    signal_map, candle_results = asyncio.run(runner())

    summary = {
        "entered": 0,
        "target_hit": 0,
        "stoploss_hit": 0,
        "market_closed": 0,
        "not_entered": 0,
    }

    results = {}

    for sym, candles in candle_results:
        sig = signal_map[sym]

        open_price = sig["open"]
        entry = round(open_price * (1 + breakout_pct / 100), 2)
        target = round(entry * (1 + profit_pct / 100), 2)

        sig = {**sig, "entry": entry, "target": target}

        analysis = analyze_trade(candles, sig, entry_after_time)
        status = analysis["status"]

        if status == "EXITED_TARGET":
            summary["entered"] += 1
            summary["target_hit"] += 1
        elif status == "EXITED_SL":
            summary["entered"] += 1
            summary["stoploss_hit"] += 1
        elif status == "EXITED_MARKET_CLOSE":
            summary["entered"] += 1
            summary["market_closed"] += 1
        elif status == "ENTERED":
            summary["entered"] += 1
        else:
            summary["not_entered"] += 1

        results[sym] = {**sig, **analysis}

    return jsonify({
        "status": "ok",
        "breakout_pct": breakout_pct,
        "profit_pct": profit_pct,
        "entry_after": entry_after_time.strftime("%H:%M"),
        "count": len(results),
        "summary": summary,
        "response_time_ms": int((time.perf_counter() - start_clock) * 1000),
        "the_data": results,
    })


# =====================================================
# RUN
# =====================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
