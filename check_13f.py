#!/usr/bin/env python3
"""
SEC EDGAR 13F Filing Monitor
Weekly check for new quarterly holdings disclosures from major institutional funds.
Detects new positions, biggest increases, and complete exits.
"""

import json
import os
import sys
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

HEADERS = {
    "User-Agent": "13f-monitor-routine francis117@gmail.com",
    "Accept-Encoding": "gzip, deflate",
    "Host": "data.sec.gov",
}

WEB_HEADERS = {
    "User-Agent": "13f-monitor-routine francis117@gmail.com",
    "Accept-Encoding": "gzip, deflate",
}

# CIK numbers for major funds (zero-padded to 10 digits for the API)
FUNDS = {
    "Berkshire Hathaway": "0001067983",
    "Duquesne Family Office (Druckenmiller)": "0001418848",
    "Tiger Global Management": "0001167483",
    "Renaissance Technologies": "0001037389",
    "Bridgewater Associates": "0001350694",
    "Pershing Square Capital": "0001336528",
    "Third Point LLC (Dan Loeb)": "0001040273",
    "Appaloosa Management (Tepper)": "0001041514",
    "Elliott Investment Management": "0001048268",
    "Coatue Management": "0001336099",
}

DATA_DIR = Path("data/13f_fund_history")
DATA_DIR.mkdir(parents=True, exist_ok=True)
CHANGES_PATH = Path("data/13f_latest_changes.json")


# --------------------------------------------------------------------------- #
# EDGAR helpers
# --------------------------------------------------------------------------- #

def get(url, headers=None, retries=3):
    h = headers or HEADERS
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=h, timeout=30)
            r.raise_for_status()
            return r
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


def get_submissions(cik: str) -> dict:
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    return get(url).json()


def get_13f_filings(submissions: dict) -> list:
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    filings = []
    for i, form in enumerate(forms):
        if form in ("13F-HR", "13F-HR/A"):
            filings.append({
                "form": form,
                "date": dates[i],
                "accession": accessions[i],
                "primary_doc": primary_docs[i] if i < len(primary_docs) else "",
            })
    return sorted(filings, key=lambda x: x["date"], reverse=True)


def get_holding_xml_url(cik: str, accession: str) -> str | None:
    """Return URL of the infotable XML inside a 13F filing."""
    cik_int = int(cik)
    acc_clean = accession.replace("-", "")
    index_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}"
        f"/{acc_clean}/{accession}-index.json"
    )
    try:
        index = get(index_url, headers=WEB_HEADERS).json()
    except Exception:
        return None

    items = index.get("directory", {}).get("item", [])
    for item in items:
        name = item.get("name", "").lower()
        if "infotable" in name and name.endswith(".xml"):
            return (
                f"https://www.sec.gov/Archives/edgar/data/{cik_int}"
                f"/{acc_clean}/{item['name']}"
            )
    # Fallback: any non-primary XML
    for item in items:
        name = item.get("name", "").lower()
        if name.endswith(".xml") and "primary" not in name:
            return (
                f"https://www.sec.gov/Archives/edgar/data/{cik_int}"
                f"/{acc_clean}/{item['name']}"
            )
    return None


def parse_holdings(xml_text: str) -> dict:
    """Return {cusip: {name, value_usd, shares}} from 13F infotable XML."""
    holdings = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return holdings

    ns_prefix = ""
    if root.tag.startswith("{"):
        ns_prefix = root.tag.split("}")[0] + "}"

    for entry in root.iter(f"{ns_prefix}infoTable"):
        name = (entry.findtext(f"{ns_prefix}nameOfIssuer") or "").strip()
        cusip = (entry.findtext(f"{ns_prefix}cusip") or "").strip()
        value_str = (entry.findtext(f"{ns_prefix}value") or "0").strip()
        shares_el = entry.find(f"{ns_prefix}shrsOrPrnAmt")
        shares_str = "0"
        if shares_el is not None:
            shares_str = (shares_el.findtext(f"{ns_prefix}sshPrnamt") or "0").strip()

        if name and cusip:
            try:
                holdings[cusip] = {
                    "name": name,
                    "value_usd": int(value_str.replace(",", "")) * 1000,
                    "shares": int(shares_str.replace(",", "")),
                }
            except ValueError:
                pass

    return holdings


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #

