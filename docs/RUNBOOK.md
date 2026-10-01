# Edition Runbook — XAUrum-Fintech/daily-news

Canonical spec for every edition (hourly 8am-6pm IST, 2-hourly at night). Cron workers: follow this file exactly. This copy lives in the repo, which is the pipeline's source of truth.
The user's original spec (2026-09-26) is authoritative; this file restates it.

## Repo & auth
- Repo: `XAUrum-Fintech/daily-news`, branch `main` (public).
- GitHub API tool: `~/workspace/skills/github/bin/gh-api`
  usage: `bin/gh-api METHOD /path [--data '{"key":"value"}']`
- Authenticated requests to `api.github.com` only. Never print, log, or persist credentials.

## Schedule
- The cron job fires every hour. Convert the current time to IST (UTC+5:30) and
  decide:
  - Daytime (08:00 ≤ IST < 18:00): publish an hourly edition. Slot = the UTC
    hour nearest to now; `window.from` = slot − 1h, `window.to` = slot.
  - Night (IST before 08:00 or at/after 18:00): publish only when the current
    UTC hour is even (the old 2-hourly cadence). On odd UTC hours do nothing:
    no edition, no commit, stay quiet. Slot = the even UTC hour nearest to
    now; `window.from` = slot − 2h, `window.to` = slot.
- `edition_id` = the slot, e.g. `2026-10-01T03:00Z`.
- `generated_at` = actual generation time, UTC ISO 8601 with Z.
- `window` is the publishing interval (1h daytime, 2h night); items up to 48h
  old are expected inside it.
- Slot rule: `edition_id` and `window.to` must NEVER be in the future
  (≤ `generated_at`). Take the slot hour nearest to now: if it lies in the
  future by ≤10 minutes, wait until it arrives, then generate; if it lies
  further in the future (ad-hoc/manual run), use the previous slot hour
  (floor) instead. Cron runs fire at their slot and wait out an early
  dispatch; manual runs rebuild the last completed slot.

## Sourcing (in this order)
1. `python3 src/fetch_feeds.py` — Google News RSS (India + global editions) plus
   direct publisher feeds (the script holds the current feed list; skip any feed
   that blocks server-side fetches).
2. Targeted web searches (news vertical) for what RSS misses — festival/demand
   stories and primary sources especially: MCX circulars, SEBI/RBI press releases,
   IBJA announcements, CME/COMEX notices. Festival queries stay specific:
   `Dhanteras gold`, `Diwali gold demand`, `Akshaya Tritiya gold sales`
   (never a generic "wedding season" query).
3. Open the key results to verify published date, author, and substance.
   Standing approval (2026-09-26): the user has pre-approved reading articles
   and curating/approving the selection for ALL pulls — no per-edition approval
   needed. When choosing between versions of a story, pick the most authoritative
   or detailed source.

## Canonical URLs & ids
- Resolve each RSS/search link to the publisher's ORIGINAL article URL
  (follow redirects, e.g. `curl -sIL -A <browser-UA> -o /dev/null -w '%{url_effective}'`;
  if that fails, search the article title to find the publisher page).
- Canonicalize: https only, strip tracking params (`utm_*`, `fbclid`, `gclid`, …).
- `id` = first 16 hex chars of SHA-256 of the canonical URL:
  `printf '%s' "$url" | sha256sum | cut -c1-16`.
- The same article keeps the same id across editions so repeats merge.

## Item fields (extended schema — still `orob-news.v1`)
Every item carries the base fields (id, rank, title, summary, url, source,
source_domain, published_at, image_url, tags) PLUS:
1. `category` (required) — exactly one, the story's MAIN driver:
   - `mcx`: Indian market moves — MCX gold/silver prices, domestic premiums and
     discounts, Indian trading and demand news;
   - `global`: international prices (COMEX, London), US dollar, central banks
     and the Fed, global demand, geopolitics;
   - `policy`: Indian government/regulator actions — import duty, GST, RBI,
     hallmarking rules, budget, SEBI;
   - `festive`: seasonal buying tied to Indian festivals and weddings —
     Dhanteras, Diwali, Akshaya Tritiya, wedding season.
