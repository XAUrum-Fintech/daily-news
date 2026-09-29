#!/usr/bin/env python3
"""One-time import: convert the user's Customs_Duty_Rates.xlsx into the
seed customs-duty-tracker/latest.json (+ latest.csv, state.json).

Recomputes every derived column with src/build_customs_tracker.compute_row
and asserts the results match the sheet (tolerance 0.01), so the seed is
provably the same data.
"""
import importlib.util
import json
import openpyxl
from datetime import datetime

REPO = "/home/hatch/workspace/daily-news"
XLSX = "/home/hatch/workspace/user/files/Customs_Duty_Rates.xlsx"
OUT = "/tmp/tracker-seed"

spec = importlib.util.spec_from_file_location(
    "bct", f"{REPO}/src/build_customs_tracker.py")
bct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bct)

wb = openpyxl.load_workbook(XLSX, read_only=True, data_only=True)
ws = wb["Sheet2"]

EVENT_MAP = {"Tariff value": "tariff_value", "Exchange rate": "exchange_rate"}
rows = []
for r in ws.iter_rows(values_only=True):
    if r[0] is None or not isinstance(r[0], datetime):
        continue
    pub = r[0].strftime("%Y-%m-%d")
    eff = r[1].strftime("%Y-%m-%d")
    ev = {
        "published": pub,
        "effective": eff,
        "event": EVENT_MAP[r[2]],
        "tariff_notification": r[3],
        "eram_notification": r[4],
        "gold_tariff_usd_10g": r[5],
        "silver_tariff_usd_kg": r[6],
        "usd_inr_import": r[7],
        "usd_inr_export": r[8],
        "gold_london_fix_usd_oz": r[9],
        "silver_london_fix_usd_oz": r[11],
        "fix_date": r[13].strftime("%Y-%m-%d") if isinstance(r[13], datetime) else None,
    }
    prev = rows[-1] if rows else None
    if prev is None:
        # first row: FX rates come from the sheet itself (no previous row)
        ev["usd_inr_import"] = r[7]
        ev["usd_inr_export"] = r[8]
    row = bct.compute_row(prev, ev)
    # verify against the sheet's stored values
    sheet = {
        "gold_tariff_usd_troy_oz": r[10], "silver_tariff_usd_troy_oz": r[12],
        "gold_value_inr_kg": r[14], "gold_duty_inr_kg": r[15],
        "gold_duty_change_inr_kg": r[16], "silver_value_inr_kg": r[17],
        "silver_duty_inr_kg": r[18], "silver_duty_change_inr_kg": r[19],
    }
    for k, v in sheet.items():
        mine = row[k]
        if v is None:
            assert mine is None, (pub, k, mine, v)
        else:
            assert abs(mine - v) < 0.01, (pub, k, mine, v)
    # validate append rules too (order, carry-forward, recompute)
    bct.validate_append(rows, row)
    rows.append(row)

print("seed rows:", len(rows))
print("first:", rows[0]["published"], rows[0]["event"], rows[0]["tariff_notification"])
print("last:", rows[-1]["published"], rows[-1]["event"], rows[-1]["eram_notification"])

import os
os.makedirs(OUT, exist_ok=True)
bct.write_outputs(OUT, rows, "2026-09-29T06:50:00Z")
print("wrote", OUT)
# show a sample row for eyeballing
print(json.dumps(rows[-1], indent=2))
