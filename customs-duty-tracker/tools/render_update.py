#!/usr/bin/env python3
"""Render customs-duty-tracker/latest.md from latest.json.

Writes the human-readable update (values + commentary + verify links) for
the newest event row, in the agreed update style. Deterministic: the
commentary is derived from the row and its predecessor, never hand-written.

Usage: render_update.py --tracker-dir <customs-duty-tracker dir>
"""

import argparse
import json
import os
from datetime import datetime

ICEGATE_SCREEN = "https://foservices.icegate.gov.in/#/services/notifyPublishScreen"
CBIC_PORTAL = "https://taxinformation.cbic.gov.in/"
LBMA_GOLD = "https://prices.lbma.org.uk/json/gold_pm.json"
LBMA_SILVER = "https://prices.lbma.org.uk/json/silver.json"


def inr(x, decimals=2):
    neg = x < 0
    s = f"{abs(x):.{decimals}f}"
    i, _, f = s.partition(".")
    if len(i) > 3:
        tail, head = i[-3:], i[:-3]
        parts = []
        while head:
            parts.append(head[-2:])
            head = head[:-2]
        i = ",".join(reversed(parts)) + "," + tail
    out = "\u20b9" + i + (("." + f) if decimals else "")
    return ("-" if neg else "") + out


def inr_trim(x):
    s = inr(x, 2)
    return s[:-3] if s.endswith(".00") else s


def inr_signed_trim(x):
    return ("+" if x >= 0 else "-") + inr_trim(abs(x))


def usd_int(x):
    return f"${x:,.0f}"


def dstr(iso):
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%-d %b %Y")


def commentary(last, prev, rows):
    parts = []
    rate_changed = bool(
        prev and last["usd_inr_import"] != prev["usd_inr_import"])
    tariff_changed = bool(
        prev and (last["gold_tariff_usd_10g"] != prev["gold_tariff_usd_10g"]
                  or last["silver_tariff_usd_kg"] != prev["silver_tariff_usd_kg"]))
    if rate_changed:
        d = last["usd_inr_import"] - prev["usd_inr_import"]
        move = "weakened" if d > 0 else "strengthened"
        verb = "rose" if d > 0 else "fell"
        parts.append(
            f"The rupee {move} \u2014 the import rate {verb} \u20b9{abs(d):.2f} "
            f"to {last['usd_inr_import']:.2f}.")
    if tariff_changed:
        parts.append(
            f"CBIC revised tariff values: gold "
            f"{usd_int(prev['gold_tariff_usd_10g'])} \u2192 "
            f"{usd_int(last['gold_tariff_usd_10g'])}/10g, silver "
            f"{usd_int(prev['silver_tariff_usd_kg'])} \u2192 "
            f"{usd_int(last['silver_tariff_usd_kg'])}/kg.")
    if rate_changed and not tariff_changed:
        tn = tp = None
        for r in reversed(rows[:-1]):
            if r["event"] == "Tariff value":
                tn, tp = r["tariff_notification"], r["published"]
                break
        since = f" since {dstr(tp)} ({tn})" if tn else ""
        parts.append(
            f"Tariff values are unchanged{since}, so the entire duty move "
            "comes from the exchange rate.")
    if tariff_changed and not rate_changed:
        parts.append(
            "The exchange rate is unchanged, so the duty move comes from "
            "the tariff revision alone.")
    if not rate_changed and not tariff_changed and prev:
        parts.append(
            "Neither tariff values nor the exchange rate moved versus the "
            "previous event.")
    per10 = last["gold_duty_inr_kg"] / 100
    parts.append(
        f"Gold duty now works out to about {inr(per10, 0)} per 10g.")
    return " ".join(parts)


def metal_line(name, tkey, unit, duty_key, chg_key, last, prev):
    t1 = last[tkey]
    t0 = prev[tkey] if prev else None
    if t0 is None:
        tag = f"{usd_int(t1)}{unit}"
    elif t0 == t1:
        tag = f"{usd_int(t1)}{unit} (no change)"
    else:
        tag = f"{usd_int(t0)} \u2192 {usd_int(t1)}{unit}"
    chg = last[chg_key]
    chg_s = f" ({inr_signed_trim(chg)})" if chg is not None else ""
    return f"- {name}: tariff {tag} \u00b7 duty {inr_trim(last[duty_key])}/kg{chg_s}"


def render(rows):
    last = rows[-1]
    prev = rows[-2] if len(rows) > 1 else None

    if last["event"] == "Exchange rate":
        label = f"ERAM {last['eram_notification']}"
        event_line = f"Exchange-rate circular {last['eram_notification']}"
        move_line = (f"USD/INR import: {prev['usd_inr_import']:.2f} \u2192 "
                     f"{last['usd_inr_import']:.2f}") if prev else ""
        verify = (f"- [ICEGATE exchange-rate notifications]({ICEGATE_SCREEN})"
                  f" \u2014 find {last['eram_notification']}, Download PDF.")
    else:
        label = last["tariff_notification"]
        event_line = f"Tariff-value notification {last['tariff_notification']}"
        move_line = ""  # tariff moves show in the metal lines below
        verify = (f"- [CBIC tax information portal]({CBIC_PORTAL})"
                  f" \u2014 Non-Tariff notifications; "
                  f"search {last['tariff_notification']}.")
        if last.get("gold_london_fix_usd_oz"):
            verify += (f"\n- [LBMA gold fix]({LBMA_GOLD}) / "
                       f"[LBMA silver fix]({LBMA_SILVER})"
                       " \u2014 full history, v[0] is USD.")

    lines = [
        f"# Customs duty update \u2014 {label}",
        "",
        f"**Effective:** {dstr(last['effective'])} "
        f"(published {dstr(last['published'])})",
        f"**Event:** {event_line}",
    ]
    if move_line:
        lines.append(f"**{move_line}**")
    lines += [
        "",
        metal_line("Gold", "gold_tariff_usd_10g", "/10g",
                   "gold_duty_inr_kg", "gold_duty_change_inr_kg", last, prev),
        metal_line("Silver", "silver_tariff_usd_kg", "/kg",
                   "silver_duty_inr_kg", "silver_duty_change_inr_kg", last, prev),
        "",
        "## Why it moved",
        "",
        commentary(last, prev, rows),
        "",
        "## Verify",
        "",
        verify,
        "",
        "---",
        "*Generated from `latest.json` (schema customs-tracker.v1). "
        "Machine-owned \u2014 do not hand-edit.*",
        "",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracker-dir", required=True)
    args = ap.parse_args()
    with open(os.path.join(args.tracker_dir, "latest.json")) as f:
        latest = json.load(f)
    md = render(latest["rows"])
    out = os.path.join(args.tracker_dir, "latest.md")
    with open(out, "w") as f:
        f.write(md)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
