# Edition Runbook — XAUrum-Fintech/daily-news

Canonical spec for every 2-hourly edition. Cron workers: follow this file exactly.
The user's original spec (2026-09-26) is authoritative; this file restates it.

**This repo is the source of truth for the pipeline.** The scripts, data files,
and this runbook live here under version control (`src/`, `data/`, `docs/`) so
the pipeline can be iterated on. The working copies under
`~/workspace/gold-silver-digest/` are scratch, not canonical.

## Repo & auth
- Repo: `XAUrum-Fintech/daily-news`, branch `main` (public).
- GitHub API tool: `~/workspace/skills/github/bin/gh-api`
  usage: `bin/gh-api METHOD /path [--data '{"key":"value"}']`
- Authenticated requests to `api.github.com` only. Never print, log, or persist credentials.

## Schedule
- Every 2 hours at minute 0 UTC: 00, 02, 04, …, 22. (Cron: `0 */2 * * *`, timezone UTC.)
- `edition_id` = the slot, e.g. `2026-09-27T08:00Z`.
- `generated_at` = actual generation time, UTC ISO 8601 with Z.
- `window.from` = slot − 2h, `window.to` = slot (e.g. slot 08:00Z → 06:00–08:00Z).

## Sourcing (in this order)
1. Google News RSS — India edition (implemented by `src/fetch_feeds.py`):
   `https://news.google.com/rss/search?q=<query>&hl=en-IN&gl=IN&ceid=IN:en`
   Queries: `MCX gold silver` · `gold import duty India` · `IBJA gold` ·
   `RBI gold reserves` · `gold price India` · `silver price India`
2. Google News RSS — global edition (also `src/fetch_feeds.py`):
   `https://news.google.com/rss/search?q=<query>&hl=en-US&gl=US&ceid=US:en`
   Queries: `gold price` · `silver price` · `COMEX gold futures` ·
   `Federal Reserve gold` · `central bank gold buying` · `gold ETF flows`
3. Targeted web searches (news vertical) for what RSS misses — primary sources
   especially: MCX circulars, SEBI/RBI press releases, IBJA announcements,
   CME/COMEX notices. (Agent step.)
4. Open the key results to verify published date, author, and substance. (Agent step.)
   Standing approval (2026-09-26): the user has pre-approved reading articles
   and curating/approving the selection for ALL pulls — no per-edition approval
   needed. When choosing between versions of a story, pick the most authoritative
   or detailed source.

## Canonical URLs & ids
- Google News `rss/articles/...` links do NOT resolve to publishers via curl.
  Resolve each link to the publisher's ORIGINAL article URL by searching the
  exact article title. (Agent step.)
- Canonicalize: https only, strip tracking params (`utm_*`, `fbclid`, `gclid`, …).
- `id` = first 16 hex chars of SHA-256 of the canonical URL:
  `printf '%s' "$url" | sha256sum | cut -c1-16`.
- The same article keeps the same id across editions so repeats merge.

## Item fields (extended schema — still `orob-news.v1`)
Every item carries the base fields (id, rank, title, summary, url, source,
source_domain, published_at, image_url, tags) PLUS:
1. `category` (required) — exactly one, the story's MAIN driver. Definitions in
   `data/taxonomy.json`:
   - `mcx`: Indian market moves — MCX gold/silver prices, domestic premiums and
     discounts, Indian trading and demand news;
   - `global`: international prices (COMEX, London), US dollar, central banks
     and the Fed, global demand, geopolitics;
   - `policy`: Indian government/regulator actions — import duty, GST, RBI,
     hallmarking rules, budget, SEBI;
   - `festive`: seasonal buying tied to Indian festivals and weddings —
     Dhanteras, Diwali, Akshaya Tritiya, wedding season.
2. `metals` (required) — `["gold"]`, `["silver"]`, or `["gold", "silver"]`.
3. `source_domain` — normalized per `data/publishers.json` (no `www.`, always the
   same string per publisher; the app uses it for logos).
4. `source` — display name exactly as in `data/publishers.json` for that domain
   (e.g. always "Economic Times", never "ET"). When adding a publisher, update
   `data/publishers.json` in the same format; never invent variants.
