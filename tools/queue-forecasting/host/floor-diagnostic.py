#!/usr/bin/env python3
"""The served-p90 floor diagnostic. Protocol §5 (evaluation-protocol.md).

Reads the per-row `eval.parquet` the evaluator publishes for a scored run
(`/var/lib/qf-eval/<run_id>/out/eval.parquet`, readable by root or the qfeval
group) and answers, on DEVELOPMENT data only, the four questions §5.3 asks:

  * where over-coverage lives, by PREDICTION-TIME bucket (the pinned baseline's
    p50 cut at the contract's edges), not by the realised wait;
  * whether the tail is under-covered raw and rescued by the floor;
  * what a floor restricted to the 5-30m and 30m+ prediction-time buckets
    would do to coverage, pinball and width (the "candidate policy");
  * what the provisional per-bucket bands would say about each run.

It separates, per §5.2, floor ELIGIBILITY (the evaluator's `guard_applied`,
which is true whenever the baseline level is strong and its sample size meets
the minimum, even when the raw p90 already exceeds the floor) from the floor's
EFFECT (rows whose p90 actually rose because of the baseline floor), the
magnitude of that rise, and the further rise from enforcing p90 >= p50.

EVALUATOR-ONLY numbers, computed from the evaluator's own per-row artifact.
Nothing here is a verdict; nothing here writes into the eval store.

Usage:
  floor-diagnostic.py --eval LABEL=/var/lib/qf-eval/<run>/out/eval.parquet [...]
                      [--runs /var/lib/qf-extracts/<hash>/runs.parquet]  # by-queue table
                      [--bands bands.json] [--top-queues 15] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np
import pyarrow
import pyarrow.compute as pc
import pyarrow.parquet as pq

# Transcribed from evaluator/metrics.py, half-open. A test pins them.
WAIT_BUCKETS = (
    ("<1m", 0.0, 60.0),
    ("1-5m", 60.0, 300.0),
    ("5-30m", 300.0, 1800.0),
    ("30m+", 1800.0, float("inf")),
)
LONG_BUCKETS = ("5-30m", "30m+")       # the candidate policy floors only here
MIN_ELIGIBLE_N = 200                   # protocol §4.2 proposal
# Protocol §5.3 provisional bands. `null` high = no ceiling.
DEFAULT_BANDS = {"<1m": [0.85, None], "1-5m": [0.85, None],
                 "5-30m": [0.85, 0.95], "30m+": [0.85, 0.95]}


def bucket_of(values):
    out = np.full(len(values), "", dtype=object)
    v = np.asarray(values, dtype=float)
    for name, lo, hi in WAIT_BUCKETS:
        out[np.isfinite(v) & (v >= lo) & (v < hi)] = name
    return out


def pinball(yt, q, alpha=0.9):
    d = yt - q
    return float(np.mean(np.maximum(alpha * d, (alpha - 1.0) * d))) if len(yt) else float("nan")


def _quantiles(x):
    if len(x) == 0:
        return {"median": None, "p90": None}
    return {"median": float(np.median(x)), "p90": float(np.percentile(x, 90))}


def load(path):
    t = pq.read_table(path)
    cols = {c: t.column(c).to_numpy(zero_copy_only=False) for c in
            ("y_true", "p50", "p90_raw", "p90_guarded", "bl_p50", "bl_p90",
             "bl_sample_size", "guard_applied", "day")}
    cols["bucket_actual"] = t.column("bucket").to_numpy(zero_copy_only=False)
    cols["task_id"] = t.column("task_id").to_numpy(zero_copy_only=False)
    cols["run_id"] = t.column("run_id").to_numpy(zero_copy_only=False)
    cols["bl_level"] = t.column("bl_level").to_numpy(zero_copy_only=False)
    for k in ("y_true", "p50", "p90_raw", "p90_guarded", "bl_p50", "bl_p90",
              "bl_sample_size"):
        cols[k] = np.asarray(cols[k], dtype=float)
    ga = cols["guard_applied"]
    cols["guard_applied"] = np.array([bool(x) if x is not None else False
                                      for x in ga])
    if not np.isfinite(cols["p90_guarded"]).any():
        sys.exit(f"{path}: p90_guarded is null throughout -- this run was"
                 f" scored under a v1 contract with no served p90; re-evaluate"
                 f" it under a guarded contract first")
    return cols


def served_under_policy(d, long_only):
    """The served p90 under the current rule, or under the candidate policy
    that applies the baseline floor only in LONG prediction-time buckets."""
    applied = d["guard_applied"].copy()
    if long_only:
        applied &= np.isin(d["bucket_key"], LONG_BUCKETS)
    floored = np.where(applied, np.maximum(d["p90_raw"], d["bl_p90"]),
                       d["p90_raw"])
    return np.maximum(d["p50"], floored), applied


def slice_stats(d, sel, served, served_alt):
    yt, p50, raw = d["y_true"][sel], d["p50"][sel], d["p90_raw"][sel]
    g, alt = served[sel], served_alt[sel]
    blp50, blp90 = d["bl_p50"][sel], d["bl_p90"][sel]
    elig = d["guard_applied"][sel]
    n = int(sel.sum())
    if n == 0:
        return {"n": 0}
    bl_served = np.maximum(blp50, blp90)
    # Floor EFFECT, separated from eligibility (§5.2).
    raised_by_floor = elig & (blp90 > raw)
    rise_s = (blp90 - raw)[raised_by_floor]
    rise_ratio = (blp90 / np.where(raw > 0, raw, np.nan))[raised_by_floor]
    after_floor = np.where(elig, np.maximum(raw, blp90), raw)
    raised_by_p50 = p50 > after_floor
    out = {
        "n": n,
        "share_realised_30m_plus": float(np.mean(d["bucket_actual"][sel] == "30m+")),
        "coverage": {
            "raw": float(np.mean(yt <= raw)),
            "guarded": float(np.mean(yt <= g)),
            "baseline_served": float(np.mean(yt <= bl_served)),
            "candidate_policy": float(np.mean(yt <= alt)),
        },
        "pinball_p90": {
            "raw": pinball(yt, raw), "guarded": pinball(yt, g),
            "baseline_served": pinball(yt, bl_served),
            "candidate_policy": pinball(yt, alt),
        },
        "floor": {
            "eligible_rate": float(np.mean(elig)),
            "raised_by_baseline_floor_rate": float(np.mean(raised_by_floor)),
            "raised_by_baseline_floor_among_eligible": (
                float(raised_by_floor.sum() / elig.sum()) if elig.sum() else None),
            "rise_seconds": _quantiles(rise_s),
            "rise_ratio": _quantiles(rise_ratio[np.isfinite(rise_ratio)]),
            "raised_by_p50_floor_rate": float(np.mean(raised_by_p50)),
            "p50_floor_rise_seconds": _quantiles((p50 - after_floor)[raised_by_p50]),
        },
        "width": {
            "raw_mean_s": float(np.mean(raw - p50)),
            "guarded_mean_s": float(np.mean(g - p50)),
            "candidate_policy_mean_s": float(np.mean(alt - p50)),
            "baseline_served_mean_s": float(np.mean(bl_served - blp50)),
            # model width - baseline width = (p90 part) + (p50 part)
            "attribution_vs_baseline": {
                "p90_part_s": float(np.mean(g - bl_served)),
                "p50_part_s": float(np.mean(blp50 - p50)),
            },
        },
    }
    return out


def band_check(cov, band, n):
    if n < MIN_ELIGIBLE_N:
        return "inconclusive"
    lo, hi = band
    if cov < lo:
        return "below"
    if hi is not None and cov > hi:
        return "above"
    return "pass"


def analyse(label, d, bands, runs=None, top_queues=15):
    d["bucket_key"] = bucket_of(d["bl_p50"])
    served, _ = served_under_policy(d, long_only=False)
    served_alt, _ = served_under_policy(d, long_only=True)
    # The evaluator's own served p90 and ours must agree; otherwise the
    # transcription here is wrong and nothing below can be trusted.
    ok = np.isfinite(d["p90_guarded"])
    if not np.allclose(served[ok], d["p90_guarded"][ok], rtol=0, atol=1e-6):
        sys.exit(f"{label}: recomputed served p90 disagrees with the"
                 f" evaluator's p90_guarded on {int((~np.isclose(served[ok], d['p90_guarded'][ok])).sum())}"
                 f" rows; refusing to report")
    rep = {"label": label, "rows": int(len(d["y_true"])),
           "aggregate": slice_stats(d, np.ones(len(d["y_true"]), bool), served, served_alt),
           "by_prediction_time_bucket": {}, "crosstab": {}, "bands": {}}
    for name, _lo, _hi in WAIT_BUCKETS:
        sel = d["bucket_key"] == name
        s = slice_stats(d, sel, served, served_alt)
        rep["by_prediction_time_bucket"][name] = s
        rep["crosstab"][name] = {a: int(((d["bucket_actual"] == a) & sel).sum())
                                 for a, _l, _h in WAIT_BUCKETS}
        if s["n"]:
            rep["bands"][name] = {
                "band": bands[name],
                "guarded": band_check(s["coverage"]["guarded"], bands[name], s["n"]),
                "candidate_policy": band_check(s["coverage"]["candidate_policy"], bands[name], s["n"]),
            }
    # Realised-wait tail, REPORT ONLY (outcome-conditioned, protocol §4.4).
    tail = d["bucket_actual"] == "30m+"
    if tail.any():
        yt = d["y_true"][tail]
        rep["realised_30m_plus_report"] = {
            "n": int(tail.sum()),
            "miss_raw": float(np.mean(yt > d["p90_raw"][tail])),
            "miss_guarded": float(np.mean(yt > served[tail])),
            "miss_candidate_policy": float(np.mean(yt > served_alt[tail])),
        }
    if runs is not None:
        rep["by_queue"] = by_queue(d, runs, served, served_alt, top_queues)
    return rep


def by_queue(d, runs_path, served, served_alt, top):
    t = pq.read_table(runs_path, columns=["task_id", "run_id", "task_queue_id"])
    key = {(a, int(b)): q for a, b, q in zip(
        t.column("task_id").to_pylist(), t.column("run_id").to_pylist(),
        t.column("task_queue_id").to_pylist())}
    queues = np.array([key.get((a, int(b)), "?") for a, b in
                       zip(d["task_id"].tolist(), d["run_id"].tolist())], dtype=object)
    names, counts = np.unique(queues, return_counts=True)
    order = np.argsort(-counts)[:top]
    out = {}
    for i in order:
        sel = queues == names[i]
        s = slice_stats(d, sel, served, served_alt)
        out[str(names[i])] = {
            "n": s["n"], "share_realised_30m_plus": s["share_realised_30m_plus"],
            "coverage": s["coverage"], "floor_eligible_rate": s["floor"]["eligible_rate"],
            "raised_by_baseline_floor_rate": s["floor"]["raised_by_baseline_floor_rate"],
        }
    return out


def render(rep):
    L = [f"## {rep['label']}  ({rep['rows']} rows)", ""]
    L += ["| prediction-time bucket | n | realised 30m+ share | cov raw | cov guarded | cov baseline | cov candidate | pinball raw | pinball guarded | pinball candidate | eligible | raised by floor | rise median s | raised by p50 | width guarded s | width baseline s |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    rows = list(rep["by_prediction_time_bucket"].items()) + [("all", rep["aggregate"])]
    for name, s in rows:
        if not s["n"]:
            L.append(f"| {name} | 0 | | | | | | | | | | | | | | |")
            continue
        c, p, f, w = s["coverage"], s["pinball_p90"], s["floor"], s["width"]
        med = f["rise_seconds"]["median"]
        L.append(f"| {name} | {s['n']} | {s['share_realised_30m_plus']:.3f} | {c['raw']:.4f} | {c['guarded']:.4f} | {c['baseline_served']:.4f} | {c['candidate_policy']:.4f} | {p['raw']:.1f} | {p['guarded']:.1f} | {p['candidate_policy']:.1f} | {f['eligible_rate']:.3f} | {f['raised_by_baseline_floor_rate']:.3f} | {'' if med is None else f'{med:.0f}'} | {f['raised_by_p50_floor_rate']:.3f} | {w['guarded_mean_s']:.0f} | {w['baseline_served_mean_s']:.0f} |")
    L += ["", "Bands (provisional, protocol §5.3):",
          "| bucket | band | guarded | candidate policy |", "|---|---|---|---|"]
    for name, b in rep["bands"].items():
        L.append(f"| {name} | {b['band']} | {b['guarded']} | {b['candidate_policy']} |")
    L += ["", "Cross-tab: prediction-time bucket (rows) x realised bucket (cols), row counts:",
          "| | " + " | ".join(n for n, _l, _h in WAIT_BUCKETS) + " |",
          "|---|" + "---|" * len(WAIT_BUCKETS)]
    for name, row in rep["crosstab"].items():
        L.append(f"| {name} | " + " | ".join(str(row[a]) for a, _l, _h in WAIT_BUCKETS) + " |")
    if "realised_30m_plus_report" in rep:
        r = rep["realised_30m_plus_report"]
        L += ["", f"Realised 30m+ miss (REPORT ONLY, outcome-conditioned; n={r['n']}): raw {r['miss_raw']:.4f}, guarded {r['miss_guarded']:.4f}, candidate policy {r['miss_candidate_policy']:.4f}"]
    if "by_queue" in rep:
        L += ["", "| queue | n | realised 30m+ share | cov raw | cov guarded | cov candidate | eligible | raised by floor |",
              "|---|---|---|---|---|---|---|---|"]
        for q, s in rep["by_queue"].items():
            c = s["coverage"]
            L.append(f"| {q} | {s['n']} | {s['share_realised_30m_plus']:.3f} | {c['raw']:.4f} | {c['guarded']:.4f} | {c['candidate_policy']:.4f} | {s['floor_eligible_rate']:.3f} | {s['raised_by_baseline_floor_rate']:.3f} |")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--eval", action="append", required=True, metavar="LABEL=PATH")
    ap.add_argument("--runs", help="runs.parquet of the extract, for the by-queue table")
    ap.add_argument("--bands", help="JSON file {bucket: [low, high|null]}")
    ap.add_argument("--top-queues", type=int, default=15)
    ap.add_argument("--json", help="write the full report here")
    args = ap.parse_args(argv)
    bands = DEFAULT_BANDS
    if args.bands:
        with open(args.bands) as fh:
            bands = json.load(fh)
    reports = []
    for spec in args.eval:
        label, _, path = spec.partition("=")
        if not path:
            sys.exit(f"--eval wants LABEL=PATH, got {spec!r}")
        reports.append(analyse(label, load(path), bands, args.runs, args.top_queues))
        print(render(reports[-1]))
        print()
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"buckets": [b[0] for b in WAIT_BUCKETS], "long_buckets": LONG_BUCKETS,
                       "min_eligible_n": MIN_ELIGIBLE_N, "bands": bands,
                       "reports": reports}, fh, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
