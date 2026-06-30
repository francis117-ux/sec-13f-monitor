#!/usr/bin/env python3
"""
SEC 13D/13G Activist Filing Scanner
Detects when investors cross the 5% ownership threshold in a public company.

Why this matters: crossing 5% triggers a mandatory SEC disclosure (13D or 13G).
A 13D specifically means the investor has active intentions — they plan to push for
change (new management, buyback, sale of the company, etc.). This is a high-conviction
signal because the investor is taking a large, concentrated bet.

13G is a passive version (index funds, etc.) — less actionable but still notable.
"""

import json
import re
import time
import requests
from datetime import datetime, timedelta
from pathlib import Path

HEADERS = {
    "User-Agent": "sec-monitor-routine francis117@gmail.com",
    "Accept-Encoding": "gzip, deflate",
}

WEB_HEADERS = {
    "User-Agent": "sec-monitor-routine francis117@gmail.com",
}

SIGNALS_PATH = Path("data/activist_signals.json")
Path("data").mkdir(exist_ok=True)


def search_filings(form_type: str = "SC 13D", days_back: int = 2) -> list[dict]:
    """Search EDGAR for recent 13D or 13G filings."""
    end   = datetime.utcnow().strftime("%Y-%m-%d")
    start = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    url = (
        f"https://efts.sec.gov/LATEST/search-index?forms={form_type.replace(' ', '+')}"
        f"&dateRange=custom&startdt={start}&enddt={end}"
        f"&hits.hits.total.value=true"
    )

    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        data = r.json()
    except Exception as e:
        print(f"  13D search error: {e}")
        return []

    hits = data.get("hits", {}).get("hits", [])
    return [
        {
            "filer":     h.get("_source", {}).get("entity_name", ""),
            "file_date": h.get("_source", {}).get("file_date", ""),
            "accession": h.get("_source", {}).get("accession_no", ""),
            "form_type": form_type,
        }
        for h in hits
    ]


def get_filing_excerpt(accession_no: str) -> str:
    """Download first ~6KB of the primary 13D document for parsing."""
    cik_part = accession_no.split("-")[0].lstrip("0")
    acc_clean = accession_no.replace("-", "")
    index_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik_part}"
        f"/{acc_clean}/{accession_no}-index.json"
    )
    try:
        items = requests.get(index_url, headers=WEB_HEADERS, timeout=20).json() \
                        .get("directory", {}).get("item", [])
    except Exception:
        return ""

    for item in items:
        name = item.get("name", "").lower()
        if name.endswith((".htm", ".html", ".txt")) and "index" not in name:
            doc_url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik_part}"
                f"/{acc_clean}/{item['name']}"
            )
            try:
                r = requests.get(doc_url, headers=WEB_HEADERS, timeout=20, stream=True)
                # Read only first 6KB to keep it fast
                chunk = b""
                for c in r.iter_content(chunk_size=1024):
                    chunk += c
                    if len(chunk) >= 6144:
                        break
                return chunk.decode("utf-8", errors="ignore")
            except Exception:
                return ""
    return ""


def _clean(text: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def extract_issuer_and_pct(text: str) -> tuple[str, str, str]:
    """
    Parse 13D filing text for: issuer company name, ticker symbol, % ownership.
    Returns (company, ticker, pct_str). Any field may be empty string.
    """
    clean = _clean(text)

    # Company name — common 13D cover page pattern
    company = ""
    for pattern in [
        r"[Nn]ame\s+of\s+[Ii]ssuer[\s:]+([A-Z][A-Za-z0-9,\.\s&'/-]{4,60}?)(?:\s*[\r\n]|\s{3,}|[,;])",
        r"[Ii]ssuer[\s:]+([A-Z][A-Za-z0-9,\.\s&'/-]{4,60}?)(?:\s+(?:Inc|Corp|Ltd|LLC|Co\.|plc))",
    ]:
        m = re.search(pattern, clean)
        if m:
            company = m.group(1).strip().rstrip(".,;")
            break

    # Ticker — look for exchange: TICKER pattern
    ticker = ""
    m = re.search(
        r"(?:NASDAQ|NYSE|NYSE\s*Arca|AMEX|Nasdaq)[:\s]+([A-Z]{1,5})\b",
        clean
    )
    if m:
        ticker = m.group(1)
    else:
        m = re.search(r"\((?:ticker[:\s]*|symbol[:\s]*)?([A-Z]{2,5})\)", clean)
        if m:
            ticker = m.group(1)

    # Ownership percentage
    pct = "5+"
    for pattern in [
        r"(\d{1,2}\.?\d?)\s*%\s+of\s+(?:the\s+)?(?:outstanding|common|issued)",
        r"approximately\s+(\d{1,2}\.?\d?)\s*(?:%|percent)",
        r"(\d{1,2}\.\d)\s*%\s+of\s+(?:the\s+)?[Cc]ommon",
    ]:
        m = re.search(pattern, clean)
        if m:
            pct = m.group(1)
            break

    return company, ticker, pct


def run() -> list[dict]:
    """Search for recent 13D/13G filings, parse them, write data/activist_signals.json."""
    print("  Searching 13D activist filings (last 2 days)...", flush=True)
    filings = search_filings("SC 13D", days_back=2)
    print(f"  Found {len(filings)} 13D filings", flush=True)

    signals = []
    for filing in filings[:25]:  # cap to avoid rate limiting
        time.sleep(0.5)
        text = get_filing_excerpt(filing["accession"])
        if not text:
            signals.append({
                "acquirer":  filing["filer"],
                "company":   "",
                "us_ticker": "",
                "pct":       "5+",
                "file_date": filing["file_date"],
                "accession": filing["accession"],
            })
            continue

        company, ticker, pct = extract_issuer_and_pct(text)

        signals.append({
            "acquirer":  filing["filer"],
            "company":   company or filing["filer"],
            "us_ticker": ticker,
            "pct":       pct,
            "file_date": filing["file_date"],
            "accession": filing["accession"],
        })

    with open(SIGNALS_PATH, "w") as f:
        json.dump(signals, f, indent=2)

    print(f"  Activist scan done: {len(signals)} filings parsed")
    return signals


if __name__ == "__main__":
    results = run()
    for s in results[:5]:
        print(f"  {s['acquirer']} → {s['company']} ({s['us_ticker']}) ~{s['pct']}%")
