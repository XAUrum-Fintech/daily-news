#!/usr/bin/env python3
"""Daily sync for the customs-duty tracker.

Pipeline:
  1. Shallow-clone XAUrum-Fintech/daily-news (branch main) to a temp dir.
  2. Run fetch.py (official sources: ICEGATE, CBIC, LBMA) for new events.
  3. Backfill missing source_url/source_kind on existing rows
     (tools/backfill_source_urls.py; harmless when nothing is missing).
  4. Append each new event via tools/build_customs_tracker.py (validates).
  4b. Backfill again so just-appended rows get their source links the
     same day instead of waiting for the next run.
  5. Render tools/render_update.py -> latest.md (values + commentary).
  6. Commit latest.json + latest.csv + state.json + latest.md in ONE atomic commit via
     the GitHub git-database API (gh-api), and verify the ref moved.

Never touches news/ or any feed file. No new events -> quiet (a
backfill-only commit of source links may still happen, silently).
Any failure -> the remote repo is left untouched; the error is printed
plainly and the exit code is nonzero.

Usage: sync.py [--workdir DIR] [--repo URL] [--branch BRANCH]
"""

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile

GH_API = os.path.expanduser("~/workspace/skills/github/bin/gh-api")
REPO = "XAUrum-Fintech/daily-news"
TRACKER_SUBDIR = "customs-duty-tracker"
TRACKER_FILES = ["latest.json", "latest.csv", "state.json", "latest.md"]


def log(msg):
    print(f"sync: {msg}", file=sys.stderr)


def run(cmd, **kw):
    log("$ " + " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])} failed: {r.stderr[:500]}")
    return r


def gh(method, path, data=None):
    cmd = [GH_API, method, path]
    if data is not None:
        cmd += ["--data", json.dumps(data)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh-api {method} {path} failed: "
                           f"{r.stderr[:500]}")
    return json.loads(r.stdout) if r.stdout.strip() else {}


def _commit(args, tracker_dir, msg):
    """Atomic commit of the tracker files via the git-database API."""
    ref = gh("GET", f"/repos/{args.repo}/git/refs/heads/{args.branch}")
    base_sha = ref["object"]["sha"]
    commit = gh("GET", f"/repos/{args.repo}/git/commits/{base_sha}")
    base_tree = commit["tree"]["sha"]

    blobs = []
    for name in TRACKER_FILES:
        with open(os.path.join(tracker_dir, name), "rb") as f:
            content = base64.b64encode(f.read()).decode()
        b = gh("POST", f"/repos/{args.repo}/git/blobs",
               {"content": content, "encoding": "base64"})
        blobs.append({"path": f"{TRACKER_SUBDIR}/{name}",
                      "mode": "100644", "type": "blob",
                      "sha": b["sha"]})
    tree = gh("POST", f"/repos/{args.repo}/git/trees",
              {"base_tree": base_tree, "tree": blobs})
    new_commit = gh(
        "POST", f"/repos/{args.repo}/git/commits",
        {"message": msg, "tree": tree["sha"], "parents": [base_sha]})
    try:
        gh("PATCH", f"/repos/{args.repo}/git/refs/heads/{args.branch}",
           {"sha": new_commit["sha"]})
    except RuntimeError:
        # 409 race: rebase onto the new head once, then give up loudly
        log("ref update conflict; retrying once on fresh head")
        ref = gh("GET",
                 f"/repos/{args.repo}/git/refs/heads/{args.branch}")
        base_sha = ref["object"]["sha"]
        commit = gh("GET", f"/repos/{args.repo}/git/commits/{base_sha}")
        tree = gh("POST", f"/repos/{args.repo}/git/trees",
                  {"base_tree": commit["tree"]["sha"], "tree": blobs})
        new_commit = gh("POST", f"/repos/{args.repo}/git/commits",
                        {"message": msg, "tree": tree["sha"],
                         "parents": [base_sha]})
        gh("PATCH", f"/repos/{args.repo}/git/refs/heads/{args.branch}",
           {"sha": new_commit["sha"]})

    # verify
    ref = gh("GET", f"/repos/{args.repo}/git/refs/heads/{args.branch}")
    if ref["object"]["sha"] != new_commit["sha"]:
        raise RuntimeError("ref verification failed after commit")
    print(f"committed {new_commit['sha'][:7]}: {msg}")
    return new_commit["sha"]


