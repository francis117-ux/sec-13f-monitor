#!/usr/bin/env python3
"""
SEC + TWSE Smart Money Monitor — Main Orchestrator
Runs daily. Coordinates Form 4 insider scanner, 13F institutional scanner,
TWSE 三大法人 scanner, supply chain correlation, and push notification.
"""

import json
import sys
import subprocess
from datetime import datetime
from pathlib import Path

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)


def load_json(path):
    p = Path(path)
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return {}


def load_supply_chain():
    with open("supply_chain.json") as f:
        return json.load(f)


def fmt_usd(v):
    if v >= 1_000_000_000:
        return f"${v/1_000_000_000:.1f}B"
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    return f"${v/1_000:.0f}K"


def fmt_shares(n):
    if abs(n) >= 1_000_000:
        return f"{n/1_000_000:+.1f}M"
    if abs(n) >= 1_000:
        return f"{n/1_000:+.0f}K"
    return f"{n:+,}"


def find_tw_suppliers(us_ticker, supply_chain):
    """Return Taiwan suppliers for a given US ticker."""
    ticker_clean = us_ticker.upper().replace(".", "")
    return supply_chain.get(ticker_clean, {}).get("tw_suppliers", [])


def correlate_with_twse(tw_suppliers, twse_data):
    """Check if any Taiwan suppliers appear in TWSE signals."""
    all_tw_signals = (
        twse_data.get("aligned_buys", []) +
        twse_data.get("aligned_sells", []) +
        twse_data.get("single_institution_buys", []) +
        twse_data.get("single_institution_sells", [])
    )
    tw_index = {s["ticker"]: s for s in all_tw_signals}

    correlated = []
    for supplier in tw_suppliers:
        tw_ticker = supplier["ticker"]
        if tw_ticker in tw_index:
            correlated.append({
                **supplier,
                "twse_signal": tw_index[tw_ticker],
            })
    return correlated


