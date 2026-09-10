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
  * The GUARDED p90 is the SERVED p90: `guarded_p90` transcribes
    `applyP90Guardrail` from serving -- floor the model p90 with the baseline
    p90 only when the baseline level is strong for the target and its sample
    size is at least `GUARDRAIL_MIN_SAMPLE`, then floor by p50. Scoring only the
    raw p90 judges a number no user is ever shown.
  * `pinball_p90` is the quantile (pinball) loss at alpha=0.9 -- the one score
    that punishes both a missed tail and a needlessly wide one, so a config
    cannot buy coverage with inflation alone.
  * `p90_excess_guarded` is the seconds beyond the served p90, SUMMED over
    misses, and its ratio is that sum over the WHOLE eligible POPULATION --
    every row with a finite actual and a finite served p90, counting zero for
    the rows that were covered -- not a mean among the misses. The denominator
    is the reason: two models miss DIFFERENT rows, so a per-miss mean divides
    each one by a denominator its own errors chose, and a model that eliminates
    a hundred small misses and leaves one large one scores WORSE than the model
    it beat. Over one fixed population it is a one-sided expected excess, so
    model and baseline are comparable. `miss_n` is still emitted, as a
    description of how many rows contributed. A MEAN rather than a median
    because a median is not a sum of per-day parts, and this file emits only
    counts that add up. EVALUATOR-ONLY: the trainer has no counterpart, so the
    two-route comparison does not cover it; it is a report, not a gate.
  * `interval_width_guarded` is `p90_guarded - p50`, summed: how much of the
    calibration was bought with width. Read next to coverage, it is what
    separates a calibrated tail from an inflated one. EVALUATOR-ONLY, like
    `p90_excess_guarded`: the trainer has no counterpart, so the two-route
    comparison does not cover it; it is a report, not a gate.
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


# THE SERVING RULE, transcribed from src/live-predictor/p90-guardrail.js and
# predict.js (WAIT_P90_GUARDRAIL_MIN_SAMPLE / DURATION_P90_GUARDRAIL_MIN_SAMPLE
# are both 20; the strong levels are in wait-p90-guardrail.js and
# duration-p90-guardrail.js). The trainer transcribes the same rule in
# `compute_guarded_p90`; `test_metrics.py` pins the two together. This is what
# a user is served, so it is what a contract that claims to judge the served
# number must score.
GUARDRAIL_MIN_SAMPLE = 20
STRONG_BASELINE_LEVELS = {
    "wait_time": ("queue+priority+bucket",),
    "run_duration": ("metadata_name",),
}


def guarded_p90(*, p50, p90_raw, bl_p90, bl_level, bl_sample_size, target):
    """`(guarded, applied)`: the served p90 and whether the floor fired.

    Floor the raw p90 with the baseline p90 only when the baseline level is
    strong for this target AND its sample size is at least the minimum; then
    floor by p50 so p90 >= p50 always holds. Exactly `applyP90Guardrail`.
    """
    p50 = np.asarray(p50, dtype=float)
    raw = np.asarray(p90_raw, dtype=float)
    blp = np.asarray(bl_p90, dtype=float)
    level = np.asarray(bl_level, dtype=object)
    n = np.asarray(bl_sample_size, dtype=float)
    strong = STRONG_BASELINE_LEVELS[target]
    applied = (np.isin(level, list(strong)) & np.isfinite(blp)
               & np.isfinite(n) & (n >= GUARDRAIL_MIN_SAMPLE))
    guarded = np.where(applied, np.maximum(raw, blp), raw)
    return np.maximum(p50, guarded), applied


def _pinball(yt, q, alpha):
    """Counts for the pinball (quantile) loss at `alpha`. Transcribed from
    the trainer's `evaluate.pinball_loss`: the SUM and the eligible count, never
    the mean."""
    yt = np.asarray(yt, dtype=float)
    q = np.asarray(q, dtype=float)
    mask = np.isfinite(yt) & np.isfinite(q)
    diff = yt[mask] - q[mask]
    loss = np.maximum(alpha * diff, (alpha - 1.0) * diff)
    return {"eligible_n": int(mask.sum()), "sum": float(loss.sum())}


