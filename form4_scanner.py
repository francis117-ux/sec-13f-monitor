#!/usr/bin/env python3
"""
SEC Form 4 Insider Trade Scanner
Fetches Form 4 filings from the last 24 hours and filters for meaningful transactions.
Threshold: $500,000+ per transaction, open-market buys (P) and sells (S) only.
"""

import json
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

HEADERS = {
    "User-Agent": "sec-monitor-routine francis117@gmail.com",
    "Accept-Encoding": "gzip, deflate",
}

THRESHOLD_USD = 500_000
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)


def get(url, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


def search_recent_form4(days_back=1):
    """Search EDGAR for Form 4 filings in the last N days."""
    end_date = datetime.utcnow().strftime("%Y-%m-%d")
    start_date = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    url = (
        f"https://efts.sec.gov/LATEST/search-index?forms=4"
        f"&dateRange=custom&startdt={start_date}&enddt={end_date}"
        f"&hits.hits.total.value=true&hits.hits._source.period_of_report=true"
        f"&hits.hits._source.entity_name=true&hits.hits._source.file_date=true"
        f"&hits.hits._source.accession_no=true"
    )

    results = []
    from_offset = 0
    page_size = 40

    while True:
        paged_url = url + f"&from={from_offset}&hits.hits._source.accession_no=true"
        try:
            data = get(paged_url).json()
        except Exception as e:
            print(f"  Search error: {e}")
            break

        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            break

        for hit in hits:
            src = hit.get("_source", {})
            results.append({
                "entity_name": src.get("entity_name", ""),
                "file_date": src.get("file_date", ""),
                "accession_no": src.get("accession_no", ""),
                "period": src.get("period_of_report", ""),
            })

        from_offset += page_size
        total = data.get("hits", {}).get("total", {}).get("value", 0)
        if from_offset >= min(total, 400):
            break
        time.sleep(0.3)

    return results


def get_filing_xml(accession_no):
    """Download and return the primary Form 4 XML."""
    cik_part = accession_no.split("-")[0].lstrip("0")
    acc_clean = accession_no.replace("-", "")
    index_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik_part}"
        f"/{acc_clean}/{accession_no}-index.json"
    )
    try:
        index = get(index_url).json()
    except Exception:
        return None

    items = index.get("directory", {}).get("item", [])
    for item in items:
        name = item.get("name", "")
        if name.endswith(".xml") and not name.endswith("-index.xml"):
            url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik_part}"
                f"/{acc_clean}/{name}"
            )
            try:
                return get(url).text
            except Exception:
                return None
    return None


def parse_form4(xml_text, entity_name, file_date):
    """Parse Form 4 XML and return qualifying transactions."""
    transactions = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return transactions

    # Issuer info
    issuer = root.find("issuer")
    company = entity_name
    ticker = ""
    if issuer is not None:
        company = issuer.findtext("issuerName") or entity_name
        ticker = issuer.findtext("issuerTradingSymbol") or ""

    # Reporter info
    reporter = root.find("reportingOwner")
    exec_name = ""
    exec_title = ""
    if reporter is not None:
        exec_name = reporter.findtext(".//rptOwnerName") or ""
        exec_title = reporter.findtext(".//officerTitle") or reporter.findtext(".//isOfficer") or ""

    # Non-derivative transactions (actual stock purchases/sales)
    for tx in root.iter("nonDerivativeTransaction"):
        code_el = tx.find("transactionCoding/transactionCode")
        if code_el is None:
            continue
        code = code_el.text.strip() if code_el.text else ""

        # Only open-market purchases (P) and sales (S)
        if code not in ("P", "S"):
            continue

        shares_el = tx.findtext("transactionAmounts/transactionShares/value") or "0"
        price_el = tx.findtext("transactionAmounts/transactionPricePerShare/value") or "0"

        try:
            shares = float(shares_el.replace(",", ""))
            price = float(price_el.replace(",", ""))
            total_value = shares * price
        except ValueError:
            continue

        if total_value < THRESHOLD_USD:
            continue

        direction = tx.findtext("transactionAmounts/transactionAcquiredDisposedCode/value") or ""
        action = "BUY" if direction == "A" else "SELL"

        # Check if new position
        post_shares_el = tx.findtext("postTransactionAmounts/sharesOwnedFollowingTransaction/value") or "0"
        try:
            post_shares = float(post_shares_el.replace(",", ""))
            is_new = post_shares <= shares * 1.05
        except ValueError:
            is_new = False

        transactions.append({
            "company": company,
            "ticker": ticker.upper(),
            "exec_name": exec_name,
            "exec_title": exec_title,
            "action": action,
            "shares": int(shares),
            "price_per_share": round(price, 2),
            "total_value": int(total_value),
            "is_new_position": is_new,
            "file_date": file_date,
            "transaction_code": code,
        })

    return transactions


def run(days_back=1):
    print(f"Scanning Form 4 filings (last {days_back} day(s), threshold ${THRESHOLD_USD:,})...")
    filings = search_recent_form4(days_back)
    print(f"Found {len(filings)} Form 4 filings to scan")

    all_transactions = []
    processed = 0

    for filing in filings:
        time.sleep(0.4)
        xml_text = get_filing_xml(filing["accession_no"])
        if not xml_text:
            continue

        txs = parse_form4(xml_text, filing["entity_name"], filing["file_date"])
        all_transactions.extend(txs)
        processed += 1

        if processed % 20 == 0:
            print(f"  Processed {processed}/{len(filings)} filings, {len(all_transactions)} signals so far")

    buys = [t for t in all_transactions if t["action"] == "BUY"]
    sells = [t for t in all_transactions if t["action"] == "SELL"]

    buys.sort(key=lambda x: x["total_value"], reverse=True)
    sells.sort(key=lambda x: x["total_value"], reverse=True)

    result = {"buys": buys, "sells": sells, "scan_date": datetime.utcnow().strftime("%Y-%m-%d")}

    out_path = DATA_DIR / "form4_signals.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\nForm 4 scan complete: {len(buys)} buys, {len(sells)} sells above ${THRESHOLD_USD:,}")
    return result


if __name__ == "__main__":
    result = run()
    print("\nTop 5 Insider BUYS:")
    for t in result["buys"][:5]:
        print(f"  {t['ticker']} ({t['company']}) — {t['exec_name']} ({t['exec_title']}) bought ${t['total_value']:,}")
    print("\nTop 5 Insider SELLS:")
    for t in result["sells"][:5]:
        print(f"  {t['ticker']} ({t['company']}) — {t['exec_name']} ({t['exec_title']}) sold ${t['total_value']:,}")