def build_report(form4, twse, supply_chain, run_mode="daily"):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"SMART MONEY MONITOR — {now}",
        f"Mode: {run_mode.upper()}",
        "=" * 56,
    ]

    double_confirmed = []
    bullish_signals = []
    bearish_signals = []

    # --- Form 4 Insider signals ---
    insider_buys = form4.get("buys", [])[:10]
    insider_sells = form4.get("sells", [])[:10]

    for tx in insider_buys:
        ticker = tx["ticker"]
        tw_suppliers = find_tw_suppliers(ticker, supply_chain)
        tw_corr = correlate_with_twse(tw_suppliers, twse)

        signal = {
            "source": "Insider BUY (Form 4)",
            "us_ticker": ticker,
            "us_company": tx["company"],
            "detail": f"{tx['exec_name']} ({tx['exec_title']}) bought {fmt_usd(tx['total_value'])}",
            "tw_correlated": tw_corr,
            "direction": "BULLISH",
        }
        bullish_signals.append(signal)
        if tw_corr:
            double_confirmed.append(signal)

    for tx in insider_sells:
        ticker = tx["ticker"]
        tw_suppliers = find_tw_suppliers(ticker, supply_chain)
        tw_corr = correlate_with_twse(tw_suppliers, twse)

        signal = {
            "source": "Insider SELL (Form 4)",
            "us_ticker": ticker,
            "us_company": tx["company"],
            "detail": f"{tx['exec_name']} ({tx['exec_title']}) sold {fmt_usd(tx['total_value'])}",
            "tw_correlated": tw_corr,
            "direction": "BEARISH",
        }
        bearish_signals.append(signal)
        if tw_corr:
            double_confirmed.append(signal)

    # --- 13F Institutional signals (if available) ---
    inst_path = DATA_DIR / "13f_latest_changes.json"
    if inst_path.exists():
        with open(inst_path) as f:
            inst_data = json.load(f)

        for fund, changes in inst_data.items():
            for pos in changes.get("new_positions", [])[:5]:
                ticker = pos.get("ticker", "")
                tw_suppliers = find_tw_suppliers(ticker, supply_chain)
                tw_corr = correlate_with_twse(tw_suppliers, twse)
                signal = {
                    "source": f"13F New Position ({fund})",
                    "us_ticker": ticker,
                    "us_company": pos.get("name", ""),
                    "detail": f"New position {fmt_usd(pos.get('value_usd', 0))}",
                    "tw_correlated": tw_corr,
                    "direction": "BULLISH",
                }
                bullish_signals.append(signal)
                if tw_corr:
                    double_confirmed.append(signal)

            for pos in changes.get("exited", [])[:5]:
                ticker = pos.get("ticker", "")
                tw_suppliers = find_tw_suppliers(ticker, supply_chain)
                tw_corr = correlate_with_twse(tw_suppliers, twse)
                signal = {
                    "source": f"13F Full Exit ({fund})",
                    "us_ticker": ticker,
                    "us_company": pos.get("name", ""),
                    "detail": f"Exited, was {fmt_usd(pos.get('value_usd', 0))}",
                    "tw_correlated": tw_corr,
                    "direction": "BEARISH",
                }
                bearish_signals.append(signal)
                if tw_corr:
                    double_confirmed.append(signal)

    # --- Build report sections ---

    # Double-confirmed first
    if double_confirmed:
        lines.append(f"\n🔴 DOUBLE-CONFIRMED SIGNALS ({len(double_confirmed)}) — US + TW moving together")
        for s in double_confirmed:
            icon = "▲" if s["direction"] == "BULLISH" else "▼"
            lines.append(f"\n  {icon} {s['us_ticker']} | {s['source']}")
            lines.append(f"    {s['detail']}")
            for tw in s["tw_correlated"]:
                tw_sig = tw["twse_signal"]
                insts = ", ".join(i["institution"] for i in tw_sig["signals"])
                lines.append(
                    f"    → TW {tw['ticker']} {tw['name']} ({tw['relationship']})"
                    f" | {insts} {tw_sig['overall_direction']} {fmt_shares(tw_sig['total_net'])} shares"
                )
    else:
        lines.append("\n(No double-confirmed signals today)")

    # Bullish
    lines.append(f"\n{'━'*56}")
    lines.append(f"BULLISH SIGNALS ({len(bullish_signals)} total)")
    for s in bullish_signals[:8]:
        lines.append(f"\n  ▲ {s['us_ticker']} — {s['source']}")
        lines.append(f"    {s['detail']}")
        if s["tw_correlated"]:
            for tw in s["tw_correlated"][:2]:
                lines.append(f"    → TW supplier: {tw['ticker']} {tw['name']}")

    # Bearish
    lines.append(f"\n{'━'*56}")
    lines.append(f"BEARISH SIGNALS ({len(bearish_signals)} total)")
    for s in bearish_signals[:8]:
        lines.append(f"\n  ▼ {s['us_ticker']} — {s['source']}")
        lines.append(f"    {s['detail']}")
        if s["tw_correlated"]:
            for tw in s["tw_correlated"][:2]:
                lines.append(f"    → TW supplier: {tw['ticker']} {tw['name']}")

    # TWSE summary
    lines.append(f"\n{'━'*56}")
    lines.append("TAIWAN 三大法人 SUMMARY")
    aligned_buys = twse.get("aligned_buys", [])
    aligned_sells = twse.get("aligned_sells", [])
    lines.append(f"  Aligned Buys: {len(aligned_buys)} stocks | Aligned Sells: {len(aligned_sells)} stocks")
    if aligned_buys:
        lines.append("  Top Buys:")
        for s in aligned_buys[:5]:
            insts = " + ".join(i["institution"].split()[0] for i in s["signals"])
            lines.append(f"    {s['ticker']} {s['name']} [{insts}] net {fmt_shares(s['total_net'])}")
    if aligned_sells:
        lines.append("  Top Sells:")
        for s in aligned_sells[:5]:
            insts = " + ".join(i["institution"].split()[0] for i in s["signals"])
            lines.append(f"    {s['ticker']} {s['name']} [{insts}] net {fmt_shares(s['total_net'])}")

    return "\n".join(lines), len(double_confirmed), len(bullish_signals), len(bearish_signals)


def run():
    supply_chain = load_supply_chain()

    # Step 1: Form 4 insider trades
    print("Step 1/4: Scanning Form 4 insider trades...")
    try:
        import form4_scanner
        form4 = form4_scanner.run(days_back=1)
    except Exception as e:
        print(f"  Form 4 error: {e}")
        form4 = {"buys": [], "sells": []}

    # Step 2: TWSE 三大法人
    print("Step 2/4: Scanning TWSE 三大法人...")
    try:
        import twse_scanner
        twse = twse_scanner.run(days_back=0)
    except Exception as e:
        print(f"  TWSE error: {e}")
        twse = {}

    # Step 3: 13F institutional fund filings
    print("Step 3/4: Checking 13F institutional fund filings...")
    try:
        import check_13f
        check_13f.run_and_save()
    except Exception as e:
        print(f"  13F error: {e}")

    # Step 4: Build report
    print("Step 4/4: Correlating signals and building report...")
    report, n_double, n_bull, n_bear = build_report(form4, twse, supply_chain)

    # Save report
    report_path = DATA_DIR / "latest_report.txt"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    print("\n" + "=" * 56)
    print(report)

    # Build notification summary
    if n_double > 0:
        banner = (
            f"{n_double} double-confirmed signal(s) today — US smart money + TW 三大法人 aligned. "
            f"{n_bull} bullish, {n_bear} bearish."
        )
    elif n_bull + n_bear > 0:
        banner = f"Smart Money Monitor: {n_bull} bullish, {n_bear} bearish signals. No TW cross-confirmation today."
    else:
        banner = "Smart Money Monitor: No signals above threshold today."

    return report, banner


if __name__ == "__main__":
    report, banner = run()
    print(f"\nBANNER: {banner}")
