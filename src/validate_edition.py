#!/usr/bin/env python3
"""Validate a news/latest.json edition against the orob-news.v1 contract.

Usage: validate_edition.py <latest.json>
Exit 0 if valid; exit 1 with error messages on stderr otherwise.
Stdlib only.
"""

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qsl

CATEGORIES = {"mcx", "global", "policy", "festive"}
METALS = {"gold", "silver"}
DOMAIN_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")
EDITION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:00Z$")
TRACKING_PREFIX = "utm_"
TRACKING_EXACT = {"fbclid", "gclid"}

TOP_KEYS = {"schema", "edition_id", "generated_at", "window", "insights", "items"}
ITEM_REQUIRED = {
    "id", "rank", "title", "summary", "url", "source",
    "source_domain", "published_at", "tags", "category", "metals",
}


def load_taxonomy():
    builtin = ["gold", "silver", "mcx", "comex", "rupee", "rbi", "fed",
               "import-duty", "india", "global", "jewellery", "central-banks"]
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "..", "data", "taxonomy.json")
    try:
        with open(path, encoding="utf-8") as f:
            tags = json.load(f).get("tags")
            return list(tags) if tags else builtin
    except OSError:
        return builtin


def parse_z(value):
    """Parse strict ISO-8601 with Z suffix -> aware datetime, or None."""
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        dt = datetime.fromisoformat(value[:-1] + "+00:00")
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


class Checker:
    def __init__(self):
        self.errors = []

    def err(self, msg):
        self.errors.append(msg)

    def check(self, cond, msg):
        if not cond:
            self.err(msg)


def check_url_tracking(url, label, c):
    try:
        q = parse_qsl(urlparse(url).query, keep_blank_values=True)
    except ValueError:
        c.err("%s: unparsable URL %r" % (label, url))
        return
    for name, _ in q:
        if name.startswith(TRACKING_PREFIX) or name in TRACKING_EXACT:
            c.err("%s: tracking parameter %r in URL" % (label, name))
            break


