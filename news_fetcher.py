#!/usr/bin/env python3
"""
News Fetcher
Gets recent news headlines and links for US and Taiwan stocks.
Uses Yahoo Finance RSS feeds — no API key required.

For TW stocks, the ticker format is "{ticker}.TW" (e.g., "2330.TW" for TSMC).
"""

import time
import xml.etree.ElementTree as ET
import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


def fetch_news(ticker: str, market: str = "US", max_items: int = 3) -> list[dict]:
    """
    Fetch recent news for a stock.

    market: "US" for US stocks, "TW" for Taiwan stocks
    Returns list of {title, url, published} dicts.
    """
    if market == "TW":
        symbol = f"{ticker}.TW"
        region = "TW"
        lang   = "zh-TW"
    else:
        symbol = ticker
        region = "US"
        lang   = "en-US"

    url = (
        f"https://feeds.finance.yahoo.com/rss/2.0/headline"
        f"?s={symbol}&region={region}&lang={lang}"
    )

    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return []

        # Yahoo Finance RSS sometimes has namespace issues — strip them
        text = r.text.replace(' xmlns="', ' xmlnsx="')
        root = ET.fromstring(text)

        items = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link")  or "").strip()
            pub   = (item.findtext("pubDate") or "").strip()

            if title and link and "finance.yahoo.com" in link:
                items.append({"title": title, "url": link, "published": pub})

            if len(items) >= max_items:
                break

        return items

    except Exception:
        return []


def fetch_news_batch(us_tickers: list, tw_tickers: list) -> dict:
    """
    Fetch news for a batch of US and Taiwan tickers.
    Returns {"US": {ticker: [news_items]}, "TW": {ticker: [news_items]}}.
    """
    results = {"US": {}, "TW": {}}

    for ticker in us_tickers[:6]:
        news = fetch_news(ticker, "US", max_items=3)
        if news:
            results["US"][ticker] = news
        time.sleep(0.4)

    for ticker in tw_tickers[:8]:
        news = fetch_news(ticker, "TW", max_items=3)
        if news:
            results["TW"][ticker] = news
        time.sleep(0.4)

    return results


if __name__ == "__main__":
    print("Fetching US news for NVDA:")
    for item in fetch_news("NVDA", "US"):
        print(f"  {item['title'][:60]}")
        print(f"  {item['url']}")

    print("\nFetching TW news for 2330 (TSMC):")
    for item in fetch_news("2330", "TW"):
        print(f"  {item['title'][:60]}")
        print(f"  {item['url']}")
