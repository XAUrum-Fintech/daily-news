#!/usr/bin/env python3
"""Validate macro-events/latest.json against SPEC.md sections 3, 4 and 11.

Usage: python3 validate_macro_events.py <path>
Exits 0 on success; prints every violation and exits 1 on failure.
This mirrors what the reader backend enforces (all-or-nothing): one bad
record rejects the whole file.
"""
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

KINDS = {"us_jobs", "us_cpi", "fed_rate", "rbi_policy", "union_budget"}
STATUSES = {"scheduled", "released", "cancelled"}
UNITS = {"thousand_jobs": (-30000, 30000),
         "percent": (-100, 1000),
         "index": (-1000000, 1000000)}
KINDS_TZ = {"us_jobs": "America/New_York", "us_cpi": "America/New_York",
            "fed_rate": "America/New_York", "rbi_policy": "Asia/Kolkata",
            "union_budget": "Asia/Kolkata"}

ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,46}[a-z0-9])?$")
KEY_RE = re.compile(r"^[a-z0-9_]{1,40}$")
# Bare-host TLDs from section 11 (checked case-insensitively).
_TLDS = ("com net org io in co ly app xyz info me biz online site link to gl "
         "us uk ru cn de fr jp au ca eu tk cc tv ws ai gov edu dev").split()
BARE_HOST_RE = re.compile(
    r"(?<![\w.-])[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\."
    r"(?:" + "|".join(_TLDS) + r")\b", re.IGNORECASE)
INVISIBLE_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f   　‌‍‎‏]")

ERRORS = []


def err(msg):
    ERRORS.append(msg)


def is_plain_text(value, field):
    """Section 11 plain-text rules."""
    if not isinstance(value, str):
        return
    if any(c in value for c in "<>[]"):
        err(f"{field}: must not contain <, >, [ or ]: {value!r}")
    if "://" in value or "www." in value.lower():
        err(f"{field}: must not contain a web address: {value!r}")
    elif BARE_HOST_RE.search(value):
        err(f"{field}: must not contain a bare host name: {value!r}")
    if "xn--" in value.lower():
        err(f"{field}: must not contain a punycode label: {value!r}")
    if INVISIBLE_RE.search(value):
        err(f"{field}: contains a line break, tab or other control/invisible character")


def parse_ts(value, field):
    """Parse an RFC 3339 timestamp with an explicit offset. Returns aware dt."""
    if not isinstance(value, str):
        err(f"{field}: must be a string, got {type(value).__name__}")
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        err(f"{field}: not a valid RFC 3339 timestamp: {value!r}")
        return None
    if dt.tzinfo is None:
        err(f"{field}: timestamp must carry a zone/offset: {value!r}")
        return None
    return dt


def check_number(value, field, lo, hi):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        err(f"{field}: must be a JSON number, got {type(value).__name__}")
        return
    if not math.isfinite(value):
        err(f"{field}: must be finite")
        return
    if not (lo <= value <= hi):
        err(f"{field}: {value} out of bounds [{lo}, {hi}]")


def validate_metric(m, i, rec_id):
    f = f"{rec_id}.metrics[{i}]"
    if not isinstance(m, dict):
        err(f"{f}: must be an object")
        return
    for k in ("key", "label", "unit", "actual", "forecast",
              "previous", "previous_first_print"):
        if k not in m:
            err(f"{f}: missing key {k!r}")
    key = m.get("key")
    if not isinstance(key, str) or not KEY_RE.match(key or ""):
        err(f"{f}.key: 1-40 chars of [a-z0-9_]: {key!r}")
    label = m.get("label")
    if not isinstance(label, str) or not (1 <= len(label) <= 30):
        err(f"{f}.label: 1-30 chars: {label!r}")
    else:
        is_plain_text(label, f"{f}.label")
    unit = m.get("unit")
    if unit not in UNITS:
        err(f"{f}.unit: must be one of {sorted(UNITS)}: {unit!r}")
        return
    lo, hi = UNITS[unit]
    for num_field in ("actual", "forecast", "previous", "previous_first_print"):
        v = m.get(num_field)
        if v is not None:
            check_number(v, f"{f}.{num_field}", lo, hi)
    if m.get("previous") is None and m.get("previous_first_print") is not None:
        err(f"{f}.previous_first_print: must be null when previous is null")


