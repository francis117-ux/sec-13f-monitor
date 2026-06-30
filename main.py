#!/usr/bin/env python3
"""
Smart Money Monitor — Main Orchestrator
Daily scan: watches US institutional money → maps to Taiwan supply chain stocks →
identifies specific TW stocks worth considering as short-to-medium-term trades.

Pipeline:
  1. Form 4 insider trades (SEC)
  2. TWSE 三大法人 institutional flows (Taiwan Stock Exchange)
  3. 13F quarterly fund holdings (10 major funds, SEC)
  4. ARK daily fund holdings (Cathie Wood, daily)
  5. Activist 13D/13G filings (5%+ stake crosses, SEC)
  6. TW stock price + valuation + technicals
  7. News for flagged stocks
  8. Build report with trade setups
"""

import json
import sys
from datetime import datetime
from pathlib import Path

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)


# ── Utility ───────────────────────────────────────────────────────────────────

def load_json(path, default=None):
    p = Path(path)
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return default if default is not None else {}


def load_supply_chain() -> dict:
    with open("supply_chain.json") as f:
        d = json.load(f)
    d.pop("_comment", None)
    return d


def fmt_usd(v: float) -> str:
    v = abs(v)
    if v >= 1_000_000_000:
        return f"${v/1_000_000_000:.1f}B"
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    return f"${v/1_000:.0f}K"


def fmt_shares(n: int) -> str:
    if abs(n) >= 1_000_000:
        return f"{n/1_000_000:+.1f}M"
    if abs(n) >= 1_000:
        return f"{n/1_000:+.0f}K"
    return f"{n:+,}"


# ── Step runners ──────────────────────────────────────────────────────────────

def run_form4() -> dict:
    print("Step 1/5: Form 4 insider trades...", flush=True)
    try:
        import form4_scanner
        return form4_scanner.run(days_back=1)
    except Exception as e:
        print(f"  error: {e}")
        return {"buys": [], "sells": []}


def run_twse() -> dict:
    print("Step 2/5: TWSE 三大法人...", flush=True)
    try:
        import twse_scanner
        return twse_scanner.run(days_back=0)
    except Exception as e:
        print(f"  error: {e}")
        return {}


def run_13f() -> dict:
    print("Step 3/5: 13F institutional fund filings...", flush=True)
    try:
        import check_13f
        check_13f.run_and_save()
    except Exception as e:
        print(f"  error: {e}")
    return load_json("data/13f_latest_changes.json", {})


def run_ark() -> dict:
    print("Step 4/5: ARK daily fund changes...", flush=True)
    try:
        import ark_scanner
        return ark_scanner.run()
    except Exception as e:
        print(f"  error: {e}")
        return {"new_positions": {}, "increased": {}, "exited": {}}


def run_activist() -> list:
    print("Step 5/5: Activist 13D filings...", flush=True)
    try:
        import activist_scanner
        return activist_scanner.run()
    except Exception as e:
        print(f"  error: {e}")
        return []


# ── Signal aggregation ────────────────────────────────────────────────────────

