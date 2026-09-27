# Pipeline scripts (`src/`)

Python 3, standard library only. These scripts do the deterministic parts of
each 2-hourly edition; the scheduled agent does the judgment parts.

## Scripts

- **`fetch_feeds.py`** — pulls the Google News RSS feeds for the runbook's
  India-edition queries (`hl=en-IN&gl=IN&ceid=IN:en`) and global-edition queries
  (`hl=en-US&gl=US&ceid=US:en`), parses `<item>` entries, splits the
  `" - Source"` suffix off titles into `source_name`, dedupes by
  (normalized title, source), and prints a JSON list to stdout.
  Note: `rss/articles/...` links do NOT resolve to publishers via curl —
  leave `link` as-is; the agent resolves the canonical publisher URL by
  searching the exact article title.
- **`validate_edition.py <latest.json>`** — validates an edition against the
  `orob-news.v1` contract plus the extended item fields (`category`, `metals`,
  normalized `source`/`source_domain`, `breaking`, `related_urls`). Exit 0 =
  valid, exit 1 = errors on stderr. Loads the tag taxonomy from
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

## How the agent and scripts split the work each edition

| Step | Who |
|---|---|
| Pull RSS feeds (broad coverage) | `src/fetch_feeds.py` |
| Targeted searches for primary sources (MCX circulars, SEBI/RBI/IBJA releases) | agent |
| Open key articles, verify dates/authors, resolve canonical publisher URLs | agent |
| Rank stories (MCX first, Indian-buyer relevance), dedupe via `related_urls` | agent |
| Write summaries in own words, insights, assign `category`/`metals`/`tags`/`breaking` | agent |
| Normalize `source`/`source_domain` | agent, using `data/publishers.json` |
| Validate the edition JSON | `src/validate_edition.py` |
| Change-detect and publish | `src/publish.py` |

## Edition flow (detail in `docs/RUNBOOK.md`)

1. `python3 src/fetch_feeds.py > /tmp/feeds.json`
2. Agent: searches, opens articles, assembles `latest.json` + `latest.md`
3. `python3 src/validate_edition.py latest.json`
4. `python3 src/publish.py latest.json latest.md`
