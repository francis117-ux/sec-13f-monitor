#!/usr/bin/env python3
"""
Taiwan Stock Data
Fetches closing price, P/E ratio, P/B ratio, and price history for TWSE-listed stocks.
Calculates technical trade parameters: entry zone, stop-loss, target, risk/reward.

All data from TWSE public API — no API key required.
"""

import json
import time
import requests
from datetime import datetime, timedelta
from pathlib import Path

HEADERS = {
    "User-Agent": "sec-monitor-routine francis117@gmail.com",
    "Referer": "https://www.twse.com.tw/",
}

Path("data").mkdir(exist_ok=True)


def _tw_now():
    return datetime.utcnow() + timedelta(hours=8)


def _parse_price(s) -> float:
    try:
        return float(str(s).replace(",", "").replace("+", "").strip())
    except (ValueError, AttributeError):
        return 0.0


def get_price_history(ticker: str) -> list[dict]:
    """
    Fetch ~60 trading days of OHLCV data from TWSE (fetches 2 months).
    TWSE STOCK_DAY returns one month at a time.
    Fields: [日期, 成交股數, 成交金額, 開盤價, 最高價, 最低價, 收盤價, 漲跌價差, 成交筆數]
    """
    all_rows = []
    now_tw = _tw_now()

    # Fetch previous month first, then current month (chronological order)
    prev_month = (now_tw.replace(day=1) - timedelta(days=1))
    dates_to_fetch = [prev_month.strftime("%Y%m%d"), now_tw.strftime("%Y%m%d")]

    for date_str in dates_to_fetch:
        url = (
            f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
            f"?date={date_str}&stockNo={ticker}&response=json"
        )
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            data = r.json()
        except Exception:
            time.sleep(0.5)
            continue

        if data.get("stat") != "OK":
            time.sleep(0.5)
            continue

        for row in data.get("data", []):
            try:
                o = _parse_price(row[3])
                h = _parse_price(row[4])
                lo = _parse_price(row[5])
                c = _parse_price(row[6])
                if c > 0:
                    all_rows.append({"date": row[0], "open": o, "high": h, "low": lo, "close": c})
            except (IndexError, ValueError):
                continue

        time.sleep(0.5)

    return all_rows


def get_valuation(ticker: str) -> dict:
    """
    Fetch P/E ratio, P/B ratio, dividend yield from TWSE BWIBBU_d.
    Fields: [日期, 殖利率(%), 股利年度, 本益比, 股價淨值比, 財報年/季]
    """
    date_str = _tw_now().strftime("%Y%m%d")
    url = (
        f"https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d"
        f"?date={date_str}&stockNo={ticker}&response=json"
    )
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        data = r.json()
    except Exception:
        return {}

    if data.get("stat") != "OK":
        return {}

    rows = data.get("data", [])
    if not rows:
        return {}

    row = rows[-1]
    try:
        return {
            "div_yield": _parse_price(row[1]),
            "pe": _parse_price(row[3]),
            "pb": _parse_price(row[4]),
        }
    except (IndexError, ValueError):
        return {}


def valuation_label(pe: float, pb: float) -> str:
    if pe <= 0:
        return "Valuation N/A"
    if pe < 12:
        return f"P/E {pe:.1f}x — looks cheap"
    if pe < 18:
        return f"P/E {pe:.1f}x — reasonable value"
    if pe < 25:
        return f"P/E {pe:.1f}x — fair, not a bargain"
    if pe < 35:
        return f"P/E {pe:.1f}x — somewhat expensive"
    return f"P/E {pe:.1f}x — priced for high growth"


def calculate_technicals(history: list[dict], current_price: float) -> dict:
    """
    Calculate moving averages, entry zone, stop-loss, target, and risk/reward.

    Logic:
    - Entry zone: current price ±2% (don't chase if it's already run up)
    - Stop-loss: just below the 20-day moving average (if stock is above it),
      or a fixed 8% stop if already below the MA20
    - Target: previous 60-day high (if meaningfully higher), else +12% from here
    - R/R: how much you could gain vs. how much you risk losing
    """
    if not history or current_price <= 0:
        return {}

    closes = [r["close"] for r in history]
    highs  = [r["high"]  for r in history]
    lows   = [r["low"]   for r in history]

    n20 = min(len(closes), 20)
    n60 = min(len(closes), 60)

    ma20 = sum(closes[-n20:]) / n20
    ma60 = sum(closes[-n60:]) / n60

    recent_high = max(highs[-n60:]) if highs else current_price * 1.1
    recent_low  = min(lows[-n60:])  if lows  else current_price * 0.9

    entry_low  = round(current_price * 0.98, 1)
    entry_high = round(current_price * 1.02, 1)

    if current_price > ma20 * 1.02:
        stop = round(ma20 * 0.97, 1)
    else:
        stop = round(current_price * 0.92, 1)

    if recent_high > current_price * 1.10:
        target = round(recent_high * 0.97, 1)
    else:
        target = round(current_price * 1.12, 1)

    risk   = current_price - stop
    reward = target - current_price
    rr     = round(reward / risk, 1) if risk > 0 else 0
    risk_pct = round(risk / current_price * 100, 1)

    return {
        "ma20": round(ma20, 1),
        "ma60": round(ma60, 1),
        "recent_high_60d": round(recent_high, 1),
        "recent_low_60d":  round(recent_low, 1),
        "entry_low":  entry_low,
        "entry_high": entry_high,
        "stop_loss":  stop,
        "target":     target,
        "rr_ratio":   rr,
        "risk_pct":   risk_pct,
        "above_ma20": current_price > ma20,
        "above_ma60": current_price > ma60,
    }


def get_tw_stock_data(ticker: str) -> dict:
    """
    Main entry point. Returns full data dict for a TWSE stock.
    Returns dict with "error" key if data unavailable.
    """
    history = get_price_history(ticker)
    if not history:
        return {"ticker": ticker, "error": "no price data"}

    current_price = history[-1]["close"]
    if current_price <= 0:
        return {"ticker": ticker, "error": "invalid price"}

    valuation = get_valuation(ticker)
    time.sleep(0.3)

    technicals = calculate_technicals(history, current_price)

    return {
        "ticker": ticker,
        "current_price": current_price,
        "pe":  valuation.get("pe", 0),
        "pb":  valuation.get("pb", 0),
        "div_yield": valuation.get("div_yield", 0),
        "valuation_label": valuation_label(valuation.get("pe", 0), valuation.get("pb", 0)),
        **technicals,
    }


def get_tw_stocks_batch(tickers: list, max_stocks: int = 8) -> dict:
    """
    Fetch data for multiple TW stocks. Caps at max_stocks to respect rate limits.
    Returns {ticker: data_dict}.
    """
    results = {}
    for ticker in tickers[:max_stocks]:
        print(f"  Fetching data: {ticker}...", flush=True)
        data = get_tw_stock_data(ticker)
        if data and "error" not in data:
            results[ticker] = data
        time.sleep(0.8)
    return results


if __name__ == "__main__":
    import sys
    ticker = sys.argv[1] if len(sys.argv) > 1 else "2330"
    print(f"Fetching data for {ticker}...")
    d = get_tw_stock_data(ticker)
    print(json.dumps(d, indent=2, ensure_ascii=False))
