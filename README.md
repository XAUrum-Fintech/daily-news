# daily-news

A gold & silver news feed for Indian buyers, published as data — hourly
during the day (08:00–18:00 IST), 2-hourly at night on even UTC hours.

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

Hourly during 08:00–18:00 IST, 2-hourly at night on even UTC hours
(odd UTC night hours stay quiet — see `docs/RUNBOOK.md` for the slot rules):

1. `python3 src/fetch_feeds.py` — Google News RSS (India + global editions)
2. Agent: targeted searches for primary sources (MCX circulars, SEBI/RBI/IBJA
   releases), opens key articles, resolves canonical publisher URLs
3. Agent: ranks stories (MCX first, Indian-buyer relevance), writes summaries
   in its own words, insights, and assigns category/metals/tags
4. `python3 src/validate_edition.py news/latest.json` — must exit 0
5. `python3 src/publish.py news/latest.json news/latest.md` — change-detects,
   then commits with message `news: <generated_at>`

Content rules: 10–30 items per edition, ranked by impact for Indian buyers, no
investment advice, publisher-original https links with tracking stripped,
publisher's own preview image or `null`.

Editions re-host images under `assets/news/` (committed; the reader relies on
them) instead of hotlinking publisher images.

## Other feeds in this repo

- **`customs-duty-tracker/`** — machine-published event log of customs duty
  changes (tariff-value notices, exchange-rate circulars, duty-rate changes).
  Spec: `customs-duty-tracker/SPEC.md`; agent tools in
  `customs-duty-tracker/tools/`.
- **`macro-events/`** — machine-published schedule and results of the macro
  releases that move gold and silver (US jobs, US CPI, Fed rate decisions,
  RBI policy, Union Budget). Schema `macro-events.v1`; the backend polls
  `macro-events/latest.json`. Spec: `macro-events/SPEC.md`; agent tools in
  `macro-events/tools/` (validation, calendar refresh, release-window
  polling, daily rebuild). A GitHub workflow validates `latest.json` on
  every push touching `macro-events/`.
