#!/usr/bin/env python3
"""Find and qualify the publisher's preview image for an article page.

Usage:
    fetch_image.py <article-url> [--id <item-id>] [--rehost-dir <dir>]

Prints one JSON line on stdout:
    {"image_url": "<https url>"|null, "image_kind": "photo"|"none",
     "rehosted": bool, "detail": "<short note>"}

Pipeline:
  1. Fetch the article page, take og:image (fallback twitter:image),
     absolutized. No image -> image_kind "none".
  2. Qualify the candidate against orob's rules: absolute https, JPEG/PNG/
     WebP, downloadable with NO cookies and NO Referer, at most 2 MB, at
     least 800 px wide. Honest extension: the saved extension must match
     the real content type.
  3. If the publisher URL qualifies, send it as-is.
  4. Otherwise, if --id is given, download the bytes, resize to a 1280-px-
     wide JPEG (quality 82, ~16:9 crop when the source is wider than 16:9),
     save as <rehost-dir>/<id>.jpg and send its raw.githubusercontent.com
     URL. If the download itself is blocked or --id is absent, the result
     is image_kind "none" (caller sends image_url null).

Exit 0 always on a well-formed run (even when the answer is "none");
exit 1 only on usage errors. Stdlib + Pillow (for resize/dimensions).
"""
import html
import io
import json
import os
import re
import sys
import urllib.request
from urllib.parse import urljoin, urlparse

try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "Chrome/126.0 Safari/537.36")

PATS = [
    r'<meta\s[^>]*?(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\'][^>]*?content=["\']([^"\']+)["\']',
    r'<meta\s[^>]*?content=["\']([^"\']+)["\'][^>]*?(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\']',
]

MAX_BYTES = 2 * 1024 * 1024
MIN_WIDTH = 800
REHOST_WIDTH = 1280

CTYPE_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
EXT_TO_CTYPE = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png", ".webp": "image/webp"}

REHOST_OWNER = "XAUrum-Fintech"
REHOST_REPO = "daily-news"


def log(msg):
    print("fetch_image: %s" % msg, file=sys.stderr)


def fetch_page(url):
    """GET the article HTML with a browser UA, no cookies, no referrer."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        if "html" not in r.headers.get("Content-Type", ""):
            raise ValueError("not html: %s" % r.headers.get("Content-Type"))
        raw = r.read(1500000).decode("utf-8", "replace")
        return raw, r.url


def extract_og_image(raw, final_url):
    for pat in PATS:
        m = re.search(pat, raw, re.IGNORECASE)
        if m:
            img = html.unescape(m.group(1)).strip()
            if img.startswith("data:"):
                continue
            absu = urljoin(final_url, img)
            if urlparse(absu).scheme in ("http", "https"):
                return absu
    return None


def sniff_ctype(data):
    """Content type from magic bytes (some CDNs omit the header)."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return ""


