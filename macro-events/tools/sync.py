#!/usr/bin/env python3
"""macro-events agent: keep macro-events/latest.json current and publish it.

Modes:
  window            Print QUIET, or IN-WINDOW lines for scheduled releases whose
                    scheduled_at is within [now-15min, now+90min]. Exit 0.
  release --payload FILE
                    Apply a verified result payload to one scheduled record
                    (flips it to released), validate, and commit if changed.
  refresh           Calendar refresh: drop records older than 90 days, add
                    releases that entered the 120-day window from the verified
                    schedule table, roll reschedules. Commit if changed.
  rebuild [--heartbeat]
                    Full rebuild from the verified schedule table reconciled
                    against the committed file (never rewrites a published
                    id's figures or published_at). With --heartbeat, commit
                    even when nothing changed (daily 06:00 IST heartbeat).

Reads the committed file from https://raw.githubusercontent.com (public repo,
no auth). Writes via the GitHub git-database API through the gh-api helper,
one atomic commit per publish, message "macro-events: <what changed>".
Never rewrites history or force-pushes.

Reachability from this network (verified 2026-10-04; re-check yearly):
  - bls.gov: curl is BLOCKED (403 Access Denied). Read BLS schedule/release
    pages with the text-fetch browser tool instead:
      jobs schedule  https://www.bls.gov/schedule/news_release/empsit.htm
      cpi schedule   https://www.bls.gov/schedule/news_release/cpi.htm
      jobs result    https://www.bls.gov/news.release/empsit.nr0.htm
      cpi result     https://www.bls.gov/news.release/cpi.nr0.htm
  - federalreserve.gov: curl works.
      meetings       https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
                     (statement link is on the second day of each meeting;
                     released 2:00 pm New York time)
      statements     https://www.federalreserve.gov/newsevents/pressreleases.htm
  - rbi.org.in: curl works.
      MPC schedule   https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx?prid=62422
      results        https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx
  - indiabudget.gov.in / pib.gov.in: reachable. Publish a budget record only
    when the date is officially announced; never guess.

All figures and dates come from the official source only. If an official
page cannot be read, publish nothing new for that release.
"""
import argparse
import base64
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

REPO = "XAUrum-Fintech/daily-news"
BRANCH = "main"
SUBDIR = "macro-events"
COMMIT_FILES = ["latest.json", "state.json"]
RAW = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{SUBDIR}"
GH_API = os.path.expanduser("~/workspace/skills/github/bin/gh-api")

RETENTION_PAST_DAYS = 90
RETENTION_FUTURE_DAYS = 120
WINDOW_BEFORE_MIN = 15
WINDOW_AFTER_MIN = 90
STALE_COMMIT_HOURS = 30

TITLES = {"us_jobs": "US jobs report", "us_cpi": "US inflation (CPI)",
          "fed_rate": "Fed rate decision", "rbi_policy": "RBI policy decision",
          "union_budget": "Union Budget"}
SCHED_SUMMARY = {
    "us_jobs": "Monthly count of US jobs added, and the unemployment rate.",
    "us_cpi": "How much US consumer prices rose over the past year.",
    "fed_rate": "The US central bank announces its policy interest rate.",
    "rbi_policy": "India's central bank announces the repo rate.",
    "union_budget": ("The Finance Minister presents India's annual budget, "
                     "including customs duty changes."),
}
TZ = {"us_jobs": "America/New_York", "us_cpi": "America/New_York",
      "fed_rate": "America/New_York", "rbi_policy": "Asia/Kolkata",
      "union_budget": "Asia/Kolkata"}

# Officially verified 2026-10-04. BLS via text-fetch (curl blocked);
# Fed calendar via curl; RBI MPC schedule press release prid=62422.
# (kind, y, m, d, hh, mm, offset, source_url)
VERIFIED_SCHEDULE = [
    ("us_jobs", 2026, 11, 6, 8, 30, "-05:00",
     "https://www.bls.gov/schedule/news_release/empsit.htm"),
    ("us_jobs", 2026, 12, 4, 8, 30, "-05:00",
     "https://www.bls.gov/schedule/news_release/empsit.htm"),
    ("us_cpi", 2026, 10, 14, 8, 30, "-04:00",
     "https://www.bls.gov/schedule/news_release/cpi.htm"),
    ("us_cpi", 2026, 11, 10, 8, 30, "-05:00",
     "https://www.bls.gov/schedule/news_release/cpi.htm"),
    ("us_cpi", 2026, 12, 10, 8, 30, "-05:00",
     "https://www.bls.gov/schedule/news_release/cpi.htm"),
    ("fed_rate", 2026, 10, 28, 14, 0, "-04:00",
     "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"),
    ("fed_rate", 2026, 12, 9, 14, 0, "-05:00",
     "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"),
    ("fed_rate", 2027, 1, 27, 14, 0, "-05:00",
     "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"),
    ("rbi_policy", 2026, 10, 7, 10, 0, "+05:30",
     "https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx?prid=62422"),
    ("rbi_policy", 2026, 12, 4, 10, 0, "+05:30",
     "https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx?prid=62422"),
    ("rbi_policy", 2027, 2, 5, 10, 0, "+05:30",
     "https://www.rbi.org.in/scripts/BS_PressReleaseDisplay.aspx?prid=62422"),
]
# kind -> date string for the slug date part of the id.
ID_SLUGS = {"us_jobs": "us-jobs", "us_cpi": "us-cpi", "fed_rate": "fed",
            "rbi_policy": "rbi", "union_budget": "budget"}


