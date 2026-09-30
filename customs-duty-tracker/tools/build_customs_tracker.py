#!/usr/bin/env python3
"""Build/append rows for the customs-duty tracker dataset.

The tracker is a 24-column event table (one row per event that changes
customs duty on gold/silver imports): CBIC tariff-value fixations and
ICEGATE ERAM exchange-rate notifications, with the LBMA London fix and
INR duty computed at the duty rate in force on the row's effective date
(6% before 13 May 2026; 15% from 13 May 2026, when the government raised
total import duty on gold/silver from 6% to 15% — BCD 5%->10%, AIDC 1%->5%).
Each row also carries the official source link for its notice (`source_url`;
`source_kind` is "notice" when the link opens the notice itself, "portal"
when it opens the official listing portal where the notice number can be
found), a `notice_url` copy for orob's app, and the row's own `duty_rate`.

Usage:
    python3 src/build_customs_tracker.py append \\
        --published 2026-09-18 --effective 2026-09-19 --event exchange_rate \\
        --eram-notification 28/2026 --usd-inr-import 96.80 --usd-inr-export 95.10

    python3 src/build_customs_tracker.py append \\
        --published 2026-10-01 --effective 2026-10-02 --event tariff_value \\
        --tariff-notification "76/2026-Customs (N.T.)" \\
        --gold-tariff 1390 --silver-tariff 2050 \\
        --gold-fix 4350.00 --silver-fix 64.20

The script reads customs-duty-tracker/latest.json, carries forward the
values the new event does not change, computes all derived columns,
validates, and rewrites latest.json, latest.csv and state.json.
It never touches customs-duty/ or news/.
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

DUTY_RATE = 0.15  # current rate (top-level/state.json); per-row rates vary
# Total import duty on gold/silver was 6% (BCD 5% + AIDC 1%) until the
# government raised it to 15% (BCD 10% + AIDC 5%) effective 13 May 2026
# (notified 12 May 2026). Rows effective before that date use 6%.
DUTY_CHANGE_EFFECTIVE = "2026-05-13"
DUTY_RATE_PRE = Decimal("0.06")
DUTY_RATE_POST = Decimal("0.15")
# Exact factors used by the reference spreadsheet (verified constant):
# USD/10g -> USD/troy oz ; USD/kg -> USD/troy oz
GOLD_10G_TO_TROY_OZ = 3.11034768
SILVER_KG_TO_TROY_OZ = 0.0311034768

SCHEMA = "customs-tracker.v3"

SOURCE_KINDS = {"notice", "portal"}

# (json key, csv header)
COLUMNS = [
    ("published", "Published"),
    ("effective", "Effective"),
    ("event", "Event"),
    ("tariff_notification", "Tariff Notification"),
    ("eram_notification", "ERAM Notification"),
    ("gold_tariff_usd_10g", "Gold Tariff (USD/10g)"),
    ("silver_tariff_usd_kg", "Silver Tariff (USD/kg)"),
    ("usd_inr_import", "USD INR (Import)"),
    ("usd_inr_export", "USD INR (Export)"),
    ("gold_london_fix_usd_oz", "Gold London Fix (USD/oz)"),
    ("gold_tariff_usd_troy_oz", "Gold Tariff (USD/troy oz)"),
    ("silver_london_fix_usd_oz", "Silver London Fix (USD/oz)"),
    ("silver_tariff_usd_troy_oz", "Silver Tariff (USD/troy oz)"),
    ("fix_date", "Fix Date"),
    ("gold_value_inr_kg", "Gold Value (INR/kg)"),
    ("gold_duty_inr_kg", "Gold Duty (INR/kg)"),
    ("gold_duty_change_inr_kg", "Gold Duty Increase/Decrease (INR/kg)"),
    ("silver_value_inr_kg", "Silver Value (INR/kg)"),
    ("silver_duty_inr_kg", "Silver Duty (INR/kg)"),
    ("silver_duty_change_inr_kg", "Silver Duty Increase/Decrease (INR/kg)"),
    ("source_url", "Source URL"),
    ("source_kind", "Source Type"),
    ("notice_url", "Notice URL"),
    ("duty_rate", "Duty Rate"),
]

EVENT_LABELS = {"tariff_value": "Tariff value", "exchange_rate": "Exchange rate"}

_Q2 = Decimal("0.01")


def duty_rate_for(effective):
    """Duty rate in force on `effective` (YYYY-MM-DD) as a Decimal."""
    return DUTY_RATE_PRE if effective < DUTY_CHANGE_EFFECTIVE else DUTY_RATE_POST


def _d(x):
    return Decimal(str(x))


def derive(gold_tariff_10g, silver_tariff_kg, usd_inr_import, rate):
    """Money math in Decimal (half-up, like the spreadsheet).

    Returns (gold_value, gold_duty, silver_value, silver_duty) as Decimals
    rounded to 2dp. `rate` is the duty rate in force on the row's effective
    date (see duty_rate_for).
    """
    g10 = _d(gold_tariff_10g)
    skg = _d(silver_tariff_kg)
    fxi = _d(usd_inr_import)
    gold_value = (g10 * 100 * fxi).quantize(_Q2, rounding=ROUND_HALF_UP)
    gold_duty = (gold_value * _d(rate)).quantize(_Q2, rounding=ROUND_HALF_UP)
    silver_value = (skg * fxi).quantize(_Q2, rounding=ROUND_HALF_UP)
    silver_duty = (silver_value * _d(rate)).quantize(_Q2, rounding=ROUND_HALF_UP)
    return gold_value, gold_duty, silver_value, silver_duty


def _chg(now, prev):
    return (_d(now) - _d(prev)).quantize(_Q2, rounding=ROUND_HALF_UP)


def die(msg):
    print(f"build_customs_tracker: ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def compute_row(prev, ev):
    """Build one tracker row dict. `prev` is the previous row dict or None."""
    event = ev["event"]
    if event not in EVENT_LABELS:
        die(f"unknown event '{event}'")

    if event == "tariff_value":
        for k in ("tariff_notification", "gold_tariff_usd_10g", "silver_tariff_usd_kg",
                  "gold_london_fix_usd_oz", "silver_london_fix_usd_oz"):
            if ev.get(k) is None:
                die(f"--{k.replace('_', '-')} is required for tariff_value events")
        gold_tariff = float(ev["gold_tariff_usd_10g"])
        silver_tariff = float(ev["silver_tariff_usd_kg"])
        usd_inr_import = (float(ev["usd_inr_import"]) if ev.get("usd_inr_import") is not None
                          else (prev["usd_inr_import"] if prev else None))
        usd_inr_export = (float(ev["usd_inr_export"]) if ev.get("usd_inr_export") is not None
                          else (prev["usd_inr_export"] if prev else None))
        if usd_inr_import is None or usd_inr_export is None:
            die("tariff_value event needs USD/INR rates: pass --usd-inr-import/--usd-inr-export")
        gold_fix = float(ev["gold_london_fix_usd_oz"])
        silver_fix = float(ev["silver_london_fix_usd_oz"])
        fix_date = ev.get("fix_date") or ev["published"]
        tariff_notif = ev["tariff_notification"]
        eram_notif = None
    else:  # exchange_rate
        for k in ("eram_notification", "usd_inr_import", "usd_inr_export"):
            if ev.get(k) is None:
                die(f"--{k.replace('_', '-')} is required for exchange_rate events")
        if prev is None:
            die("cannot start the table with an exchange_rate event")
        gold_tariff = prev["gold_tariff_usd_10g"]
        silver_tariff = prev["silver_tariff_usd_kg"]
        usd_inr_import = float(ev["usd_inr_import"])
        usd_inr_export = float(ev["usd_inr_export"])
        gold_fix = None
        silver_fix = None
        fix_date = None
        tariff_notif = None
        eram_notif = ev["eram_notification"]

    gold_troy_oz = round(gold_tariff * GOLD_10G_TO_TROY_OZ, 6)
    silver_troy_oz = round(silver_tariff * SILVER_KG_TO_TROY_OZ, 6)

    rate = duty_rate_for(ev["effective"])
    gold_value, gold_duty, silver_value, silver_duty = derive(
        gold_tariff, silver_tariff, usd_inr_import, rate)

    if prev is None:
        gold_chg = None
        silver_chg = None
    else:
        gold_chg = float(_chg(gold_duty, prev["gold_duty_inr_kg"]))
        silver_chg = float(_chg(silver_duty, prev["silver_duty_inr_kg"]))

    source_url = ev.get("source_url")
    source_kind = ev.get("source_kind")
    if source_url is not None and not str(source_url).startswith("https://"):
        die("source_url must be an https URL or null")
    if source_kind is not None and source_kind not in SOURCE_KINDS:
        die(f"source_kind must be one of {sorted(SOURCE_KINDS)} or null")

    return {
        "published": ev["published"],
        "effective": ev["effective"],
        "event": EVENT_LABELS[event],
        "tariff_notification": tariff_notif,
        "eram_notification": eram_notif,
        "gold_tariff_usd_10g": gold_tariff,
        "silver_tariff_usd_kg": silver_tariff,
        "usd_inr_import": usd_inr_import,
        "usd_inr_export": usd_inr_export,
        "gold_london_fix_usd_oz": gold_fix,
        "gold_tariff_usd_troy_oz": gold_troy_oz,
        "silver_london_fix_usd_oz": silver_fix,
        "silver_tariff_usd_troy_oz": silver_troy_oz,
        "fix_date": fix_date,
        "gold_value_inr_kg": float(gold_value),
        "gold_duty_inr_kg": float(gold_duty),
        "gold_duty_change_inr_kg": gold_chg,
        "silver_value_inr_kg": float(silver_value),
        "silver_duty_inr_kg": float(silver_duty),
        "silver_duty_change_inr_kg": silver_chg,
        "source_url": source_url,
        "source_kind": source_kind,
        "notice_url": source_url,
        "duty_rate": float(rate),
    }


def validate_append(rows, row):
    """Sanity checks before appending `row` after `rows`."""
    keys = [k for k, _ in COLUMNS]
    if [k for k in row] != keys:
        die("row keys do not match the 24-column schema")
    try:
        pub = datetime.strptime(row["published"], "%Y-%m-%d").date()
        eff = datetime.strptime(row["effective"], "%Y-%m-%d").date()
    except ValueError:
        die("published/effective must be YYYY-MM-DD")
    if eff < pub:
        die("effective date is before published date")
    if rows:
        last = rows[-1]
        last_pub = datetime.strptime(last["published"], "%Y-%m-%d").date()
        if pub < last_pub:
            die(f"published {row['published']} is before last row {last['published']}")
        if (pub, row["event"]) == (last_pub, last["event"]):
            die("duplicate (published, event) row")
        if pub == last_pub and not (
            last["event"] == "Tariff value" and row["event"] == "Exchange rate"
        ):
            die("same-date rows are only allowed as tariff_value then exchange_rate")
        # numeric carry-forward consistency
        if row["event"] == "Exchange rate":
            for k in ("gold_tariff_usd_10g", "silver_tariff_usd_kg",
                      "gold_tariff_usd_troy_oz", "silver_tariff_usd_troy_oz"):
                if row[k] != last[k]:
                    die(f"{k} must carry forward unchanged on exchange_rate rows")
        # recompute derived columns from raw inputs (Decimal, half-up),
        # using the row's own duty rate
        if row["duty_rate"] not in (0.06, 0.15):
            die(f"duty_rate must be 0.06 or 0.15, got {row['duty_rate']}")
        gv, gd, sv, sd = derive(row["gold_tariff_usd_10g"],
                                row["silver_tariff_usd_kg"],
                                row["usd_inr_import"],
                                row["duty_rate"])
        if abs(float(gv) - row["gold_value_inr_kg"]) > 0.005:
            die("gold_value_inr_kg does not recompute")
        if abs(float(sv) - row["silver_value_inr_kg"]) > 0.005:
            die("silver_value_inr_kg does not recompute")
        if abs(float(gd) - row["gold_duty_inr_kg"]) > 0.005:
            die("gold_duty_inr_kg does not recompute")
        if abs(float(sd) - row["silver_duty_inr_kg"]) > 0.005:
            die("silver_duty_inr_kg does not recompute")
        gc = float(_chg(row["gold_duty_inr_kg"], last["gold_duty_inr_kg"]))
        if abs(gc - row["gold_duty_change_inr_kg"]) > 0.005:
            die("gold_duty_change_inr_kg does not recompute")
        sc = float(_chg(row["silver_duty_inr_kg"], last["silver_duty_inr_kg"]))
        if abs(sc - row["silver_duty_change_inr_kg"]) > 0.005:
            die("silver_duty_change_inr_kg does not recompute")
    for k in ("gold_tariff_usd_10g", "silver_tariff_usd_kg",
              "usd_inr_import", "usd_inr_export"):
        v = row[k]
        if not isinstance(v, (int, float)) or v <= 0:
            die(f"{k} must be a positive number")
    if not (500 <= row["gold_tariff_usd_10g"] <= 6000):
        die("gold tariff outside plausible range")
    if not (500 <= row["silver_tariff_usd_kg"] <= 12000):
        die("silver tariff outside plausible range")
    if not (50 <= row["usd_inr_import"] <= 160):
        die("USD/INR import rate outside plausible range")
    su, sk = row["source_url"], row["source_kind"]
    if su is not None and not str(su).startswith("https://"):
        die("source_url must be an https URL or null")
    if sk is not None and sk not in SOURCE_KINDS:
        die("source_kind must be 'notice' or 'portal' or null")
    nu = row["notice_url"]
    if nu is not None and not str(nu).startswith("https://"):
        die("notice_url must be an https URL or null")
    if (nu is None) != (su is None) or nu != su:
        die("notice_url must equal source_url")


def write_outputs(tracker_dir, rows, generated_at):
    latest = {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "duty_rate": DUTY_RATE,
        "row_count": len(rows),
        "source_notes": ("Tariff values: CBIC 'Fixation of Tariff Value' notifications "
                         "amending 36/2001-Customs (N.T.). Exchange rates: ICEGATE ERAM "
                         "notifications. London fix: LBMA gold PM / silver price."),
        "columns": [h for _, h in COLUMNS],
        "rows": rows,
    }
    with open(os.path.join(tracker_dir, "latest.json"), "w") as f:
        json.dump(latest, f, indent=2)
        f.write("\n")
    with open(os.path.join(tracker_dir, "latest.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([h for _, h in COLUMNS])
        for r in rows:
            w.writerow(["" if r[k] is None else r[k] for k, _ in COLUMNS])
    last = rows[-1]
    state = {
        "duty_rate": DUTY_RATE,
        "row_count": len(rows),
        "last_row_published": last["published"],
        "last_row_event": last["event"],
        "last_tariff_notification": next(
            (r["tariff_notification"] for r in reversed(rows) if r["tariff_notification"]), None),
        "last_eram_notification": next(
            (r["eram_notification"] for r in reversed(rows) if r["eram_notification"]), None),
        "updated_at": generated_at,
    }
    with open(os.path.join(tracker_dir, "state.json"), "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")
    return state


def cmd_append(args):
    tracker_dir = os.path.join(args.repo_dir, "customs-duty-tracker")
    os.makedirs(tracker_dir, exist_ok=True)
    latest_path = os.path.join(tracker_dir, "latest.json")
    if os.path.exists(latest_path):
        with open(latest_path) as f:
            data = json.load(f)
        if data.get("schema") != SCHEMA:
            die(f"unexpected schema in latest.json: {data.get('schema')}")
        rows = data["rows"]
    else:
        rows = []

    ev = {
        "published": args.published,
        "effective": args.effective,
        "event": args.event,
        "tariff_notification": args.tariff_notification,
        "eram_notification": args.eram_notification,
        "gold_tariff_usd_10g": args.gold_tariff,
        "silver_tariff_usd_kg": args.silver_tariff,
        "usd_inr_import": args.usd_inr_import,
        "usd_inr_export": args.usd_inr_export,
        "gold_london_fix_usd_oz": args.gold_fix,
        "silver_london_fix_usd_oz": args.silver_fix,
        "fix_date": args.fix_date,
        "source_url": args.source_url,
        "source_kind": args.source_kind,
    }
    prev = rows[-1] if rows else None
    row = compute_row(prev, ev)
    validate_append(rows, row)
    rows.append(row)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = write_outputs(tracker_dir, rows, generated_at)
    print(f"appended {row['published']} {row['event']} "
          f"(gold duty {row['gold_duty_inr_kg']:,.2f}, "
          f"silver duty {row['silver_duty_inr_kg']:,.2f}); "
          f"rows={len(rows)}")


def main():
    ap = argparse.ArgumentParser(description="Append a row to the customs-duty tracker.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("append", help="append one event row")
    a.add_argument("--repo-dir", default=".",
                   help="repo root (default: current directory)")
    a.add_argument("--published", required=True, help="YYYY-MM-DD notification date")
    a.add_argument("--effective", required=True, help="YYYY-MM-DD effective date")
    a.add_argument("--event", required=True, choices=["tariff_value", "exchange_rate"])
    a.add_argument("--tariff-notification", default=None,
                   help='e.g. "76/2026-Customs (N.T.)" (tariff_value only)')
    a.add_argument("--eram-notification", default=None,
                   help='e.g. "28/2026" (exchange_rate only)')
    a.add_argument("--gold-tariff", type=float, default=None,
                   help="USD per 10g (tariff_value only)")
    a.add_argument("--silver-tariff", type=float, default=None,
                   help="USD per kg (tariff_value only)")
    a.add_argument("--usd-inr-import", type=float, default=None)
    a.add_argument("--usd-inr-export", type=float, default=None)
    a.add_argument("--gold-fix", type=float, default=None,
                   help="LBMA gold PM fix USD/oz (tariff_value only)")
    a.add_argument("--silver-fix", type=float, default=None,
                   help="LBMA silver fix USD/oz (tariff_value only)")
    a.add_argument("--fix-date", default=None,
                   help="YYYY-MM-DD of the fix (default: published, tariff_value only)")
    a.add_argument("--source-url", default=None,
                   help="official link for this notice (https URL or omit)")
    a.add_argument("--source-kind", default=None, choices=["notice", "portal"],
                   help="'notice' opens the notice itself; 'portal' opens the "
                        "official listing portal (find the notice number there)")
    args = ap.parse_args()
    if args.cmd == "append":
        cmd_append(args)


if __name__ == "__main__":
    main()