def aggregate_tw_signals(
    form4: dict,
    twse: dict,
    inst_13f: dict,
    ark_changes: dict,
    activist: list,
    supply_chain: dict,
) -> dict:
    """
    Maps all US + TW signals to specific Taiwan stocks.
    Returns {tw_ticker: {"score", "name", "us_signals", "twse_signal", "direction"}}.

    Scoring guide:
      +2  13F new position or activist 13D stake — high conviction, deliberate new bet
      +2  TWSE aligned buy (two or more institutions moving together)
      +1  Form 4 insider buy, ARK new/increased position, TWSE single-institution buy
    """
    tw_stocks = {}

    def ensure(ticker, name):
        if ticker not in tw_stocks:
            tw_stocks[ticker] = {
                "ticker": ticker, "name": name,
                "us_signals": [], "twse_signal": None,
                "score": 0, "direction": "BULLISH",
            }
        return tw_stocks[ticker]

    # Form 4 insider buys → BULLISH +1
    for tx in form4.get("buys", []):
        for sup in supply_chain.get(tx["ticker"], {}).get("tw_suppliers", []):
            s = ensure(sup["ticker"], sup["name"])
            s["us_signals"].append(
                f"Insider BUY {tx['ticker']}: {tx['exec_name']} +{fmt_usd(tx['total_value'])}"
            )
            s["score"] += 1

    # Form 4 insider sells → note as BEARISH if no bullish signals yet
    for tx in form4.get("sells", [])[:5]:
        for sup in supply_chain.get(tx["ticker"], {}).get("tw_suppliers", []):
            s = ensure(sup["ticker"], sup["name"])
            s["us_signals"].append(
                f"Insider SELL {tx['ticker']}: {tx['exec_name']} -{fmt_usd(tx['total_value'])}"
            )
            bullish_already = any(
                any(word in sig for word in ("BUY", "New Position", "new", "ARK", "13F", "13D"))
                for sig in s["us_signals"][:-1]
            )
            if not bullish_already:
                s["direction"] = "BEARISH"
            s["score"] += 1

    # 13F new positions → BULLISH +2 (fund took a brand-new deliberate stake)
    for fund, changes in inst_13f.items():
        for pos in changes.get("new_positions", [])[:5]:
            ticker = pos.get("ticker", "")
            for sup in supply_chain.get(ticker, {}).get("tw_suppliers", []):
                s = ensure(sup["ticker"], sup["name"])
                s["us_signals"].append(
                    f"13F {fund}: NEW position {pos.get('name','?')} {fmt_usd(pos.get('value_usd',0))}"
                )
                s["score"] += 2

    # 13F increased positions → BULLISH +1
    for fund, changes in inst_13f.items():
        for pos in changes.get("increased", [])[:5]:
            ticker = pos.get("ticker", "")
            for sup in supply_chain.get(ticker, {}).get("tw_suppliers", []):
                s = ensure(sup["ticker"], sup["name"])
                s["us_signals"].append(
                    f"13F {fund}: increased {pos.get('name','?')} +{pos.get('pct_change',0):.0f}%"
                )
                s["score"] += 1

    # ARK new positions → BULLISH +1
    for ticker, c in (ark_changes or {}).get("new_positions", {}).items():
        for sup in supply_chain.get(ticker, {}).get("tw_suppliers", []):
            s = ensure(sup["ticker"], sup["name"])
            s["us_signals"].append(
                f"ARK {c['fund']}: NEW {ticker} ({c['shares']:,} shares)"
            )
            s["score"] += 1

    # ARK increases → BULLISH +1
    for ticker, c in (ark_changes or {}).get("increased", {}).items():
        for sup in supply_chain.get(ticker, {}).get("tw_suppliers", []):
            s = ensure(sup["ticker"], sup["name"])
            s["us_signals"].append(
                f"ARK {c['fund']}: increased {ticker} +{c['pct_change']:.0f}%"
            )
            s["score"] += 1

    # Activist 13D → BULLISH +2 (activist with >5% stake is a strong signal)
    for sig in (activist or []):
        ticker = sig.get("us_ticker", "")
        if not ticker:
            continue
        for sup in supply_chain.get(ticker, {}).get("tw_suppliers", []):
            s = ensure(sup["ticker"], sup["name"])
            s["us_signals"].append(
                f"Activist 13D: {sig['acquirer']} took ~{sig.get('pct','5+')}% in {sig['company']}"
            )
            s["score"] += 2

    # TWSE signals — build indexes for efficient lookup
    aligned_buy_set  = {s["ticker"] for s in twse.get("aligned_buys", [])}
    single_buy_set   = {s["ticker"] for s in twse.get("single_institution_buys", [])}
    aligned_sell_set = {s["ticker"] for s in twse.get("aligned_sells", [])}
    twse_index = {
        s["ticker"]: s
        for s in (
            twse.get("aligned_buys", [])
            + twse.get("aligned_sells", [])
            + twse.get("single_institution_buys", [])
        )
    }

    # Apply TWSE scores to all relevant TW tickers (whether US-signal exists or not)
    seen = set()
    for tw_ticker in set(tw_stocks) | set(twse_index):
        if tw_ticker in twse_index and tw_ticker not in seen:
            twse_stock = twse_index[tw_ticker]
            s = ensure(tw_ticker, twse_stock["name"])
            s["twse_signal"] = twse_stock
            seen.add(tw_ticker)

            if tw_ticker in aligned_buy_set:
                s["score"] += 2
            elif tw_ticker in single_buy_set:
                s["score"] += 1
            elif tw_ticker in aligned_sell_set:
                s["score"] += 1
                if not s["us_signals"]:
                    s["direction"] = "BEARISH"

    return tw_stocks