def log(msg):
    print(msg, flush=True)


def utcnow():
    return datetime.now(timezone.utc)


def iso_z(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s):
    v = s.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    return datetime.fromisoformat(v)


def gh(method, path, data=None):
    cmd = [GH_API, method, path]
    if data is not None:
        cmd += ["--data", json.dumps(data)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh-api {method} {path} failed: {r.stderr[:500]}")
    return json.loads(r.stdout) if r.stdout.strip() else {}


def fetch_committed(name):
    r = subprocess.run(["curl", "-s", "--max-time", "30", f"{RAW}/{name}"],
                       capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip().startswith("{"):
        raise RuntimeError(f"could not fetch committed {name}")
    return json.loads(r.stdout)


def validate_file_obj(doc):
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json",
                                     delete=False) as f:
        json.dump(doc, f)
        path = f.name
    here = os.path.dirname(os.path.abspath(__file__))
    val = os.path.join(here, "validate_macro_events.py")
    r = subprocess.run([sys.executable, val, path],
                       capture_output=True, text=True)
    os.unlink(path)
    if r.returncode != 0:
        raise RuntimeError("validation failed:\n" + r.stdout + r.stderr)
    return True


def canonical(doc):
    d = json.loads(json.dumps(doc))
    d["generated_at"] = "NORMALIZED"
    return json.dumps(d, sort_keys=True)


def scheduled_record(kind, y, mo, d, hh, mm, off, source_url):
    slug = f"{y:04d}-{mo:02d}-{d:02d}"
    return {
        "id": f"{ID_SLUGS[kind]}-{slug}",
        "kind": kind,
        "title": TITLES[kind],
        "status": "scheduled",
        "scheduled_at": f"{slug}T{hh:02d}:{mm:02d}:00{off}",
        "scheduled_tz": TZ[kind],
        "metrics": [],
        "revisions": [],
        "summary": SCHED_SUMMARY[kind],
        "forecast_source": None,
        "source_url": source_url,
        "published_at": None,
    }


def commit(files, msg):
    """Atomic commit of macro-events files via the git-database API."""
    ref = gh("GET", f"/repos/{REPO}/git/refs/heads/{BRANCH}")
    base_sha = ref["object"]["sha"]
    base_tree = gh("GET",
                   f"/repos/{REPO}/git/commits/{base_sha}")["tree"]["sha"]
    blobs = []
    for path, name in files:
        with open(path, "rb") as f:
            content = base64.b64encode(f.read()).decode()
        b = gh("POST", f"/repos/{REPO}/git/blobs",
               {"content": content, "encoding": "base64"})
        blobs.append({"path": f"{SUBDIR}/{name}", "mode": "100644",
                      "type": "blob", "sha": b["sha"]})
    tree = gh("POST", f"/repos/{REPO}/git/trees",
              {"base_tree": base_tree, "tree": blobs})
    new_commit = gh("POST", f"/repos/{REPO}/git/commits",
                    {"message": msg, "tree": tree["sha"],
                     "parents": [base_sha]})
    try:
        gh("PATCH", f"/repos/{REPO}/git/refs/heads/{BRANCH}",
           {"sha": new_commit["sha"]})
    except RuntimeError:
        log("ref update conflict; retrying once on fresh head")
        base_sha = gh("GET",
                      f"/repos/{REPO}/git/refs/heads/{BRANCH}")["object"]["sha"]
        base_tree = gh("GET",
                       f"/repos/{REPO}/git/commits/{base_sha}")["tree"]["sha"]
        tree = gh("POST", f"/repos/{REPO}/git/trees",
                  {"base_tree": base_tree, "tree": blobs})
        new_commit = gh("POST", f"/repos/{REPO}/git/commits",
                        {"message": msg, "tree": tree["sha"],
                         "parents": [base_sha]})
        gh("PATCH", f"/repos/{REPO}/git/refs/heads/{BRANCH}",
           {"sha": new_commit["sha"]})
    ref = gh("GET", f"/repos/{REPO}/git/refs/heads/{BRANCH}")
    if ref["object"]["sha"] != new_commit["sha"]:
        raise RuntimeError("ref verification failed after commit")
    log(f"committed {new_commit['sha'][:7]}: {msg}")
    return new_commit["sha"]


def cmd_window(committed):
    now = utcnow()
    hits = []
    for r in committed["events"]:
        if r["status"] != "scheduled":
            continue
        try:
            sched = parse_ts(r["scheduled_at"])
        except ValueError:
            continue
        delta = (now - sched).total_seconds() / 60
        if -WINDOW_BEFORE_MIN <= delta <= WINDOW_AFTER_MIN:
            hits.append(r)
    if not hits:
        log("QUIET: no scheduled release in the release window")
        return 0
    for r in hits:
        log(f"IN-WINDOW id={r['id']} kind={r['kind']} "
            f"scheduled_at={r['scheduled_at']} source={r['source_url']}")
    return 0


def cmd_release(committed, state, payload_path, workdir):
    payload = json.load(open(payload_path))
    rid = payload["id"]
    rec = next((r for r in committed["events"] if r["id"] == rid), None)
    if rec is None:
        raise RuntimeError(f"unknown id {rid}; never change ids")
    if rec["status"] != "scheduled":
        log(f"no-op: {rid} is already {rec['status']}")
        return 0
    now = utcnow()
    rec["status"] = "released"
    rec["metrics"] = payload["metrics"]
    rec["revisions"] = payload.get("revisions", [])
    rec["summary"] = payload["summary"]
    rec["published_at"] = iso_z(now)
    committed["generated_at"] = iso_z(now)
    committed["event_count"] = len(committed["events"])
    validate_file_obj(committed)
    if canonical(committed) == canonical(fetch_committed("latest.json")):
        log("no-op: file unchanged")
        return 0
    lp = os.path.join(workdir, "latest.json")
    sp = os.path.join(workdir, "state.json")
    state.update({"last_run": iso_z(now), "last_release_commit": rid})
    json.dump(committed, open(lp, "w"), indent=2, ensure_ascii=False)
    json.dump(state, open(sp, "w"), indent=2)
    sha = commit([(lp, "latest.json"), (sp, "state.json")],
                 f"macro-events: {rid} released")
    log(f"RELEASED {rid} commit={sha[:7]}")
    return 0


def build_file(committed):
    """Reconcile the verified schedule table against the committed file."""
    now = utcnow()
    by_id = {r["id"]: r for r in committed["events"]}
    out = []
    cutoff_past = now - timedelta(days=RETENTION_PAST_DAYS)
    cutoff_future = now + timedelta(days=RETENTION_FUTURE_DAYS)

    # 1. Carry over released/cancelled records inside the retention window.
    for r in committed["events"]:
        if r["status"] in ("released", "cancelled"):
            try:
                sched = parse_ts(r["scheduled_at"])
            except ValueError:
                continue
            if sched >= cutoff_past:
                out.append(r)

    # 2. Scheduled records from the verified table inside the future window.
    for (kind, y, mo, d, hh, mm, off, surl) in VERIFIED_SCHEDULE:
        rec = scheduled_record(kind, y, mo, d, hh, mm, off, surl)
        sched = parse_ts(rec["scheduled_at"])
        if sched > cutoff_future:
            continue  # enters the window later
        existing = by_id.get(rec["id"])
        if existing and existing["status"] != "scheduled":
            continue  # released already carried over
        if existing and existing["status"] == "scheduled":
            # Roll a reschedule, keep the id forever.
            if existing["scheduled_at"] != rec["scheduled_at"]:
                log(f"rescheduled {rec['id']}: "
                    f"{existing['scheduled_at']} -> {rec['scheduled_at']}")
            existing["scheduled_at"] = rec["scheduled_at"]
            existing["scheduled_tz"] = rec["scheduled_tz"]
            existing["title"] = rec["title"]
            existing["summary"] = rec["summary"]
            existing["source_url"] = rec["source_url"]
            out.append(existing)
        else:
            out.append(rec)
            log(f"added {rec['id']}")

    # 3. Keep committed scheduled records the table no longer lists
    #    (e.g. future dates the worker verified by hand), inside window.
    known_ids = {scheduled_record(k, y, mo, d, hh, mm, off, s)["id"]
                 for (k, y, mo, d, hh, mm, off, s) in VERIFIED_SCHEDULE}
    for r in committed["events"]:
        if (r["status"] == "scheduled" and r["id"] not in known_ids
                and r["id"] not in {x["id"] for x in out}):
            try:
                sched = parse_ts(r["scheduled_at"])
            except ValueError:
                continue
            if cutoff_past <= sched <= cutoff_future:
                out.append(r)

    doc = {"schema": "macro-events.v1",
           "generated_at": iso_z(now),
           "event_count": len(out),
           "events": out}
    return doc


def staleness_warnings(committed):
    now = utcnow()
    warns = []
    try:
        head = gh("GET", f"/repos/{REPO}/commits?path={SUBDIR}/latest.json"
                         f"&sha={BRANCH}&per_page=1")
        if head:
            committed_at = parse_ts(head[0]["commit"]["committer"]["date"])
            age_h = (now - committed_at).total_seconds() / 3600
            if age_h > STALE_COMMIT_HOURS:
                warns.append(
                    f"STALE: no commit to {SUBDIR}/latest.json for "
                    f"{age_h:.1f}h (alert threshold {STALE_COMMIT_HOURS}h)")
    except Exception as e:  # never fail the run on the warning path
        warns.append(f"staleness check failed: {e}")
    for r in committed.get("events", []):
        if r.get("status") != "scheduled":
            continue
        try:
            sched = parse_ts(r["scheduled_at"])
        except ValueError:
            continue
        if sched < now and (now - sched).total_seconds() / 60 > WINDOW_AFTER_MIN:
            warns.append(
                f"MISSED: {r['id']} was scheduled at {r['scheduled_at']} "
                f"({r['scheduled_tz']}) and has no result yet")
    return warns


def write_and_maybe_commit(committed, state, msg, force=False):
    validate_file_obj(committed)
    try:
        live = fetch_committed("latest.json")
    except RuntimeError:
        live = None  # first publish: nothing committed yet
    changed = live is None or canonical(committed) != canonical(live)
    if not changed and not force:
        log("UNCHANGED: no commit")
        return None
    workdir = os.environ.get("MACRO_WORKDIR", "/tmp/macro-events")
    os.makedirs(workdir, exist_ok=True)
    lp = os.path.join(workdir, "latest.json")
    sp = os.path.join(workdir, "state.json")
    json.dump(committed, open(lp, "w"), indent=2, ensure_ascii=False)
    json.dump(state, open(sp, "w"), indent=2)
    return commit([(lp, "latest.json"), (sp, "state.json")], msg)


def cmd_refresh(committed, state):
    now = utcnow()
    for w in staleness_warnings(committed):
        log("WARNING: " + w)
    new = build_file(committed)
    state.update({"last_run": iso_z(now), "last_refresh": iso_z(now)})
    sha = write_and_maybe_commit(new, state, "macro-events: calendar refresh")
    if sha:
        state["last_commit_sha"] = sha[:7]
    return 0


def cmd_rebuild(committed, state, heartbeat):
    now = utcnow()
    for w in staleness_warnings(committed):
        log("WARNING: " + w)
    new = build_file(committed)
    state.update({"last_run": iso_z(now), "last_full_rebuild": iso_z(now)})
    if heartbeat:
        state["last_heartbeat_commit"] = iso_z(now)
    msg = ("macro-events: daily rebuild (heartbeat)" if heartbeat
           else "macro-events: full rebuild")
    sha = write_and_maybe_commit(new, state, msg, force=heartbeat)
    if sha:
        state["last_commit_sha"] = sha[:7]
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True,
                    choices=["window", "release", "refresh", "rebuild"])
    ap.add_argument("--payload")
    ap.add_argument("--heartbeat", action="store_true")
    args = ap.parse_args()

    committed = fetch_committed("latest.json")
    try:
        state = fetch_committed("state.json")
    except RuntimeError:
        state = {"schema": "macro-events-state.v1"}
    workdir = os.environ.get("MACRO_WORKDIR", "/tmp/macro-events")
    os.makedirs(workdir, exist_ok=True)

    if args.mode == "window":
        return cmd_window(committed)
    if args.mode == "release":
        if not args.payload:
            raise RuntimeError("--payload is required for release mode")
        return cmd_release(committed, state, args.payload, workdir)
    if args.mode == "refresh":
        return cmd_refresh(committed, state)
    if args.mode == "rebuild":
        return cmd_rebuild(committed, state, args.heartbeat)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"FATAL: {e}")
        sys.exit(1)
