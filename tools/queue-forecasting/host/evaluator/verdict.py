"""The oracle. Phase 2c Task 22 (2c-2/2c-3 boundary).

IT EMITS A VERDICT, NEVER A DECISION TO ACT (D27). Nothing here promotes a model,
writes to `trainer/data/models/`, or touches the live predictor -- those are
explicitly outside 2b and 2c, and a judge that could act on its own finding is
not a judge.

EVERY RATIO IS COMPUTED HERE, FROM COUNTS. The candidate's path emits
`eligible_n`, `sum_abs_error`, `hit_n` and `covered_n`; nothing it produces is a
quotient, so nothing it produces can be a quotient that flatters. This is the
property that makes the whole arrangement work, and it was already true of
`trainer/src/evaluate.py` before 2c existed -- had that emitted ratios, this file
would have had to either trust them or re-derive them from the whole trainer.

A METRIC WITH NO BAR IS NOT REPORTED AS PASSING. Every metric in the contract is
evaluated; a metric the contract does not name is not evaluated at all, and a
metric whose value cannot be computed (an empty eligible set) is a REFUSAL rather
than a pass. "There were no rows to check" is not evidence that a bar was met.
"""
from __future__ import annotations

import metrics as metrics_mod

# The metric names this file knows how to evaluate, and the function that turns
# counts into the value the bar is compared against. A contract naming anything
# else is refused BY NAME rather than skipped: silently ignoring a metric would
# make a contract look stricter than the judgement it produced.
VALUE_OF = {
    "mae": metrics_mod.mae,
    "within_2x": metrics_mod.within_2x,
    "p90_coverage": metrics_mod.coverage,
    "p90_miss_tail": metrics_mod.p90_miss,
}


class VerdictError(ValueError):
    """The contract cannot be applied to these numbers."""


def _improvement(kind, value, baseline, direction):
    """How much better than the baseline, in the units the bar is stated in."""
    if baseline is None:
        raise VerdictError(
            "this contract states a bar relative to a baseline, and no baseline"
            " numbers are available. A relative bar with nothing to be relative"
            " to cannot be evaluated, and treating it as met would pass every"
            " run that forgot its baseline.")
    if kind == "relative_improvement":
        if baseline == 0:
            raise VerdictError(
                "the baseline value is zero, so a relative improvement is"
                " undefined")
        # For a lower-is-better metric, improvement is a REDUCTION.
        delta = (baseline - value) if direction == "lower_is_better" \
            else (value - baseline)
        return delta / abs(baseline)
    # absolute_improvement: percentage POINTS, not percent. Reading "5pp" as 5%
    # relative would be a materially looser bar.
    return (baseline - value) if direction == "lower_is_better" \
        else (value - baseline)


def _passed(spec, value, baseline):
    bar = spec["bar"]
    kind = bar["kind"]
    if kind == "band":
        return bar["low"] <= value <= bar["high"], value
    if kind == "absolute":
        return ((value <= bar["value"]) if spec["direction"] == "lower_is_better"
                else (value >= bar["value"])), value
    improvement = _improvement(kind, value, baseline, spec["direction"])
    return improvement >= bar["value"], improvement


def _value(name, counts, bucket):
    if bucket is not None:
        counts = (counts.get("buckets") or {}).get(bucket)
        if counts is None:
            raise VerdictError(
                f"the contract's metric {name!r} names bucket {bucket!r}, which"
                f" these numbers do not carry. `metrics.WAIT_BUCKETS` owns that"
                f" vocabulary; a contract naming a bucket outside it is refused"
                f" here rather than scored as zero.")
    fn = VALUE_OF.get(name)
    if fn is None:
        raise VerdictError(
            f"the contract names metric {name!r}, which this evaluator cannot"
            f" compute. Known: {sorted(VALUE_OF)}. Refused by name rather than"
            f" skipped -- a skipped metric makes a contract look stricter than"
            f" the judgement it produced.")
    return fn(counts)


def decide(contract, *, model, baseline=None):
    """`{verdict, metrics, consistency}` for one contract and one result.

    `model` and `baseline` are the shapes `metrics.compute` returns.
    """
    per_metric = {}
    for name, spec in sorted(contract["metrics"].items()):
        bucket = spec.get("bucket")
        value = _value(name, model["aggregate"] if bucket is None else model,
                       bucket)
        if value is None:
            raise VerdictError(
                f"metric {name!r} has no eligible rows, so it has no value."
                f" Refusing rather than passing it: 'there were no rows to"
                f" check' is not evidence that a bar was met.")
        base = None
        if baseline is not None:
            base = _value(name, baseline["aggregate"] if bucket is None
                          else baseline, bucket)
        ok, measured = _passed(spec, value, base)
        per_metric[name] = {
            "value": value, "baseline": base, "measured": measured,
            "bar": spec["bar"], "direction": spec["direction"],
            "passed": bool(ok),
        }
        if bucket is not None:
            per_metric[name]["bucket"] = bucket

    # CONSISTENCY. Counted on the days where every metric that HAS a per-day
    # value passes -- not on a single metric -- because "consistent across at
    # least 3 of the 5 holdout days" is a statement about the result, and a rule
    # that counted one metric could pass a day the model lost on the others.
    days = sorted(model["per_day"])
    required = contract["consistency"]["days_required"]
    if len(days) != contract["holdout_days"]:
        raise VerdictError(
            f"the prediction set covers {len(days)} holdout day(s) but the"
            f" contract describes {contract['holdout_days']}. A 3-of-5 rule"
            f" applied to 2 days is not the rule that was agreed.")
    day_pass = []
    for day in days:
        ok = True
        for name, spec in sorted(contract["metrics"].items()):
            if spec.get("bucket") is not None:
                continue          # bucket metrics are aggregate-only
            value = _value(name, model["per_day"][day], None)
            if value is None:
                ok = False
                break
            base = None
            if baseline is not None and day in baseline["per_day"]:
                base = _value(name, baseline["per_day"][day], None)
            try:
                passed, _m = _passed(spec, value, base)
            except VerdictError:
                ok = False
                break
            ok = ok and passed
        day_pass.append(day if ok else None)
    days_passed = [d for d in day_pass if d]

    verdict = "go" if (all(m["passed"] for m in per_metric.values())
                       and len(days_passed) >= required) else "no-go"
    return {
        "verdict": verdict,
        "metrics": per_metric,
        "consistency": {"days_required": required,
                        "days_passed": len(days_passed),
                        "days": days_passed, "holdout_days": days},
    }
