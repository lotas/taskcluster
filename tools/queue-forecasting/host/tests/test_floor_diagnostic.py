"""floor-diagnostic.py: the transcription agrees with the evaluator, eligibility
and effect are separated, and the candidate policy touches only long buckets."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

import numpy as np
import pyarrow
import pyarrow.parquet as pq

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "evaluator"))
import metrics  # noqa: E402  the evaluator's own guarded_p90, the thing transcribed

spec = importlib.util.spec_from_file_location("fd", os.path.join(HERE, "floor-diagnostic.py"))
fd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fd)


def synthetic(n=4000, seed=0):
    rng = np.random.default_rng(seed)
    y = rng.lognormal(4.5, 1.6, n)                      # spans every bucket
    bl_p50 = y * rng.lognormal(0, 1.4, n)                # baseline p50, noisy enough to cross buckets
    bl_p90 = bl_p50 * rng.uniform(1.5, 4.0, n)
    p50 = y * rng.lognormal(0, 0.4, n)
    p90_raw = p50 * rng.uniform(1.1, 2.5, n)
    level = np.where(rng.random(n) < 0.7, "queue+priority+bucket", "queue")
    size = np.where(rng.random(n) < 0.9, 50.0, 5.0)
    guarded, applied = metrics.guarded_p90(p50=p50, p90_raw=p90_raw, bl_p90=bl_p90,
                                           bl_level=level, bl_sample_size=size,
                                           target="wait_time")
    bucket = fd.bucket_of(y)
    days = np.array([f"2026-08-{22 + i % 5:02d}" for i in range(n)])
    return pyarrow.table({
        "row_id": pyarrow.array([f"t{i}:0" for i in range(n)]),
        "task_id": pyarrow.array([f"t{i}" for i in range(n)]),
        "run_id": pyarrow.array(np.zeros(n, dtype=np.int32)),
        "day": pyarrow.array(days), "y_true": pyarrow.array(y),
        "p50": pyarrow.array(p50), "p90_raw": pyarrow.array(p90_raw),
        "bucket": pyarrow.array(bucket.tolist()), "bl_p50": pyarrow.array(bl_p50),
        "bl_p90": pyarrow.array(bl_p90), "bl_level": pyarrow.array(level.tolist()),
        "bl_sample_size": pyarrow.array(size), "p90_guarded": pyarrow.array(guarded),
        "guard_applied": pyarrow.array(applied.tolist()),
    })


class TestFloorDiagnostic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "eval.parquet")
        pq.write_table(synthetic(), self.path)
        self.d = fd.load(self.path)
        self.rep = fd.analyse("t", self.d, fd.DEFAULT_BANDS)

    def test_the_bucket_edges_are_the_evaluators(self):
        self.assertEqual(fd.WAIT_BUCKETS, metrics.WAIT_BUCKETS)

    def test_the_served_p90_transcription_matches_the_evaluator(self):
        served, _ = fd.served_under_policy(self.d, long_only=False)
        np.testing.assert_allclose(served, self.d["p90_guarded"], atol=1e-9)

    def test_eligibility_is_not_effect(self):
        # Eligible rows whose raw p90 already exceeds the floor are eligible and
        # NOT raised: the two rates must differ, and effect <= eligibility.
        f = self.rep["aggregate"]["floor"]
        self.assertGreater(f["eligible_rate"], f["raised_by_baseline_floor_rate"])
        self.assertLess(f["raised_by_baseline_floor_among_eligible"], 1.0)

    def test_the_candidate_policy_changes_only_long_buckets(self):
        b = self.rep["by_prediction_time_bucket"]
        for short in ("<1m", "1-5m"):
            # Without the baseline floor, only the p50 floor remains: coverage
            # under the candidate policy equals coverage of max(p50, raw).
            sel = self.d["bucket_key"] == short
            yt = self.d["y_true"][sel]
            expect = float(np.mean(yt <= np.maximum(self.d["p50"][sel], self.d["p90_raw"][sel])))
            self.assertAlmostEqual(b[short]["coverage"]["candidate_policy"], expect, places=12)
        for long in fd.LONG_BUCKETS:
            self.assertEqual(b[long]["coverage"]["candidate_policy"], b[long]["coverage"]["guarded"])

    def test_buckets_are_chosen_by_the_baseline_p50_not_the_outcome(self):
        # The cross-tab is not diagonal: predicted-short rows include long
        # realised waits, which is exactly why the gate buckets on the prediction.
        ct = self.rep["crosstab"]
        self.assertGreater(ct["<1m"]["30m+"] + ct["1-5m"]["30m+"], 0)
        total = sum(sum(r.values()) for r in ct.values())
        self.assertEqual(total, int(np.isfinite(self.d["bl_p50"]).sum()))

    def test_band_check_is_inconclusive_under_the_minimum(self):
        self.assertEqual(fd.band_check(0.90, [0.85, 0.95], 199), "inconclusive")
        self.assertEqual(fd.band_check(0.96, [0.85, 0.95], 200), "above")
        self.assertEqual(fd.band_check(0.96, [0.85, None], 200), "pass")
        self.assertEqual(fd.band_check(0.80, [0.85, None], 200), "below")

    def test_the_cli_renders_and_writes_json(self):
        out = os.path.join(self.tmp, "r.json")
        r = subprocess.run([sys.executable, os.path.join(HERE, "floor-diagnostic.py"),
                            "--eval", f"t={self.path}", "--json", out],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("| 30m+ |", r.stdout)
        with open(out) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["reports"][0]["label"], "t")
        self.assertIn("realised_30m_plus_report", doc["reports"][0])

    def test_a_v1_eval_without_a_served_p90_is_refused(self):
        t = pq.read_table(self.path)
        n = t.num_rows
        t = t.set_column(t.schema.get_field_index("p90_guarded"), "p90_guarded",
                         pyarrow.array(np.full(n, np.nan)))
        p = os.path.join(self.tmp, "v1.parquet")
        pq.write_table(t, p)
        with self.assertRaises(SystemExit):
            fd.load(p)


if __name__ == "__main__":
    unittest.main()
