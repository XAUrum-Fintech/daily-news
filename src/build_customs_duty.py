#!/usr/bin/env python3
"""Build and validate a customs-duty tariff-value event file.

Helper for the daily customs-duty watcher (docs/CUSTOMS_DUTY.md).
Additive only -- it never touches the news-feed pipeline (news/, RUNBOOK.md).

Usage:
    python3 src/build_customs_duty.py \
        --notification-no "75/2026-Customs (N.T.)" --short "75/2026" \
        --dated 2026-09-15 --effective 2026-09-16 \
        --source-url "https://..." --source-name "CA Sansaar" \
        --source-domain "casansaar.com" \
        --gold 1373 --gold-prev 1468 --silver 2028 --silver-prev 2267 \
        --generated-at "2026-09-27T19:30:00Z" \
        --out /tmp/customs-duty-out

Writes into --out:
    <effective>-<short>.json   (e.g. 2026-09-16-75-2026.json)
    latest.json                (full copy of the event)
    state.json                 (watcher state for change detection)
"""
import argparse
import hashlib
import json
import os
import sys

SCHEMA = "orob-customs-duty.v1"
STATE_SCHEMA = "orob-customs-duty-state.v1"
PLACEHOLDER_METALS = ("https://raw.githubusercontent.com/XAUrum-Fintech/"
                      "daily-news/main/assets/placeholder-metals.jpg")
