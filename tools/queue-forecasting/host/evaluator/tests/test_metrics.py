"""Phase 2c Task 21. The metric definitions, the two routes, and the row set."""
import importlib.util
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
        importlib.util.find_spec("pandas"),
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
        counts_fn = body[body.index("def _pinball("):body.index("def _empty")]
        code = "\n".join(l for l in counts_fn.splitlines()
                         if not l.lstrip().startswith("#"))
        # The only division is inside the within_2x RATIO, which is the metric's
        # definition rather than an aggregation. `_pinball` and the guarded
        # blocks have none at all. The scan starts at `_pinball` so that every
        # counting helper `_counts` calls is inside it.
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

    def test_an_empty_bucket_has_zero_GUARDED_counts_too(self):
        # An absent key would make the verdict read a missing tail bucket as a
        # missing METRIC rather than as no rows.
        r = metrics.compute(y_true=np.array([30.0]), p50=np.array([30.0]),
                            p90=np.array([40.0]), p90_guarded=np.array([40.0]),
                            days=np.array(["d"]), buckets=True)
        empty = r["buckets"]["30m+"]
        self.assertEqual(empty["p90_excess_guarded"],
                         {"eligible_n": 0, "miss_n": 0, "sum_excess": 0.0})
        self.assertEqual(empty["interval_width_guarded"],
                         {"eligible_n": 0, "sum_width": 0.0})
        self.assertEqual(empty["p90_coverage_guarded"],
                         {"eligible_n": 0, "covered_n": 0})
        self.assertEqual(empty["pinball_p90_guarded"],
                         {"eligible_n": 0, "sum": 0.0})
        self.assertEqual(empty["pinball_p90"], {"eligible_n": 0, "sum": 0.0})

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
        """Returns the extract POSITIONS, which is what `check` returns: the
        caller reads days and y_true off one index, so there is no way to check
        one alignment and score another."""
        task_id = np.asarray(task_id)
        run_id = np.asarray(run_id)
        if row_id is None:
            row_id = rows.row_ids(task_id, run_id)
        return rows.check(pred_task_id=task_id, pred_run_id=run_id,
                          pred_row_id=np.asarray(row_id),
                          extract_task_id=self.et, extract_run_id=self.er,
                          extract_days=self.ed, extract_in_slice=self.es)

    def days_of(self, *args, **kwargs):
        return self.ed[self.check(*args, **kwargs)]

    def test_a_complete_day_is_accepted_and_returns_its_days(self):
        # THE CANARY. Without it every refusal below could pass because the
        # check refuses everything.
        index = self.check(["t1", "t2"], [0, 0])
        self.assertEqual(index.tolist(), [0, 1])
        self.assertEqual(self.days_of(["t1", "t2"], [0, 0]).tolist(),
                         ["2026-08-01", "2026-08-01"])

    def test_the_returned_index_aligns_the_predictions_to_the_extract(self):
        # Predicted in the OPPOSITE order to the extract, so an index that
        # merely happened to be a range would not survive.
        index = self.check(["t2", "t1"], [0, 0])
        self.assertEqual(index.tolist(), [1, 0])
        self.assertEqual(self.et[index].tolist(), ["t2", "t1"])

    def test_two_complete_days_are_accepted(self):
        days = self.days_of(["t1", "t2", "t3"], [0, 0, 0])
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


