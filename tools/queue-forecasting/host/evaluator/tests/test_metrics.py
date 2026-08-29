"""Phase 2c Task 21. The metric definitions, the two routes, and the row set."""
import os
import re
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import metrics
import rows


TRAINER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))), "trainer")


class TestTheTwoRoutesAgree(unittest.TestCase):
    """D26's cross-check, and the only thing a second route can actually detect:
    slice disagreement, day-boundary disagreement, join errors, double counting.
    NOT a shared misunderstanding of what MAE is -- the definitions are
    transcribed on purpose, because two different formulas would be a
    disagreement with no arbiter."""

    def random_case(self, seed, n=500, ndays=5):
        rng = np.random.default_rng(seed)
        yt = rng.lognormal(4, 2, n)
        # NaNs and zeros on purpose: they are what the masks exist for.
        yt[rng.random(n) < 0.05] = np.nan
        yt[rng.random(n) < 0.05] = 0.0
        p50 = np.maximum(yt * rng.lognormal(0, 0.5, n), 0)
        p50[rng.random(n) < 0.03] = np.nan
        p90 = p50 * rng.uniform(1.0, 2.0, n)
        days = np.array([f"2026-08-{1 + i % ndays:02d}" for i in range(n)])
        return yt, p50, p90, days

    def test_single_pass_matches_sum_of_days_on_every_count(self):
        for seed in range(8):
            with self.subTest(seed=seed):
                yt, p50, p90, days = self.random_case(seed)
                r = metrics.compute(y_true=yt, p50=p50, p90=p90, days=days)
                summed = metrics.aggregate(r["per_day"])
                for key in ("mae", "within_2x", "p90_coverage"):
                    for field, value in summed[key].items():
                        if field == "sum_abs_error":
                            # A float sum over a different ORDER, so exact
                            # equality is not the right assertion. The bound is
                            # relative and tight: anything larger is a real
                            # disagreement, not reassociation.
                            self.assertAlmostEqual(
                                value, r["aggregate"][key][field],
                                delta=abs(value) * 1e-9 + 1e-9)
                        else:
                            self.assertEqual(value, r["aggregate"][key][field],
                                             f"{key}.{field}")

    def test_a_deliberate_day_boundary_error_is_caught_by_the_comparison(self):
        # The failure the cross-check exists to find: if the per-day split
        # dropped or duplicated a day, the counts stop matching.
        yt, p50, p90, days = self.random_case(1)
        r = metrics.compute(y_true=yt, p50=p50, p90=p90, days=days)
        dropped = {k: v for k, v in list(r["per_day"].items())[:-1]}
        self.assertNotEqual(metrics.aggregate(dropped)["mae"]["eligible_n"],
                            r["aggregate"]["mae"]["eligible_n"])


class TestTheDefinitionsAreTheTrainers(unittest.TestCase):
    def test_the_bucket_edges_match_the_trainer_exactly(self):
        # Parsed rather than imported: `evaluate.py` imports pandas, which is
        # deliberately outside this environment's closure.
        with open(os.path.join(TRAINER, "src", "evaluate.py")) as fh:
            block = fh.read().split("WAIT_BUCKETS = [", 1)[1].split("]", 1)[0]
        found = re.findall(r'\("([^"]+)",\s*([\d.]+),\s*([\w.()"\']+)\)', block)
        self.assertEqual([name for name, _lo, _hi in found],
                         [name for name, _lo, _hi in metrics.WAIT_BUCKETS])
        for (name, lo, hi), (mine, mylo, myhi) in zip(found,
                                                      metrics.WAIT_BUCKETS):
            self.assertEqual(name, mine)
            self.assertEqual(float(lo), mylo)
            self.assertEqual("inf" in hi, myhi == float("inf"), name)

    @unittest.skipUnless(
        __import__("importlib").util.find_spec("pandas"),
        "pandas is outside the evaluator's closure by design (D26); this parity"
        " test runs where pandas exists and skips on the evaluator host")
    def test_it_agrees_with_the_trainers_own_functions(self):
        # THE STRONGEST AVAILABLE CHECK, and it is a transcription check rather
        # than an independence one: it proves this file computes what the trainer
        # computes, which is what makes the two-route comparison meaningful.
        sys.path.insert(0, TRAINER)
        from src import evaluate as trainer_eval           # noqa: PLC0415
        rng = np.random.default_rng(7)
        for _ in range(5):
            n = 400
            yt = rng.lognormal(4, 2, n)
            yt[rng.random(n) < 0.05] = np.nan
            yt[rng.random(n) < 0.05] = 0.0
            p50 = np.maximum(yt * rng.lognormal(0, 0.6, n), 0)
            p90 = p50 * rng.uniform(1.0, 2.0, n)
            theirs = trainer_eval.per_row_metrics(yt, p50, y_pred_p90=p90)
            mine = metrics._counts(yt, p50, p90)
            self.assertEqual(mine["mae"]["eligible_n"],
                             theirs["mae"]["eligible_n"])
            self.assertAlmostEqual(mine["mae"]["sum_abs_error"],
                                   theirs["mae"]["sum_abs_error"], places=6)
            self.assertEqual(mine["within_2x"], theirs["within_2x"])
            self.assertEqual(mine["p90_coverage"], theirs["p90_coverage"])