2. `metals` (required) — `["gold"]`, `["silver"]`, or `["gold", "silver"]`.
3. `source_domain` — normalized per `data/publishers.json`
   (no `www.`, always the same string per publisher; the app uses it for logos).
4. `source` — display name exactly as in data/publishers.json for that domain
   (e.g. always "Economic Times", never "ET"). Add missing publishers to
   data/publishers.json in the same format (`{"name": ..., "publisher_id": ...}`);
   never invent variants.
5. `publisher_id` (required) — stable slug per publisher from data/publishers.json
   (e.g. `economic-times`, `cnbc-tv18`). When adding a publisher not in orob's
   table, coin a slug in the same style (lowercase, hyphens) and record it in
   the edition report so the app team can add the logo mapping.
6. `image_url` / `image_kind` (required) — see Content rule 6. When there is no
   real photo: `"image_url": null, "image_kind": "none"`.
7. `breaking` (optional, default `false`) — `true` only for rare, genuinely
   market-moving news (import-duty change, Fed surprise, big MCX limit move).
   At most a couple per day: check `~/workspace/gold-silver-digest/breaking-log.jsonl` (machine-local,
   never committed) for entries in the last 7 days; if 2 already, do not set `true`. When set,
   append `{"date":"<YYYY-MM-DD>","id":"<item id>","title":"<title>"}` to the log.
   If the log is missing/unreadable, default `false`.
8. `related_urls` (optional) — story-level dedup: when several outlets cover the
   same story, include it ONCE from the most authoritative/detailed source and
   put the other versions' canonical URLs here. Each item in `items` must be a
   different story.

## Content rules
1. Items: 10–30 per edition (aim for 20–30). Rank by IMPACT for Indian
   gold/silver buyers, not recency or feed position — rank 1 is the most
   important market development of the period. Every rank unique, sequential 1..N.
   Never pad with low-quality items to reach the minimum — report short instead.
2. Freshness: only articles from the last 48h. Each article appears once per edition.
3. Summaries in your own words. Never copy article text beyond the title.
   Never reproduce paywalled content.
4. No investment advice: no buy/sell/hold, no price targets, no recommendations.
   Factual wording only, e.g. "Gold rose 0.8% on MCX after…". Applies to insights
   and trending too.
5. Links: https only, publisher's original (never an aggregator), tracking stripped.
6. Images — the app copies each photo to its own storage, so every real photo
   must be fetchable and big enough:
   - For EVERY item, run
     `python3 src/fetch_image.py <article-url> --id <item-id>`
     and use the JSON it prints: `image_url` + `image_kind`.
   - A real photo (`image_kind: "photo"`): the article's own lead image
     (`og:image`, fallback `twitter:image`) — absolute https, JPEG/PNG/WebP,
     at least 800 px wide, at most 2 MB, downloadable with NO cookies and NO
     referrer, and the file's real type matching its extension. The script
     checks all of this.
   - Blocked, too big, too small, or dishonest-typed images: the script
     downloads the bytes, resizes to a 1280-px-wide JPEG and re-hosts it as
     `assets/news/<item-id>.jpg`, and prints the
     `https://raw.githubusercontent.com/XAUrum-Fintech/daily-news/main/assets/news/<item-id>.jpg`
     URL with `"rehosted": true`. Commit re-hosted files BEFORE the edition
     (one Contents-API commit for all of them; see Commit section) so the
     URLs resolve when the edition lands.
   - No real photo at all: send `"image_url": null, "image_kind": "none"`.
     The app then shows the publisher's cover or its own topic artwork.
   - NEVER send the repo's old generic placeholders
     (`assets/placeholder-gold.jpg` etc.) as `image_url` — the app treats
     them as real photos. They remain in the repo only for history.
   - Never generated-per-article or stock images.
   - Rank 1 is the edition's top story; it should have a real photo
     (`image_kind: "photo"`) whenever one exists.
