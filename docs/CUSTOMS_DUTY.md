# Customs Duty Watch — XAUrum-Fintech/daily-news

Tracks CBIC tariff-value notifications issued under Section 14(2) of the
Customs Act, 1962 (each amending Notification No. 36/2001-Customs (N.T.)).
These set the USD tariff values for gold (per 10g) and silver (per kg) that
fix the assessable value for customs duty on specified imports.

This watcher is SEPARATE from the 2-hourly news feed. It never reads or
writes `news/`, never touches `docs/RUNBOOK.md`, `src/validate_edition.py`,
or any other feed pipeline file. Feed structure stays exactly as it is.

## Schedule

- Daily at 05:30 UTC (`30 5 * * *`, timezone UTC).
- Tariff-value notifications land roughly fortnightly (around the 1st and
  16th of each month, drifting by a few days); daily polling absorbs the drift.

## State

- `customs-duty/state.json` — last seen tariff-value notification:
  `last_notification_short` (e.g. `"75/2026"`), `dated`, `effective`,
  gold/silver tariff values, `source_url`, `updated_at`.
- The state file is the baseline. Never guess; always read it first.

## Detection

1. Search the web for new **"Fixation of Tariff Value"** notifications
   amending 36/2001-Customs (N.T.) with a notification number NEWER than
   `state.json`'s `last_notification_short` (compare year first, then number;
   handle year rollover, e.g. `01/2027` > `75/2026`).
   - Fast secondary listings: TaxTMI customs notifications, TaxGuru
     custom-duty section, weekly notification digests.
   - CAUTION: the `Customs (N.T.)` series also carries non-tariff
     notifications (e.g. 73/2026 and 74/2026 in September 2026 were a Sea
     Cargo Manifest extension and an adjudicating-authority appointment).
     Only "Fixation of Tariff Value" notifications count.
2. Open the notification text. Prefer the official CBIC/gazette link when
   reachable; otherwise use the secondary page that reproduces the full
   notification text and label `source`/`source_domain` as that publisher.
3. Extract: notification number, dated date, effective date, and the TABLE-2
   gold (USD per 10 grams) and silver (USD per kilogram) tariff values.
   Recent notifications carry one gold value and one silver value across
   their entries; if entries ever differ, use the entry-1 (Sl. 194/195 of
   45/2025-Customs) values and note the divergence in the commit message.
4. Change detection is NUMERIC ONLY: compare the extracted gold/silver values
   against `state.json`. Ignore "(i.e., no change)" annotations in secondary
   reporting — they have been inconsistent; the numbers are authoritative.

## On a new notification

1. Build the event files with the repo script (it validates):
   `python3 src/build_customs_duty.py --notification-no ... --short ... ...`
   Run `python3 src/build_customs_duty.py --help` for the full argument list.
2. Write three files under `customs-duty/`:
   - `<effective>-<short>.json` (e.g. `2026-09-16-75-2026.json`) — the event
   - `latest.json` — full copy of the event (stable path for consumers)
   - `state.json` — updated baseline
3. Commit all changed files in ONE atomic commit via the Contents API:
   message `customs-duty: <short> effective <effective>`
   (e.g. `customs-duty: 75/2026 effective 2026-09-16`).
   Never include `news/` or any feed file in the commit.
4. On 409 (sha changed underneath): re-fetch, re-compare, retry once.
5. Verify after PUT: GET each file, confirm content and commit message.

## On no new notification

- Do nothing. No commit, no output file changes, no message.

## On failure

- Leave the repo untouched (previous `latest.json`/`state.json` stay valid).
- Report the failure plainly: what step failed and what is still unknown.
