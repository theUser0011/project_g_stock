import json
import os
import time
import asyncio
from datetime import datetime, timedelta, timezone, time as dtime

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
MARKET_OPEN = dtime(9, 0)
MARKET_CLOSE = dtime(15, 30)

MAX_LOOKBACK_DAYS = 7

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

    start_ts = candles[0][0]
    end_ts = candles[-1][0]

    return (
        datetime.fromtimestamp(start_ts, IST).strftime("%H:%M:%S"),
        datetime.fromtimestamp(end_ts, IST).strftime("%H:%M:%S"),
    )

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
def analyze_trade(candles, signal):
    entry = signal["entry"]
    target = signal["target"]
    stoploss = signal["stoploss"]
    qty = signal["qty"]

    entered = False
    entry_time = None

    for ts, o, h, l, c, v in candles:
        t = datetime.fromtimestamp(ts, IST).strftime("%H:%M:%S")

        if not entered and h >= entry:
            entered = True
            entry_time = t

        if entered:
            if h >= target:
                pnl = round((target - entry) * qty, 2)
                return {
                    "status": "EXITED_TARGET",
                    "entry_time": entry_time,
                    "exit_time": t,
                    "exit_ltp": target,
                    "pnl": pnl,
                }

            if l <= stoploss:
                pnl = round((stoploss - entry) * qty, 2)
                return {
                    "status": "EXITED_SL",
                    "entry_time": entry_time,
                    "exit_time": t,
                    "exit_ltp": stoploss,
                    "pnl": pnl,
                }

    if entered:
        return {
            "status": "ENTERED",
            "entry_time": entry_time,
            "exit_time": None,
            "exit_ltp": None,
            "pnl": None,
        }

    return {
        "status": "NOT_ENTERED",
        "entry_time": None,
        "exit_time": None,
        "exit_ltp": None,
        "pnl": None,
    }

# =====================================================
# ROUTES
# =====================================================
@app.route("/")
def home():
    return jsonify({
        "status": "ok",
        "message": "Server running 🚀",
        "time": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.route("/api/symbols")
def get_symbols():
    with open(COMPANY_FILE) as f:
        companies = json.load(f)

    symbols = sorted({
        c.split("__")[0].strip()
        for c in companies if "__" in c
    })

    return jsonify({
        "status": "ok",
        "count": len(symbols),
        "symbols": symbols
    })


@app.route("/api/live-candles")
def live_candles():
    latest = request.args.get("latest", "false").lower() == "true"

    now = datetime.now(IST)
    trade_date = now.strftime("%Y-%m-%d")
    start_ms, end_ms = market_range_for_date(trade_date)

    with open(COMPANY_FILE) as f:
        companies = json.load(f)

    total = len(companies)
    batch_size = max(1, total // TOTAL_BATCHES)

    batch_no = min(BATCH_NO, (total + batch_size - 1) // batch_size)
    start = (batch_no - 1) * batch_size
    end = min(start + batch_size, total)

    batch = companies[start:end]
    symbols = [c.split("__")[0].strip() for c in batch]

    async def runner():
        connector = aiohttp.TCPConnector(limit=MAX_WORKERS)
        async with aiohttp.ClientSession(connector=connector) as session:
            tasks = [
                fetch_groww_candles(session, s, start_ms, end_ms)
                for s in symbols
            ]
            return await asyncio.gather(*tasks)

    candle_data = asyncio.run(runner())

    candles_needed = max(1, LATEST_WINDOW_MINUTES // INTERVAL_MINUTES)
    results = {}

    for sym, candles in candle_data:
        results[sym] = candles[-candles_needed:] if latest else candles

    all_candles = [c for v in results.values() for c in v]
    start_time, end_time = candle_time_range(all_candles)

    return jsonify({
        "mode": "latest" if latest else "full",
        "batch_no": batch_no,
        "interval_minutes": INTERVAL_MINUTES,
        "count": len(results),
        "start_time": start_time,
        "end_time": end_time,
        "data": results,
    })


@app.route("/api/analyze-signals")
def analyze_signals():
    start_clock = time.perf_counter()

    async def runner():
        connector = aiohttp.TCPConnector(limit=MAX_WORKERS)
        async with aiohttp.ClientSession(connector=connector) as session:
            signals = await fetch_signals(session)
            signal_map = {s["symbol"]: s for s in signals}

            symbols = list(signal_map.keys())

            now = datetime.now(IST)
            start_ms = to_ms(now - timedelta(minutes=45))
            end_ms = to_ms(now)

            tasks = [
                fetch_groww_candles(session, s, start_ms, end_ms)
                for s in symbols
            ]

            candle_results = await asyncio.gather(*tasks)
            return signal_map, candle_results

    signal_map, candle_results = asyncio.run(runner())

    summary = {
        "entered": 0,
        "target_hit": 0,
        "stoploss_hit": 0,
        "not_entered": 0,
    }

    results = {}

    for sym, candles in candle_results:
        sig = signal_map[sym]
        analysis = analyze_trade(candles, sig)
        status = analysis["status"]

        if status == "EXITED_TARGET":
            summary["entered"] += 1
            summary["target_hit"] += 1
        elif status == "EXITED_SL":
            summary["entered"] += 1
            summary["stoploss_hit"] += 1
        elif status == "ENTERED":
            summary["entered"] += 1
        else:
            summary["not_entered"] += 1

        results[sym] = {**sig, **analysis}

    elapsed = time.perf_counter() - start_clock

    return jsonify({
        "status": "ok",
        "count": len(results),
        "summary": summary,
        "response_time_ms": int(elapsed * 1000),
        "data": results,
    })


# =====================================================
# RUN
# =====================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