ALLOWED_TAGS = {"gold", "silver", "mcx", "comex", "rupee", "rbi", "fed",
                "import-duty", "india", "global", "jewellery", "central-banks"}
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def article_id(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def fmt_date(iso):
    y, m, d = iso.split("-")
    return f"{MONTHS[int(m) - 1]} {int(d)}, {y}"


def move_phrase(cur, prev, unit):
    if cur == prev:
        return f"holds at ${cur:,}{unit}"
    direction = "falls to" if cur < prev else "rises to"
    return f"{direction} ${cur:,}{unit} from ${prev:,}{unit}"


def build_event(a):
    gold_changed = a.gold != a.gold_prev
    silver_changed = a.silver != a.silver_prev

    if gold_changed or silver_changed:
        bits = []
        if gold_changed:
            bits.append(f"gold to ${a.gold:,}/10g")
        if silver_changed:
            bits.append(f"silver to ${a.silver:,}/kg")
        title = f"CBIC revises {' and '.join(bits)} from {fmt_date(a.effective)}"
    else:
        title = ("CBIC holds gold, silver tariff values unchanged "
                 f"from {fmt_date(a.effective)}")
    if len(title) > 160:
        raise ValueError(f"title too long ({len(title)} chars)")

    summary = (
        f"Notification No. {a.notification_no} dated {fmt_date(a.dated)} "
        f"substitutes the tariff-value tables under 36/2001-Customs (N.T.), "
        f"effective {fmt_date(a.effective)}. "
        f"Gold's tariff value {move_phrase(a.gold, a.gold_prev, '/10g')}; "
        f"silver's {move_phrase(a.silver, a.silver_prev, '/kg')}. "
        f"These USD values set the assessable value for customs duty on "
        f"specified gold and silver imports."
    )
    if len(summary) > 400:
        raise ValueError(f"summary too long ({len(summary)} chars)")

    return {
        "schema": SCHEMA,
        "notification_no": a.notification_no,
        "notification_short": a.short,
        "dated": a.dated,
        "effective": a.effective,
        "amends": "36/2001-Customs (N.T.)",
        "source_url": a.source_url,
        "gold_tariff_usd_per_10g": a.gold,
        "gold_tariff_usd_per_10g_prev": a.gold_prev,
        "silver_tariff_usd_per_kg": a.silver,
        "silver_tariff_usd_per_kg_prev": a.silver_prev,
        "gold_changed": gold_changed,
        "silver_changed": silver_changed,
        "generated_at": a.generated_at,
        "article": {
            "id": article_id(a.source_url),
            "title": title,
            "summary": summary,
            "url": a.source_url,
            "source": a.source_name,
            "source_domain": a.source_domain,
            "published_at": f"{a.dated}T00:00:00Z",
            "image_url": PLACEHOLDER_METALS,
            "tags": ["gold", "silver", "import-duty", "india"],
            "category": "policy",
            "metals": ["gold", "silver"],
            "breaking": False,
        },
    }


def validate_event(ev):
    top = {"schema", "notification_no", "notification_short", "dated",
           "effective", "amends", "source_url", "gold_tariff_usd_per_10g",
           "gold_tariff_usd_per_10g_prev", "silver_tariff_usd_per_kg",
           "silver_tariff_usd_per_kg_prev", "gold_changed", "silver_changed",
           "generated_at", "article"}
    if set(ev.keys()) != top:
        raise ValueError(f"top-level keys mismatch: {sorted(ev.keys())}")
    if ev["schema"] != SCHEMA:
        raise ValueError("bad schema")
    for k in ("gold_tariff_usd_per_10g", "gold_tariff_usd_per_10g_prev",
              "silver_tariff_usd_per_kg", "silver_tariff_usd_per_kg_prev"):
        if not isinstance(ev[k], (int, float)) or ev[k] <= 0:
            raise ValueError(f"bad value for {k}")
    if not (ev["source_url"].startswith("https://")):
        raise ValueError("source_url must be https")

    art = ev["article"]
    base = {"id", "title", "summary", "url", "source", "source_domain",
            "published_at", "image_url", "tags", "category", "metals",
            "breaking"}
    if set(art.keys()) != base:
        raise ValueError(f"article keys mismatch: {sorted(art.keys())}")
    if art["id"] != article_id(art["url"]):
        raise ValueError("article id != sha256(url)[:16]")
    if not (1 <= len(art["title"]) <= 160):
        raise ValueError("article title length")
    if not (1 <= len(art["summary"]) <= 400):
        raise ValueError("article summary length")
    if not art["image_url"].startswith("https://"):
        raise ValueError("article image_url must be https")
    if not set(art["tags"]) <= ALLOWED_TAGS:
        raise ValueError("article tags not in allowed set")
    if art["category"] != "policy":
        raise ValueError("article category must be policy")
    if not art["metals"] or not set(art["metals"]) <= {"gold", "silver"}:
        raise ValueError("article metals invalid")
    if not isinstance(art["breaking"], bool):
        raise ValueError("article breaking must be boolean")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--notification-no", required=True)
    p.add_argument("--short", required=True)
    p.add_argument("--dated", required=True)
    p.add_argument("--effective", required=True)
    p.add_argument("--source-url", required=True)
    p.add_argument("--source-name", required=True)
    p.add_argument("--source-domain", required=True)
    p.add_argument("--gold", type=float, required=True)
    p.add_argument("--gold-prev", type=float, required=True)
    p.add_argument("--silver", type=float, required=True)
    p.add_argument("--silver-prev", type=float, required=True)
    p.add_argument("--generated-at", required=True)
    p.add_argument("--out", required=True)
    a = p.parse_args()

    def num(x):
        return int(x) if x == int(x) else x
    a.gold, a.gold_prev = num(a.gold), num(a.gold_prev)
    a.silver, a.silver_prev = num(a.silver), num(a.silver_prev)

    ev = build_event(a)
    validate_event(ev)

    os.makedirs(a.out, exist_ok=True)
    fname = f"{a.effective}-{a.short.replace('/', '-')}.json"
    with open(os.path.join(a.out, fname), "w", encoding="utf-8") as f:
        json.dump(ev, f, ensure_ascii=False, indent=2)
        f.write("\n")
    with open(os.path.join(a.out, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(ev, f, ensure_ascii=False, indent=2)
        f.write("\n")
    state = {
        "schema": STATE_SCHEMA,
        "last_notification_short": a.short,
        "last_notification_no": a.notification_no,
        "dated": a.dated,
        "effective": a.effective,
        "gold_tariff_usd_per_10g": a.gold,
        "silver_tariff_usd_per_kg": a.silver,
        "source_url": a.source_url,
        "updated_at": a.generated_at,
    }
    with open(os.path.join(a.out, "state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"wrote {fname}, latest.json, state.json -> {a.out}")
    print(f"title: {ev['article']['title']}")


if __name__ == "__main__":
    try:
        main()
    except ValueError as e:
        print(f"VALIDATION FAILED: {e}", file=sys.stderr)
        sys.exit(1)