class TestTheDayBlock(unittest.TestCase):
    """The other half of the cherry-picking vector: choosing the DAYS.

    The required set is DERIVED by the caller from the extract's `as_of_date` and
    the contract's `holdout_days`, and anything else is refused. The first
    version enforced only contiguity and merely RECORDED whether the block was
    the most recent one -- hedging against a partial final day the trainer has no
    mechanism to produce, while leaving a candidate free to hold out an easier
    earlier block. A finding that does not gate is not a control.
    """

    AVAILABLE = ["2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04",
                 "2026-08-05"]
    REQUIRED = ["2026-08-03", "2026-08-04", "2026-08-05"]

    def block(self, claimed, available=None, required=None):
        return rows.check_day_block(
            claimed,
            self.AVAILABLE if available is None else available,
            required=self.REQUIRED if required is None else required)

    def test_the_required_set_is_accepted(self):
        # THE CANARY. Without it every refusal below could hold because the
        # check refuses everything.
        out = self.block(list(self.REQUIRED))
        self.assertEqual(out["claimed"], self.REQUIRED)
        self.assertEqual(out["required"], self.REQUIRED)
        self.assertEqual(out["available_days"], 5)

    def test_an_earlier_contiguous_block_is_now_REFUSED(self):
        # The finding this whole rewrite exists for. It used to be accepted with
        # `is_tail: false` recorded and nothing gating on it.
        with self.assertRaises(rows.RowSetError) as cm:
            self.block(["2026-08-01", "2026-08-02", "2026-08-03"])
        message = str(cm.exception)
        self.assertIn("not the candidate's to choose", message)
        self.assertIn("2026-08-01", message)

    def test_a_gapped_block_is_refused(self):
        with self.assertRaises(rows.RowSetError) as cm:
            self.block(["2026-08-01", "2026-08-03", "2026-08-05"])
        self.assertIn("holdout is", str(cm.exception))

    def test_a_short_block_names_the_missing_days(self):
        with self.assertRaises(rows.RowSetError) as cm:
            self.block(["2026-08-04", "2026-08-05"])
        self.assertIn("Missing: ['2026-08-03']", str(cm.exception))

    def test_a_day_outside_the_holdout_is_named_as_such(self):
        with self.assertRaises(rows.RowSetError) as cm:
            self.block(self.REQUIRED + ["2026-08-02"])
        self.assertIn("Not in the holdout: ['2026-08-02']", str(cm.exception))

    def test_a_required_day_the_extract_cannot_supply_reads_differently(self):
        """A gap in the EXTRACT is not the candidate's doing, and the two
        refusals must not read alike -- one sends somebody to the prediction set
        and the other to the collector."""
        with self.assertRaises(rows.RowSetError) as cm:
            self.block(["2026-08-04", "2026-08-05"],
                       available=["2026-08-04", "2026-08-05"])
        message = str(cm.exception)
        self.assertIn("gap in the EXTRACT", message)
        self.assertNotIn("not the candidate's to choose", message)

    def test_an_empty_claim_is_refused(self):
        with self.assertRaises(rows.RowSetError) as cm:
            self.block([])
        self.assertIn("no holdout day", str(cm.exception))

    def test_an_empty_requirement_is_refused_not_treated_as_satisfied(self):
        # `claimed == sorted([])` would be False for a non-empty claim, but an
        # empty requirement means the derivation produced nothing, and accepting
        # whatever was claimed is the failure mode this whole class is about.
        with self.assertRaises(rows.RowSetError) as cm:
            self.block(list(self.REQUIRED), required=[])
        self.assertIn("no rule", str(cm.exception))

    def test_duplicate_claims_collapse(self):
        out = self.block(self.REQUIRED + [self.REQUIRED[-1]])
        self.assertEqual(out["claimed"], self.REQUIRED)

    def test_a_calendar_gap_in_the_required_set_is_honoured(self):
        # The required set comes from the calendar, so if the extract genuinely
        # has no in-slice rows on a required day that is the extract's gap --
        # asserted above. What is checked here is that a required set spanning a
        # month boundary is compared literally, not reshaped.
        required = ["2026-07-31", "2026-08-01"]
        out = self.block(required, available=required, required=required)
        self.assertEqual(out["required"], required)