def validate_record(r, idx, seen_ids, generated_at):
    f = f"events[{idx}]"
    if not isinstance(r, dict):
        err(f"{f}: must be an object")
        return
    required = ("id", "kind", "title", "status", "scheduled_at",
                "scheduled_tz", "metrics", "revisions", "summary",
                "forecast_source", "source_url", "published_at")
    for k in required:
        if k not in r:
            err(f"{f}: missing required field {k!r}")

    rid = r.get("id")
    if not isinstance(rid, str) or not ID_RE.match(rid or ""):
        err(f"{f}.id: 3-48 chars [a-z0-9-], start/end alnum: {rid!r}")
        rid = f"{f}"
    elif rid in seen_ids:
        err(f"{f}.id: duplicate id {rid!r}")
    seen_ids.add(rid)

    kind = r.get("kind")
    if kind not in KINDS:
        err(f"{rid}.kind: must be one of {sorted(KINDS)}: {kind!r}")

    title = r.get("title")
    if not isinstance(title, str) or not (1 <= len(title) <= 60):
        err(f"{rid}.title: 1-60 chars: {title!r}")
    else:
        is_plain_text(title, f"{rid}.title")

    status = r.get("status")
    if status not in STATUSES:
        err(f"{rid}.status: must be one of {sorted(STATUSES)}: {status!r}")

    sched_tz = r.get("scheduled_tz")
    if kind in KINDS_TZ and sched_tz != KINDS_TZ[kind]:
        err(f"{rid}.scheduled_tz: must be {KINDS_TZ[kind]} for kind {kind}: "
            f"{sched_tz!r}")
    scheduled = parse_ts(r.get("scheduled_at"), f"{rid}.scheduled_at")
    if scheduled is not None and sched_tz:
        try:
            zi = ZoneInfo(sched_tz)
        except Exception:
            err(f"{rid}.scheduled_tz: unknown IANA zone: {sched_tz!r}")
            zi = None
        if zi is not None:
            real_off = scheduled.astimezone(zi).utcoffset()
            if real_off != scheduled.utcoffset():
                err(f"{rid}.scheduled_at: offset {scheduled.utcoffset()} does "
                    f"not match {sched_tz} offset {real_off} at that instant "
                    f"(daylight-saving slip?)")
        if generated_at is not None:
            if abs((scheduled - generated_at).total_seconds()) > 400 * 86400:
                err(f"{rid}.scheduled_at: more than 400 days from generated_at")

    metrics = r.get("metrics")
    if not isinstance(metrics, list) or len(metrics) > 6:
        err(f"{rid}.metrics: 1-6 items (empty allowed only for "
            f"scheduled/cancelled): got {type(metrics).__name__}")
        metrics = []
    keys = set()
    for i, m in enumerate(metrics):
        validate_metric(m, i, rid)
        k = m.get("key") if isinstance(m, dict) else None
        if isinstance(k, str):
            if k in keys:
                err(f"{rid}.metrics: duplicate key {k!r}")
            keys.add(k)

    revisions = r.get("revisions")
    if not isinstance(revisions, list) or len(revisions) > 4:
        err(f"{rid}.revisions: 0-4 items")
        revisions = []
    for i, rev in enumerate(revisions):
        rf = f"{rid}.revisions[{i}]"
        if not isinstance(rev, dict):
            err(f"{rf}: must be an object")
            continue
        for k in ("label", "from", "to"):
            if k not in rev:
                err(f"{rf}: missing key {k!r}")
        label = rev.get("label")
        if not isinstance(label, str) or not (1 <= len(label) <= 20):
            err(f"{rf}.label: 1-20 chars: {label!r}")
        else:
            is_plain_text(label, f"{rf}.label")
        for num_field in ("from", "to"):
            v = rev.get(num_field)
            if not isinstance(v, (int, float)) or isinstance(v, bool) \
                    or not math.isfinite(v):
                err(f"{rf}.{num_field}: must be a finite JSON number")

    summary = r.get("summary")
    if not isinstance(summary, str) or not (1 <= len(summary) <= 160):
        err(f"{rid}.summary: 1-160 chars (got "
            f"{len(summary) if isinstance(summary, str) else type(summary).__name__})")
    else:
        is_plain_text(summary, f"{rid}.summary")

    fs = r.get("forecast_source")
    has_forecast = any(
        isinstance(m, dict) and m.get("forecast") is not None
        for m in metrics)
    if has_forecast:
        if not isinstance(fs, str) or not (1 <= len(fs) <= 40):
            err(f"{rid}.forecast_source: required (1-40 chars) when a "
                f"forecast exists: {fs!r}")
        else:
            is_plain_text(fs, f"{rid}.forecast_source")
    elif fs is not None:
        err(f"{rid}.forecast_source: must be null when no forecast exists")

    surl = r.get("source_url")
    if not isinstance(surl, str):
        err(f"{rid}.source_url: must be a string")
    else:
        try:
            p = urlparse(surl)
        except Exception:
            p = None
        host = (p.hostname or "").lower() if p else ""
        ok_host = (host == "rbi.org.in" or host == "www.rbi.org.in"
                   or host.endswith(".gov") or host.endswith(".gov.in"))
        if not p or p.scheme != "https" or not ok_host:
            err(f"{rid}.source_url: https on .gov/.gov.in/rbi.org.in only: "
                f"{surl!r}")

    pub = r.get("published_at")
    published = None
    if pub is not None:
        published = parse_ts(pub, f"{rid}.published_at")
        if published is not None and scheduled is not None:
            if published < scheduled - timedelta(hours=1):
                err(f"{rid}.published_at: before scheduled_at minus one hour")

    # Section 3.4 status rules.
    if status == "scheduled":
        if any(isinstance(m, dict) and m.get("actual") is not None
               for m in metrics):
            err(f"{rid}: scheduled record must have all actuals null")
        if pub is not None:
            err(f"{rid}: scheduled record must have published_at null")
        if revisions:
            err(f"{rid}: scheduled record must have empty revisions")
    elif status == "released":
        if not metrics:
            if kind != "union_budget":
                err(f"{rid}: released record needs at least one metric")
        elif not (isinstance(metrics[0], dict)
                  and metrics[0].get("actual") is not None):
            err(f"{rid}: released record's first metric needs non-null actual")
        if pub is None:
            err(f"{rid}: released record needs published_at")
        if revisions and not metrics:
            err(f"{rid}: released record with revisions needs metrics")
    elif status == "cancelled":
        if any(isinstance(m, dict) and m.get("actual") is not None
               for m in metrics):
            err(f"{rid}: cancelled record must have all actuals null")
        if pub is not None:
            err(f"{rid}: cancelled record must have published_at null")
        if revisions:
            err(f"{rid}: cancelled record must have empty revisions")