class TestTheMasksAreTheReasonTheNumbersMeanAnything(unittest.TestCase):
    def test_a_nan_prediction_is_excluded_not_scored(self):
        counts = metrics._counts(np.array([100.0, 200.0]),
                                 np.array([110.0, np.nan]))
        self.assertEqual(counts["mae"]["eligible_n"], 1)
        self.assertEqual(counts["mae"]["sum_abs_error"], 10.0)

    def test_within_2x_excludes_zeros_on_either_side(self):
        # The ratio is max(p/t, t/p); a zero is not a ratio, and including it
        # would either divide by zero or score a bounded metric unboundedly.
        counts = metrics._counts(np.array([0.0, 100.0, 100.0]),
                                 np.array([100.0, 0.0, 150.0]))
        self.assertEqual(counts["within_2x"]["eligible_n"], 1)
        self.assertEqual(counts["within_2x"]["hit_n"], 1)

    def test_within_2x_is_a_two_sided_ratio(self):
        # Over-prediction and under-prediction by the same factor both count.
        counts = metrics._counts(np.array([100.0, 100.0]),
                                 np.array([201.0, 49.0]))
        self.assertEqual(counts["within_2x"]["hit_n"], 0)
        counts = metrics._counts(np.array([100.0, 100.0]),
                                 np.array([199.0, 51.0]))
        self.assertEqual(counts["within_2x"]["hit_n"], 2)

    def test_coverage_counts_actual_at_or_below_p90(self):
        counts = metrics._counts(np.array([100.0, 100.0]),
                                 np.array([100.0, 100.0]),
                                 np.array([100.0, 99.0]))
        self.assertEqual(counts["p90_coverage"],
                         {"eligible_n": 2, "covered_n": 1})

    def test_nothing_here_divides(self):
        # Counts, never ratios: the quotient is computed once, by the verdict,
        # from summed counts. That is what lets a trusted process recompute every
        # number from the parts rather than trusting a candidate's quotient.
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "metrics.py")
        with open(path) as fh:
            body = fh.read()
        counts_fn = body[body.index("def _counts("):body.index("def _empty")]
        code = "\n".join(l for l in counts_fn.splitlines()
                         if not l.lstrip().startswith("#"))
        # The only division is inside the within_2x RATIO, which is the metric's
        # definition rather than an aggregation.
        self.assertEqual(code.count("/"), 2, code)

    def test_buckets_are_assigned_on_the_actual_not_the_prediction(self):
        # Bucketing on the prediction would let a model move rows out of the
        # bucket it is bad at.
        yt = np.array([30.0, 3600.0])
        p50 = np.array([3600.0, 30.0])
        r = metrics.compute(y_true=yt, p50=p50, p90=None,
                            days=np.array(["d", "d"]), buckets=True)
        self.assertEqual(r["buckets"]["<1m"]["mae"]["eligible_n"], 1)
        self.assertEqual(r["buckets"]["30m+"]["mae"]["eligible_n"], 1)

    def test_an_empty_bucket_is_zero_counts_not_a_missing_key(self):
        r = metrics.compute(y_true=np.array([30.0]), p50=np.array([30.0]),
                            days=np.array(["d"]), buckets=True)
        self.assertEqual(r["buckets"]["30m+"]["mae"]["eligible_n"], 0)

    def test_the_ratios_are_none_rather_than_zero_when_nothing_is_eligible(self):
        # A metric with no eligible rows has NO VALUE. Returning 0.0 would let
        # "there were no rows to check" satisfy a lower-is-better bar.
        empty = metrics._counts(np.array([np.nan]), np.array([np.nan]),
                                np.array([np.nan]))
        self.assertIsNone(metrics.mae(empty))
        self.assertIsNone(metrics.within_2x(empty))
        self.assertIsNone(metrics.coverage(empty))
        self.assertIsNone(metrics.p90_miss(empty))


