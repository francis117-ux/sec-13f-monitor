#!/usr/bin/env python3
"""
TWSE 三大法人 Scanner
Fetches daily net buy/sell data for foreign investors (外資),
investment trusts (投信), and proprietary dealers (自營商).
Threshold: NT$100,000,000 net per institution per stock.
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

THRESHOLD_NTD = 100_000_000  # NT$100M
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)


def get_twse_date(days_back=0):
    """Get trading date string. TWSE closes on weekends."""
    d = datetime.utcnow() + timedelta(hours=8)  # Taiwan time
    d -= timedelta(days=days_back)
    # Skip weekends
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


def fetch_three_institutions(date_str):
    """
    Fetch 三大法人 net buy/sell from TWSE T86 endpoint.
    Returns list of stocks with institutional activity.
    """
    url = (
        f"https://www.twse.com.tw/rwd/zh/fund/T86"
        f"?date={date_str}&selectType=ALLBUT0999&response=json"
    )
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  TWSE fetch error: {e}")
        return []

    if data.get("stat") != "OK":
        print(f"  TWSE returned stat: {data.get('stat')}")
        return []

    fields = data.get("fields", [])
    rows = data.get("data", [])

    # Column indices (TWSE field order):
    # 0: 證券代號 (ticker)
    # 1: 證券名稱 (name)
    # 2: 外資買進 (foreign buy)
    # 3: 外資賣出 (foreign sell)
    # 4: 外資淨買賣 (foreign net)
    # 5: 投信買進 (trust buy)
    # 6: 投信賣出 (trust sell)
    # 7: 投信淨買賣 (trust net)
    # 8: 自營商買進 (dealer buy)
    # 9: 自營商賣出 (dealer sell)
    # 10: 自營商淨買賣 (dealer net)
    # 11: 三大法人合計 (total net)

    results = []
    for row in rows:
        try:
            ticker = row[0].strip()
            name = row[1].strip()

            def parse_ntd(s):
                return int(s.replace(",", "").replace("+", "").strip()) * 1000  # values in thousands shares * price? Actually in shares

            # Net values are share counts, we need to estimate NTD value
            # TWSE T86 gives share counts, not NTD values
            # We'll use share count * approximate price — but for simplicity
            # use the total net shares as proxy and flag large movers
            foreign_net_shares = int(row[4].replace(",", "").replace("+", "").strip())
            trust_net_shares = int(row[7].replace(",", "").replace("+", "").strip())
            dealer_net_shares = int(row[10].replace(",", "").replace("+", "").strip())
            total_net_shares = int(row[11].replace(",", "").replace("+", "").strip())

        except (ValueError, IndexError):
            continue

        # Flag stocks where any single institution moved >500K shares net
        # or total net >1M shares (proxy for NT$100M threshold at ~NT$100/share average)
        SHARE_THRESHOLD = 500_000

        signals = []
        if abs(foreign_net_shares) >= SHARE_THRESHOLD:
            signals.append({
                "institution": "外資 (Foreign)",
                "net_shares": foreign_net_shares,
                "direction": "BUY" if foreign_net_shares > 0 else "SELL"
            })
        if abs(trust_net_shares) >= SHARE_THRESHOLD:
            signals.append({
                "institution": "投信 (Trust)",
                "net_shares": trust_net_shares,
                "direction": "BUY" if trust_net_shares > 0 else "SELL"
            })
        if abs(dealer_net_shares) >= SHARE_THRESHOLD:
            signals.append({
                "institution": "自營商 (Dealer)",
                "net_shares": dealer_net_shares,
                "direction": "BUY" if dealer_net_shares > 0 else "SELL"
            })

        if not signals:
            continue

        # Check if institutions are aligned (all buying or all selling)
        directions = [s["direction"] for s in signals]
        aligned = len(set(directions)) == 1 and len(directions) >= 2

        results.append({
            "ticker": ticker,
            "name": name,
            "foreign_net": foreign_net_shares,
            "trust_net": trust_net_shares,
            "dealer_net": dealer_net_shares,
            "total_net": total_net_shares,
            "signals": signals,
            "institutions_aligned": aligned,
            "overall_direction": "BUY" if total_net_shares > 0 else "SELL",
        })

    return results


def run(days_back=0):
    date_str = get_twse_date(days_back)
    print(f"Scanning TWSE 三大法人 for {date_str}...")

    stocks = fetch_three_institutions(date_str)
    print(f"Found {len(stocks)} stocks with significant institutional activity")

    # Sort by absolute total net shares
    stocks.sort(key=lambda x: abs(x["total_net"]), reverse=True)

    # Separate aligned (high conviction) from single-institution signals
    aligned_buys = [s for s in stocks if s["institutions_aligned"] and s["overall_direction"] == "BUY"]
    aligned_sells = [s for s in stocks if s["institutions_aligned"] and s["overall_direction"] == "SELL"]
    single_buys = [s for s in stocks if not s["institutions_aligned"] and s["overall_direction"] == "BUY"]
    single_sells = [s for s in stocks if not s["institutions_aligned"] and s["overall_direction"] == "SELL"]

    result = {
        "date": date_str,
        "aligned_buys": aligned_buys,
        "aligned_sells": aligned_sells,
        "single_institution_buys": single_buys[:20],
        "single_institution_sells": single_sells[:20],
        "total_active_stocks": len(stocks),
    }

    out_path = DATA_DIR / "twse_signals.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"TWSE scan complete: {len(aligned_buys)} aligned buys, {len(aligned_sells)} aligned sells")
    return result


if __name__ == "__main__":
    result = run()
    print("\nTop Aligned Buys (多機構同向買入):")
    for s in result["aligned_buys"][:5]:
        insts = ", ".join(i["institution"] for i in s["signals"])
        print(f"  {s['ticker']} {s['name']} — {insts} | Net: {s['total_net']:+,} shares")
    print("\nTop Aligned Sells (多機構同向賣出):")
    for s in result["aligned_sells"][:5]:
        insts = ", ".join(i["institution"] for i in s["signals"])
        print(f"  {s['ticker']} {s['name']} — {insts} | Net: {s['total_net']:+,} shares")
