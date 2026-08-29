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


if __name__ == "__main__":
    # Without this, `python tests/test_verdict.py` runs NOTHING and exits 0 --
    # a file that reports success for having done no work.
    unittest.main()