5. `breaking` (optional, default `false`) — `true` only for rare, major
   market-moving news (import-duty change, very large single-day price move).
   At most 1–2 per week: check the worker-local
   `~/workspace/gold-silver-digest/breaking-log.jsonl` for entries in the last
   7 days (this log is machine-local state — never committed to the repo);
   if 2 already, do not set `true`. When set, append
   `{"date":"<YYYY-MM-DD>","id":"<item id>","title":"<title>"}` to the log.
   If the log is missing/unreadable, default `false`.
6. `related_urls` (optional) — story-level dedup: when several outlets cover the
   same story, include it ONCE from the most authoritative/detailed source and
   put the other versions' canonical URLs here. Each item in `items` must be a
   different story.

## Content rules
1. Items: 5–20 per edition. Rank by relevance to Indian gold/silver buyers,
   MCX first. Rank 1 = most important; every rank unique, sequential 1..N.
2. Freshness: only articles from the last 48h. Each article appears once per edition.
3. Summaries in your own words. Never copy article text beyond the title.
   Never reproduce paywalled content.
4. No investment advice: no buy/sell/hold, no price targets, no recommendations.
   Factual wording only, e.g. "Gold rose 0.8% on MCX after…". Applies to insights too.
5. Links: https only, publisher's original (never an aggregator), tracking stripped.
6. `image_url`: the publisher's own preview image (`og:image`/`twitter:image`) or
   `null`. Never generated or stock images.
7. Tags: only from the taxonomy in `data/taxonomy.json`
   (`gold, silver, mcx, comex, rupee, rbi, fed, import-duty, india, global,
   jewellery, central-banks`).
8. Language: plain English; rupee amounts as ₹, lakh/crore where natural. UTF-8.

## Assembling the edition
1. Run `python3 src/fetch_feeds.py` for broad RSS coverage (agent adds targeted
   searches and opens key articles).
2. Fetch the current `news/latest.json`:
   `bin/gh-api GET /repos/XAUrum-Fintech/daily-news/contents/news/latest.json`
   (base64-decode `content`; 404 = first edition, no previous file).
3. Carry over still-relevant items < 48h old from the previous edition by id
   (merge repeats — same id, keep one).
4. New items found in this window rank first (freshness), then carried items;
   re-rank everything by Indian-buyer relevance, MCX first. Ranks 1..N unique.
5. Write `insights`: headline (≤120 chars, one sentence on what is moving gold/silver),
   summary (2–4 sentences, ≤600 chars), points (≤5, each ≤160 chars). Factual, no advice.

## Validation (BEFORE any write — abort and leave repo untouched on failure)
Run `python3 src/validate_edition.py news/latest.json` (exit 0 = valid).
It checks: top-level keys exactly
`{schema, edition_id, generated_at, window, insights, items}`;
`schema == "orob-news.v1"`; `edition_id` on an even 2-hourly UTC slot;
`window` exactly 2h ending at the slot; headline ≤120, summary ≤600,
0–5 points each ≤160; 5 ≤ items ≤ 20; ranks exactly 1..N unique;
every item has `category` ∈ {mcx, global, policy, festive}, `metals` non-empty
⊆ {gold, silver}, non-empty `source`/`source_domain` (domain lowercase, no
`www.`, well-formed), `breaking` boolean (default false), `related_urls`
absent or a list of https URLs; title ≤160, summary ≤400; url https with no
tracking params; `published_at` within 48h before `generated_at`;
`id == sha256(canonical_url)[:16]`; tags non-empty ⊆ taxonomy;
`image_url` https or null.

## Change detection
- Compare new items (id + title + summary + url) against the previous edition.
- Identical → SKIP the commit entirely. Do not commit.
- (`src/publish.py` implements this.)

## Commit (only when changed AND valid)
- `python3 src/publish.py news/latest.json news/latest.md` — PUTs both files
  with message `news: <generated_at>` (sha included when updating; on 409,
  re-fetch and retry once), then verifies with a final GET.
- NEVER commit images or any other files. NEVER rewrite history.
- If the run fails at any point: leave the previous `latest.json` untouched.

## news/latest.md format
```markdown
# Gold & Silver for Indian buyers · <edition_id>

> <insights.headline>

<insights.summary>

- <point>
- …

## Stories
1. [title](url) — *source*, <published_at>
   <summary>
...
```