class TestTheGuardedP90IsServingsRule(unittest.TestCase):
    def test_it_transcribes_p90_guardrail_js(self):
        p50 = np.array([10.0, 10.0, 10.0, 10.0, 30.0])
        raw = np.array([12.0, 12.0, 12.0, 12.0, 12.0])
        blp = np.array([50.0, 50.0, 50.0, np.nan, np.nan])
        lvl = np.array(["queue+priority+bucket", "queue+bucket",
                        "queue+priority+bucket", "queue+priority+bucket", None],
                       dtype=object)
        n = np.array([20.0, 999.0, 19.0, 20.0, np.nan])
        guarded, applied = metrics.guarded_p90(
            p50=p50, p90_raw=raw, bl_p90=blp, bl_level=lvl, bl_sample_size=n,
            target="wait_time")
        self.assertEqual(guarded.tolist(), [50.0, 12.0, 12.0, 12.0, 30.0])
        self.assertEqual(applied.tolist(), [True, False, False, False, False])

    def test_run_duration_uses_metadata_name_as_the_strong_level(self):
        guarded, applied = metrics.guarded_p90(
            p50=np.array([1.0]), p90_raw=np.array([2.0]), bl_p90=np.array([9.0]),
            bl_level=np.array(["metadata_name"], dtype=object),
            bl_sample_size=np.array([20.0]), target="run_duration")
        self.assertEqual(guarded.tolist(), [9.0])
        self.assertTrue(applied[0])

    @unittest.skipUnless(
        importlib.util.find_spec("pandas"),
        "pandas is outside the evaluator's closure by design (D26); this parity"
        " test runs where pandas exists and skips on the evaluator host")
    def test_it_agrees_with_the_trainers_compute_guarded_p90(self):
        sys.path.insert(0, TRAINER)
        from src import evaluate as trainer_eval           # noqa: PLC0415
        rng = np.random.default_rng(3)
        n = 300
        p50 = rng.lognormal(3, 1, n)
        raw = p50 * rng.uniform(0.8, 3.0, n)
        blp = p50 * rng.uniform(0.5, 4.0, n)
        blp[rng.random(n) < 0.1] = np.nan
        lvl = rng.choice(np.array(["queue+priority+bucket", "queue+bucket",
                                   "queue", None], dtype=object), n)
        size = rng.integers(0, 60, n).astype(float)
        mine, _ = metrics.guarded_p90(p50=p50, p90_raw=raw, bl_p90=blp,
                                      bl_level=lvl, bl_sample_size=size,
                                      target="wait_time")
        theirs = trainer_eval.compute_guarded_p90(
            p50=p50, model_p90=raw, baseline_p90=blp, baseline_level=lvl,
            baseline_sample_size=size,
            strong_levels=trainer_eval.STRONG_BASELINE_LEVELS["wait"])
        np.testing.assert_array_equal(mine, theirs)
        self.assertEqual(metrics.GUARDRAIL_MIN_SAMPLE, trainer_eval.GUARDRAIL_MIN_SAMPLE)


