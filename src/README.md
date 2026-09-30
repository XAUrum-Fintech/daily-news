# Pipeline scripts (`src/`)

Python 3, standard library only. These scripts do the deterministic parts of
each 2-hourly edition; the scheduled agent does the judgment parts.

## Scripts

- **`fetch_feeds.py`** — pulls candidate news and prints a JSON list to stdout.
  Two source kinds:
  1. Google News RSS for the runbook's India-edition queries
     (`hl=en-IN&gl=IN&ceid=IN:en`) and global-edition queries
     (`hl=en-US&gl=US&ceid=US:en`) — broadened with festival/demand queries
     (`Dhanteras gold`, `Diwali gold demand`, `Akshaya Tritiya gold sales`,
     `gold jewellery demand India`, `silver demand India`, `MCX silver`,
     `gold ETF India inflows`, `sovereign gold bond`, `gold hallmarking BIS`,
     `RBI gold buying`, `gold import India`) and global adds
     (`COMEX silver futures`, `central bank gold reserves`, `London gold price`).
  2. Direct publisher feeds: Economic Times markets, Mint markets,
     Business Standard markets, Hindu BusinessLine commodities, Investing.com
     commodities, World Gold Council (see `DIRECT_FEEDS` in the script).
  Every candidate is tagged with `feed_origin` (`google_news_in`,
  `google_news_us`, or `direct:<source_domain>`). A failing feed prints a WARN
  to stderr and is skipped — it never crashes the run. Items are deduped by
  (normalized title, source).
  Note: `rss/articles/...` links do NOT resolve to publishers via curl —
  leave `link` as-is; the agent resolves the canonical publisher URL by
  searching the exact article title.
- **`fetch_image.py <article-url>`** — prints the publisher's own preview image
  URL for an article page (browser UA, 15s timeout). Reads `og:image`
  (fallback `twitter:image`) from the page HTML and prints the absolute URL.
  Prints nothing, exit 1, when no suitable image is found (non-HTML response,
  fetch failure, or no meta tag). Used by the agent to fill `image_url` for
  every edition item. Fallback chain: publisher image → repo AI placeholder
  (see `assets/` below) — `image_url` is never null.
- **`validate_edition.py <latest.json>`** — validates an edition against the
  `orob-news.v1` contract plus the extended item fields (`category`, `metals`,
  normalized `source`/`source_domain`, `breaking`, `related_urls`) and the
  hand-curated `trending` section. Exit 0 = valid, exit 1 = errors on stderr.
  Checks include: 10–30 items, ranks 1..N unique, ≥2 `mcx` items, no category
  more than half the items, at most 2 items per `source_domain`, every item's
  `image_url` a non-null https URL (publisher image or repo placeholder —
  exit 1 on any null; stderr warning only when rank 1 uses a placeholder),
  3–5 trending entries (title ≤80, summary ≤200,
  `item_ids` referencing real items). Loads the tag taxonomy from
  `data/taxonomy.json` (relative to the script).
- **`publish.py <latest.json> <latest.md>`** — publishes one edition via the
  GitHub Contents API using `~/workspace/skills/github/bin/gh-api`
  (override with `GH_API_BIN`). Flow: GET current `news/latest.json`
  (404 = first publish) → compare items on `(id, title, summary, url)` and
  print `SKIP: no changes` with exit 0 if identical → validate via
  `validate_edition.py` (aborts, repo untouched, on failure) → PUT both files
  with message `news: <generated_at>` (sha included when updating; on 409,
  re-fetch and retry once) → final GET to verify, prints the commit SHA.
  Never commits any other files; never rewrites history.

## Assets (`assets/`)

AI-generated placeholder images used as the `image_url` fallback when a
publisher's article page provides no usable `og:image`/`twitter:image`.
Referenced by their raw URLs, e.g.
`https://raw.githubusercontent.com/XAUrum-Fintech/daily-news/main/assets/placeholder-gold.jpg`.

Fallback chain per item: publisher image → placeholder → (never null).

| File | Use for |
|---|---|
| `assets/placeholder-gold.jpg` | gold-led items |
| `assets/placeholder-silver.jpg` | silver-led items |
| `assets/placeholder-metals.jpg` | items covering both metals |

Rank 1 should prefer a publisher image when one exists (the validator warns
if rank 1 uses a placeholder). Never generate per-article images and never
use stock photos.

## How the agent and scripts split the work each edition

| Step | Who |
|---|---|
| Pull RSS + direct publisher feeds (broad coverage) | `src/fetch_feeds.py` |
| Targeted searches for primary sources (MCX circulars, SEBI/RBI/IBJA releases) | agent |
| Open key articles, verify dates/authors, resolve canonical publisher URLs | agent |
| Extract each article's preview image (`og:image`/`twitter:image`), qualify it (https, ≤2MB, ≥800px, no-cookie fetch) or re-host | agent, via `src/fetch_image.py` |
| Rank stories by impact for Indian buyers, dedupe via `related_urls` | agent |
| Write summaries in own words, insights, trending themes; assign `category`/`metals`/`tags`/`breaking`/`publisher_id`/`image_kind` | agent |
| Normalize `source`/`source_domain`/`publisher_id` | agent, using `data/publishers.json` |
| Validate the edition JSON (incl. trending + balance rules) | `src/validate_edition.py` |
| Change-detect and publish | `src/publish.py` |

## Edition flow (detail in `docs/RUNBOOK.md`)

1. `python3 src/fetch_feeds.py > /tmp/feeds.json`
2. Agent: searches, opens articles, assembles `latest.json` + `latest.md`
3. `python3 src/validate_edition.py latest.json`
4. `python3 src/publish.py latest.json latest.md`

## Customs-duty watcher (separate from the feed)

- **`build_customs_duty.py`** — builds and validates one tariff-value event
  file for the daily customs-duty watcher (spec: `docs/CUSTOMS_DUTY.md`).
  Writes `<effective>-<short>.json`, `latest.json`, and `state.json` into
  `--out`; the scheduled agent commits them under `customs-duty/`.
  Never touches `news/` or any feed file.