7. Tags: only from `gold, silver, mcx, comex, rupee, rbi, fed, import-duty, india,
   global, jewellery, central-banks` (the app's known set; "More / Less like
   this" and topic artwork use them). Drop stories outside the app's topics:
   gold, silver, bullion, customs duty and policy, rupee/forex, MCX/COMEX,
   central banks and the Fed as they move bullion, jewellery demand.
8. Language: plain English; rupee amounts as ₹, lakh/crore where natural. UTF-8.
9. `published_at`: the ARTICLE's own publication time (UTC, `Z` suffix) —
   never the crawl time. The app groups Today / Yesterday / Earlier and orders
   newest-first on this field. When the page shows no time, estimate from the
   feed/RSS entry and prefer the earliest defensible value over the crawl time.
10. `rank`: rank 1 is the TOP STORY of the edition (the single most important
    story, shown at the head of the feed). Everything else is shown by
    `published_at` newest-first; ranks after 1 are only a tie-breaker.

## Selection rules (feedback on edition 2026-09-27T06:00Z, applied 2026-09-27)
9. EXCLUDE, always: "gold rate today" / city price-table / rate-listing articles.
   Exclude other countries' domestic-market stories (e.g. Malaysian gold futures).
   Include global stories only when they explain what moved COMEX or London prices.
10. Source tiers — prefer originals: Economic Times, Mint, The Hindu BusinessLine,
    Moneycontrol, Business Standard, CNBC-TV18, Reuters, Bloomberg, Kitco,
    World Gold Council, MCX, RBI, IBJA. Demote SEO/aggregator/republisher sites.
    When a story is republished, use the ORIGINAL as the item source and put the
    republisher's canonical URL in `related_urls` (e.g. a Gold-Eagle survey that
    originates from Kitco → source is Kitco).
11. Category by main driver: `festive` ONLY for festival/wedding buying stories
    (Dhanteras, Diwali, Akshaya Tritiya, wedding-season demand) — never for price
    articles. `policy` ONLY for actual government/regulator decisions (import duty,
    GST, hallmarking, RBI/SEBI rules). RBI weekly reserve data is market data →
    `mcx` (Indian angle) or `global`.
12. Balance: at least 2–3 `mcx` items; at least one silver-focused story when one
    exists in the news; no single category more than half the items.
13. Forecasts: always attributed to their source ("ICICI Bank expects…"). Never
    state a forecast in the agent's own voice. Never buy/sell/hold wording, never
    price targets.
14. Insights consistency: every claim and number in headline/summary/points must
    match an item in the edition. Re-read the items before writing insights.
15. Source cap: at most 2 items per `source_domain` per edition — keep the best 2.
16. One item per story: when several candidates report the same story (e.g. the
    weekly gold decline on Fed/yield pressure), keep the single best version and
    move the others' canonical URLs into its `related_urls`. Use the freed slots
    for DIFFERENT stories: silver, festive demand, policy, domestic premiums.
    Themes spanning several different stories belong in `trending`, not as
    duplicate items.
17. Silver: include at least one silver-led story (silver the main subject, not a
    passing mention) whenever one exists in the window.
18. Category by main subject: e.g. a bank forecast quoted in $/oz for global gold
    is `global`, even when the bank is Indian.

## Assembling the edition
1. Fetch the current `news/latest.json`:
   `bin/gh-api GET /repos/XAUrum-Fintech/daily-news/contents/news/latest.json`
   (base64-decode `content`; 404 = first edition, no previous file).
2. Carry over still-relevant items < 48h old from the previous edition by id
   (merge repeats — same id, keep one).
3. Pool new and carried-over items; rank everything by IMPACT for Indian buyers
   (not recency). Ranks 1..N unique.