class TestTheNewCountsAreCountsAndMatchTheTrainer(unittest.TestCase):
    @unittest.skipUnless(
        importlib.util.find_spec("pandas"),
        "pandas is outside the evaluator's closure by design (D26); this parity"
        " test runs where pandas exists and skips on the evaluator host")
    def test_pinball_matches_the_trainers_pinball_loss(self):
        sys.path.insert(0, TRAINER)
        from src import evaluate as trainer_eval           # noqa: PLC0415
        rng = np.random.default_rng(11)
        yt = rng.lognormal(4, 2, 400); yt[rng.random(400) < 0.05] = np.nan
        p50 = yt * rng.lognormal(0, 0.5, 400)
        p90 = p50 * rng.uniform(1.0, 2.0, 400)
        mine = metrics._counts(yt, p50, p90, p90_guarded=p90)
        theirs = trainer_eval.pinball_loss(yt, p90, alpha=0.9)
        self.assertEqual(mine["pinball_p90"]["eligible_n"], theirs["eligible_n"])
        self.assertAlmostEqual(mine["pinball_p90"]["sum"], theirs["sum"], places=6)
        self.assertEqual(mine["pinball_p90_guarded"], mine["pinball_p90"])

    def test_excess_and_width_are_sums_over_the_guarded_p90(self):
        yt = np.array([100.0, 100.0, 100.0, np.nan])
        p50 = np.array([50.0, 50.0, 50.0, 50.0])
        raw = np.array([60.0, 120.0, 90.0, 90.0])
        g = np.array([80.0, 120.0, 90.0, 90.0])
        c = metrics._counts(yt, p50, raw, p90_guarded=g)
        # misses: rows 0 (100>80, excess 20) and 2 (100>90, excess 10). The
        # eligible count is the coverage population (3 finite rows), because the
        # severity ratio is an excess PER ELIGIBLE ROW, not per miss.
        self.assertEqual(c["p90_excess_guarded"],
                         {"miss_n": 2, "sum_excess": 30.0, "eligible_n": 3})
        # width = g - p50 over rows with finite yt, p50 and g: 30 + 70 + 40
        self.assertEqual(c["interval_width_guarded"], {"eligible_n": 3, "sum_width": 140.0})
        self.assertEqual(c["p90_coverage_guarded"], {"eligible_n": 3, "covered_n": 1})

    def test_the_single_pass_and_the_sum_of_days_agree_on_the_new_counts(self):
        rng = np.random.default_rng(5)
        n = 500
        yt = rng.lognormal(4, 2, n); yt[rng.random(n) < 0.05] = np.nan
        p50 = yt * rng.lognormal(0, 0.5, n)
        raw = p50 * rng.uniform(1.0, 2.0, n)
        g = np.maximum(raw, p50 * rng.uniform(1.0, 3.0, n))
        days = np.array([f"2026-08-{1 + i % 5:02d}" for i in range(n)])
        r = metrics.compute(y_true=yt, p50=p50, p90=raw, p90_guarded=g, days=days)
        summed = metrics.aggregate(r["per_day"])
        for key in ("pinball_p90", "pinball_p90_guarded", "p90_coverage_guarded",
                    "p90_excess_guarded", "interval_width_guarded"):
            for field, value in summed[key].items():
                self.assertAlmostEqual(value, r["aggregate"][key][field],
                                       delta=abs(value) * 1e-9 + 1e-9, msg=f"{key}.{field}")

    def test_the_ratios_derive_from_counts_and_are_none_when_empty(self):
        empty = metrics._empty_counts(True, with_guarded=True)
        self.assertIsNone(metrics.pinball_p90_guarded(empty))
        self.assertIsNone(metrics.coverage_guarded(empty))
        self.assertIsNone(metrics.p90_miss_guarded(empty))
        self.assertIsNone(metrics.miss_severity_guarded(empty))
        self.assertIsNone(metrics.interval_width_guarded(empty))
        c = {"pinball_p90_guarded": {"eligible_n": 4, "sum": 10.0},
             "p90_coverage_guarded": {"eligible_n": 4, "covered_n": 3},
             "p90_excess_guarded": {"eligible_n": 4, "miss_n": 1,
                                    "sum_excess": 600.0},
             "interval_width_guarded": {"eligible_n": 4, "sum_width": 40.0}}
        self.assertEqual(metrics.pinball_p90_guarded(c), 2.5)
        self.assertEqual(metrics.coverage_guarded(c), 0.75)
        self.assertEqual(metrics.p90_miss_guarded(c), 0.25)
        # 600 over the 4 ELIGIBLE rows, not over the 1 miss.
        self.assertEqual(metrics.miss_severity_guarded(c), 150.0)
        self.assertEqual(metrics.interval_width_guarded(c), 10.0)

    def test_severity_is_none_only_when_nothing_is_eligible_not_when_no_misses(self):
        # Zero misses is a VALUE of 0.0: the model's excess over that population
        # really was zero seconds per row. Returning None there would hide a
        # perfectly calibrated tail behind a null.
        no_misses = {"p90_excess_guarded": {"eligible_n": 7, "miss_n": 0,
                                            "sum_excess": 0.0}}
        self.assertEqual(metrics.miss_severity_guarded(no_misses), 0.0)
        nothing = {"p90_excess_guarded": {"eligible_n": 0, "miss_n": 0,
                                          "sum_excess": 0.0}}
        self.assertIsNone(metrics.miss_severity_guarded(nothing))

    def test_severity_ranks_many_small_misses_worse_than_one_large_one(self):
        # WHY the denominator is the population and not the miss count. Both
        # models are scored on the SAME 10 rows. A leaves 3 misses of 10s each
        # (30s of excess); B leaves 1 miss of 20s. A per-miss mean would call B
        # worse (20 > 10) even though it spills less total excess over the same
        # rows, so a model that removed two of A's misses would be reported as a
        # regression. Over the fixed population the order is the honest one.
        yt = np.full(10, 100.0)
        p50 = np.full(10, 50.0)
        a = np.full(10, 200.0); a[:3] = 90.0        # 3 misses, excess 10 each
        b = np.full(10, 200.0); b[0] = 80.0         # 1 miss, excess 20
        ca = metrics._counts(yt, p50, a, p90_guarded=a)["p90_excess_guarded"]
        cb = metrics._counts(yt, p50, b, p90_guarded=b)["p90_excess_guarded"]
        self.assertEqual((ca["miss_n"], ca["sum_excess"]), (3, 30.0))
        self.assertEqual((cb["miss_n"], cb["sum_excess"]), (1, 20.0))
        # The rejected definition: per-miss means would be 10.0 and 20.0.
        self.assertLess(ca["sum_excess"] / ca["miss_n"],
                        cb["sum_excess"] / cb["miss_n"])
        # The one in force: A is worse, because it spills more over the same
        # rows.
        sev_a = metrics.miss_severity_guarded({"p90_excess_guarded": ca})
        sev_b = metrics.miss_severity_guarded({"p90_excess_guarded": cb})
        self.assertEqual((sev_a, sev_b), (3.0, 2.0))
        self.assertGreater(sev_a, sev_b)


