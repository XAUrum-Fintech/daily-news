#!/usr/bin/env python3
"""Fetch candidate news for the gold/silver digest (stdlib only).

Two source kinds, printed as one JSON list to stdout:

    [{"title": ..., "link": ..., "pubDate": ..., "source_name": ...,
      "query": ..., "edition": "IN"|"US"|null, "feed_origin": ...}]

1. Google News RSS (India + global editions) for the runbook's query sets.
   `source_name` is parsed from the " - Source" suffix Google News appends to
   titles. `feed_origin` is "google_news_in" or "google_news_us".
2. Direct publisher feeds (commodity/market sections preferred).
   `source_name` is the publisher's display name, `feed_origin` is
   "direct:<source_domain>".

Items are deduped by (normalized title, source_name).

A feed that fails (timeout/403/non-XML) prints a WARN to stderr and is
skipped — it never crashes the run.

NOTE (from experience): Google News ``rss/articles/...`` links do NOT resolve
to the publisher via ``curl -sIL``. Leave ``link`` as-is; the edition agent
resolves the canonical publisher URL by searching the exact article title.
"""

import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html import unescape

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
TIMEOUT = 20

IN_QUERIES = [
    "MCX gold silver",
    "gold import duty India",
    "IBJA gold",
    "RBI gold reserves",
    "gold price India",
    "silver price India",
    "Dhanteras gold",
    "Diwali gold demand",
    "Akshaya Tritiya gold sales",
    "gold jewellery demand India",
    "silver demand India",
    "MCX silver",
    "gold ETF India inflows",
    "sovereign gold bond",
    "gold hallmarking BIS",
    "RBI gold buying",
    "gold import India",
]

US_QUERIES = [
    "gold price",
    "silver price",
    "COMEX gold futures",
    "Federal Reserve gold",
    "central bank gold buying",
    "gold ETF flows",
    "COMEX silver futures",
    "central bank gold reserves",
    "London gold price",
]

# (source_domain, feed_url, display_name) — all verified 200 OK with recent items.
DIRECT_FEEDS = [
    ("economictimes.indiatimes.com",
     "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
     "Economic Times"),
    ("livemint.com",
     "https://www.livemint.com/rss/markets",
     "Mint"),
    ("business-standard.com",
     "https://www.business-standard.com/rss/markets-106.rss",
     "Business Standard"),
    ("thehindubusinessline.com",
     "https://www.thehindubusinessline.com/markets/commodities/feeder/default.rss",
     "The Hindu BusinessLine"),
    ("investing.com",
     "https://www.investing.com/rss/commodities.rss",
     "Investing.com"),
    # Low frequency (~monthly), still worth polling.
    ("gold.org",
     "https://www.gold.org/rss.xml",
     "World Gold Council"),
]


def feed_url(query, hl, gl, ceid):
    q = urllib.parse.quote(query)
    return (
        "https://news.google.com/rss/search?q=%s&hl=%s&gl=%s&ceid=%s"
        % (q, hl, gl, ceid)
    )


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def split_source(raw_title):
    """Split 'Headline text - Source Name' -> (headline, source)."""
    title = unescape((raw_title or "").strip())
    if " - " in title:
        head, _, src = title.rpartition(" - ")
        head, src = head.strip(), src.strip()
        if head and src:
            return head, src
    return title, ""


def norm_title(title):
    return re.sub(r"\s+", " ", title).strip().lower()


def parse_gnews_items(xml_bytes, query, edition, origin):
    items = []
    root = ET.fromstring(xml_bytes)
    for item in root.iter("item"):
        raw_title = item.findtext("title") or ""
        title, source_name = split_source(raw_title)
        if not title:
            continue
        items.append(
            {
                "title": title,
                "link": (item.findtext("link") or "").strip(),
                "pubDate": (item.findtext("pubDate") or "").strip(),
                "source_name": source_name,
                "query": query,
                "edition": edition,
                "feed_origin": origin,
            }
        )
    return items


def parse_direct_items(xml_bytes, display_name, domain):
    items = []
    root = ET.fromstring(xml_bytes)
    for item in root.iter("item"):
        title = unescape((item.findtext("title") or "").strip())
        if not title:
            continue
        items.append(
            {
                "title": title,
                "link": (item.findtext("link") or "").strip(),
                "pubDate": (item.findtext("pubDate") or "").strip(),
                "source_name": display_name,
                "query": None,
                "edition": None,
                "feed_origin": "direct:" + domain,
            }
        )
    return items


def pull(url, parse, label):
    """Fetch+parse one feed; return items (possibly []) — never raises."""
    try:
        xml_bytes = fetch(url)
    except Exception as exc:  # noqa: BLE001 - skip failed feeds
        print("WARN: feed failed %r: %s" % (label, exc), file=sys.stderr)
        return None
    try:
        return parse(xml_bytes)
    except Exception as exc:  # noqa: BLE001
        print("WARN: parse failed %r: %s" % (label, exc), file=sys.stderr)
        return None


def main():
    all_items = []
    failures = 0

    for edition, hl, gl, ceid, queries, origin in [
        ("IN", "en-IN", "IN", "IN:en", IN_QUERIES, "google_news_in"),
        ("US", "en-US", "US", "US:en", US_QUERIES, "google_news_us"),
    ]:
        for query in queries:
            url = feed_url(query, hl, gl, ceid)
            got = pull(url,
                       lambda b, q=query, e=edition, o=origin: parse_gnews_items(b, q, e, o),
                       "gnews:%s:%s" % (edition, query))
            if got is None:
                failures += 1
            else:
                all_items.extend(got)

    for domain, url, display_name in DIRECT_FEEDS:
        got = pull(url,
                   lambda b, n=display_name, d=domain: parse_direct_items(b, n, d),
                   "direct:" + domain)
        if got is None:
            failures += 1
        else:
            all_items.extend(got)

    # Dedupe by (normalized title, source).
    seen = set()
    deduped = []
    for it in all_items:
        key = (norm_title(it["title"]), it["source_name"].lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)

    print(json.dumps(deduped, ensure_ascii=False, indent=2))
    if not deduped:
        print("ERROR: no items fetched from any feed", file=sys.stderr)
        return 1
    if failures:
        print("WARN: %d feed(s) failed" % failures, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
