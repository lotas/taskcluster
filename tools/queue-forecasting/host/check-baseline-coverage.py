#!/usr/bin/env python3
"""Pre-promotion validation of a staged baseline set. Protocol §2.5 / §6.

Two checks, and a set is promotable only if both pass:

1. TRAINING-ROW COVERAGE. For every cohort the package must serve, every run
   whose pending_at falls in the cohort's train+validation window must have a
   row in the staged NDJSON. The trainer fills a missing baseline row with 0.0
   rather than dropping it (`_clean_baseline`), which silently redefines the
   residual target, so one missing row is one row too many.

2. HISTORY COMPLETENESS, against a PRESERVED input. The database inventory
   cannot tell whether a day's 7-day resolved history is complete (a task
   deleted for its task_created age may have resolved inside a later window).
   What can be checked: rows the promoted baseline 9c150d75 computed when the
   history WAS complete must be reproduced exactly by the new export on every
   row whose 7-day history contains no date on which the two exclusion lists
   differ. A row that disagrees means the history behind it has changed --
   i.e. rows were lost -- and the export is not the record it claims to be.

Usage:
  check-baseline-coverage.py --ndjson STAGE/baseline_predictions.ndjson \
      --exclude-dates 2026-07-04,2026-08-09 \
      --cohort 2026-08-20=/var/lib/qf-extracts/<975aea71...>/runs.parquet \
      --cohort 2026-08-27=/var/lib/qf-extracts/<bd29b39a...>/runs.parquet ... \
      [--reference /var/lib/qf-baselines/<9c150d75...>/baseline_predictions.ndjson \
       --reference-exclude-dates <its MANIFEST.json exclude_dates, comma-separated>] \
      [--holdout-days 5 --validation-days 1 --lookback-days 14]
Exit 0 only when every check passes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

import numpy as np
import pyarrow.compute as pc
import pyarrow.parquet as pq

HISTORY_DAYS = 7           # src/predictor.js: resolved_at > date - INTERVAL '7 days'
VALUE_KEYS = ("bl_wait_p50", "bl_wait_p90", "bl_wait_level", "bl_wait_sample_size")


def read_ndjson(path):
    """{(task_id, run_id): (pending_day, values)}; refuses a torn last line."""
    rows = {}
    with open(path) as fh:
        for i, line in enumerate(fh, 1):
            try:
                r = json.loads(line)
            except ValueError:
                sys.exit(f"{path}:{i}: not JSON -- a truncated export")
            rows[(r["task_id"], int(r["run_id"]))] = (
                str(r["pending_at"])[:10], tuple(r.get(k) for k in VALUE_KEYS))
    return rows


def train_window(as_of, holdout, validation, lookback):
    start = as_of - dt.timedelta(days=holdout + validation + lookback)
    end = as_of - dt.timedelta(days=holdout)      # exclusive: holdout starts here
    return start, end


def check_cohort(label, runs_path, ndjson, window):
    start, end = window
    t = pq.read_table(runs_path, columns=["task_id", "run_id", "pending_at"])
    pending = t.column("pending_at").cast("timestamp[us]").to_pylist()
    sel = [start <= p.replace(tzinfo=None) < end for p in pending]
    tids = t.column("task_id").to_pylist()
    rids = t.column("run_id").to_pylist()
    missing_by_day = {}
    n = 0
    for keep, tid, rid, p in zip(sel, tids, rids, pending):
        if not keep:
            continue
        n += 1
        if (tid, int(rid)) not in ndjson:
            d = p.strftime("%Y-%m-%d")
            missing_by_day[d] = missing_by_day.get(d, 0) + 1
    return {"cohort": label, "train_window": [start.isoformat(), end.isoformat()],
            "train_rows": n, "missing_rows": sum(missing_by_day.values()),
            "missing_by_day": dict(sorted(missing_by_day.items()))}


def check_reference(new, ref, new_excl, ref_excl):
    differing = sorted(set(new_excl) ^ set(ref_excl))
    diff_days = {dt.date.fromisoformat(d) for d in differing}

    def comparable(day_str):
        d = dt.date.fromisoformat(day_str)
        hist = {d - dt.timedelta(days=k) for k in range(0, HISTORY_DAYS + 1)}
        return not (hist & diff_days)

    compared = disagree = absent = 0
    disagree_by_day = {}
    for key, (day, vals) in ref.items():
        if not comparable(day):
            continue
        got = new.get(key)
        if got is None:
            absent += 1
            continue
        compared += 1
        if got[1] != vals:
            disagree += 1
            disagree_by_day[day] = disagree_by_day.get(day, 0) + 1
    return {"exclusion_dates_differing": differing, "reference_rows_comparable": compared,
            "reference_rows_absent_from_new": absent, "rows_disagreeing": disagree,
            "disagreeing_by_day": dict(sorted(disagree_by_day.items()))}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ndjson", required=True)
    ap.add_argument("--exclude-dates", default="")
    ap.add_argument("--cohort", action="append", default=[], metavar="AS_OF=runs.parquet")
    ap.add_argument("--reference")
    ap.add_argument("--reference-exclude-dates", default="")
    ap.add_argument("--holdout-days", type=int, default=5)
    ap.add_argument("--validation-days", type=int, default=1)
    ap.add_argument("--lookback-days", type=int, default=14)
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    new = read_ndjson(a.ndjson)
    out = {"ndjson_rows": len(new), "cohorts": [], "reference": None}
    ok = True
    for spec in a.cohort:
        as_of_s, _, path = spec.partition("=")
        as_of = dt.datetime.fromisoformat(as_of_s)
        r = check_cohort(as_of_s, path, new, train_window(as_of, a.holdout_days, a.validation_days, a.lookback_days))
        out["cohorts"].append(r)
        ok &= r["missing_rows"] == 0
        print(f"cohort as_of {as_of_s}: train rows {r['train_rows']}, missing {r['missing_rows']}"
              + (f" by day {r['missing_by_day']}" if r["missing_rows"] else ""))
    if a.reference:
        ref = read_ndjson(a.reference)
        r = check_reference(new, ref, [d for d in a.exclude_dates.split(",") if d],
                            [d for d in a.reference_exclude_dates.split(",") if d])
        out["reference"] = r
        ok &= r["rows_disagreeing"] == 0 and r["reference_rows_absent_from_new"] == 0
        print(f"reference: {r['reference_rows_comparable']} comparable rows, "
              f"{r['rows_disagreeing']} disagree, {r['reference_rows_absent_from_new']} absent"
              + (f"; exclusion lists differ on {r['exclusion_dates_differing']}" if r["exclusion_dates_differing"] else ""))
        if r["rows_disagreeing"]:
            print(f"  disagreeing by day: {r['disagreeing_by_day']}")
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(out, fh, indent=2, sort_keys=True)
    print("PROMOTABLE" if ok else "NOT PROMOTABLE: history or coverage is incomplete (see above)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
