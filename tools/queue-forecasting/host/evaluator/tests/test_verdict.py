"""Phase 2c Task 22. The oracle: every ratio computed here, from counts."""
import os
import sys
import unittest

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "shared"))

import contract as contract_mod
import metrics
import verdict


def a_contract(**over):
    body = {
        "schema": 1, "name": "w", "target": "wait_time",
        "baseline_hash": "a" * 64,
        "primary_slice": {"reason_resolved": ["completed"]},
        "metrics": {"mae": {"direction": "lower_is_better",
                            "bar": {"kind": "relative_improvement",
                                    "value": 0.15}}},
        "consistency": {"days_required": 2}, "holdout_days": 3,
    }
    body.update(over)
    return contract_mod.validate(body)


def result(p50_factor, *, ndays=3, n_per_day=20, p90_factor=None, seed=0):
    """A synthetic result. `p50_factor` scales the prediction off the actual, so
    1.0 is a perfect model and 2.0 is twice the truth.

    THE ACTUALS SPAN EVERY BUCKET on purpose. The first draft used
    `lognormal(4, 1)` -- a median around 55 seconds -- so the `30m+` bucket was
    EMPTY and every bucket metric refused for want of eligible rows. A fixture
    that cannot exercise the tail cannot test a tail gate, and the refusal it
    produced looked like a bug in the oracle.
    """
    rng = np.random.default_rng(seed)
    yt, p50, p90, days = [], [], [], []
    for d in range(ndays):
        actual = np.concatenate([
            rng.lognormal(4, 1, n_per_day - 4),          # <1m .. 5-30m
            np.array([2000.0, 4000.0, 9000.0, 30.0]),    # 30m+ and a <1m
        ])
        yt.extend(actual)
        p50.extend(actual * p50_factor)
        p90.extend(actual * (p90_factor if p90_factor else p50_factor * 1.5))
        days.extend([f"2026-08-{d + 1:02d}"] * n_per_day)
    return metrics.compute(y_true=np.array(yt), p50=np.array(p50),
                           p90=np.array(p90), days=np.array(days),
                           buckets=True)


