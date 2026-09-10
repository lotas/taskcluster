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

A REPORT METRIC IS SHOWN AND NEVER DECIDES. A metric can carry `role: report`,
and such a metric is computed and printed beside the gates but is excluded from
the verdict AND from the day-consistency count. Each of the three has its own
reason, and they are NOT the same reason:

  * `p90_miss_tail_guarded` -- the `30m+` miss RATE is `1 - coverage` over every
    eligible row in that bucket, and the bucket is chosen by the OUTCOME:
    `actual > 30m` selects exceedances by construction. So the rate is not a
    calibration measure, and it goes to 0 for any model that inflates its
    interval far enough. Gating on it pays for inflation.
  * `p90_miss_severity_tail` -- the MEAN excess over every `30m+` row, zero
    where the served p90 covered the actual, so it is measured over a fixed
    population and two models ARE comparable on it (an earlier version divided
    by each model's own miss count, which is a denominator the model's errors
    chose). It is a report for two other reasons: it is EVALUATOR-ONLY, with no
    trainer counterpart to cross-check it, and the `30m+` population is itself
    outcome-selected in the same way the miss rate above is.
  * `interval_width_guarded` -- not gameable by inflation at all; inflation
    makes it WORSE. It is a report because it is UNVERIFIED.

The last two are additionally EVALUATOR-ONLY (see `metrics`' docstring bullets):
the trainer emits no counterpart, so the two-route cross-check that backs every
gate does not cover them, and a bar nothing independently reproduces is a bar on
trust. Gating on any of the three is the wrong trade, and dropping them loses the
alert; `report` is the third option -- visible, so a tail regression is
impossible to miss, and unable to buy a `go`. Because it never decides, a report
metric whose JUDGEMENT cannot be reached carries `passed: None` (and `value:
None` if the value itself is missing) rather than raising; a GATE in that
position is still refused, since leniency there would pass an unchecked bar.

INCONCLUSIVE IS "NOT ENOUGH ROWS TO JUDGE", WHICH IS NOT "NO VALUE". A metric
can carry `min_eligible_n`, and a slice with fewer eligible rows than that is
reported as INCONCLUSIVE: the value IS computed and shown, because a reader
still wants the number, but no bar is applied to it. The distinction from the
paragraph above matters because the two look the same in a table and are not: a
metric with no value at all was measured over nothing, while an inconclusive one
was measured over too little. The `30m+` bucket is the case this exists for --
on a quiet day it can hold a handful of rows, and a one-row bucket rendering as
`ok (report)` beside a 40,000-row one reads as evidence when it is noise. A
GATE below its minimum is REFUSED, for the reason every gate refusal has: too
few rows is not evidence a bar was met, so it must not be able to buy a `go`.
Every per-metric entry also carries `eligible_n`, minimum or not, because "how
many rows is this over?" is a question a reader has about all of them.
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
    # v2: the SERVED p90 (level-aware guardrail applied), scored properly.
    "pinball_p90_guarded": metrics_mod.pinball_p90_guarded,
    "p90_coverage_guarded": metrics_mod.coverage_guarded,
    "p90_miss_tail_guarded": metrics_mod.p90_miss_guarded,
    "p90_miss_severity_tail": metrics_mod.miss_severity_guarded,
    "interval_width_guarded": metrics_mod.interval_width_guarded,
}

# The subset of `VALUE_OF` that scores the SERVED p90, which is a function of the
# BASELINE's level and sample size (`metrics.guarded_p90`) and not of the model's
# output alone. Naming any one of these is what makes a baseline export carrying
# `bl_*_level` / `bl_*_sample_size` a REQUIREMENT rather than a convenience --
# `evaluate` refuses an older export instead of scoring it with no floor. Defined
# HERE, beside the table it is a subset of, so the two cannot drift.
GUARDED_METRICS = ("pinball_p90_guarded", "p90_coverage_guarded",
                   "p90_miss_tail_guarded", "p90_miss_severity_tail",
                   "interval_width_guarded")


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


def _too_few(name, spec, eligible):
    """The `min_eligible_n` check, as a message or None.

    Separate from `_judge` only so the same sentence is not written twice: the
    aggregate judgement and the per-day consistency check both need it, and a
    minimum enforced in one place and not the other is a floor with a hole in
    it.
    """
    minimum = spec.get("min_eligible_n")
    if minimum is None or eligible >= minimum:
        return None
    return (f"INCONCLUSIVE: metric {name!r} has {eligible} eligible rows, the"
            f" contract requires at least {minimum}; too few rows is not"
            f" evidence a bar was met.")