def _tracker_dirty(clone):
    r = subprocess.run(
        ["git", "-C", clone, "status", "--porcelain", "--", TRACKER_SUBDIR],
        capture_output=True, text=True)
    return bool(r.stdout.strip())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--branch", default="main")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    fetch_py = os.path.join(here, "fetch.py")
    build_py = os.path.join(here, "build_customs_tracker.py")
    backfill_py = os.path.join(here, "backfill_source_urls.py")

    workdir = args.workdir or tempfile.mkdtemp(prefix="cdt-sync-")
    clone = os.path.join(workdir, "daily-news")
    if os.path.isdir(clone):
        shutil.rmtree(clone)
    try:
        run(["git", "clone", "--depth", "1", "--branch", args.branch,
             f"https://github.com/{args.repo}.git", clone])
        tracker_dir = os.path.join(clone, TRACKER_SUBDIR)

        # 1. detect
        r = subprocess.run(
            [sys.executable, fetch_py, "--tracker-dir", tracker_dir],
            capture_output=True, text=True, timeout=900)
        sys.stderr.write(r.stderr)
        if r.returncode != 0:
            raise RuntimeError("fetch.py failed:\n" + r.stderr[-2000:])
        detected = json.loads(r.stdout)
        events = detected.get("events", [])
        for w in detected.get("warnings", []):
            log("warning: " + w)
        # 3. backfill source links on existing rows: official CBIC PDF
        # links first, caalley.com mirror fallback when CBIC is
        # unreachable (official links replace mirror links whenever CBIC
        # becomes reachable). Runs even with no new events so links keep
        # filling in.
        run([sys.executable, backfill_py, "--tracker-dir", tracker_dir])

        if not events:
            # Quiet day: commit backfill-only changes (if any) without
            # any chat report; truly nothing changed -> stay silent.
            if _tracker_dirty(clone):
                _commit(args, tracker_dir,
                        "customs-duty-tracker: backfill source links")
                print("backfilled source links (no new events)")
            else:
                print("no new events")
            return 0

        # 4. append (builder validates every row; aborts on failure)
        for ev in events:
            cmd = [sys.executable, build_py, "append",
                   "--repo-dir", clone,
                   "--published", ev["published"],
                   "--effective", ev["effective"],
                   "--event", ev["event"]]
            if ev.get("source_url"):
                cmd += ["--source-url", ev["source_url"],
                        "--source-kind", ev.get("source_kind") or "notice"]
            if ev["event"] == "tariff_value":
                cmd += ["--tariff-notification", ev["tariff_notification"],
                        "--gold-tariff", str(ev["gold_tariff"]),
                        "--silver-tariff", str(ev["silver_tariff"]),
                        "--gold-fix", str(ev["gold_fix"]),
                        "--silver-fix", str(ev["silver_fix"]),
                        "--fix-date", ev["fix_date"]]
            else:
                cmd += ["--eram-notification", ev["eram_notification"],
                        "--usd-inr-import", str(ev["usd_inr_import"]),
                        "--usd-inr-export", str(ev["usd_inr_export"])]
            run(cmd)

        # 4b. backfill again so rows appended just now get their source
        # links the same day (official CBIC PDF first, caalley mirror
        # fallback) instead of waiting for tomorrow's run.
        run([sys.executable, backfill_py, "--tracker-dir", tracker_dir])

        # 5. render the human-readable update for the newest row
        render_py = os.path.join(here, "render_update.py")
        run([sys.executable, render_py, "--tracker-dir", tracker_dir])

        # 6. atomic commit of the four regenerated files
        first, last = events[0], events[-1]
        msg = (f"customs-duty-tracker: {len(events)} new event(s) "
               f"{first['published']}..{last['published']}")
        _commit(args, tracker_dir, msg)
        return 0
    finally:
        if not args.workdir:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"sync: FAILED: {e}", file=sys.stderr)
        sys.exit(1)