def _counts(y_true, y_pred, p90=None, p90_guarded=None):
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
        out["pinball_p90"] = _pinball(yt, p90, 0.9)
    if p90_guarded is not None:
        g = np.asarray(p90_guarded, dtype=float)
        mask = np.isfinite(yt) & np.isfinite(g)
        out["p90_coverage_guarded"] = {
            "eligible_n": int(mask.sum()),
            "covered_n": int((yt[mask] <= g[mask]).sum())}
        out["pinball_p90_guarded"] = _pinball(yt, g, 0.9)
        # Miss severity: seconds beyond the served p90, summed over misses,
        # over the SAME eligible population coverage is scored on -- `mask`, so
        # `eligible_n` here is `p90_coverage_guarded`'s. The ratio is that sum
        # per eligible row (zero for a covered row), never per miss: a per-miss
        # denominator is chosen by the model's own errors and is not comparable
        # between two models that miss different rows. `miss_n` stays as a
        # description of the population, not as the divisor.
        miss = mask & (yt > g)
        out["p90_excess_guarded"] = {
            "eligible_n": int(mask.sum()),
            "miss_n": int(miss.sum()),
            "sum_excess": float((yt[miss] - g[miss]).sum())}
        # Interval width: how much of the calibration was bought with width.
        # `mask` already requires a finite y_true, so the width is read over the
        # same rows coverage is scored on -- a width averaged over a wider row
        # set than its coverage is not comparable with it.
        wmask = mask & np.isfinite(yp)
        out["interval_width_guarded"] = {
            "eligible_n": int(wmask.sum()),
            "sum_width": float((g[wmask] - yp[wmask]).sum())}
    return out


def _empty_counts(with_p90, with_guarded=False):
    out = {"mae": {"eligible_n": 0, "sum_abs_error": 0.0},
           "within_2x": {"eligible_n": 0, "hit_n": 0}}
    if with_p90:
        out["p90_coverage"] = {"eligible_n": 0, "covered_n": 0}
        out["pinball_p90"] = {"eligible_n": 0, "sum": 0.0}
    if with_guarded:
        out["p90_coverage_guarded"] = {"eligible_n": 0, "covered_n": 0}
        out["pinball_p90_guarded"] = {"eligible_n": 0, "sum": 0.0}
        out["p90_excess_guarded"] = {"eligible_n": 0, "miss_n": 0,
                                     "sum_excess": 0.0}
        out["interval_width_guarded"] = {"eligible_n": 0, "sum_width": 0.0}
    return out


def compute(*, y_true, p50, p90=None, p90_guarded=None, days, buckets=False):
    """`{"aggregate": counts, "per_day": {day: counts}, "buckets": {...}}`.

    ONE PASS over the row set, with the per-day split derived from `days` rather
    than from a re-read. `days` is a per-row array of `YYYY-MM-DD` strings.
    """
    yt = np.asarray(y_true, dtype=float)
    result = {"aggregate": _counts(yt, p50, p90, p90_guarded), "per_day": {}}
    days = np.asarray(days)
    for day in sorted(set(days.tolist())):
        sel = days == day
        result["per_day"][str(day)] = _counts(
            yt[sel], np.asarray(p50, dtype=float)[sel],
            None if p90 is None else np.asarray(p90, dtype=float)[sel],
            None if p90_guarded is None
            else np.asarray(p90_guarded, dtype=float)[sel])
    if buckets:
        result["buckets"] = {}
        for name, lo, hi in WAIT_BUCKETS:
            # Bucketed on the ACTUAL, not the prediction, matching
            # `compute_bucket_metrics`. Bucketing on the prediction would let a
            # model move rows out of the bucket it is bad at.
            sel = np.isfinite(yt) & (yt >= lo) & (yt < hi)
            if not sel.any():
                result["buckets"][name] = _empty_counts(
                    p90 is not None, p90_guarded is not None)
                continue
            result["buckets"][name] = _counts(
                yt[sel], np.asarray(p50, dtype=float)[sel],
                None if p90 is None else np.asarray(p90, dtype=float)[sel],
                None if p90_guarded is None
                else np.asarray(p90_guarded, dtype=float)[sel])
    return result


