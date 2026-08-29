"""The metric definitions, computed in ONE PASS. Phase 2c Task 21 (2c-2).

WHAT MAKES THIS AN INDEPENDENT DERIVATION (D26), AND WHAT DOES NOT. The trainer
computes these per day, into per-day objects, and then aggregates the counts
(`evaluate.compute_day_metrics` then `aggregate_days`). This computes the same
quantities in a single vectorised pass over all rows, deriving the per-day split
from the same pass. Agreement between the two catches slice disagreement,
day-boundary disagreement, join errors and double counting -- the failures that
actually happen.

It does NOT catch a shared misunderstanding of what MAE is, because the
definitions below are transcribed from `trainer/src/evaluate.py` on purpose: two
different formulas would not be a cross-check, they would be a disagreement with
no arbiter. **This limitation is stated rather than implied away**: "verified
independently" that means less than a reader assumes is worse than no label.

The definitions are transcribed EXACTLY, including the parts that look like
details and are not:

  * `mae_mask` requires both values FINITE. A NaN prediction is excluded rather
    than scored as an error, so `eligible_n` is what the ratio is over.
  * `within_2x` additionally requires both STRICTLY POSITIVE, because the ratio
    is `max(p/t, t/p)` and a zero on either side is not a ratio. A model that
    predicted zero everywhere would otherwise score infinitely badly on a metric
    that is supposed to be bounded, or divide by zero.
  * `p90_coverage` counts `y_true <= p90`, so it is a COVERAGE, and the contract
    checks it as a band. A one-sided reading of it rewards inflation.
  * Counts, never ratios. Nothing here divides: the ratio is computed once, by
    the verdict, from summed counts -- which is what lets a trusted process
    recompute every number from the parts rather than trusting a quotient.
"""
from __future__ import annotations

import numpy as np

# Half-open, matching `evaluate.WAIT_BUCKETS` and `predictor.js`. Transcribed,
# and a test pins them to the trainer's list: a bucket edge that disagrees would
# move rows between buckets and change a tail gate silently.
WAIT_BUCKETS = (
    ("<1m", 0.0, 60.0),
    ("1-5m", 60.0, 300.0),
    ("5-30m", 300.0, 1800.0),
    ("30m+", 1800.0, float("inf")),
)


def _counts(y_true, y_pred, p90=None):
    """Every count for one set of rows. No division anywhere."""
    yt = np.asarray(y_true, dtype=float)
    yp = np.asarray(y_pred, dtype=float)
    mae_mask = np.isfinite(yt) & np.isfinite(yp)
    out = {
        "mae": {"eligible_n": int(mae_mask.sum()),
                "sum_abs_error": float(np.abs(yp[mae_mask]
                                              - yt[mae_mask]).sum())},
    }
    w2x_mask = mae_mask & (yt > 0) & (yp > 0)
    n = int(w2x_mask.sum())
    if n:
        ratio = np.maximum(yp[w2x_mask] / yt[w2x_mask],
                           yt[w2x_mask] / yp[w2x_mask])
        hits = int((ratio <= 2).sum())
    else:
        hits = 0
    out["within_2x"] = {"eligible_n": n, "hit_n": hits}
    if p90 is not None:
        p90 = np.asarray(p90, dtype=float)
        mask = np.isfinite(yt) & np.isfinite(p90)
        out["p90_coverage"] = {"eligible_n": int(mask.sum()),
                              "covered_n": int((yt[mask] <= p90[mask]).sum())}
    return out


def _empty_counts(with_p90):
    out = {"mae": {"eligible_n": 0, "sum_abs_error": 0.0},
           "within_2x": {"eligible_n": 0, "hit_n": 0}}
    if with_p90:
        out["p90_coverage"] = {"eligible_n": 0, "covered_n": 0}
    return out


def compute(*, y_true, p50, p90=None, days, buckets=False):
    """`{"aggregate": counts, "per_day": {day: counts}, "buckets": {...}}`.

    ONE PASS over the row set, with the per-day split derived from `days` rather
    than from a re-read. `days` is a per-row array of `YYYY-MM-DD` strings.
    """
    yt = np.asarray(y_true, dtype=float)
    result = {"aggregate": _counts(yt, p50, p90), "per_day": {}}
    days = np.asarray(days)
    for day in sorted(set(days.tolist())):
        sel = days == day
        result["per_day"][str(day)] = _counts(
            yt[sel], np.asarray(p50, dtype=float)[sel],
            None if p90 is None else np.asarray(p90, dtype=float)[sel])
    if buckets:
        result["buckets"] = {}
        for name, lo, hi in WAIT_BUCKETS:
            # Bucketed on the ACTUAL, not the prediction, matching
            # `compute_bucket_metrics`. Bucketing on the prediction would let a
            # model move rows out of the bucket it is bad at.
            sel = np.isfinite(yt) & (yt >= lo) & (yt < hi)
            if not sel.any():
                result["buckets"][name] = _empty_counts(p90 is not None)
                continue
            result["buckets"][name] = _counts(
                yt[sel], np.asarray(p50, dtype=float)[sel],
                None if p90 is None else np.asarray(p90, dtype=float)[sel])
    return result


def aggregate(per_day):
    """Sum per-day counts. The trainer's route, kept HERE so the two can be
    compared: if `compute`'s single pass and this sum-of-days disagree, the
    disagreement is the finding, and it is the one thing a second route can
    actually detect."""
    days = list(per_day)
    if not days:
        return _empty_counts(False)
    has_p90 = "p90_coverage" in per_day[days[0]]
    out = {
        "mae": {"eligible_n": sum(per_day[d]["mae"]["eligible_n"]
                                  for d in days),
                "sum_abs_error": sum(per_day[d]["mae"]["sum_abs_error"]
                                     for d in days)},
        "within_2x": {"eligible_n": sum(per_day[d]["within_2x"]["eligible_n"]
                                        for d in days),
                      "hit_n": sum(per_day[d]["within_2x"]["hit_n"]
                                   for d in days)},
    }
    if has_p90:
        out["p90_coverage"] = {
            "eligible_n": sum(per_day[d]["p90_coverage"]["eligible_n"]
                              for d in days),
            "covered_n": sum(per_day[d]["p90_coverage"]["covered_n"]
                             for d in days)}
    return out


# --- ratios, computed ONCE, from counts ----------------------------------
def mae(counts):
    n = counts["mae"]["eligible_n"]
    return counts["mae"]["sum_abs_error"] / n if n else None


def within_2x(counts):
    n = counts["within_2x"]["eligible_n"]
    return counts["within_2x"]["hit_n"] / n if n else None


def coverage(counts):
    entry = counts.get("p90_coverage")
    if not entry or not entry["eligible_n"]:
        return None
    return entry["covered_n"] / entry["eligible_n"]


def p90_miss(counts):
    """1 - coverage. The tail gate is stated as a MISS rate
    (`trainer-phase2-decision.md`: "<30% broad"), and converting it here rather
    than in the contract keeps the contract in the units the decision document
    used -- a bar transcribed into different units is a bar nobody can check
    against the source."""
    cov = coverage(counts)
    return None if cov is None else 1.0 - cov
