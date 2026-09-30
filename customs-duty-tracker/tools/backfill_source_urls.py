#!/usr/bin/env python3
"""Backfill source_url/source_kind on existing tracker rows.

- Exchange-rate rows: ICEGATE exposes no per-notice URL (detail is a POST
  API; the portal's PDF is generated client-side), so the official listing
  portal is recorded with source_kind "portal". No network needed.
- Tariff-value rows: query the CBIC notification API for the notification
  number and record the official notice PDF URL (source_kind "notice").
- Also tries CBIC's "Exchange Rate" category for ERAM numbers, in case
  CBIC hosts those circulars as PDFs too.
- `notice_url` is set to `source_url` on every row (the field orob's app
  reads).
- `duty_rate` is set per row from the rate in force on its effective date
  (6% before 13 May 2026, 15% from that date), and the INR duty columns are
  recomputed at that rate. This corrects the pre-13-May-2026 rows, whose
  duties were seeded at 15% in the reference spreadsheet.

Idempotent: rows that already have a source_url are skipped for the source
lookup (duty recompute still runs, and is itself idempotent). Migrates
customs-tracker.v1/v2 files to v3 (adds the new columns). On any validation
failure nothing is written. CBIC failures are warnings; affected rows keep
nulls for a later run (the daily sync re-runs this).

Usage: backfill_source_urls.py --tracker-dir <customs-duty-tracker dir>
"""

import argparse
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_customs_tracker as B
import fetch as F

CBIC_CATEGORIES_TARIFF = ["Non Tariff"]
CBIC_CATEGORIES_ERAM = ["Exchange Rate", "Exchange rate"]


def log(msg):
    print(f"backfill: {msg}", file=sys.stderr)


def norm_num(s):
    return "".join((s or "").lower().split())


def cbic_probe():
    """True if the CBIC API answers at all right now (its WAF blocks us
    intermittently). One cheap request; callers skip CBIC lookups when
    this is False."""
    url = (f"{F.CBIC_LIST}?taxId=1000002&category=Exchange%20Rate"
           f"&year={date.today().year}&page=0&size=1")
    try:
        F.http_json("GET", url, retries=1)
        return True
    except Exception as e:
        log(f"CBIC unreachable, skipping CBIC lookups this run: {e}")
        return False


def cbic_find_pdf(number, categories, year):
    """Return the official notice PDF URL for `number`, or None."""
    want = norm_num(number)
    for category in categories:
        for page in range(10):
            url = (f"{F.CBIC_LIST}?taxId=1000002&category="
                   f"{category.replace(' ', '%20')}"
                   f"&year={year}&page={page}&size=50")
            try:
                d = F.http_json("GET", url)
            except Exception as e:
                log(f"CBIC request failed ({category} {year} p{page}): {e}")
                return None
            items = []
            if isinstance(d, list):
                items = d
            elif isinstance(d, dict):
                for k in ("content", "data", "items", "results",
                          "notifications", "notificationList"):
                    if isinstance(d.get(k), list):
                        items = d[k]
                        break
            if not items:
                break
            for it in items:
                num = F._pick(it, "notificationNo", "notificationNumber",
                              "notNo", "docNumber")
                if norm_num(num) == want:
                    pdf = F._pick(it, "docFilePath", "filePath", "pdfPath",
                                  "documentPath")
                    if pdf:
                        return F.CBIC_PDF + pdf.lstrip("/")
                    return None
            if len(items) < 50:
                break
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracker-dir", required=True)
    args = ap.parse_args()

    path = os.path.join(args.tracker_dir, "latest.json")
    with open(path) as f:
        data = json.load(f)
    if data.get("schema") not in ("customs-tracker.v1", "customs-tracker.v2",
                                   "customs-tracker.v3"):
        print(f"backfill: ERROR: unexpected schema {data.get('schema')}",
              file=sys.stderr)
        sys.exit(1)
    rows = data["rows"]
    if not rows:
        print("backfill: no rows")
        return 0

    filled_notice = filled_portal = skipped = 0
    cbic_ok = cbic_probe()
    new_rows = []
    for r in rows:
        r = dict(r)
        if r.get("source_url"):
            skipped += 1
        elif r["event"] == "Exchange rate":
            url = None
            if cbic_ok and r.get("eram_notification"):
                try:
                    year = int(r["published"][:4])
                except ValueError:
                    year = date.today().year
                url = cbic_find_pdf(r["eram_notification"],
                                    CBIC_CATEGORIES_ERAM, year)
            if url:
                r["source_url"], r["source_kind"] = url, "notice"
                filled_notice += 1
            else:
                r["source_url"], r["source_kind"] = F.ICEGATE_PORTAL, "portal"
                filled_portal += 1
        elif r["event"] == "Tariff value":
            url = None
            if cbic_ok and r.get("tariff_notification"):
                try:
                    year = int(r["published"][:4])
                except ValueError:
                    year = date.today().year
                url = cbic_find_pdf(r["tariff_notification"],
                                    CBIC_CATEGORIES_TARIFF, year)
            if url:
                r["source_url"], r["source_kind"] = url, "notice"
                filled_notice += 1
            else:
                log(f"no CBIC PDF yet for {r.get('tariff_notification')}; "
                    "leaving null")
                r["source_url"], r["source_kind"] = None, None
        # reorder keys to the v3 column order; set notice_url + duty_rate
        # and recompute the INR duty columns at the row's own rate
        rate = B.duty_rate_for(r["effective"])
        _, gd, _, sd = B.derive(r["gold_tariff_usd_10g"],
                                r["silver_tariff_usd_kg"],
                                r["usd_inr_import"], rate)
        r["gold_duty_inr_kg"] = float(gd)
        r["silver_duty_inr_kg"] = float(sd)
        if new_rows:
            prev = new_rows[-1]
            r["gold_duty_change_inr_kg"] = float(
                B._chg(r["gold_duty_inr_kg"], prev["gold_duty_inr_kg"]))
            r["silver_duty_change_inr_kg"] = float(
                B._chg(r["silver_duty_inr_kg"], prev["silver_duty_inr_kg"]))
        else:
            r["gold_duty_change_inr_kg"] = None
            r["silver_duty_change_inr_kg"] = None
        r["notice_url"] = r.get("source_url")
        r["duty_rate"] = float(rate)
        new_rows.append({k: r.get(k) for k, _ in B.COLUMNS})

    # validate every row exactly as the builder would on append
    checked = []
    for row in new_rows:
        B.validate_append(checked, row)
        checked.append(row)

    # No-op runs must not touch the outputs: write_outputs stamps a fresh
    # generated_at, which would make every quiet daily run look dirty and
    # produce a needless backfill-only commit. The duty recompute above is
    # deterministic, so a second run changes nothing.
    if data.get("schema") == B.SCHEMA and checked == rows:
        print("backfill: no changes")
        return 0

    from datetime import datetime, timezone
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    B.write_outputs(args.tracker_dir, checked, generated_at)
    print(f"backfill: notice={filled_notice} portal={filled_portal} "
          f"skipped={skipped} still-null="
          f"{sum(1 for r in checked if not r['source_url'])}; "
          f"rows={len(checked)} schema={B.SCHEMA}")


if __name__ == "__main__":
    main()