class TestTheRatiosAreComputedHere(unittest.TestCase):
    def test_a_clear_improvement_is_a_go(self):
        # THE CANARY. Without it every no-go below could be a judge that never
        # says go.
        out = verdict.decide(a_contract(), model=result(1.0),
                             baseline=result(2.0))
        self.assertEqual(out["verdict"], "go")
        self.assertTrue(out["metrics"]["mae"]["passed"])

    def test_no_improvement_is_a_no_go(self):
        out = verdict.decide(a_contract(), model=result(2.0),
                             baseline=result(2.0))
        self.assertEqual(out["verdict"], "no-go")

    def test_a_relative_bar_is_measured_as_a_reduction_for_error(self):
        # 15% better MAE means 15% LOWER, and a sign error here would invert
        # every wait-target judgement.
        out = verdict.decide(a_contract(), model=result(1.5),
                             baseline=result(2.0))
        self.assertGreater(out["metrics"]["mae"]["measured"], 0)

    def test_an_absolute_improvement_bar_is_percentage_points(self):
        # "within-2x improves by >=5pp". Reading pp as percent relative would be
        # a materially looser bar.
        c = a_contract(metrics={
            "within_2x": {"direction": "higher_is_better",
                          "bar": {"kind": "absolute_improvement",
                                  "value": 0.05}}})
        model, base = result(1.0), result(3.0)
        out = verdict.decide(c, model=model, baseline=base)
        expected = (metrics.within_2x(model["aggregate"])
                    - metrics.within_2x(base["aggregate"]))
        self.assertAlmostEqual(out["metrics"]["within_2x"]["measured"], expected)

    def test_the_p90_band_is_two_sided(self):
        # A model that never misses its p90 is not calibrated, it is inflated.
        c = a_contract(metrics={"p90_coverage": {
            "direction": "band",
            "bar": {"kind": "band", "low": 0.85, "high": 0.95}}})
        perfect = result(1.0, p90_factor=1000.0)      # covers everything
        out = verdict.decide(c, model=perfect, baseline=result(2.0))
        self.assertEqual(metrics.coverage(perfect["aggregate"]), 1.0)
        self.assertFalse(out["metrics"]["p90_coverage"]["passed"])
        self.assertEqual(out["verdict"], "no-go")

    def test_an_absolute_bar_on_a_bucket_reads_the_bucket(self):
        c = a_contract(metrics={"p90_miss_tail": {
            "direction": "lower_is_better", "bucket": "30m+",
            "bar": {"kind": "absolute", "value": 0.30}}})
        model = result(1.0, p90_factor=1000.0)        # misses nothing
        out = verdict.decide(c, model=model, baseline=None)
        self.assertEqual(out["metrics"]["p90_miss_tail"]["bucket"], "30m+")
        self.assertTrue(out["metrics"]["p90_miss_tail"]["passed"])

    def test_a_relative_bar_with_no_baseline_is_a_refusal(self):
        # Treating it as met would pass every run that forgot its baseline.
        with self.assertRaises(verdict.VerdictError) as cm:
            verdict.decide(a_contract(), model=result(1.0), baseline=None)
        self.assertIn("relative to", str(cm.exception))

    def test_a_metric_with_no_eligible_rows_is_a_refusal_not_a_pass(self):
        # "There were no rows to check" is not evidence that a bar was met.
        empty = metrics.compute(y_true=np.array([np.nan] * 3),
                                p50=np.array([np.nan] * 3),
                                p90=np.array([np.nan] * 3),
                                days=np.array(["2026-08-01", "2026-08-02",
                                               "2026-08-03"]))
        with self.assertRaises(verdict.VerdictError) as cm:
            verdict.decide(a_contract(), model=empty, baseline=empty)
        self.assertIn("no eligible rows", str(cm.exception))

    def test_a_metric_the_evaluator_cannot_compute_is_refused_by_name(self):
        # Skipping it would make the contract look stricter than the judgement.
        c = a_contract(metrics={"sharpe": {
            "direction": "higher_is_better",
            "bar": {"kind": "absolute", "value": 1.0}}})
        with self.assertRaises(verdict.VerdictError) as cm:
            verdict.decide(c, model=result(1.0), baseline=result(2.0))
        self.assertIn("sharpe", str(cm.exception))

    def test_an_unknown_bucket_is_refused_rather_than_scored_as_zero(self):
        c = a_contract(metrics={"p90_miss_tail": {
            "direction": "lower_is_better", "bucket": "2h+",
            "bar": {"kind": "absolute", "value": 0.3}}})
        with self.assertRaises(verdict.VerdictError) as cm:
            verdict.decide(c, model=result(1.0), baseline=None)
        self.assertIn("2h+", str(cm.exception))


class TestConsistency(unittest.TestCase):
    def test_a_day_count_that_disagrees_with_the_contract_is_refused(self):
        # A 3-of-5 rule applied to 2 days is not the rule that was agreed.
        with self.assertRaises(verdict.VerdictError) as cm:
            verdict.decide(a_contract(holdout_days=5), model=result(1.0),
                           baseline=result(2.0))
        self.assertIn("holdout day", str(cm.exception))

    def test_it_counts_days_where_every_per_day_metric_passes(self):
        out = verdict.decide(a_contract(), model=result(1.0),
                             baseline=result(2.0))
        self.assertEqual(out["consistency"]["days_passed"], 3)
        self.assertEqual(out["consistency"]["days_required"], 2)

    def test_too_few_consistent_days_is_a_no_go_even_when_the_aggregate_passes(self):
        # The rule exists so a single outlier day cannot carry a verdict, so a
        # contract requiring more days than the result won must refuse it.
        c = a_contract(consistency={"days_required": 3}, holdout_days=3)
        # A model that wins overall but loses on one day. SET the error rather
        # than scale it: `result(1.0)` is a perfect model, so its
        # `sum_abs_error` is 0.0 and multiplying it by 1000 was a no-op -- the
        # perturbation this test depends on did nothing, and the test passed
        # anyway on the aggregate.
        model = result(1.0)
        worst = sorted(model["per_day"])[-1]
        model["per_day"][worst]["mae"]["sum_abs_error"] = 1e12
        out = verdict.decide(c, model=model, baseline=result(2.0))
        self.assertLess(out["consistency"]["days_passed"], 3)
        self.assertEqual(out["verdict"], "no-go")

    def test_a_bucket_metric_is_aggregate_only(self):
        # WAIT_BUCKETS over one day is a small sample by construction; requiring
        # a tail gate per day would fail on days with three tail rows.
        c = a_contract(metrics={
            "mae": {"direction": "lower_is_better",
                    "bar": {"kind": "relative_improvement", "value": 0.15}},
            "p90_miss_tail": {"direction": "lower_is_better", "bucket": "30m+",
                              "bar": {"kind": "absolute", "value": 0.99}}})
        out = verdict.decide(c, model=result(1.0), baseline=result(2.0))
        self.assertEqual(out["consistency"]["days_passed"], 3)


