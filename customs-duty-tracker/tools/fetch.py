#!/usr/bin/env python3
"""Detect new customs-duty events from official sources.

Reads the tracker's latest.json from a repo checkout, queries the official
sources, and prints new events as JSON to stdout (builder-ready dicts).

Official sources:
  ICEGATE ERAM circulars:
    POST https://foservices.icegate.gov.in/cbu/icegateapi/getnotdetails
      body: null  -> [{notificationNumber, notPublishDate}]  (dd-mm-yyyy,
      the PUBLICATION date)
    POST https://foservices.icegate.gov.in/cbu/icegateapi/igexratepublishnot
      body: {"notNum": "<notificationNumber>"}
      -> {notificationNumber, notPublishDate (dd-mm-yyyy, the EFFECTIVE date),
          currencyDetail: [{currencyCode, cbicImport, cbicExport, units}]}
    (Reverse-engineered from the portal's own Angular bundle 2026-09-29:
    downloadPdf(e) calls fetchCurrencyDetails({notNum: e}); the "PDF" the
    portal offers is generated client-side from this JSON, so the JSON *is*
    the official data -- no PDF parsing needed.)
  CBIC tariff-value notifications ("Fixation of Tariff Value",
  amending 36/2001-Customs (N.T.)):
    GET https://taxinformation.cbic.gov.in/api/cbic-notification-msts/
        fetchNotificationByYearAndCategory?taxId=1000002&category=Non Tariff
      (paginated; a large size parameter returns truncated results)
    GET https://taxinformation.cbic.gov.in/content/pdf/<docFilePath>
      -> pdftotext -> TABLE-2: gold USD/10g, silver USD/kg
  LBMA: https://prices.lbma.org.uk/json/gold_pm.json and .../silver.json
    (full history; v[0] is the USD leg). PM gold fix and silver benchmark on
    the notification's published date, falling back to the most recent
    earlier quote when that day had no auction. Tariff rows only.

Exit 0: detection ran (possibly zero new events).
Exit 1: hard failure (tracker unreadable, or every source failed).
A CBIC failure is a *warning*, not a fatal error: the CBIC WAF
intermittently rejects requests, so ICEGATE/LBMA results are still
reported and the warning is surfaced for the operator.

Usage:
    fetch.py --tracker-dir <path-to>/customs-duty-tracker [--out events.json]
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta

ICEGATE_BASE = "https://foservices.icegate.gov.in/cbu/icegateapi"
ICEGATE_PORTAL = "https://foservices.icegate.gov.in/#/services/notifyPublishScreen"
CBIC_LIST = ("https://taxinformation.cbic.gov.in/api/cbic-notification-msts/"
             "fetchNotificationByYearAndCategory")
CBIC_PDF = "https://taxinformation.cbic.gov.in/content/pdf/"
LBMA_GOLD = "https://prices.lbma.org.uk/json/gold_pm.json"
LBMA_SILVER = "https://prices.lbma.org.uk/json/silver.json"

TIMEOUT = 30


def log(msg):
    print(f"fetch: {msg}", file=sys.stderr)


def _request_once(method, url, body, timeout):
    """HTTP via curl subprocess. (Python's urllib is fingerprinted and
    dropped by the ICEGATE/CBIC frontends; curl works reliably.)"""
    cmd = ["curl", "-sS", "--max-time", str(timeout), "-X", method, url]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json",
                "-d", json.dumps(body)]
    else:
        cmd += ["-H", "Accept: application/json"]
    cmd += ["-A", "Mozilla/5.0 (X11; Linux x86_64) customs-duty-tracker/1.0"]
    r = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
    if r.returncode != 0:
        raise ConnectionError(r.stderr.decode()[:200] or
                              f"curl exit {r.returncode}")
    return r.stdout


def _request(method, url, body=None, timeout=TIMEOUT, retries=3):
    """HTTP with retries: the ICEGATE/CBIC frontends intermittently drop
    connections or return empty replies (WAF)."""
    last = None
    for attempt in range(retries):
        try:
            return _request_once(method, url, body, timeout)
        except (ConnectionError, subprocess.TimeoutExpired,
                subprocess.SubprocessError) as e:
            last = e
            log(f"request failed (attempt {attempt + 1}/{retries} "
                f"{method} {url.split('?')[0]}): {e}")
            time.sleep(2 ** attempt)
    raise RuntimeError(f"{method} {url.split('?')[0]} failed after "
                       f"{retries} attempts: {last}")


def http_json(method, url, body=None, timeout=TIMEOUT, retries=3):
    last = None
    for attempt in range(retries):
        try:
            raw = _request(method, url, body, timeout, retries=1)
            return json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, RuntimeError) as e:
            # ValueError: truncated/empty body that failed json decoding
            last = e
            log(f"bad response (attempt {attempt + 1}/{retries} "
                f"{method} {url.split('?')[0]}): {e}")
            time.sleep(2 ** attempt)
    raise RuntimeError(f"{method} {url.split('?')[0]}: no usable response "
                       f"after {retries} attempts: {last}")


def http_bytes(url, timeout=TIMEOUT):
    return _request("GET", url, None, timeout)


def ddmmyyyy_to_iso(s):
    return datetime.strptime(s.strip(), "%d-%m-%Y").date().isoformat()


# ---------------------------------------------------------------- repo state

def load_tracker(tracker_dir):
    path = os.path.join(tracker_dir, "latest.json")
    with open(path) as f:
        data = json.load(f)
    if data.get("schema") not in ("customs-tracker.v1", "customs-tracker.v2"):
        raise RuntimeError(f"unexpected schema in {path}")
    known_tariff, known_eram = set(), set()
    cutoff = ""
    for r in data["rows"]:
        if r.get("tariff_notification"):
            known_tariff.add(r["tariff_notification"])
        if r.get("eram_notification"):
            known_eram.add(r["eram_notification"])
        cutoff = max(cutoff, r["published"])
    # The tracker continues the .xlsx event log from 2026-01-13; rows only
    # ever grow. A source item counts as "new" only if it is unknown AND
    # published after the last tracked row -- older unknown items are
    # outside the tracker's coverage window, not new events.
    return known_tariff, known_eram, cutoff


# ------------------------------------------------------------------ ICEGATE

def icegate_list():
    items = http_json("POST", f"{ICEGATE_BASE}/getnotdetails", body=None)
    if not isinstance(items, list):
        raise RuntimeError("getnotdetails did not return a list")
    out = []
    for it in items:
        num = (it.get("notificationNumber") or "").strip()
        pub = (it.get("notPublishDate") or "").strip()
        if not num or not pub:
            continue
        out.append({"number": num, "published": ddmmyyyy_to_iso(pub)})
    return out


def icegate_detail(number):
    d = http_json("POST", f"{ICEGATE_BASE}/igexratepublishnot",
                  body={"notNum": number})
    detail = d.get("currencyDetail") or []
    usd = next((c for c in detail
                if (c.get("currencyCode") or "").upper() == "USD"), None)
    if usd is None:
        raise RuntimeError(f"{number}: no USD row in currencyDetail")
    imp = float(str(usd.get("cbicImport")).replace(",", ""))
    exp = float(str(usd.get("cbicExport")).replace(",", ""))
    effective = ddmmyyyy_to_iso(d.get("notPublishDate", ""))
    return {"usd_import": imp, "usd_export": exp, "effective": effective}


def icegate_new_events(known_eram, cutoff):
    events = []
    for item in icegate_list():
        if item["number"] in known_eram or item["published"] <= cutoff:
            continue
        log(f"new ERAM circular {item['number']} published {item['published']}")
        det = icegate_detail(item["number"])
        pub = datetime.strptime(item["published"], "%Y-%m-%d").date()
        if det["effective"] != (pub + timedelta(days=1)).isoformat():
            log(f"warning: {item['number']} effective {det['effective']} != "
                f"published+1; using the circular's own date")
        events.append({
            "event": "exchange_rate",
            "published": item["published"],
            "effective": det["effective"],
            "eram_notification": item["number"],
            "usd_inr_import": det["usd_import"],
            "usd_inr_export": det["usd_export"],
            # ICEGATE exposes no per-notice URL (detail is a POST API; the
            # portal's PDF is generated client-side), so the official
            # listing portal is the source link.
            "source_url": ICEGATE_PORTAL,
            "source_kind": "portal",
        })
    return events


# ---------------------------------------------------------------------- LBMA

_lbma_cache = {}


def lbma_history(url):
    if url in _lbma_cache:
        return _lbma_cache[url]
    data = http_json("GET", url, timeout=60)
    hist = {}
    for row in data:
        try:
            v = row.get("v") or []
            hist[row["d"]] = float(v[0])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    _lbma_cache[url] = hist
    return hist


def lbma_fix(hist_gold, hist_silver, day_iso):
    """(gold USD fix, silver USD fix, fix_date): fixes on `day_iso`, falling
    back to the most recent earlier day with quotes for both metals
    (no auction on weekends/holidays). Single fix_date, as in the seed."""
    day = datetime.strptime(day_iso, "%Y-%m-%d").date()
    for _ in range(12):
        iso = day.isoformat()
        if iso in hist_gold and iso in hist_silver:
            return hist_gold[iso], hist_silver[iso], iso
        day -= timedelta(days=1)
    raise RuntimeError(f"no LBMA fixes within 12 days before {day_iso}")


# ---------------------------------------------------------------------- CBIC

def cbic_fetch_page(year, page, size):
    url = (f"{CBIC_LIST}?taxId=1000002&category=Non%20Tariff"
           f"&year={year}&page={page}&size={size}")
    try:
        d = http_json("GET", url)
    except Exception as e:
        log(f"CBIC list request failed (year={year} page={page}): {e}")
        return None
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for k in ("content", "data", "items", "results", "notifications",
                  "notificationList"):
            if isinstance(d.get(k), list):
                return d[k]
    log(f"CBIC list returned unrecognized shape for year={year} page={page}")
    return None


def _pick(d, *keys):
    for k in keys:
        v = d.get(k)
        if v:
            return str(v).strip()
    return ""


def cbic_tariff_notifications():
    """All 'Fixation of Tariff Value' notifications from the CBIC API."""
    found = []
    today = date.today()
    for year in (today.year, today.year - 1):
        seen_pages = 0
        for page in range(20):
            items = cbic_fetch_page(year, page, 50)
            if items is None:
                break  # request failed; WAF may be blocking
            if not items:
                break
            seen_pages += 1
            for it in items:
                subject = _pick(it, "subject", "notificationSubject", "title")
                if "fixation of tariff value" not in subject.lower():
                    continue
                found.append({
                    "number": _pick(it, "notificationNo", "notificationNumber",
                                    "notNo", "docNumber"),
                    "dated": _pick(it, "notificationDate", "dated", "date",
                                    "publishDate", "notDate"),
                    "pdf": _pick(it, "docFilePath", "filePath", "pdfPath",
                                  "documentPath"),
                    "subject": subject,
                })
            if len(items) < 50:
                break
        if seen_pages:
            log(f"CBIC: scanned {seen_pages} page(s) for {year}, "
                f"{len(found)} tariff-value notifications total")
    # de-dupe by number, keep first
    uniq = {}
    for n in found:
        if n["number"] and n["number"] not in uniq:
            uniq[n["number"]] = n
    return list(uniq.values())


def cbic_parse_date(s):
    s = s.strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y",
                "%d-%b-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    m = re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", s)
    if m:
        a, b, y = m.groups()
        return f"{y}-{b.zfill(2)}-{a.zfill(2)}"
    raise RuntimeError(f"unparseable CBIC date: {s!r}")


def cbic_extract_tariff_values(pdf_bytes):
    """TABLE-2 gold (USD/10g) and silver (USD/kg) via pdftotext."""
    if not shutil.which("pdftotext"):
        raise RuntimeError("pdftotext not available")
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf_bytes)
        pdf_path = f.name
    txt_path = pdf_path + ".txt"
    try:
        r = subprocess.run(["pdftotext", "-layout", pdf_path, txt_path],
                           capture_output=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError("pdftotext failed: " +
                               r.stderr.decode()[:200])
        text = open(txt_path, encoding="utf-8", errors="replace").read()
    finally:
        for p in (pdf_path, txt_path):
            try:
                os.unlink(p)
            except OSError:
                pass
    lines = text.splitlines()
    gold = silver = None
    gold_line = silver_line = ""
    for ln in lines:
        low = ln.lower()
        nums = [float(n.replace(",", ""))
                for n in re.findall(r"\d{1,3}(?:,\d{3})*(?:\.\d+)?", ln)]
        if "gold" in low and "silver" not in low and "10" in ln:
            cand = [n for n in nums if 500 <= n <= 6000]
            if cand:
                gold, gold_line = cand[-1], ln.strip()
        if "silver" in low and "kg" in low:
            cand = [n for n in nums if 500 <= n <= 12000]
            if cand:
                silver, silver_line = cand[-1], ln.strip()
    if gold is None or silver is None:
        raise RuntimeError("TABLE-2 gold/silver values not found in PDF text")
    return gold, silver, gold_line, silver_line


def cbic_new_events(known_tariff, cutoff, warnings):
    events = []
    try:
        notifications = cbic_tariff_notifications()
    except Exception as e:
        warnings.append(f"CBIC list fetch failed: {e}")
        return events
    if not notifications:
        warnings.append("CBIC list fetch returned no tariff-value "
                        "notifications (site may be blocking requests)")
        return events
    gold_hist = lbma_history(LBMA_GOLD)
    silver_hist = lbma_history(LBMA_SILVER)
    for n in notifications:
        if not n["number"] or n["number"] in known_tariff:
            continue
        if not n["pdf"]:
            warnings.append(f"CBIC {n['number']}: no PDF path; skipped")
            continue
        try:
            published = cbic_parse_date(n["dated"])
        except Exception as e:
            warnings.append(f"CBIC {n['number']}: bad date "
                            f"{n['dated']!r}: {e}")
            continue
        if published <= cutoff:
            continue
        log(f"new tariff notification {n['number']} dated {n['dated']}")
        try:
            pdf_url = CBIC_PDF + n["pdf"].lstrip("/")
            pdf_bytes = http_bytes(pdf_url)
            gold, silver, gl, sl = cbic_extract_tariff_values(pdf_bytes)
            log(f"  TABLE-2: gold {gold}/10g ({gl[:60]}), "
                f"silver {silver}/kg ({sl[:60]})")
            gold_fix, silver_fix, fix_date = lbma_fix(
                gold_hist, silver_hist, published)
            events.append({
                "event": "tariff_value",
                "published": published,
                "effective": (datetime.strptime(published, "%Y-%m-%d").date()
                              + timedelta(days=1)).isoformat(),
                "tariff_notification": n["number"],
                "gold_tariff": gold,
                "silver_tariff": silver,
                "gold_fix": gold_fix,
                "silver_fix": silver_fix,
                "fix_date": fix_date,
                # The official notice PDF itself.
                "source_url": pdf_url,
                "source_kind": "notice",
            })
        except Exception as e:
            warnings.append(f"CBIC {n['number']}: extraction failed: {e}")
    return events


# ---------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracker-dir", required=True,
                    help="path to customs-duty-tracker/ checkout dir")
    ap.add_argument("--out", default=None,
                    help="write events JSON here (default: stdout)")
    args = ap.parse_args()

    known_tariff, known_eram, cutoff = load_tracker(args.tracker_dir)
    log(f"known: {len(known_tariff)} tariff, {len(known_eram)} ERAM, "
        f"cutoff {cutoff}")

    warnings = []
    events = []

    try:
        events.extend(icegate_new_events(known_eram, cutoff))
    except Exception as e:
        # ICEGATE failing entirely is fatal: without it we cannot know
        # whether exchange-rate events are missing.
        print(f"fetch: FATAL: ICEGATE detection failed: {e}",
              file=sys.stderr)
        sys.exit(1)

    events.extend(cbic_new_events(known_tariff, cutoff, warnings))

    # chronological, tariff rows before exchange rows on the same date
    events.sort(key=lambda e: (e["published"],
                               0 if e["event"] == "tariff_value" else 1))

    out = {"events": events, "warnings": warnings}
    payload = json.dumps(out, indent=2)
    if args.out:
        with open(args.out, "w") as f:
            f.write(payload + "\n")
    else:
        print(payload)
    log(f"done: {len(events)} new event(s), {len(warnings)} warning(s)")


if __name__ == "__main__":
    main()