def load_prev(fund_name: str) -> dict:
    path = DATA_DIR / f"{_safe(fund_name)}.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def save_curr(fund_name: str, holdings: dict, filing_date: str):
    path = DATA_DIR / f"{_safe(fund_name)}.json"
    with open(path, "w") as f:
        json.dump({"date": filing_date, "holdings": holdings}, f)


def _safe(name: str) -> str:
    return name.replace(" ", "_").replace("/", "_").replace("(", "").replace(")", "")


# --------------------------------------------------------------------------- #
# Diff logic
# --------------------------------------------------------------------------- #

def diff_holdings(prev_data: dict, curr: dict) -> dict:
    prev = prev_data.get("holdings", {})

    new_pos, increased, exited = [], [], []

    for cusip, c in curr.items():
        if cusip not in prev:
            new_pos.append({**c, "cusip": cusip})
        else:
            p = prev[cusip]
            if p["shares"] == 0:
                continue
            pct = (c["shares"] - p["shares"]) / p["shares"] * 100
            if pct >= 15:
                increased.append({**c, "cusip": cusip, "pct_change": pct,
                                   "prev_shares": p["shares"]})

    for cusip, p in prev.items():
        if cusip not in curr:
            exited.append({**p, "cusip": cusip})

    key = lambda x: x["value_usd"]
    return {
        "new_positions": sorted(new_pos, key=key, reverse=True),
        "increased": sorted(increased, key=key, reverse=True),
        "exited": sorted(exited, key=lambda x: x.get("value_usd", 0), reverse=True),
    }


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #

def fmt_usd(v: int) -> str:
    if v >= 1_000_000_000:
        return f"${v/1_000_000_000:.2f}B"
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    return f"${v/1_000:.0f}K"