def download_image(url, limit=MAX_BYTES + 65536):
    """GET image bytes with no cookies and no Referer. Returns (bytes, ctype).

    The server's Content-Type is used when present; otherwise (or when it
    disagrees) the type is sniffed from magic bytes. Only JPEG/PNG/WebP are
    accepted. Raises on any failure (blocked, too big, wrong type).
    """
    req = urllib.request.Request(url, headers={
        # No image/avif: orob only accepts JPEG/PNG/WebP, and we cannot
        # resize AVIF without extra plugins.
        "User-Agent": UA,
        "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        header_ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        length = r.headers.get("Content-Length")
        if length is not None and int(length) > MAX_BYTES:
            raise ValueError("content-length %s exceeds 2 MB" % length)
        buf = io.BytesIO()
        total = 0
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ValueError("image exceeds 2 MB")
            buf.write(chunk)
        data = buf.getvalue()
    sniffed = sniff_ctype(data)
    ctype = header_ct if header_ct in CTYPE_TO_EXT else sniffed
    if not header_ct:
        log("no content-type header, sniffed %r" % sniffed)
    elif header_ct != sniffed and sniffed:
        log("header said %r, bytes are %r; trusting bytes" % (header_ct, sniffed))
    if ctype not in CTYPE_TO_EXT:
        raise ValueError("unsupported content-type %r" % (header_ct or sniffed))
    return data, ctype


def honest_extension(url, ctype):
    """True if the URL's extension plausibly matches the real content type."""
    path = urlparse(url).path.lower()
    ext = os.path.splitext(path)[1]
    want = CTYPE_TO_EXT.get(ctype)
    if ext in ("", want):
        return True
    # .jpeg vs .jpg is fine
    if want == ".jpg" and ext == ".jpeg":
        return True
    return False


def qualifies(url):
    """Check a publisher image URL against orob's rules without re-hosting.

    Returns (ok, detail, width, ctype, data_or_None). Data is returned only
    when already downloaded.
    """
    if not url.startswith("https://"):
        return False, "not https", 0, "", None
    try:
        data, ctype = download_image(url)
    except Exception as e:
        return False, "download failed: %s" % e, 0, "", None
    if not honest_extension(url, ctype):
        return False, "extension does not match content-type %s" % ctype, 0, ctype, data
    if not HAVE_PIL:
        return False, "Pillow unavailable, cannot verify dimensions", 0, ctype, data
    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
    except Exception as e:
        return False, "unreadable image: %s" % e, 0, ctype, data
    if w < MIN_WIDTH:
        return False, "width %d < 800 px" % w, w, ctype, data
    return True, "ok (%dx%d %s)" % (w, h, ctype), w, ctype, data


def rehost(item_id, data, rehost_dir):
    """Resize to a 1280-px-wide JPEG and save as <rehost-dir>/<item_id>.jpg.

    Images narrower than 800 px are upscaled to 800 (LANCZOS) when they are
    at least 400 px wide; anything smaller is rejected (raise) so the caller
    falls back to image_kind "none". Returns the raw.githubusercontent.com
    URL. `rehost_dir` is repo-relative (e.g. "assets/news").
    """
    if not HAVE_PIL:
        raise RuntimeError("Pillow required for re-host resize")
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGB")
        w, h = im.size
        if w < 400:
            raise ValueError("image too small to re-host (%dx%d)" % (w, h))
        target_w = 800 if w < MIN_WIDTH else min(w, REHOST_WIDTH)
        # Crop to ~16:9 when the source is wider than 16:9, else keep ratio.
        if w / h > 16 / 9:
            crop_w = int(h * 16 / 9)
            left = (w - crop_w) // 2
            im = im.crop((left, 0, left + crop_w, h))
            w, h = im.size
        if w != target_w:
            im = im.resize((target_w, int(h * target_w / w)), Image.LANCZOS)
        os.makedirs(rehost_dir, exist_ok=True)
        path = os.path.join(rehost_dir, "%s.jpg" % item_id)
        im.save(path, "JPEG", quality=82, optimize=True)
    return ("https://raw.githubusercontent.com/%s/%s/main/%s/%s.jpg"
            % (REHOST_OWNER, REHOST_REPO,
               rehost_dir.strip("/"), item_id))


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__.strip().split("\n")[2], file=sys.stderr)
        return 1
    url = argv[1]
    item_id = None
    rehost_dir = "assets/news"
    i = 2
    while i < len(argv):
        if argv[i] == "--id" and i + 1 < len(argv):
            item_id = argv[i + 1]
            i += 2
        elif argv[i] == "--rehost-dir" and i + 1 < len(argv):
            rehost_dir = argv[i + 1]
            i += 2
        else:
            log("unknown arg %r" % argv[i])
            return 1

    if not HAVE_PIL:
        log("WARNING: Pillow not installed; dimensions cannot be verified")

    try:
        raw, final = fetch_page(url)
    except Exception as e:
        log("article fetch failed: %s" % e)
        print(json.dumps({"image_url": None, "image_kind": "none",
                          "rehosted": False,
                          "detail": "article fetch failed"}))
        return 0
    og = extract_og_image(raw, final)
    if not og:
        log("no og:image found")
        print(json.dumps({"image_url": None, "image_kind": "none",
                          "rehosted": False, "detail": "no og:image"}))
        return 0

    ok, detail, width, ctype, data = qualifies(og)
    log("candidate %s -> %s" % (og[:80], detail))
    if ok:
        print(json.dumps({"image_url": og, "image_kind": "photo",
                          "rehosted": False, "detail": detail}))
        return 0

    # Publisher URL unusable: re-host a resized copy when possible.
    if not item_id:
        log("no --id given, cannot re-host")
        print(json.dumps({"image_url": None, "image_kind": "none",
                          "rehosted": False, "detail": detail}))
        return 0
    if data is None:
        try:
            data, _ = download_image(og)
        except Exception as e:
            log("re-host download failed: %s" % e)
            print(json.dumps({"image_url": None, "image_kind": "none",
                              "rehosted": False,
                              "detail": "re-host download failed: %s" % e}))
            return 0
    try:
        hosted = rehost(item_id, data, rehost_dir)
    except Exception as e:
        log("re-host failed: %s" % e)
        print(json.dumps({"image_url": None, "image_kind": "none",
                          "rehosted": False,
                          "detail": "re-host failed: %s" % e}))
        return 0
    log("re-hosted -> %s" % hosted)
    print(json.dumps({"image_url": hosted, "image_kind": "photo",
                      "rehosted": True,
                      "detail": "re-hosted: " + detail}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
