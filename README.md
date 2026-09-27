# daily-news

A 2-hourly gold & silver news feed for Indian buyers, published as data.

## App contract

- **`news/latest.json`** — overwritten in place every edition. Schema
  `orob-news.v1`: top-level `{schema, edition_id, generated_at, window,
  insights, items}`. Each item carries `id` (first 16 hex chars of the
  SHA-256 of the canonical article URL), `rank`, `title`, `summary`, `url`,
  `source`, `source_domain`, `published_at`, `image_url`, `tags`,
  plus `category` (mcx / global / policy / festive), `metals`
  (`["gold"]`, `["silver"]`, or both), optional `breaking`, and optional
  `related_urls`. All times UTC ISO 8601 with `Z`.
- **`news/latest.md`** — human-readable mirror of the same edition.
- One commit per edition, message `news: <generated_at>`. Editions with no
  new or changed items are skipped (no commit).
- Only `news/latest.json` and `news/latest.md` are ever committed by editions.
  No images, no other files, no history rewrites.

## Layout

| Path | What |
|---|---|
| `news/` | Published feed: `latest.json` + `latest.md` (overwritten per edition) |
| `src/` | Pipeline scripts: `fetch_feeds.py`, `validate_edition.py`, `publish.py` (+ `src/README.md`) |
| `data/` | `publishers.json` (canonical source/domain mapping), `taxonomy.json` (categories, metals, tags) |
| `docs/` | `RUNBOOK.md` — the full edition spec; this repo is its source of truth |

## How editions run

Every 2 hours at minute 0 UTC (`0 */2 * * *`):

1. `python3 src/fetch_feeds.py` — Google News RSS (India + global editions)
2. Agent: targeted searches for primary sources (MCX circulars, SEBI/RBI/IBJA
   releases), opens key articles, resolves canonical publisher URLs
3. Agent: ranks stories (MCX first, Indian-buyer relevance), writes summaries
   in its own words, insights, and assigns category/metals/tags
4. `python3 src/validate_edition.py news/latest.json` — must exit 0
5. `python3 src/publish.py news/latest.json news/latest.md` — change-detects,
   then commits with message `news: <generated_at>`

Content rules: 5–20 items per edition, only articles from the last 48h, no
investment advice, publisher-original https links with tracking stripped,
publisher's own preview image or `null`.
