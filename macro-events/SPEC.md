# Macro events feed — SPEC

Copy of sections 3, 4 and 11 of the contract ("The macro events feed (contract
and agent handoff)"). This is the normative file format the agent publishes
and the backend validates. The full contract lives with the feed owner.

## 3. The file

A single JSON object, UTF-8, at most 1 MiB:

```json
{
  "schema": "macro-events.v1",
  "generated_at": "2026-10-02T12:41:07Z",
  "event_count": 6,
  "events": [ ... ]
}
```

| Field | Rule |
|---|---|
| `schema` | Exactly `macro-events.v1`. |
| `generated_at` | RFC 3339 timestamp with a zone, for example `2026-10-02T12:41:07Z`. Time the agent wrote the file. Not more than 10 minutes in the future. |
| `event_count` | Must equal the number of items in `events`. |
| `events` | 1 to 400 records, one per release, in any order. |

### 3.1 One record per release

```json
{
  "id": "us-jobs-2026-10-02",
  "kind": "us_jobs",
  "title": "US jobs report",
  "status": "released",
  "scheduled_at": "2026-10-02T08:30:00-04:00",
  "scheduled_tz": "America/New_York",
  "metrics": [
    { "key": "nonfarm_payrolls", "label": "Payrolls", "unit": "thousand_jobs",
      "actual": 29, "forecast": 89, "previous": 133, "previous_first_print": 162 },
    { "key": "unemployment_rate", "label": "Unemployment", "unit": "percent",
      "actual": 4.2, "forecast": null, "previous": 4.3, "previous_first_print": null }
  ],
  "revisions": [
    { "label": "July", "from": 21, "to": -10 },
    { "label": "August", "from": 162, "to": 133 }
  ],
  "summary": "Payrolls rose 29k against 89k expected; unemployment 4.2%.",
  "forecast_source": "Public economic calendar",
  "source_url": "https://www.bls.gov/news.release/empsit.nr0.htm",
  "published_at": "2026-10-02T12:31:40Z"
}
```

| Field | Type | Rule |
|---|---|---|
| `id` | string | Stable identity of the release. 3 to 48 characters, lowercase letters, digits and hyphens, starting and ending with a letter or digit. **Never change it after publishing.** A release that moves to another day keeps its id and changes `scheduled_at`. Convention: `<kind-slug>-<original scheduled date>`, with kind slugs `us-jobs`, `us-cpi`, `fed`, `rbi`, `budget`. Unique in the file. |
| `kind` | string | One of `us_jobs`, `us_cpi`, `fed_rate`, `rbi_policy`, `union_budget`. |
| `title` | string | 1 to 60 characters. Plain words for people, for example `US jobs report`, `US inflation (CPI)`, `Fed rate decision`, `RBI policy decision`, `Union Budget`. |
| `status` | string | `scheduled`, `released` or `cancelled`. |
| `scheduled_at` | string | RFC 3339 with the offset that was in force at that moment in `scheduled_tz`. **Store the release time in the zone the agency uses**, not in IST; the backend converts. |
| `scheduled_tz` | string | IANA zone: `America/New_York` for `us_jobs`, `us_cpi` and `fed_rate`; `Asia/Kolkata` for `rbi_policy` and `union_budget`. The offset in `scheduled_at` must equal the zone's real offset at that instant (this catches a wrong daylight-saving offset: the US clocks change on 1 November 2026, so 8:30 am on 6 November is `-05:00`, while 8:30 am on 2 October is `-04:00`). |
| `metrics` | array | 1 to 6 items. The **first** item is the headline figure (payrolls for the jobs report). A `scheduled` or `cancelled` record may use an empty array; a `released` record needs at least one. |
| `revisions` | array | 0 to 4 items. Only for `released` records whose release revised earlier figures; must be empty on `scheduled` and `cancelled` records. |
| `summary` | string | One plain-language sentence, 1 to 160 characters. Required on every record. For a `scheduled` record it says what the release measures ("Monthly count of US jobs added, and the unemployment rate."). For a `released` record it says what happened. No markdown, no line breaks, no advice or opinion about gold or silver. |
| `forecast_source` | string or null | Name of where forecasts came from, 1 to 40 characters. **Required (non-null) when any metric has a non-null `forecast`; must be null otherwise.** The app prints "expected figure from a public economic calendar; the reader does not verify it" beside it. |
| `source_url` | string | `https` link to the official release or calendar page. Host must be a `.gov` host, a `.gov.in` host, or exactly `rbi.org.in` or `www.rbi.org.in` (the same "links only to official sites" rule the customs tracker uses). Anything else rejects the file. |
| `published_at` | string or null | RFC 3339. The moment the agent first wrote the actual result into the file. `null` while `scheduled` or `cancelled`; **required when `released`**. Must not be before `scheduled_at` minus one hour. |

### 3.2 Metric

| Field | Type | Rule |
|---|---|---|
| `key` | string | Lowercase letters, digits and underscores, 1 to 40 characters. Unique within the record. |
| `label` | string | 1 to 30 characters, shown to people (`Payrolls`, `Unemployment`, `Inflation, year on year`). |
| `unit` | string | `thousand_jobs` (a signed count in thousands: 29 means +29,000 jobs), `percent` (4.2 means 4.2%), or `index`. |
| `actual` | number or null | The result. Must be non-null on a `released` record's **first** metric; must be null on `scheduled` and `cancelled` records. |
| `forecast` | number or null | The consensus forecast, if the owner's chosen source has one. Null is normal and simply hides the "expected" figure and any "below forecast" label. |
| `previous` | number or null | The previous period's figure **as revised** (the app leads with this one). |
| `previous_first_print` | number or null | The previous period's figure as first published, only when it differs from `previous`. Null otherwise. Must be null when `previous` is null. |