# ── Report builder ────────────────────────────────────────────────────────────

def _setup_block(tw_ticker: str, s: dict, td: dict, news: dict) -> list[str]:
    """Format one trade setup block (HIGH or MEDIUM conviction)."""
    lines = []
    icon = "▲" if s["direction"] == "BULLISH" else "▼"

    lines.append(f"\n{icon}  {tw_ticker}  {s['name']}")

    # Why — signal sources
    lines.append("  WHY:")
    for sig in s["us_signals"][:3]:
        lines.append(f"    • {sig}")
    if s["twse_signal"]:
        ts = s["twse_signal"]
        insts = " + ".join(i["institution"].split("(")[0].strip() for i in ts["signals"])
        lines.append(
            f"    • 三大法人: {insts} aligned {ts['overall_direction']} "
            f"{fmt_shares(ts['total_net'])} shares net"
        )

    # Trade parameters
    if td and "current_price" in td:
        cp   = td["current_price"]
        stop = td.get("stop_loss", 0)
        tgt  = td.get("target", 0)
        rr   = td.get("rr_ratio", 0)
        rr_note = "good" if rr >= 1.5 else "marginal"

        lines.append("  THE TRADE:")
        lines.append(f"    Current price:  NT${cp:>8,.1f}")
        lines.append(f"    Entry zone:     NT${td.get('entry_low',0):>8,.1f} – NT${td.get('entry_high',0):,.1f}")
        lines.append(f"    Stop-loss:      NT${stop:>8,.1f}  (limits loss to ~{td.get('risk_pct',7):.0f}%)")
        lines.append(f"    Target:         NT${tgt:>8,.1f}")
        lines.append(f"    Risk / reward:  1:{rr}  ({rr_note})")
        lines.append(f"    Timeline:       8–16 weeks")
        lines.append(f"    Position size:  5% of trading capital  (suggested max)")
        ma_note = "above" if td.get("above_ma20") else "BELOW — caution"
        lines.append(f"    Trend check:    {ma_note} 20-day average (MA20 = NT${td.get('ma20',0):,.1f})")
        lines.append(f"    Valuation:      {td.get('valuation_label', 'N/A')}")
    else:
        lines.append("  THE TRADE:  [price data unavailable — check manually]")

    # News
    tw_news = (news.get("TW") or {}).get(tw_ticker, [])
    if tw_news:
        lines.append("  NEWS:")
        for n in tw_news[:3]:
            lines.append(f"    {n['title'][:72]}")
            lines.append(f"    {n['url']}")

    return lines


