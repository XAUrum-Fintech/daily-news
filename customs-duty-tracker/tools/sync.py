#!/usr/bin/env python3
"""Daily sync for the customs-duty tracker.

Pipeline:
  1. Shallow-clone XAUrum-Fintech/daily-news (branch main) to a temp dir.
  2. Run fetch.py (official sources: ICEGATE, CBIC, LBMA) for new events.
  3. Append each new event via tools/build_customs_tracker.py (validates).
  4. Commit latest.json + latest.csv + state.json in ONE atomic commit via
     the GitHub git-database API (gh-api), and verify the ref moved.

Never touches news/ or any feed file. No new events -> no commit, quiet.
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
TRACKER_FILES = ["latest.json", "latest.csv", "state.json"]


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--branch", default="main")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    fetch_py = os.path.join(here, "fetch.py")
    build_py = os.path.join(here, "build_customs_tracker.py")

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
        if not events:
            print("no new events")
            return 0

        # 2. append (builder validates every row; aborts on failure)
        for ev in events:
            cmd = [sys.executable, build_py, "append",
                   "--repo-dir", clone,
                   "--published", ev["published"],
                   "--effective", ev["effective"],
                   "--event", ev["event"]]
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

        # 3. atomic commit of the three regenerated files
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
        first, last = events[0], events[-1]
        msg = (f"customs-duty-tracker: {len(events)} new event(s) "
               f"{first['published']}..{last['published']}")
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