def main(argv):
    if len(argv) != 2:
        print("usage: validate_edition.py <latest.json>", file=sys.stderr)
        return 2
    c = Checker()
    try:
        with open(argv[1], encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print("ERROR: cannot load JSON: %s" % exc, file=sys.stderr)
        return 1

    tags_allowed = set(load_taxonomy())

    c.check(isinstance(doc, dict), "top level must be an object")
    if not isinstance(doc, dict):
        return report(c)
    c.check(set(doc.keys()) == TOP_KEYS,
            "top-level keys must be exactly %s, got %s"
            % (sorted(TOP_KEYS), sorted(doc.keys())))
    c.check(doc.get("schema") == "orob-news.v1",
            "schema must be 'orob-news.v1', got %r" % (doc.get("schema"),))

    edition_id = doc.get("edition_id")
    slot = None
    c.check(isinstance(edition_id, str) and EDITION_RE.match(edition_id),
            "edition_id must match YYYY-MM-DDTHH:00Z, got %r" % (edition_id,))
    if isinstance(edition_id, str) and EDITION_RE.match(edition_id):
        slot = parse_z(edition_id)
        c.check(slot is not None and slot.hour % 2 == 0,
                "edition_id hour must be even (2-hourly slots), got %r" % (edition_id,))

    generated_at = parse_z(doc.get("generated_at"))
    c.check(generated_at is not None,
            "generated_at must be ISO-8601 UTC with Z, got %r" % (doc.get("generated_at"),))

    window = doc.get("window")
    c.check(isinstance(window, dict), "window must be an object")
    if isinstance(window, dict) and slot is not None:
        wfrom, wto = parse_z(window.get("from")), parse_z(window.get("to"))
        c.check(wfrom is not None and wto is not None,
                "window.from/to must be ISO-8601 UTC with Z")
        if wfrom is not None and wto is not None:
            c.check(wto == slot, "window.to must equal edition slot, got %r" % (window.get("to"),))
            c.check(wto - wfrom == timedelta(hours=2),
                    "window must span exactly 2h, got %s" % (wto - wfrom,))

    insights = doc.get("insights")
    c.check(isinstance(insights, dict), "insights must be an object")
    if isinstance(insights, dict):
        headline = insights.get("headline", "")
        summary = insights.get("summary", "")
        points = insights.get("points", [])
        c.check(isinstance(headline, str) and 0 < len(headline) <= 120,
                "insights.headline must be 1-120 chars")
        c.check(isinstance(summary, str) and 0 < len(summary) <= 600,
                "insights.summary must be 1-600 chars")
        c.check(isinstance(points, list) and len(points) <= 5,
                "insights.points must be a list of 0-5 items")
        if isinstance(points, list):
            for i, p in enumerate(points):
                c.check(isinstance(p, str) and 0 < len(p) <= 160,
                        "insights.points[%d] must be 1-160 chars" % i)

    items = doc.get("items")
    c.check(isinstance(items, list), "items must be a list")
    if isinstance(items, list):
        n = len(items)
        c.check(5 <= n <= 20, "items must have 5-20 entries, got %d" % n)
        ranks = [it.get("rank") for it in items if isinstance(it, dict)]
        c.check(sorted(ranks) == list(range(1, n + 1)),
                "ranks must be exactly 1..%d unique, got %s" % (n, sorted(ranks)))
        for i, it in enumerate(items):
            label = "items[%d]" % i
            if not isinstance(it, dict):
                c.err("%s: must be an object" % label)
                continue
            missing = ITEM_REQUIRED - set(it.keys())
            c.check(not missing, "%s: missing fields %s" % (label, sorted(missing)))

            cat = it.get("category")
            c.check(cat in CATEGORIES, "%s: category must be one of %s, got %r"
                    % (label, sorted(CATEGORIES), cat))
            metals = it.get("metals")
            c.check(isinstance(metals, list) and len(metals) > 0
                    and set(metals) <= METALS,
                    "%s: metals must be non-empty subset of %s, got %r"
                    % (label, sorted(METALS), metals))

            src, dom = it.get("source"), it.get("source_domain")
            c.check(isinstance(src, str) and src.strip() != "",
                    "%s: source must be a non-empty string" % label)
            c.check(isinstance(dom, str) and dom.strip() != "",
                    "%s: source_domain must be a non-empty string" % label)
            if isinstance(dom, str) and dom:
                c.check("www." not in dom,
                        "%s: source_domain must not contain 'www.', got %r" % (label, dom))
                c.check(dom == dom.lower(),
                        "%s: source_domain must be lowercase, got %r" % (label, dom))
                c.check(bool(DOMAIN_RE.match(dom)),
                        "%s: source_domain malformed, got %r" % (label, dom))

            br = it.get("breaking", False)
            c.check(isinstance(br, bool),
                    "%s: breaking must be boolean, got %r" % (label, br))

            rel = it.get("related_urls")
            if rel is not None:
                c.check(isinstance(rel, list)
                        and all(isinstance(u, str) and u.startswith("https://") for u in rel),
                        "%s: related_urls must be a list of https URLs" % label)

            title, summary = it.get("title"), it.get("summary")
            c.check(isinstance(title, str) and 0 < len(title) <= 160,
                    "%s: title must be 1-160 chars" % label)
            c.check(isinstance(summary, str) and 0 < len(summary) <= 400,
                    "%s: summary must be 1-400 chars" % label)

            url = it.get("url")
            c.check(isinstance(url, str) and url.startswith("https://"),
                    "%s: url must be https, got %r" % (label, url))
            if isinstance(url, str) and url.startswith("https://"):
                check_url_tracking(url, label + ".url", c)
                want = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
                c.check(it.get("id") == want,
                        "%s: id must be sha256(url)[:16] (%s), got %r"
                        % (label, want, it.get("id")))

            pub = parse_z(it.get("published_at"))
            c.check(pub is not None,
                    "%s: published_at must be ISO-8601 UTC with Z, got %r"
                    % (label, it.get("published_at")))
            if pub is not None and generated_at is not None:
                c.check(generated_at - timedelta(hours=48) <= pub <= generated_at,
                        "%s: published_at must be within 48h before generated_at" % label)

            tags = it.get("tags")
            c.check(isinstance(tags, list) and len(tags) > 0
                    and set(tags) <= tags_allowed,
                    "%s: tags must be non-empty subset of taxonomy, got %r" % (label, tags))

            img = it.get("image_url")
            if img is not None:
                c.check(isinstance(img, str) and img.startswith("https://"),
                        "%s: image_url must be https or null, got %r" % (label, img))

    return report(c)


def report(c):
    if c.errors:
        for e in c.errors:
            print("ERROR: " + e, file=sys.stderr)
        return 1
    print("OK: edition valid")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
