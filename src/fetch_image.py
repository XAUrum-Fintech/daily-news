#!/usr/bin/env python3
"""Print the publisher's preview image URL for an article page.

Usage: fetch_image.py <article-url>
Fetches the page (browser UA, 15s timeout) and prints the absolute URL from
og:image (fallback: twitter:image). Prints nothing, exit 1, when no suitable
image is found.
"""
import html, re, sys, urllib.request
from urllib.parse import urljoin, urlparse

PATS = [
    r'<meta\s[^>]*?(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\'][^>]*?content=["\']([^"\']+)["\']',
    r'<meta\s[^>]*?content=["\']([^"\']+)["\'][^>]*?(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\']',
]

def main():
    if len(sys.argv) != 2:
        print("usage: fetch_image.py <article-url>", file=sys.stderr)
        return 2
    req = urllib.request.Request(sys.argv[1], headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
        "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            if "html" not in r.headers.get("Content-Type", ""):
                return 1
            raw = r.read(1500000).decode("utf-8", "replace")
            final = r.url
    except Exception as e:
        print(f"warn: fetch failed: {e}", file=sys.stderr)
        return 1
    for pat in PATS:
        m = re.search(pat, raw, re.IGNORECASE)
        if m:
            img = html.unescape(m.group(1)).strip()
            if img.startswith("data:"):
                continue
            absu = urljoin(final, img)
            if urlparse(absu).scheme in ("http", "https"):
                print(absu)
                return 0
    return 1

if __name__ == "__main__":
    sys.exit(main())