class TestItEmitsAVerdictNeverAnAction(unittest.TestCase):
    def test_the_module_touches_no_model_or_predictor_path(self):
        import srcscan
        with open(os.path.join(HERE, "verdict.py")) as fh:
            # NINTH instance of a static scan matching its own documentation --
            # this module's docstring says "nothing here writes to
            # trainer/data/models/", which is exactly the string being scanned
            # for. `code_only` strips comments AND string literals, because a
            # line-based comment filter cannot see a docstring.
            code = srcscan.code_only(fh.read())
        for forbidden in ("models/", "predictor", "open(", "os.replace",
                          "shutil", "subprocess", "socket"):
            self.assertNotIn(forbidden, code, forbidden)

    def test_the_verdict_is_one_of_two_words(self):
        for factor in (1.0, 2.0, 5.0):
            out = verdict.decide(a_contract(), model=result(factor),
                                 baseline=result(2.0))
            self.assertIn(out["verdict"], ("go", "no-go"))

    def test_every_metric_reports_its_bar_beside_its_value(self):
        # A number with no bar beside it is a number whose rule nobody can look
        # up, and the verdict is the one artifact a later reader has.
        out = verdict.decide(a_contract(), model=result(1.0),
                             baseline=result(2.0))
        entry = out["metrics"]["mae"]
        for key in ("value", "baseline", "measured", "bar", "direction",
                    "passed"):
            self.assertIn(key, entry)


def result_guarded(p50_factor, *, p90_factor, guard_factor, ndays=3,
                   n_per_day=20, seed=0, tail=True):
    """Like `result`, plus a guarded p90 = max(raw, actual * guard_factor).

    `tail=False` omits the three `30m+` actuals, leaving that bucket EMPTY --
    the only honest way to give a bucketed metric no eligible rows now that a
    model with zero misses has a severity of 0.0 rather than no value.
    """
    rng = np.random.default_rng(seed)
    yt, p50, p90, g, days = [], [], [], [], []
    for d in range(ndays):
        fixed = np.array([2000.0, 4000.0, 9000.0, 30.0]) if tail \
            else np.array([30.0])
        actual = np.concatenate([rng.lognormal(4, 1, n_per_day - 4), fixed])
        yt.extend(actual)
        p50.extend(actual * p50_factor)
        raw = actual * p90_factor
        p90.extend(raw)
        g.extend(np.maximum(raw, actual * guard_factor))
        days.extend([f"2026-08-{d + 1:02d}"] * len(actual))
    return metrics.compute(y_true=np.array(yt), p50=np.array(p50),
                           p90=np.array(p90), p90_guarded=np.array(g),
                           days=np.array(days), buckets=True)


V2_LIKE_METRICS = {
    "mae": {"direction": "lower_is_better",
            "bar": {"kind": "relative_improvement", "value": 0.15}},
    "pinball_p90_guarded": {
        "direction": "lower_is_better",
        "bar": {"kind": "relative_improvement", "value": 0.0}},
    "p90_coverage_guarded": {"direction": "band",
                             "bar": {"kind": "band", "low": 0.88, "high": 0.93}},
    "p90_miss_tail_guarded": {"direction": "lower_is_better", "bucket": "30m+",
                              "role": "report",
                              "bar": {"kind": "absolute", "value": 0.30}},
    "p90_miss_severity_tail": {
        "direction": "lower_is_better", "bucket": "30m+", "role": "report",
        "bar": {"kind": "relative_improvement", "value": 0.0}},
    "interval_width_guarded": {
        "direction": "lower_is_better", "role": "report",
        "bar": {"kind": "relative_improvement", "value": -0.10}},
}