def aggregate(per_day):
    """Sum per-day counts. The trainer's route, kept HERE so the two can be
    compared: if `compute`'s single pass and this sum-of-days disagree, the
    disagreement is the finding, and it is the one thing a second route can
    actually detect."""
    days = list(per_day)
    if not days:
        return _empty_counts(False)
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
    # Every remaining count is a flat dict of summable fields keyed the same
    # way in every slice, so they are summed one way rather than once each: a
    # hand-written copy per key is a place for one of them to be summed wrong.
    for key, fields in (("p90_coverage", ("eligible_n", "covered_n")),
                        ("pinball_p90", ("eligible_n", "sum")),
                        ("p90_coverage_guarded", ("eligible_n", "covered_n")),
                        ("pinball_p90_guarded", ("eligible_n", "sum")),
                        ("p90_excess_guarded", ("eligible_n", "miss_n",
                                                 "sum_excess")),
                        ("interval_width_guarded", ("eligible_n", "sum_width"))):
        if key in per_day[days[0]]:
            out[key] = {f: sum(per_day[d][key][f] for d in days) for f in fields}
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


def pinball_p90_guarded(counts):
    entry = counts.get("pinball_p90_guarded")
    if not entry or not entry["eligible_n"]:
        return None
    return entry["sum"] / entry["eligible_n"]


def coverage_guarded(counts):
    entry = counts.get("p90_coverage_guarded")
    if not entry or not entry["eligible_n"]:
        return None
    return entry["covered_n"] / entry["eligible_n"]


def p90_miss_guarded(counts):
    """See `p90_miss`: same unit conversion, over the served p90."""
    cov = coverage_guarded(counts)
    return None if cov is None else 1.0 - cov


def miss_severity_guarded(counts):
    """Mean seconds beyond the served p90 PER ELIGIBLE ROW -- the one-sided
    expected excess, counting zero for every row the served p90 covered.

    NOT a mean among the misses. That version divided each model by a
    denominator its own errors selected, so two models that miss different rows
    were not comparable on it and a model that removed many small misses while
    leaving one large one looked worse than the one it beat. Over the fixed
    population (the same rows `coverage_guarded` scores) the comparison is
    between two numbers measured on the same thing.

    None only when nothing is eligible -- which for a REPORT metric is a null,
    not a refusal. Zero misses is a VALUE of 0.0, not an absence.
    """
    entry = counts.get("p90_excess_guarded")
    if not entry or not entry["eligible_n"]:
        return None
    return entry["sum_excess"] / entry["eligible_n"]


def interval_width_guarded(counts):
    entry = counts.get("interval_width_guarded")
    if not entry or not entry["eligible_n"]:
        return None
    return entry["sum_width"] / entry["eligible_n"]


# --- eligible counts, beside the ratios they are the denominator of ------
#
# WHY A SECOND TABLE RATHER THAN A SECOND RETURN VALUE. A ratio's value and the
# number of rows it was computed over are read by different callers: the verdict
# compares the value against a bar, and it separately has to know whether there
# were enough rows for that comparison to mean anything (`min_eligible_n`).
# Returning a pair from every ratio would put the count in the hands of every
# existing caller of `mae()`; a table keyed the same way `verdict.VALUE_OF` is
# keeps the two lookups symmetrical, and a test pins the two key sets together
# so a metric cannot gain a bar without gaining a row count.
#
# Each function returns THE DENOMINATOR ITS RATIO DIVIDES BY -- not "the rows in
# this slice". `p90_miss_severity_tail` divides by `p90_excess_guarded`'s
# eligible count and not by `miss_n`, so that is what is reported for it; a
# floor checked against the wrong count is a floor that does not hold.
def _eligible(key):
    def fn(counts):
        entry = (counts or {}).get(key)
        # 0 when the entry is missing, which is the honest answer: a slice that
        # carries no `p90_coverage_guarded` at all was scored over no eligible
        # rows for it. A `None` here would have to be special-cased by every
        # comparison against a minimum.
        if not isinstance(entry, dict):
            return 0
        n = entry.get("eligible_n")
        return int(n) if isinstance(n, int) and not isinstance(n, bool) else 0
    return fn


ELIGIBLE_OF = {
    "mae": _eligible("mae"),
    "within_2x": _eligible("within_2x"),
    "p90_coverage": _eligible("p90_coverage"),
    "p90_miss_tail": _eligible("p90_coverage"),
    "pinball_p90_guarded": _eligible("pinball_p90_guarded"),
    "p90_coverage_guarded": _eligible("p90_coverage_guarded"),
    "p90_miss_tail_guarded": _eligible("p90_coverage_guarded"),
    "p90_miss_severity_tail": _eligible("p90_excess_guarded"),
    "interval_width_guarded": _eligible("interval_width_guarded"),
}
