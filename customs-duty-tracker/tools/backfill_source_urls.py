#!/usr/bin/env python3
"""Backfill source_url/source_kind on existing tracker rows.

- Exchange-rate rows: ICEGATE exposes no per-notice URL (detail is a POST
  API; the portal's PDF is generated client-side), so the official listing
  portal is recorded with source_kind "portal". No network needed.
- Tariff-value rows: query the CBIC notification API for the notification
  number and record the official notice PDF URL (source_kind "notice").
  When the CBIC API is unreachable, fall back to the caalley.com mirror
  (genuine notification PDFs under a predictable pattern; each candidate
  is downloaded and verified before use).
- Duty-rate rows: same treatment for the duty notification (CBIC "Tariff"
  category, caalley mirror fallback).
- Official CBIC links always win: rows whose link is a caalley mirror are
  re-checked against the CBIC API whenever it is reachable, and the
  mirror link is replaced by the official PDF URL when found.
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
import re
import subprocess
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_customs_tracker as B
import fetch as F

CBIC_CATEGORIES_TARIFF = ["Non Tariff"]
CBIC_CATEGORIES_ERAM = ["Exchange Rate", "Exchange rate"]
CBIC_CATEGORIES_DUTY = ["Tariff"]


def caalley_candidates(number):
    """Candidate caalley.com mirror PDF URLs for a notification number.

    N.T. series: cus{yy}/csnt{nn:02d}-{yyyy}.pdf (zero-padded).
    Regular Tariff series: cus{yy}/cst-{nn}-{yyyy}.pdf (not padded).
    """
    m = re.match(r"\s*(\d{1,3})\s*/\s*(\d{4})\s*-\s*Customs",
                 number or "", re.I)
    if not m:
        return []
    nn, yyyy = int(m.group(1)), m.group(2)
    yy = yyyy[2:]
    nt = bool(re.search(r"\(N\.?T\.?\)", number or "", re.I))
    if nt:
        return [f"https://www.caalley.com/cus{yy}/csnt{nn:02d}-{yyyy}.pdf"]
    return [f"https://caalley.com/cus{yy}/cst-{nn}-{yyyy}.pdf",
            f"https://www.caalley.com/cus{yy}/cst-{nn}-{yyyy}.pdf"]


def caalley_verify(url, number):
    """Download a mirror candidate; accept on HTTP 200 + PDF magic bytes,
    plus a best-effort notification-number text match when the PDF has a
    text layer (scanned PDFs such as 01/2026 are accepted on magic bytes,
    consistent with the official PDF which also has no text layer)."""
    try:
        body = F.http_bytes(url, timeout=30)
    except Exception as e:
        log(f"caalley fetch failed ({url}): {e}")
        return False
    if not body or not body.startswith(b"%PDF-"):
        return False
    m = re.match(r"\s*(\d{1,3})\s*/\s*(\d{4})", number or "")
    if not m:
        return True
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf",
                                         delete=False) as f:
            f.write(body)
            fn = f.name
        t = subprocess.run(["pdftotext", "-layout", fn, "-"],
                           capture_output=True, text=True,
                           timeout=30).stdout
        os.unlink(fn)
    except Exception:
        return True  # no pdftotext available: accept on magic bytes
    if not t.strip():
        return True  # scanned PDF: accept on magic bytes
    return bool(re.search(
        rf"Notification No\.?\s*0?{int(m.group(1))}\s*/\s*{m.group(2)}",
        t, re.I))


def caalley_lookup(number):
    """Return a verified caalley.com mirror PDF URL for `number`, or None."""
    for url in caalley_candidates(number):
        if caalley_verify(url, number):
            return url
    return None


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
                                   "customs-tracker.v3", "customs-tracker.v4"):
        print(f"backfill: ERROR: unexpected schema {data.get('schema')}",
              file=sys.stderr)
        sys.exit(1)
    rows = data["rows"]
    if not rows:
        print("backfill: no rows")
        return 0

    filled_official = filled_mirror = replaced_mirror = 0
    filled_portal = skipped = 0
    cbic_ok = cbic_probe()

    def official_then_mirror(number, categories, year):
        """(url, origin) for a notification: official CBIC PDF first,
        caalley mirror fallback. origin is 'official', 'mirror', or None."""
        if cbic_ok and number:
            url = cbic_find_pdf(number, categories, year)
            if url:
                return url, "official"
        if number:
            url = caalley_lookup(number)
            if url:
                return url, "mirror"
        return None, None

    new_rows = []
    for r in rows:
        r = dict(r)
        is_mirror = "caalley.com" in (r.get("source_url") or "")
        # Official links always win: mirror rows are re-checked against
        # CBIC whenever it is reachable. Other filled rows are done.
        if r.get("source_url") and not (is_mirror and cbic_ok):
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
                filled_official += 1
            else:
                r["source_url"], r["source_kind"] = F.ICEGATE_PORTAL, "portal"
                filled_portal += 1
        elif r["event"] in ("Tariff value", "Duty rate"):
            number = (r.get("tariff_notification")
                      or r.get("duty_notification"))
            categories = (CBIC_CATEGORIES_TARIFF
                          if r["event"] == "Tariff value"
                          else CBIC_CATEGORIES_DUTY)
            try:
                year = int(r["published"][:4])
            except ValueError:
                year = date.today().year
            url, origin = official_then_mirror(number, categories, year)
            if url == r.get("source_url"):
                skipped += 1  # mirror kept; CBIC had nothing better
            elif url:
                r["source_url"], r["source_kind"] = url, "notice"
                if origin == "official":
                    if is_mirror:
                        replaced_mirror += 1
                        log(f"official CBIC link replaced mirror for "
                            f"{number}")
                    else:
                        filled_official += 1
                else:
                    filled_mirror += 1
            else:
                log(f"no CBIC PDF and no mirror yet for {number}; "
                    "leaving null")
                r["source_url"], r["source_kind"] = None, None
        # reorder keys to the v4 column order; set notice_url + duty_rate
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
    print(f"backfill: official={filled_official} mirror={filled_mirror} "
          f"replaced_mirror={replaced_mirror} portal={filled_portal} "
          f"skipped={skipped} still-null="
          f"{sum(1 for r in checked if not r['source_url'])}; "
          f"rows={len(checked)} schema={B.SCHEMA}")


if __name__ == "__main__":
    main()