class TestTheRowSet(unittest.TestCase):
    """NC11. Read literally -- equality with the whole extract -- it is wrong: an
    extract covers the training window too. The checkable property is well-formed
    + subset-without-duplicates + COMPLETE WITHIN EACH CLAIMED DAY."""

    def setUp(self):
        self.et = np.array(["t1", "t2", "t3", "t4", "t5"])
        self.er = np.array([0, 0, 0, 0, 1])
        self.ed = np.array(["2026-08-01", "2026-08-01", "2026-08-02",
                            "2026-08-02", "2026-08-03"])
        self.es = np.array([True, True, True, False, True])

    def check(self, task_id, run_id, row_id=None):
        task_id = np.asarray(task_id)
        run_id = np.asarray(run_id)
        if row_id is None:
            row_id = rows.row_ids(task_id, run_id)
        return rows.check(pred_task_id=task_id, pred_run_id=run_id,
                          pred_row_id=np.asarray(row_id),
                          extract_task_id=self.et, extract_run_id=self.er,
                          extract_days=self.ed, extract_in_slice=self.es)

    def test_a_complete_day_is_accepted_and_returns_its_days(self):
        # THE CANARY. Without it every refusal below could pass because the
        # check refuses everything.
        days = self.check(["t1", "t2"], [0, 0])
        self.assertEqual(days.tolist(), ["2026-08-01", "2026-08-01"])

    def test_two_complete_days_are_accepted(self):
        days = self.check(["t1", "t2", "t3"], [0, 0, 0])
        self.assertEqual(sorted(set(days.tolist())),
                         ["2026-08-01", "2026-08-02"])

    def test_a_row_outside_the_primary_slice_is_not_required(self):
        # t4 is on 2026-08-02 and out of slice; predicting t3 alone completes
        # that day as far as the contract's population goes.
        self.check(["t3"], [0])

    def test_cherry_picking_inside_a_day_is_refused(self):
        # The gaming vector the first two parts of NC11 leave wide open.
        with self.assertRaises(rows.RowSetError) as cm:
            self.check(["t1"], [0])
        self.assertIn("omits", str(cm.exception))
        self.assertIn("t2:0", str(cm.exception))

    def test_a_row_id_disagreeing_with_its_own_keys_is_refused(self):
        with self.assertRaises(rows.RowSetError) as cm:
            self.check(["t1", "t2"], [0, 0], ["t1:0", "t2:99"])
        self.assertIn("disagree", str(cm.exception))

    def test_a_duplicate_is_refused(self):
        with self.assertRaises(rows.RowSetError) as cm:
            self.check(["t1", "t1", "t2"], [0, 0, 0])
        self.assertIn("double-weights", str(cm.exception))

    def test_a_row_not_in_the_extract_is_refused(self):
        with self.assertRaises(rows.RowSetError) as cm:
            self.check(["t9"], [0])
        self.assertIn("not in the frozen extract", str(cm.exception))

    def test_a_mismatched_row_id_length_is_refused(self):
        with self.assertRaises(rows.RowSetError):
            self.check(["t1", "t2"], [0, 0], ["t1:0"])

    def test_a_duplicate_in_the_extract_itself_is_refused(self):
        # It cannot happen -- (task_id, run_id) is the table's key -- but keeping
        # the last silently would make the completeness count wrong in a way
        # nothing else would surface.
        self.et = np.array(["t1", "t1"])
        self.er = np.array([0, 0])
        self.ed = np.array(["2026-08-01", "2026-08-01"])
        self.es = np.array([True, True])
        with self.assertRaises(rows.RowSetError) as cm:
            self.check(["t1"], [0])
        self.assertIn("twice", str(cm.exception))

    def test_the_derivation_is_the_2b2_contract(self):
        self.assertEqual(rows.row_ids(np.array(["abc"]), np.array([7]))[0],
                         "abc:7")
