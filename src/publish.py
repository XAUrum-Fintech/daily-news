#!/usr/bin/env python3
"""Publish one edition: change-detect, validate, then PUT news/latest.json + latest.md.

Usage: publish.py <latest.json> <latest.md>

Flow:
  1. GET current news/latest.json (tolerate 404 = first publish).
  2. Compare new vs old items on (id, title, summary, url) — identical means
     "SKIP: no changes", exit 0, repo untouched.
  3. Validate via src/validate_edition.py — abort on failure, repo untouched.
  4. PUT news/latest.json and news/latest.md with message "news: <generated_at>"
     (include sha when updating; omit on first create). On 409, re-fetch and
     retry once.
  5. Final GET to verify; print the commit SHA.

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


def api(method, path, data=None):
    cmd = [GH_API_BIN, method, path]
    if data is not None:
        cmd += ["--data", json.dumps(data, ensure_ascii=False)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    body = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        if "404" in body or "Not Found" in body:
            raise NotFound(path)
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


def put_file(repo_path, data_bytes, message, sha):
    payload = {
        "message": message,
        "content": base64.b64encode(data_bytes).decode("ascii"),
        "branch": BRANCH,
    }
    if sha:
        payload["sha"] = sha
    try:
        return api("PUT", "/repos/%s/contents/%s" % (REPO, repo_path), payload)
    except ApiError as exc:
        if "409" in str(exc):
            raise
        raise


def put_with_retry(repo_path, data_bytes, message, sha):
    try:
        return put_file(repo_path, data_bytes, message, sha)
    except ApiError as exc:
        if "409" not in str(exc):
            raise
        print("WARN: 409 conflict on %s, re-fetching and retrying once" % repo_path,
              file=sys.stderr)
        new_sha, _ = get_file_sha_and_content(repo_path)
        return put_file(repo_path, data_bytes, message, new_sha)


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
        old_sha, old_bytes = get_file_sha_and_content(JSON_PATH)
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

    # 4. PUT both files.
    try:
        md_sha, _ = get_file_sha_and_content(MD_PATH)
        r_json = put_with_retry(JSON_PATH, new_json_bytes, message, old_sha)
        r_md = put_with_retry(MD_PATH, new_md_bytes, message, md_sha)
    except ApiError as exc:
        print("ERROR: publish failed: %s" % exc, file=sys.stderr)
        return 1

    # 5. Verify.
    try:
        _, verify_bytes = get_file_sha_and_content(JSON_PATH)
    except ApiError as exc:
        print("ERROR: verification GET failed: %s" % exc, file=sys.stderr)
        return 1
    if verify_bytes != new_json_bytes:
        print("ERROR: verification mismatch after PUT", file=sys.stderr)
        return 1

    commit_sha = (r_json.get("commit") or {}).get("sha", "?")
    print("Published commit %s" % commit_sha)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