class TestEveryRatioReportsWhatItDividedBy(unittest.TestCase):
    """`ELIGIBLE_OF`: the denominator each ratio was computed over.

    A bar applied to a slice of one row is not a judgement, and the only way to
    know that is to carry the row count beside the value. The table is checked
    against `verdict.VALUE_OF` rather than against a list written here, so a
    metric cannot gain a bar without gaining a row count.
    """

    def test_every_metric_with_a_value_also_has_an_eligible_count(self):
        import verdict
        self.assertEqual(sorted(verdict.VALUE_OF), sorted(metrics.ELIGIBLE_OF))

    def test_each_one_returns_the_count_its_own_ratio_divides_by(self):
        counts = metrics._counts(
            np.array([100.0, 200.0, 300.0]), np.array([90.0, 180.0, 310.0]),
            p90=np.array([150.0, 250.0, 350.0]),
            p90_guarded=np.array([160.0, 260.0, 360.0]))
        expected = {
            "mae": counts["mae"]["eligible_n"],
            "within_2x": counts["within_2x"]["eligible_n"],
            "p90_coverage": counts["p90_coverage"]["eligible_n"],
            "p90_miss_tail": counts["p90_coverage"]["eligible_n"],
            "pinball_p90_guarded": counts["pinball_p90_guarded"]["eligible_n"],
            "p90_coverage_guarded":
                counts["p90_coverage_guarded"]["eligible_n"],
            "p90_miss_tail_guarded":
                counts["p90_coverage_guarded"]["eligible_n"],
            # NOT `miss_n`. `miss_severity_guarded` divides by the eligible
            # POPULATION, so that is the count a minimum must be checked
            # against; checking a floor against the wrong denominator is a
            # floor that does not hold.
            "p90_miss_severity_tail": counts["p90_excess_guarded"]["eligible_n"],
            "interval_width_guarded":
                counts["interval_width_guarded"]["eligible_n"],
        }
        for name, want in expected.items():
            self.assertEqual(metrics.ELIGIBLE_OF[name](counts), want, name)
            self.assertEqual(want, 3, name)

    def test_severity_reports_the_population_not_the_miss_count(self):
        # Stated as its own assertion because the two differ only when some
        # rows were covered -- which is every real slice.
        counts = {"p90_excess_guarded": {"eligible_n": 40, "miss_n": 3,
                                         "sum_excess": 9.0}}
        self.assertEqual(
            metrics.ELIGIBLE_OF["p90_miss_severity_tail"](counts), 40)

    def test_a_missing_entry_counts_as_zero_rows(self):
        # A slice scored without a guarded p90 carries no guarded counts at
        # all, and "no rows" is the honest answer -- a None here would have to
        # be special-cased by every comparison against a minimum.
        for name, fn in metrics.ELIGIBLE_OF.items():
            self.assertEqual(fn({}), 0, name)
            self.assertEqual(fn({"mae": None}), 0, name)

    def test_a_non_integer_count_is_read_as_zero_rather_than_compared(self):
        # These counts come back through JSON from another process. `"200" >=
        # 200` is a TypeError and `True >= 200` is False; neither is a row
        # count, so neither is treated as one.
        for bad in ("200", True, None, 1.5):
            self.assertEqual(
                metrics.ELIGIBLE_OF["mae"]({"mae": {"eligible_n": bad}}), 0,
                repr(bad))


if __name__ == "__main__":
    unittest.main()
