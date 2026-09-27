#!/usr/bin/env python3
"""Publish one edition atomically: change-detect, validate, then commit
news/latest.json + news/latest.md in ONE commit via the GitHub
git-database API (blobs -> tree -> commit -> ref update).

Usage: publish.py <latest.json> <latest.md>

Flow:
  1. Read local edition files, parse JSON.
  2. GET current news/latest.json from the repo (404 = first publish).
  3. Compare new vs old items on (id, title, summary, url) — identical means
     "SKIP: no changes", exit 0, repo untouched.
  4. Validate via src/validate_edition.py — abort on failure, repo untouched.
  5. Create one blob per file, one tree on top of main's current tree, one
     commit with main's current tip as parent, then PATCH refs/heads/main to
     the new commit. On ref conflict (422), re-fetch the tip and retry once
     (new tree/commit on the new base). Either both files land in one commit
     or nothing lands at all.
  6. Final GET of both files to verify contents; print the commit SHA.

Never commits any other files. Never rewrites history. Stdlib only.

Env: GH_API_BIN overrides the GitHub API helper
     (default ~/workspace/skills/github/bin/gh-api).
"""

import base64
import json
import os
import subprocess
import sys

REPO = "XAUrum-Fintech/daily-news"
BRANCH = "main"
JSON_PATH = "news/latest.json"
MD_PATH = "news/latest.md"

GH_API_BIN = os.environ.get(
    "GH_API_BIN",
    os.path.expanduser("~/workspace/skills/github/bin/gh-api"),
)


class ApiError(Exception):
    pass


class NotFound(ApiError):
    pass


class RefConflict(ApiError):
    pass


def api(method, path, data=None):
    cmd = [GH_API_BIN, method, path]
    if data is not None:
        cmd += ["--data", json.dumps(data, ensure_ascii=False)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    body = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        if "404" in body or "Not Found" in body:
            raise NotFound(path)
        if "422" in body or "Update is not a fast forward" in body:
            raise RefConflict("%s %s: %s" % (method, path, body[:300]))
        raise ApiError("%s %s failed: %s" % (method, path, body[:600]))
    try:
        return json.loads(p.stdout) if p.stdout.strip() else None
    except json.JSONDecodeError:
        raise ApiError("%s %s returned non-JSON: %s" % (method, path, body[:300]))


def get_file_sha_and_content(repo_path):
    """Return (sha, bytes) or (None, None) on 404."""
    try:
        d = api("GET", "/repos/%s/contents/%s" % (REPO, repo_path))
    except NotFound:
        return None, None
    content = base64.b64decode(d["content"])
    return d["sha"], content


def create_blob(data_bytes):
    d = api("POST", "/repos/%s/git/blobs" % REPO, {
        "content": base64.b64encode(data_bytes).decode("ascii"),
        "encoding": "base64",
    })
    return d["sha"]


def get_ref_tip():
    d = api("GET", "/repos/%s/git/ref/heads/%s" % (REPO, BRANCH))
    return d["object"]["sha"]


def get_commit_tree_sha(commit_sha):
    d = api("GET", "/repos/%s/git/commits/%s" % (REPO, commit_sha))
    return d["tree"]["sha"]


def create_tree(base_tree_sha, entries):
    d = api("POST", "/repos/%s/git/trees" % REPO, {
        "base_tree": base_tree_sha,
        "tree": entries,
    })
    return d["sha"]


def create_commit(message, tree_sha, parent_sha):
    d = api("POST", "/repos/%s/git/commits" % REPO, {
        "message": message,
        "tree": tree_sha,
        "parents": [parent_sha],
    })
    return d["sha"]


def update_ref(commit_sha):
    # Plural /git/refs/... path works for PATCH; singular /git/ref/... fails.
    api("PATCH", "/repos/%s/git/refs/heads/%s" % (REPO, BRANCH),
        {"sha": commit_sha})


def publish_atomically(json_bytes, md_bytes, message):
    """Create blobs/tree/commit and advance main once. Returns commit sha.
    Retries once on ref conflict; raises ApiError otherwise."""
    for attempt in (1, 2):
        try:
            base_commit = get_ref_tip()
            base_tree = get_commit_tree_sha(base_commit)
            json_blob = create_blob(json_bytes)
            md_blob = create_blob(md_bytes)
            tree_sha = create_tree(base_tree, [
                {"path": JSON_PATH, "mode": "100644", "type": "blob",
                 "sha": json_blob},
                {"path": MD_PATH, "mode": "100644", "type": "blob",
                 "sha": md_blob},
            ])
            commit_sha = create_commit(message, tree_sha, base_commit)
            update_ref(commit_sha)
            return commit_sha
        except RefConflict:
            if attempt == 2:
                raise
            print("WARN: ref conflict on %s, re-fetching tip and retrying once"
                  % BRANCH, file=sys.stderr)


def item_key(it):
    return (it.get("id"), it.get("title"), it.get("summary"), it.get("url"))


def main(argv):
    if len(argv) != 3:
        print("usage: publish.py <latest.json> <latest.md>", file=sys.stderr)
        return 2
    json_path, md_path = argv[1], argv[2]

    try:
        with open(json_path, "rb") as f:
            new_json_bytes = f.read()
        with open(md_path, "rb") as f:
            new_md_bytes = f.read()
        new_doc = json.loads(new_json_bytes.decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print("ERROR: cannot load edition files: %s" % exc, file=sys.stderr)
        return 1

    generated_at = new_doc.get("generated_at", "")
    message = "news: %s" % generated_at

    # 1. Fetch current edition (404 = first publish).
    try:
        _, old_bytes = get_file_sha_and_content(JSON_PATH)
    except ApiError as exc:
        print("ERROR: failed to read current %s: %s" % (JSON_PATH, exc), file=sys.stderr)
        return 1

    # 2. Change detection.
    if old_bytes is not None:
        try:
            old_doc = json.loads(old_bytes.decode("utf-8"))
            old_items = [item_key(it) for it in old_doc.get("items", [])]
            new_items = [item_key(it) for it in new_doc.get("items", [])]
        except (json.JSONDecodeError, AttributeError) as exc:
            print("ERROR: current latest.json unparsable: %s" % exc, file=sys.stderr)
            return 1
        if old_items == new_items:
            print("SKIP: no changes")
            return 0

    # 3. Validate before any write.
    here = os.path.dirname(os.path.abspath(__file__))
    validator = os.path.join(here, "validate_edition.py")
    p = subprocess.run([sys.executable, validator, json_path],
                       capture_output=True, text=True)
    if p.returncode != 0:
        print("ERROR: validation failed, repo untouched:", file=sys.stderr)
        print(p.stdout, file=sys.stderr)
        print(p.stderr, file=sys.stderr)
        return 1

    # 4. Atomic single-commit publish.
    try:
        commit_sha = publish_atomically(new_json_bytes, new_md_bytes, message)
    except ApiError as exc:
        print("ERROR: publish failed, repo untouched: %s" % exc, file=sys.stderr)
        return 1

    # 5. Verify both files landed in the new commit.
    try:
        _, verify_json = get_file_sha_and_content(JSON_PATH)
        _, verify_md = get_file_sha_and_content(MD_PATH)
    except ApiError as exc:
        print("ERROR: verification GET failed: %s" % exc, file=sys.stderr)
        return 1
    if verify_json != new_json_bytes:
        print("ERROR: verification mismatch on %s" % JSON_PATH, file=sys.stderr)
        return 1
    if verify_md != new_md_bytes:
        print("ERROR: verification mismatch on %s" % MD_PATH, file=sys.stderr)
        return 1

    print("Published commit %s" % commit_sha)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