def build_report(
    tw_stocks: dict,
    twse: dict,
    form4: dict,
    ark_changes: dict,
    activist: list,
    tw_data: dict,
    news: dict,
) -> tuple[str, str]:
    """Build the full report. Returns (report_text, banner_text)."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines = []

    # Rank and split by conviction
    ranked  = sorted(tw_stocks.items(), key=lambda x: x[1]["score"], reverse=True)
    bullish = [(t, s) for t, s in ranked if s["direction"] == "BULLISH"]
    bearish = [(t, s) for t, s in ranked if s["direction"] == "BEARISH"]

    high    = [(t, s) for t, s in bullish if s["score"] >= 3]
    medium  = [(t, s) for t, s in bullish if s["score"] == 2]
    watch   = [(t, s) for t, s in bullish if s["score"] == 1]

    # ─ Header ─
    lines += [
        f"SMART MONEY MONITOR — {now}",
        "=" * 56,
        (
            f"High conviction: {len(high)}  |  Medium: {len(medium)}  |  "
            f"Watch list: {len(watch)}  |  Bearish: {len(bearish)}"
        ),
        "",
    ]

    # ─ High conviction ─
    lines.append("━" * 56)
    lines.append(f"HIGH CONVICTION ({len(high)})  — score 3+, multiple signals aligned")
    lines.append("━" * 56)
    if high:
        for tw_ticker, s in high[:4]:
            lines += _setup_block(tw_ticker, s, tw_data.get(tw_ticker, {}), news)
    else:
        lines.append("  None today.")

    # ─ Medium conviction ─
    lines.append("")
    lines.append("━" * 56)
    lines.append(f"MEDIUM CONVICTION ({len(medium)})  — score 2, consider waiting for one more signal")
    lines.append("━" * 56)
    if medium:
        for tw_ticker, s in medium[:4]:
            lines += _setup_block(tw_ticker, s, tw_data.get(tw_ticker, {}), news)
    else:
        lines.append("  None today.")

    # ─ Watch list ─
    if watch:
        lines.append("")
        lines.append("━" * 56)
        lines.append(f"WATCH LIST ({len(watch)})  — early signal, not actionable yet")
        lines.append("━" * 56)
        for tw_ticker, s in watch[:8]:
            twse_note = ""
            if s["twse_signal"]:
                twse_note = f"  | TW flow: {fmt_shares(s['twse_signal']['total_net'])}"
            lines.append(f"  {tw_ticker}  {s['name']}{twse_note}")
            for sig in s["us_signals"][:1]:
                lines.append(f"    → {sig}")

    # ─ Bearish ─
    if bearish:
        lines.append("")
        lines.append("━" * 56)
        lines.append(f"BEARISH SIGNALS ({len(bearish)})  — avoid or reduce if holding")
        lines.append("━" * 56)
        for tw_ticker, s in bearish[:4]:
            lines.append(f"  ▼  {tw_ticker}  {s['name']}")
            for sig in s["us_signals"][:2]:
                lines.append(f"    → {sig}")

    # ─ TWSE flows ─
    lines.append("")
    lines.append("━" * 56)
    lines.append("TAIWAN 三大法人 FLOWS")
    lines.append("━" * 56)
    abuys  = twse.get("aligned_buys", [])
    asells = twse.get("aligned_sells", [])
    lines.append(
        f"  Aligned buys: {len(abuys)} stocks  |  Aligned sells: {len(asells)} stocks"
    )
    if abuys:
        lines.append("  Top aligned buys:")
        for s in abuys[:5]:
            insts = "+".join(i["institution"].split("(")[0].strip() for i in s["signals"])
            lines.append(f"    {s['ticker']}  {s['name']}  [{insts}]  {fmt_shares(s['total_net'])}")
    if asells:
        lines.append("  Top aligned sells:")
        for s in asells[:3]:
            insts = "+".join(i["institution"].split("(")[0].strip() for i in s["signals"])
            lines.append(f"    {s['ticker']}  {s['name']}  [{insts}]  {fmt_shares(s['total_net'])}")

    # ─ US summary ─
    lines.append("")
    lines.append("━" * 56)
    lines.append("US SIGNALS SUMMARY")
    lines.append("━" * 56)

    if form4.get("buys"):
        lines.append("  Insider buys (Form 4):")
        for tx in form4["buys"][:3]:
            lines.append(
                f"    {tx['ticker']}  {tx['company']}: "
                f"{tx['exec_name']} +{fmt_usd(tx['total_value'])}"
            )

    ark_new = (ark_changes or {}).get("new_positions", {})
    if ark_new:
        lines.append("  ARK new positions:")
        for ticker, c in list(ark_new.items())[:3]:
            lines.append(f"    {ticker}  {c['company']} — {c['fund']} {c['shares']:,} sh")

    if activist:
        lines.append("  Activist 13D filings:")
        for sig in activist[:3]:
            lines.append(
                f"    {sig.get('company','?')} ({sig.get('us_ticker','?')}): "
                f"{sig['acquirer']} ~{sig.get('pct','5+')}%"
            )

    us_news_section = []
    us_news = (news.get("US") or {})
    for ticker, items in list(us_news.items())[:3]:
        if items:
            us_news_section.append(f"  {ticker}:")
            for n in items[:2]:
                us_news_section.append(f"    {n['title'][:70]}")
                us_news_section.append(f"    {n['url']}")
    if us_news_section:
        lines.append("")
        lines.append("  US News:")
        lines += us_news_section

    report = "\n".join(lines)

    # Banner (short notification summary)
    if high:
        top = ", ".join(f"{t} {s['name'].split()[0]}" for t, s in high[:2])
        banner = (
            f"{len(high)} high-conviction TW setup(s) today: {top}. "
            f"TWSE: {len(abuys)} aligned buys."
        )
    elif medium:
        top = ", ".join(f"{t} {s['name'].split()[0]}" for t, s in medium[:2])
        banner = (
            f"{len(medium)} medium-conviction TW setup(s): {top}. "
            f"No high-conviction signals yet."
        )
    else:
        banner = (
            f"No high-conviction TW setups today. "
            f"TWSE: {len(abuys)} aligned buys, {len(asells)} aligned sells."
        )

    return report, banner


# ── Main ─────────────────────────────────────────────────────────────────────

def run():
    supply_chain = load_supply_chain()

    # Run all scanners
    form4      = run_form4()
    twse       = run_twse()
    inst_13f   = run_13f()
    ark_changes = run_ark()
    activist   = run_activist()

    # Aggregate signals → ranked TW stock candidates
    print("\nAggregating signals...", flush=True)
    tw_stocks = aggregate_tw_signals(
        form4, twse, inst_13f, ark_changes, activist, supply_chain
    )

    # Identify top candidates (score >= 2) for price/valuation fetch
    ranked = sorted(tw_stocks.items(), key=lambda x: x[1]["score"], reverse=True)
    top_tw_tickers = [t for t, s in ranked if s["score"] >= 2 and s["direction"] == "BULLISH"]

    # Collect the US tickers that triggered signals (for US news fetch)
    triggered_us_tickers = list({
        tx["ticker"] for tx in form4.get("buys", [])[:5]
    } | set(list(ark_changes.get("new_positions", {}))[:3]))

    # Fetch TW price + valuation + technicals
    tw_data = {}
    if top_tw_tickers:
        print(f"\nFetching TW stock data for {len(top_tw_tickers[:8])} candidates...", flush=True)
        try:
            import tw_stock_data
            tw_data = tw_stock_data.get_tw_stocks_batch(top_tw_tickers, max_stocks=8)
        except Exception as e:
            print(f"  TW stock data error: {e}")

    # Fetch news
    all_tw_tickers = [t for t, _ in ranked[:10]]
    news = {}
    print(f"\nFetching news...", flush=True)
    try:
        import news_fetcher
        news = news_fetcher.fetch_news_batch(triggered_us_tickers, all_tw_tickers)
    except Exception as e:
        print(f"  News fetch error: {e}")

    # Build and save report
    print("\nBuilding report...", flush=True)
    report, banner = build_report(
        tw_stocks, twse, form4, ark_changes, activist, tw_data, news
    )

    report_path = DATA_DIR / "latest_report.txt"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
        f.write(f"\n\nBANNER: {banner}\n")

    print("\n" + "=" * 56)
    print(report)
    print(f"\nBANNER: {banner}")

    return report, banner


if __name__ == "__main__":
    run()