def _judge(name, spec, value, base, eligible):
    """`(passed, measured, inconclusive)`; `(None, None, ...)` when unjudgeable.

    The one place "a gate raises, a report nulls" is stated. All three ways a
    judgement can fail to exist -- no eligible rows at all, too FEW eligible
    rows to judge on, and a bar that cannot be evaluated against this
    baseline -- go through here, so none can acquire another's leniency by being
    handled somewhere else. `inconclusive` distinguishes the middle one for the
    reader; the first two are both refusals for a gate.
    """
    inconclusive = False
    try:
        if value is None:
            raise VerdictError(
                f"metric {name!r} has no eligible rows, so it has no value."
                f" Refusing rather than passing it: 'there were no rows to"
                f" check' is not evidence that a bar was met.")
        too_few = _too_few(name, spec, eligible)
        if too_few is not None:
            inconclusive = True
            raise VerdictError(too_few)
        passed, measured = _passed(spec, value, base)
        return passed, measured, False
    except VerdictError:
        if spec.get("role", "gate") == "gate":
            raise
        return None, None, inconclusive


def _counts_for(name, counts, bucket):
    """The counts one metric is read from, bucket resolved.

    Written ONCE, and the refusal message with it: the value and the eligible
    count must come from the SAME slice, and two copies of this lookup is how
    one of them would come to read the aggregate while the other read a bucket.
    """
    if bucket is None:
        return counts
    resolved = (counts.get("buckets") or {}).get(bucket)
    if resolved is None:
        raise VerdictError(
            f"the contract's metric {name!r} names bucket {bucket!r}, which"
            f" these numbers do not carry. `metrics.WAIT_BUCKETS` owns that"
            f" vocabulary; a contract naming a bucket outside it is refused"
            f" here rather than scored as zero.")
    return resolved


def _fn_for(name, table, what):
    fn = table.get(name)
    if fn is None:
        raise VerdictError(
            f"the contract names metric {name!r}, which this evaluator cannot"
            f" {what}. Known: {sorted(table)}. Refused by name rather than"
            f" skipped -- a skipped metric makes a contract look stricter than"
            f" the judgement it produced.")
    return fn


def _measure(name, counts, bucket):
    """`(value, eligible_n)` for one metric, from ONE bucket-resolved slice."""
    counts = _counts_for(name, counts, bucket)
    value = _fn_for(name, VALUE_OF, "compute")(counts)
    eligible = _fn_for(name, metrics_mod.ELIGIBLE_OF, "count rows for")(counts)
    return value, eligible


def _value(name, counts, bucket):
    """The value alone. Kept for callers that do not need the row count."""
    return _measure(name, counts, bucket)[0]


def decide(contract, *, model, baseline=None):
    """`{verdict, metrics, consistency}` for one contract and one result.

    `model` and `baseline` are the shapes `metrics.compute` returns.
    """
    per_metric = {}
    gate_names = []
    for name, spec in sorted(contract["metrics"].items()):
        bucket = spec.get("bucket")
        role = spec.get("role", "gate")
        if role == "gate":
            gate_names.append(name)
        value, eligible = _measure(
            name, model["aggregate"] if bucket is None else model, bucket)
        base = None
        if baseline is not None:
            base = _value(name, baseline["aggregate"] if bucket is None
                          else baseline, bucket)
        ok, measured, inconclusive = _judge(name, spec, value, base, eligible)
        per_metric[name] = {
            # `value` is kept even when the judgement was not reached, so the
            # reader sees the number and can see it is over too few rows.
            "value": value, "baseline": base, "measured": measured,
            "bar": spec["bar"], "direction": spec["direction"],
            "passed": None if ok is None else bool(ok),
            "eligible_n": eligible,
        }
        if bucket is not None:
            per_metric[name]["bucket"] = bucket
        if role != "gate":
            per_metric[name]["role"] = role
        if inconclusive:
            # Recorded only when True, so a reader who sees the key knows it
            # means something -- and `qfd`'s pin accepts it on the same terms.
            per_metric[name]["inconclusive"] = True

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
            # Bucket metrics are aggregate-only; report metrics never judge.
            if (spec.get("bucket") is not None
                    or spec.get("role", "gate") != "gate"):
                continue
            value, eligible = _measure(name, model["per_day"][day], None)
            if value is None or _too_few(name, spec, eligible) is not None:
                # A day below the minimum does NOT pass, and does not raise
                # either: the aggregate is what a contract's bars are stated
                # over, and a single thin day must not turn a whole result into
                # an error. Conservative in the direction that matters -- it can
                # only cost a `go`, never buy one.
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

    verdict = "go" if (all(per_metric[n]["passed"] for n in gate_names)
                       and len(days_passed) >= required) else "no-go"
    return {
        "verdict": verdict,
        "metrics": per_metric,
        "consistency": {"days_required": required,
                        "days_passed": len(days_passed),
                        "days": days_passed, "holdout_days": days},
    }
