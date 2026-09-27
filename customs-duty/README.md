# customs-duty/

Machine-readable record of CBIC gold/silver tariff-value notifications
(Section 14(2), Customs Act, 1962 — each amending 36/2001-Customs (N.T.)).
Updated by the daily customs-duty watcher (see `docs/CUSTOMS_DUTY.md`).

This directory is independent of the 2-hourly news feed in `news/`.
Nothing here changes the feed's structure or pipeline.

## Files

- `latest.json` — the most recent tariff-value notification, always current.
  Stable path for direct consumption.
- `<effective>-<short>.json` — one file per notification
  (e.g. `2026-09-16-75-2026.json`), kept as history.
- `state.json` — watcher baseline (last seen notification + values).

## Event schema (`orob-customs-duty.v1`)

| Field | Meaning |
|---|---|
| `notification_no` / `notification_short` | e.g. `75/2026-Customs (N.T.)` / `75/2026` |
| `dated` / `effective` | notification date / in-force date (ISO) |
| `amends` | `36/2001-Customs (N.T.)` |
| `source_url` | link to the notification text |
| `gold_tariff_usd_per_10g` / `silver_tariff_usd_per_kg` | current USD tariff values |
| `*_prev` | values from the previous notification |
| `gold_changed` / `silver_changed` | numeric change vs previous |
| `generated_at` | file generation time (UTC ISO) |
| `article` | the update shaped like a news-feed item: `id` (sha256 of url, 16 hex), `title`, `summary`, `url`, `source`, `source_domain`, `published_at`, `image_url`, `tags`, `category` (`policy`), `metals`, `breaking` |

A new file is written for every new tariff-value notification, even when the
gold/silver values are unchanged — check the `*_changed` flags.