class TestReportMetricsAreShownAndNeverDecide(unittest.TestCase):
    def test_a_failing_bucketed_report_metric_does_not_flip_a_go(self):
        # A model whose served p90 sits BELOW every tail actual fails the 30m+
        # report metric outright. BOTH factors must be < 1: the helper takes
        # `max(raw, actual * guard_factor)`, so a low guard alone cannot pull
        # the served p90 under the truth.
        #
        # NO COVERAGE BAND in this contract on purpose. `result_guarded` scales
        # the p90 off the actual by one factor, so its coverage is exactly 0.0
        # or exactly 1.0 and a band can NEVER pass -- with the band in, the
        # verdict was no-go for that unrelated reason and the assertion below
        # could not tell a correct judge from one that counted report metrics.
        c = a_contract(metrics={
            "mae": {"direction": "lower_is_better",
                    "bar": {"kind": "relative_improvement", "value": 0.15}},
            "pinball_p90_guarded": {
                "direction": "lower_is_better",
                "bar": {"kind": "relative_improvement", "value": 0.0}},
            "p90_miss_tail_guarded": {
                "direction": "lower_is_better", "bucket": "30m+",
                "role": "report",
                "bar": {"kind": "absolute", "value": 0.30}},
        })
        model = result_guarded(1.0, p90_factor=0.9, guard_factor=0.9)
        base = result_guarded(2.0, p90_factor=3.0, guard_factor=3.0)
        out = verdict.decide(c, model=model, baseline=base)
        rep = out["metrics"]["p90_miss_tail_guarded"]
        self.assertEqual(rep["role"], "report")
        self.assertIs(rep["passed"], False)     # it IS evaluated and shown
        self.assertEqual(out["verdict"], "go")
        self.assertEqual(out["consistency"]["days_passed"], 3)
        # THE DISCRIMINATING LINE. A judge that counted every metric rather
        # than the gates would have said no-go here, so this assertion is what
        # makes the `go` above evidence of anything.
        self.assertFalse(all(m["passed"] for m in out["metrics"].values()))

    def test_a_report_metric_with_no_eligible_rows_is_null_not_a_refusal(self):
        # NO ROWS AT ALL in the `30m+` bucket -- `tail=False`. That is what "no
        # eligible rows" has to mean for the severity metric: a model that never
        # misses now scores 0.0, which is a value (it really did spill zero
        # seconds per row), so a high guardrail no longer produces the absence
        # this test is about.
        # A gate would refuse; a report metric reports None and passes nothing.
        # The BASELINE is deliberately worse than the model (2.0 vs 1.0): an
        # identical pair makes the baseline's MAE exactly 0.0, and the `mae`
        # GATE then refuses for an undefined relative improvement -- a refusal
        # from the wrong metric entirely, which would have hidden whatever the
        # report metric did.
        model = result_guarded(1.0, p90_factor=1.0, guard_factor=100.0,
                               tail=False)
        base = result_guarded(2.0, p90_factor=1.0, guard_factor=100.0,
                              tail=False)
        # The fixture only works if the bucket really is empty.
        self.assertEqual(
            model["buckets"]["30m+"]["p90_coverage_guarded"]["eligible_n"], 0)
        out = verdict.decide(a_contract(metrics=V2_LIKE_METRICS),
                             model=model, baseline=base)
        sev = out["metrics"]["p90_miss_severity_tail"]
        self.assertIsNone(sev["value"])
        self.assertIsNone(sev["passed"])
        self.assertEqual(sev["role"], "report")

    def test_report_metrics_do_not_count_toward_day_consistency(self):
        c = a_contract(metrics={
            "mae": {"direction": "lower_is_better",
                    "bar": {"kind": "relative_improvement", "value": 0.15}},
            # A report bar nothing can meet (width must shrink 500%).
            "interval_width_guarded": {
                "direction": "lower_is_better", "role": "report",
                "bar": {"kind": "relative_improvement", "value": 5.0}},
        })
        out = verdict.decide(
            c,
            model=result_guarded(1.0, p90_factor=1.5, guard_factor=1.5),
            baseline=result_guarded(2.0, p90_factor=3.0, guard_factor=3.0))
        self.assertIs(out["metrics"]["interval_width_guarded"]["passed"], False)
        self.assertEqual(out["verdict"], "go")
        self.assertEqual(out["consistency"]["days_passed"], 3)

    def test_the_new_metric_names_are_known(self):
        for name in ("pinball_p90_guarded", "p90_coverage_guarded",
                     "p90_miss_tail_guarded", "p90_miss_severity_tail",
                     "interval_width_guarded"):
            self.assertIn(name, verdict.VALUE_OF)

    def test_a_report_metric_keeps_its_value_and_nulls_only_the_judgement(self):
        # The two ways a judgement can fail to exist are DIFFERENT: here the
        # report metric's own value exists and is worth showing, but the
        # BASELINE has no misses, so its severity is exactly 0.0 and a RELATIVE
        # improvement over zero is undefined. Nulling the value along with the
        # verdict would throw away the number the report exists to show.
        c = a_contract(metrics={
            "mae": {"direction": "lower_is_better",
                    "bar": {"kind": "relative_improvement", "value": 0.15}},
            "p90_miss_severity_tail": {
                "direction": "lower_is_better", "bucket": "30m+",
                "role": "report",
                "bar": {"kind": "relative_improvement", "value": 0.0}},
        })
        out = verdict.decide(
            c,
            model=result_guarded(1.0, p90_factor=0.5, guard_factor=0.5),
            baseline=result_guarded(2.0, p90_factor=3.0, guard_factor=3.0))
        sev = out["metrics"]["p90_miss_severity_tail"]
        self.assertIsNotNone(sev["value"])      # the number IS reported
        self.assertEqual(sev["baseline"], 0.0)  # nothing to be relative TO
        self.assertIsNone(sev["passed"])        # so no judgement is claimed
        self.assertIsNone(sev["measured"])

    def test_a_gate_with_no_eligible_rows_is_still_a_refusal(self):
        # The report-metric leniency must not leak into gates.
        c = a_contract(metrics={
            "p90_miss_severity_tail": {
                "direction": "lower_is_better", "bucket": "30m+",
                "bar": {"kind": "relative_improvement", "value": 0.0}},
        })
        no_misses = dict(p90_factor=1.0, guard_factor=100.0)
        with self.assertRaises(verdict.VerdictError):
            verdict.decide(c, model=result_guarded(1.0, **no_misses),
                           baseline=result_guarded(1.0, **no_misses))


