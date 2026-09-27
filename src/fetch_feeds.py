#!/usr/bin/env python3
"""Fetch Google News RSS feeds for the gold/silver digest (stdlib only).

Pulls the India-edition and global-edition feeds for the runbook's query sets,
parses <item> entries and prints a JSON list to stdout:

    [{"title": ..., "link": ..., "pubDate": ..., "source_name": ...,
      "query": ..., "edition": "IN"|"US"}]

`source_name` is parsed from the " - Source" suffix Google News appends to
titles. Items are deduped by (normalized title, source_name).

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
]

US_QUERIES = [
    "gold price",
    "silver price",
    "COMEX gold futures",
    "Federal Reserve gold",
    "central bank gold buying",
    "gold ETF flows",
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


def parse_items(xml_bytes, query, edition):
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
            }
        )
    return items


def main():
    feeds = [("IN", "en-IN", "IN", "IN:en", IN_QUERIES),
             ("US", "en-US", "US", "US:en", US_QUERIES)]
    all_items = []
    failures = 0
    for edition, hl, gl, ceid, queries in feeds:
        for query in queries:
            url = feed_url(query, hl, gl, ceid)
            try:
                xml_bytes = fetch(url)
            except Exception as exc:  # noqa: BLE001 - keep going on feed errors
                print("WARN: feed failed %r: %s" % (query, exc), file=sys.stderr)
                failures += 1
                continue
            try:
                all_items.extend(parse_items(xml_bytes, query, edition))
            except Exception as exc:  # noqa: BLE001
                print("WARN: parse failed %r: %s" % (query, exc), file=sys.stderr)
                failures += 1

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