def validate_file(path):
    try:
        raw = open(path, "rb").read()
    except OSError as e:
        err(f"cannot read {path}: {e}")
        return False
    if not (1 <= len(raw) <= 1_048_576):
        err(f"file size {len(raw)} outside 1 byte..1 MiB")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except Exception as e:
        err(f"not valid JSON: {e}")
        return False
    if not isinstance(doc, dict):
        err("top level must be an object")
        return False
    for k in ("schema", "generated_at", "event_count", "events"):
        if k not in doc:
            err(f"missing envelope field {k!r}")
    if doc.get("schema") != "macro-events.v1":
        err(f"schema must be 'macro-events.v1': {doc.get('schema')!r}")
    generated_at = parse_ts(doc.get("generated_at"), "generated_at")
    if generated_at is not None:
        now = datetime.now(timezone.utc)
        if generated_at > now + timedelta(minutes=10):
            err("generated_at is more than 10 minutes in the future")
    events = doc.get("events")
    if not isinstance(events, list) or not (1 <= len(events) <= 400):
        err(f"events: 1-400 records, got "
            f"{len(events) if isinstance(events, list) else type(events).__name__}")
        events = []
    if doc.get("event_count") != len(events):
        err(f"event_count {doc.get('event_count')} != events length "
            f"{len(events)}")
    seen = set()
    for i, rec in enumerate(events):
        validate_record(rec, i, seen, generated_at)
    return not ERRORS


def main():
    if len(sys.argv) != 2:
        print("usage: validate_macro_events.py <path>", file=sys.stderr)
        return 2
    ok = validate_file(sys.argv[1])
    if ok:
        print("OK: macro-events file valid")
        return 0
    for e in ERRORS:
        print("ERROR:", e)
    print(f"{len(ERRORS)} violation(s)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