4. Write `insights`: headline (≤120 chars, one sentence on what is moving gold/silver),
   summary (2–4 sentences, ≤600 chars), points (≤5, each ≤160 chars). Factual, no advice.
5. Write `trending`: 3–5 hand-curated themes — your read of what the edition's news
   adds up to (LinkedIn-style). Each entry: `title` (≤80 chars), `summary` (≤200 chars,
   interpretive but factual — every claim traces to a linked item; no forecasts in
   your own voice, no buy/sell/hold), `item_ids` (1+ edition item ids backing the
   theme). Order by importance.

## Validation (BEFORE any write — abort and leave repo untouched on failure)
With python3: JSON parses; top-level keys exactly
`{schema, edition_id, generated_at, window, insights, trending, items}`;
`schema == "orob-news.v1"`; `edition_id` matches slot; `window` 2h apart, ISO Z;
headline ≤120, summary ≤600, ≤5 points each ≤160; 10 ≤ items ≤ 30;
`trending` has 3–5 entries, each with title ≤80, summary ≤200, and 1+ `item_ids`
that all reference real item ids;
at least 2 items with category `mcx`; no category more than half the items;
at most 2 items per source_domain; every item has `publisher_id` (slug from
data/publishers.json); every item's `image_url` is either null (with
`image_kind: "none"`) or an https URL (with `image_kind: "photo"`) — never a
repo placeholder URL; warn (exit 0) when rank-1's `image_kind` is not "photo"
(the top story should have a real photo);
ranks unique 1..N, rank 1 the edition's top story; every item has all base fields
(id, rank, title, summary, url, source, source_domain, publisher_id, published_at,
image_url, image_kind, tags)
plus `category` ∈ {mcx, global, policy, festive}, `metals` non-empty ⊆ {gold, silver},
`breaking` boolean (default false), `related_urls` absent or a list of https URLs;
title ≤160, summary ≤400 (summary always present — the server requires it);
url https with no tracking params;
`published_at` is the article's own time, within 48h of `generated_at`;
`id == sha256(canonical_url)[:16]`;
tags ⊆ the app's known set only
(gold, silver, rupee, fed, rbi, mcx, comex, import-duty, central-banks, india,
global, jewellery).

## Change detection
- Compare new items (id + title + summary + url) against the previous edition.
- Identical → SKIP the commit entirely. Do not commit.

## Commit (only when changed AND valid)
- If the edition re-hosts images (`assets/news/<id>.jpg` files were written by
  fetch_image.py): commit them FIRST with one Contents-API commit
  (`message: "news assets: re-hosted images for <edition_id>"`) so the
  re-hosted URLs resolve before the edition references them.
- `PUT /repos/XAUrum-Fintech/daily-news/contents/news/latest.json`
  data: `{"message":"news: <generated_at>","content":"<base64 of UTF-8 JSON>","branch":"main","sha":"<current sha>"}`
  (omit `sha` on the very first edition).
- Include `news/latest.md` (human-readable mirror: headline, insights, trending,
  numbered stories with links and summaries) in the same commit via a second PUT
  with the same message.
- Never commit images outside `assets/news/` or any other files. NEVER rewrite
  history (no force-push; the Contents API never rewrites).
- Commit only when `latest.json` actually changed. (The first edition's double
  commit was a one-off empty-repo bootstrap; `src/publish.py` change-detects and
  commits at most once per edition.)
- On 409 (sha changed underneath you): re-fetch, re-compare, retry once.
- Verify after PUT: GET the file, confirm content sha and commit message.
- If the run fails at any point: leave the previous `latest.json` untouched.

## news/latest.md format
```markdown
# Gold & Silver for Indian buyers · <edition_id>

> <insights.headline>

<insights.summary>

- <point>
- …

## Trending
- **<theme title>**: <summary> (stories <n>, <m>)
- …

## Stories
1. [title](url) — *source*, <published_at>
   <summary>
...
```