def fmt_shares(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M sh"
    if n >= 1_000:
        return f"{n/1_000:.1f}K sh"
    return f"{n} sh"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def run() -> tuple[str, int]:
    report_parts = []
    total_new_filings = 0
    total_signals = 0

    for fund_name, cik in FUNDS.items():
        time.sleep(0.6)  # Respect EDGAR rate limits
        print(f"[{fund_name}] querying...", flush=True)

        try:
            subs = get_submissions(cik)
            filings = get_13f_filings(subs)
        except Exception as e:
            report_parts.append(f"ERROR fetching {fund_name}: {e}")
            continue

        if not filings:
            continue

        latest = filings[0]
        prev_data = load_prev(fund_name)
        prev_date = prev_data.get("date", "")

        if latest["date"] <= prev_date:
            print(f"  -> no new filing (last seen {prev_date})")
            continue

        print(f"  -> NEW filing {latest['date']}", flush=True)
        total_new_filings += 1

        time.sleep(0.6)
        xml_url = get_holding_xml_url(cik, latest["accession"])
        if not xml_url:
            report_parts.append(f"⚠️  {fund_name}: could not locate infotable XML")
            continue

        try:
            xml_text = get(xml_url, headers=WEB_HEADERS).text
        except Exception as e:
            report_parts.append(f"⚠️  {fund_name}: failed to download XML – {e}")
            continue

        curr_holdings = parse_holdings(xml_text)
        if not curr_holdings:
            report_parts.append(f"⚠️  {fund_name}: parsed 0 holdings from XML")
            continue

        print(f"  -> {len(curr_holdings)} positions parsed")

        section_lines = [
            f"\n{'━'*52}",
            f"  {fund_name.upper()}",
            f"  13F filed {latest['date']} · {len(curr_holdings)} positions",
        ]

        if prev_data:
            diff = diff_holdings(prev_data, curr_holdings)

            if diff["new_positions"]:
                section_lines.append(f"\n  NEW POSITIONS ({len(diff['new_positions'])} total):")
                for p in diff["new_positions"][:6]:
                    section_lines.append(f"    + {p['name']:<35} {fmt_usd(p['value_usd']):>10}  ({fmt_shares(p['shares'])})")
                total_signals += len(diff["new_positions"])

            if diff["increased"]:
                section_lines.append(f"\n  BIGGEST INCREASES ({len(diff['increased'])} total ≥15%):")
                for p in diff["increased"][:6]:
                    section_lines.append(
                        f"    ▲ {p['name']:<35} {fmt_usd(p['value_usd']):>10}  (+{p['pct_change']:.0f}%)"
                    )
                total_signals += len(diff["increased"])

            if diff["exited"]:
                section_lines.append(f"\n  COMPLETE EXITS ({len(diff['exited'])} total):")
                for p in diff["exited"][:5]:
                    section_lines.append(f"    ✕ {p['name']:<35} was {fmt_usd(p.get('value_usd', 0)):>10}")
                total_signals += len(diff["exited"])

            if not any([diff["new_positions"], diff["increased"], diff["exited"]]):
                section_lines.append("  (no significant position changes vs prior quarter)")
        else:
            section_lines.append("  (first capture – baseline saved, diffs will appear next week)")
            # Show top holdings on first run
            top = sorted(curr_holdings.values(), key=lambda x: x["value_usd"], reverse=True)[:10]
            section_lines.append(f"\n  TOP HOLDINGS:")
            for h in top:
                section_lines.append(f"    · {h['name']:<35} {fmt_usd(h['value_usd']):>10}")

        report_parts.append("\n".join(section_lines))
        save_curr(fund_name, curr_holdings, latest["date"])

    # Build final report
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    header = (
        f"SEC EDGAR 13F WEEKLY MONITOR – {now}\n"
        f"Funds checked: {len(FUNDS)} | New filings: {total_new_filings} | Signals: {total_signals}"
    )

    if not report_parts:
        return header + "\n\nNo new 13F filings found this week.", 0

    return header + "\n" + "\n".join(report_parts), total_new_filings


def run_and_save() -> tuple[str, int]:
    """Run the scanner and write structured changes to data/13f_latest_changes.json."""
    all_changes = {}

    for fund_name, cik in FUNDS.items():
        time.sleep(0.6)
        print(f"  [{fund_name}] checking...", flush=True)

        try:
            subs = get_submissions(cik)
            filings = get_13f_filings(subs)
        except Exception as e:
            print(f"    error: {e}")
            continue

        if not filings:
            continue

        latest = filings[0]
        prev_data = load_prev(fund_name)
        prev_date = prev_data.get("date", "")

        if latest["date"] <= prev_date:
            continue

        print(f"    new filing: {latest['date']}", flush=True)
        time.sleep(0.6)
        xml_url = get_holding_xml_url(cik, latest["accession"])
        if not xml_url:
            continue

        try:
            xml_text = get(xml_url, headers=WEB_HEADERS).text
        except Exception:
            continue

        curr_holdings = parse_holdings(xml_text)
        if not curr_holdings:
            continue

        if prev_data:
            diff = diff_holdings(prev_data, curr_holdings)
            # Add ticker field (13F filings don't include tickers, only CUSIPs)
            for lst in (diff["new_positions"], diff["increased"], diff["exited"]):
                for pos in lst:
                    pos.setdefault("ticker", "")
            if any(diff[k] for k in ("new_positions", "increased", "exited")):
                all_changes[fund_name] = diff

        save_curr(fund_name, curr_holdings, latest["date"])

    with open(CHANGES_PATH, "w") as f:
        json.dump(all_changes, f, indent=2)

    n = sum(
        len(v.get("new_positions", [])) + len(v.get("increased", [])) + len(v.get("exited", []))
        for v in all_changes.values()
    )
    print(f"13F scan complete: {len(all_changes)} funds with new filings, {n} position changes")
    return all_changes, n


if __name__ == "__main__":
    report, new_filings = run()
    print("\n" + "=" * 60)
    print(report)
    # Write report to disk for the cron prompt to read
    out = Path("/home/user/.edgar_monitor/last_report.txt")
    out.write_text(report)
    print(f"\nReport saved to {out}")
    sys.exit(0 if new_filings >= 0 else 1)
