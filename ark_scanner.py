#!/usr/bin/env python3
"""
ARK Invest Daily Holdings Scanner
ARK is unique among major funds: they publish their full holdings every single day,
not quarterly. This gives near-real-time insight into Cathie Wood's positioning.

Detects: new positions, significant increases (>20%), complete exits.
"""

import csv
import io
import json
import time
import requests
from pathlib import Path

HEADERS = {
    "User-Agent": "sec-monitor-routine francis117@gmail.com",
}

DATA_DIR = Path("data/ark_holdings")
DATA_DIR.mkdir(parents=True, exist_ok=True)

CHANGES_PATH = Path("data/ark_latest_changes.json")

# ARK fund holdings CSV URLs.
# ARK has changed these URLs periodically — multiple candidates per fund for resilience.
ARK_FUNDS = {
    "ARKK": [
        "https://assets.ark-funds.com/funds/csvs/fund-holdings/ARK_INNOVATION_ETF_ARKK_HOLDINGS.csv",
        "https://ark-funds.com/wp-content/uploads/funds-etf-csv/ARK_INNOVATION_ETF_ARKK_HOLDINGS.csv",
    ],
    "ARKQ": [
        "https://assets.ark-funds.com/funds/csvs/fund-holdings/ARK_AUTONOMOUS_TECHNOLOGY_%26_ROBOTICS_ETF_ARKQ_HOLDINGS.csv",
        "https://ark-funds.com/wp-content/uploads/funds-etf-csv/ARK_AUTONOMOUS_TECHNOLOGY_%26_ROBOTICS_ETF_ARKQ_HOLDINGS.csv",
    ],
    "ARKW": [
        "https://assets.ark-funds.com/funds/csvs/fund-holdings/ARK_NEXT_GENERATION_INTERNET_ETF_ARKW_HOLDINGS.csv",
        "https://ark-funds.com/wp-content/uploads/funds-etf-csv/ARK_NEXT_GENERATION_INTERNET_ETF_ARKW_HOLDINGS.csv",
    ],
    "ARKG": [
        "https://assets.ark-funds.com/funds/csvs/fund-holdings/ARK_GENOMIC_REVOLUTION_ETF_ARKG_HOLDINGS.csv",
        "https://ark-funds.com/wp-content/uploads/funds-etf-csv/ARK_GENOMIC_REVOLUTION_ETF_ARKG_HOLDINGS.csv",
    ],
}


def _get_field(row: dict, *keys) -> str:
    """Case-insensitive dict lookup, returns first match."""
    row_lower = {k.lower().strip(): v for k, v in row.items()}
    for key in keys:
        val = row_lower.get(key.lower().strip(), "")
        if val:
            return str(val).strip()
    return ""


def download_holdings(fund_ticker: str) -> list[dict] | None:
    """Download ARK fund holdings CSV. Returns list of holding dicts, or None on failure."""
    for url in ARK_FUNDS.get(fund_ticker, []):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code != 200:
                continue

            reader = csv.DictReader(io.StringIO(r.text))
            holdings = []
            for row in reader:
                ticker  = _get_field(row, "ticker", "Ticker", "TICKER").upper()
                company = _get_field(row, "company", "Company", "COMPANY", "name", "Name")
                shares_str = _get_field(row, "shares", "Shares", "SHARES").replace(",", "")
                value_str  = _get_field(
                    row, "market value ($)", "market value", "Market Value ($)", "Market Value"
                ).replace(",", "").replace("$", "")
                weight_str = _get_field(
                    row, "weight (%)", "weight", "Weight (%)", "Weight"
                ).replace("%", "")

                if not ticker or ticker in ("-", "N/A", ""):
                    continue

                try:
                    shares = int(float(shares_str or "0"))
                    value  = float(value_str  or "0")
                    weight = float(weight_str or "0")
                except ValueError:
                    shares, value, weight = 0, 0.0, 0.0

                holdings.append({
                    "ticker":     ticker,
                    "company":    company,
                    "shares":     shares,
                    "value_usd":  value,
                    "weight_pct": weight,
                    "fund":       fund_ticker,
                })

            if holdings:
                return holdings

        except Exception:
            continue

    return None


def load_prev(fund_ticker: str) -> dict:
    path = DATA_DIR / f"{fund_ticker}.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def save_curr(fund_ticker: str, holdings: list[dict]):
    path = DATA_DIR / f"{fund_ticker}.json"
    with open(path, "w") as f:
        json.dump({h["ticker"]: h for h in holdings}, f)


def diff_holdings(prev: dict, curr: list[dict], fund_ticker: str) -> dict:
    """Compare today's holdings to yesterday's. Returns new/increased/exited."""
    curr_idx = {h["ticker"]: h for h in curr}

    new_positions = {}
    increased     = {}
    exited        = {}

    for ticker, h in curr_idx.items():
        if ticker not in prev:
            new_positions[ticker] = {
                "fund":       fund_ticker,
                "company":    h["company"],
                "shares":     h["shares"],
                "value_usd":  h["value_usd"],
                "weight_pct": h["weight_pct"],
            }
        else:
            prev_shares = prev[ticker].get("shares", 0)
            if prev_shares > 0:
                pct = (h["shares"] - prev_shares) / prev_shares * 100
                if pct >= 20:
                    increased[ticker] = {
                        "fund":        fund_ticker,
                        "company":     h["company"],
                        "pct_change":  round(pct, 1),
                        "shares_added": h["shares"] - prev_shares,
                        "value_usd":   h["value_usd"],
                    }

    for ticker, h in prev.items():
        if ticker not in curr_idx:
            exited[ticker] = {
                "fund":           fund_ticker,
                "company":        h.get("company", ""),
                "prev_shares":    h.get("shares", 0),
                "prev_value_usd": h.get("value_usd", 0),
            }

    return {"new_positions": new_positions, "increased": increased, "exited": exited}


def run() -> dict:
    """Download ARK holdings, diff vs previous day, write data/ark_latest_changes.json."""
    all_changes = {"new_positions": {}, "increased": {}, "exited": {}}

    for fund_ticker in ARK_FUNDS:
        print(f"  [ARK {fund_ticker}] downloading...", flush=True)
        time.sleep(0.5)

        holdings = download_holdings(fund_ticker)
        if not holdings:
            print(f"    could not reach {fund_ticker} holdings URL")
            continue

        print(f"    {len(holdings)} positions", flush=True)
        prev = load_prev(fund_ticker)

        if prev:
            diff = diff_holdings(prev, holdings, fund_ticker)
            n_new  = len(diff["new_positions"])
            n_inc  = len(diff["increased"])
            n_exit = len(diff["exited"])
            if n_new + n_inc + n_exit > 0:
                print(f"    changes: +{n_new} new, {n_inc} increased, -{n_exit} exited", flush=True)
            all_changes["new_positions"].update(diff["new_positions"])
            all_changes["increased"].update(diff["increased"])
            all_changes["exited"].update(diff["exited"])
        else:
            print(f"    first run — baseline saved, diffs appear tomorrow")

        save_curr(fund_ticker, holdings)
        time.sleep(0.5)

    with open(CHANGES_PATH, "w") as f:
        json.dump(all_changes, f, indent=2)

    n = (len(all_changes["new_positions"])
         + len(all_changes["increased"])
         + len(all_changes["exited"]))
    print(f"ARK scan done: {n} changes across {len(ARK_FUNDS)} funds")
    return all_changes


if __name__ == "__main__":
    changes = run()
    print("\nNew positions:", list(changes["new_positions"].keys()))
    print("Increased:",     list(changes["increased"].keys()))
    print("Exited:",        list(changes["exited"].keys()))