class TestASliceTooThinToJudgeIsInconclusive(unittest.TestCase):
    """`min_eligible_n`. The design promised "fewer than N eligible rows yields
    INCONCLUSIVE, never PASS" and nothing implemented it: a one-row `30m+`
    bucket rendered as `ok (report)`, which reads as evidence and is noise.

    INCONCLUSIVE is not the same absence as "no value". The value IS computed
    and reported; what is withheld is the judgement.
    """

    def a_tail_report(self, minimum):
        return a_contract(metrics={
            "mae": {"direction": "lower_is_better",
                    "bar": {"kind": "relative_improvement", "value": 0.15}},
            "p90_miss_tail": {
                "direction": "lower_is_better", "bucket": "30m+",
                "role": "report", "min_eligible_n": minimum,
                "bar": {"kind": "absolute", "value": 0.30}},
        })

    def test_a_report_below_its_minimum_is_inconclusive_and_keeps_its_value(self):
        model = result(1.0, p90_factor=1000.0)     # misses nothing: a 0.0 miss
        # 3 tail rows per day over 3 days.
        self.assertEqual(model["buckets"]["30m+"]["p90_coverage"]["eligible_n"], 9)
        out = verdict.decide(self.a_tail_report(200), model=model,
                             baseline=result(2.0))
        tail = out["metrics"]["p90_miss_tail"]
        self.assertIs(tail["inconclusive"], True)
        self.assertIsNone(tail["passed"])
        self.assertIsNone(tail["measured"])
        # The NUMBER is still there -- the reader wants to see it, and to see
        # how few rows it is over.
        self.assertEqual(tail["value"], 0.0)
        self.assertEqual(tail["eligible_n"], 9)

    def test_above_its_minimum_the_same_metric_is_judged_normally(self):
        # THE CANARY. Without it, "inconclusive" could be what this returns for
        # every slice, and every assertion above would still pass.
        model = result(1.0, p90_factor=1000.0)
        out = verdict.decide(self.a_tail_report(9), model=model,
                             baseline=result(2.0))
        tail = out["metrics"]["p90_miss_tail"]
        self.assertNotIn("inconclusive", tail)
        self.assertIs(tail["passed"], True)
        self.assertEqual(tail["measured"], 0.0)
        self.assertEqual(tail["eligible_n"], 9)

    def test_an_inconclusive_report_cannot_turn_a_go_into_a_no_go(self):
        # A report never decides, minimum or not.
        out = verdict.decide(self.a_tail_report(200), model=result(1.0),
                             baseline=result(2.0))
        self.assertEqual(out["verdict"], "go")

    def test_a_gate_below_its_minimum_is_refused_by_name(self):
        # The report leniency must not leak into gates: too few rows is not
        # evidence a bar was met, so it must not be able to buy a `go`.
        c = a_contract(metrics={"p90_miss_tail": {
            "direction": "lower_is_better", "bucket": "30m+",
            "min_eligible_n": 200,
            "bar": {"kind": "absolute", "value": 0.30}}})
        with self.assertRaises(verdict.VerdictError) as cm:
            verdict.decide(c, model=result(1.0, p90_factor=1000.0),
                           baseline=None)
        message = str(cm.exception)
        self.assertIn("INCONCLUSIVE", message)
        self.assertIn("p90_miss_tail", message)
        self.assertIn("200", message)

    def test_every_metric_carries_its_row_count_minimum_or_not(self):
        # "How many rows is this over?" is a question a reader has about all of
        # them, so it is not conditional on a floor being declared.
        model, base = result(1.0), result(2.0)
        out = verdict.decide(a_contract(), model=model, baseline=base)
        self.assertEqual(out["metrics"]["mae"]["eligible_n"],
                         model["aggregate"]["mae"]["eligible_n"])

    def test_the_row_count_comes_from_the_bucket_the_value_came_from(self):
        # The value and its denominator must be read from ONE slice: an
        # aggregate row count beside a bucket value would satisfy every floor.
        model = result(1.0, p90_factor=1000.0)
        out = verdict.decide(self.a_tail_report(9), model=model,
                             baseline=result(2.0))
        self.assertEqual(out["metrics"]["p90_miss_tail"]["eligible_n"], 9)
        self.assertLess(out["metrics"]["p90_miss_tail"]["eligible_n"],
                        out["metrics"]["mae"]["eligible_n"])

    def test_a_day_below_the_minimum_does_not_count_as_a_passing_day(self):
        # Conservative on purpose: a thin day can cost a `go`, never buy one.
        # And it must not RAISE -- the bars are stated over the aggregate, so
        # one thin day must not turn a whole result into an error.
        # 3 days of 20 rows: the AGGREGATE (60) clears a floor of 25 and every
        # DAY (20) is under it, which is the only arrangement that exercises
        # the per-day branch -- a floor the aggregate also failed would have
        # been refused by the gate before any day was looked at.
        model = result(1.0)
        self.assertEqual(model["aggregate"]["mae"]["eligible_n"], 60)
        self.assertEqual(model["per_day"][sorted(model["per_day"])[0]]
                         ["mae"]["eligible_n"], 20)
        c = a_contract(metrics={"mae": {
            "direction": "lower_is_better", "min_eligible_n": 25,
            "bar": {"kind": "relative_improvement", "value": 0.15}}})
        out = verdict.decide(c, model=model, baseline=result(2.0))
        # The aggregate gate itself PASSED -- so the no-go below is the
        # consistency count and nothing else.
        self.assertIs(out["metrics"]["mae"]["passed"], True)
        self.assertEqual(out["consistency"]["days_passed"], 0)
        self.assertEqual(out["verdict"], "no-go")

    def test_a_day_above_the_minimum_still_counts(self):
        # The other half: without this the assertion above would hold for a
        # per-day check that had simply stopped counting days.
        c = a_contract(metrics={"mae": {
            "direction": "lower_is_better", "min_eligible_n": 20,
            "bar": {"kind": "relative_improvement", "value": 0.15}}})
        out = verdict.decide(c, model=result(1.0), baseline=result(2.0))
        self.assertEqual(out["consistency"]["days_passed"], 3)
        self.assertEqual(out["verdict"], "go")


if __name__ == "__main__":
    # Without this, `python tests/test_verdict.py` runs NOTHING and exits 0 --
    # a file that reports success for having done no work.
    unittest.main()