All numbers must be finite JSON numbers (never strings, never `NaN`). Bounds: `thousand_jobs`
within plus or minus 30000, `percent` within -100 and 1000, `index` within plus or
minus 1,000,000. At most 3 decimal places is expected; more are accepted but shown rounded.

For rate decisions use two metrics, upper and lower bound of the target range
(`fed_funds_upper`, `fed_funds_lower`, unit `percent`), or one metric for the RBI repo
rate (`repo_rate`, unit `percent`), with `previous` as the rate before the decision. The
Union Budget has no figures to compare: use an empty `metrics` array and a `summary`.

### 3.3 Revision

| Field | Type | Rule |
|---|---|---|
| `label` | string | The period revised, 1 to 20 characters (`July`, `August`). |
| `from` | number | The figure as first published. |
| `to` | number | The figure now. Same unit as the record's first metric. |

The app computes the net revision itself (the sum of `to - from`), so do not put a total in the file.

### 3.4 Status rules together

| status | `metrics[].actual` | `published_at` | `metrics` empty? |
|---|---|---|---|
| `scheduled` | all null | null | allowed |
| `released` | first metric non-null | required | not allowed (except `union_budget`, which may be empty) |
| `cancelled` | all null | null | allowed |

A scheduled release whose time has passed but whose result is not yet in the agency's
release stays `scheduled`; flip it to `released` only when the actual figure is in hand.

## 4. What the backend enforces (all or nothing)

One bad record rejects the whole file. The backend then keeps the last good copy, logs one
line saying why, and the app keeps showing the last good data. The checks, in order:

1. The body is valid JSON, between 1 byte and 1 MiB, and the envelope has `schema`,
   `generated_at`, `event_count` and `events`.
2. `schema` is `macro-events.v1`; `event_count` equals the number of events; 1 to 400 events.
3. Every record has every field in section 3.1 (use `null`, not a missing key, for the
   nullable ones), with the types, lengths and enumerations above. Unknown extra fields
   are ignored, so the format can grow without breaking readers.
4. `id` is well-formed and unique across the file.
5. `scheduled_at` parses, its offset matches `scheduled_tz` at that instant, and it lies
   within 400 days either side of `generated_at`.
6. The status rules of section 3.4, the forecast rule (`forecast_source` iff a forecast
   exists), the unit bounds, unique metric keys, `previous_first_print` only with a
   `previous`, no text with a line break or other control character, and the `source_url`
   host rule.
7. `generated_at` is not older than the stored copy. An older file is ignored with one
   log line (it is not an error, it just never replaces newer data).

Copy retention is your job: **keep releases from the last 90 days and every upcoming
release for the next 120 days** in the file. The backend replaces its stored copy with
the file, so a record that disappears from the file disappears from the app. It never
deletes a push record: a release that already sent its notification will not send a second.

A release result triggers a push only when all of these hold: the macro push switch is on,
the record became `released` for the first time (the backend has not pushed it before),
and its `published_at` is no more than 6 hours old when the backend first sees it. The
very first copy of the file the backend ever stores is treated as history and never pushes,
so turning the feature on never floods people with old results.

## 11. Stricter plain-text rules (now enforced)

The reader is strict about text in `title`, `summary`, metric `label`, revision `label` and `forecast_source`. A single violation still rejects the whole file. The rule reads:

| `forecast_source` | string or null | Name of where forecasts came from, 1 to 40 characters, [plain text](#plain-text) (a name, not a link). **Required (non-null) when any metric has a non-null `forecast`; must be null otherwise.** The app prints "expected figure from a public economic calendar; the reader does not verify it" beside it. |
| `source_url` | string | `https` link to the official release or calendar page. Host must be a `.gov` host, a `.gov.in` host, or exactly `rbi.org.in` or `www.rbi.org.in` (the same "links only to official sites" rule the customs tracker uses). Anything else rejects the file. |
| `published_at` | string or null | RFC 3339. The moment the agent first wrote the actual result into the file. `null` while `scheduled` or `cancelled`; **required when `released`**. Must not be before `scheduled_at` minus one hour. |

<a id="plain-text"></a>**Plain text.** `title`, `summary`, `forecast_source` and every metric and revision
`label` go out as push notifications and on screen exactly as written, so each must be plain
words. One field that breaks this rejects the **whole file**:

- no `<`, `>`, `[` or `]` (write "above 3%", not ">3%"; "revised", not "[revised]");
- no web address (the link belongs in `source_url`): nothing containing `://` or `www.`, no
  `name.tld/` host followed by a path such as `bit.ly/x`, and no bare host name ending in a
  common top-level domain (`com`, `net`, `org`, `io`, `in`, `co`, `ly`, `app`, `xyz`, `info`,
  `me`, `biz`, `online`, `site`, `link`, `to`, `gl`, `us`, `uk`, `ru`, `cn`, `de`, `fr`, `jp`,
  `au`, `ca`, `eu`, `tk`, `cc`, `tv`, `ws`, `ai`, `gov`, `edu`, `dev`, any letter case) or in a
  punycode `xn--` label, with or without `?`, `#` or a path after it (`evil.com`, `bit.ly?x`).
  Abbreviations and figures such as "U.S.", "U.S.Treasury", "a.m." and "3.5" are fine;
- no line break, tab, line or paragraph separator (U+2028, U+2029), or other control or
  invisible character (zero-width spaces, right-to-left marks).

Also: the "Expected today" line in the morning briefing is shown only while the file's `generated_at` is less than about 48 hours old, so keep the daily 06:00 IST rebuild running even when nothing changed.
