"""check-baseline-coverage.py: a missing train row fails, a changed reference
row fails, and rows whose history touches a differing exclusion date are not
compared."""
import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

import pyarrow
import pyarrow.parquet as pq

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("cbc", os.path.join(HERE, "check-baseline-coverage.py"))
cbc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cbc)


def rows(days, per_day=3, p50=10.0):
    out = []
    for d in days:
        for i in range(per_day):
            out.append({"task_id": f"{d}-t{i}", "run_id": 0,
                        "pending_at": f"{d}T12:00:00.000Z", "bl_wait_p50": p50,
                        "bl_wait_p90": 30.0, "bl_wait_level": "queue+priority+bucket",
                        "bl_wait_sample_size": 40})
    return out


def write_ndjson(path, rs):
    with open(path, "w") as fh:
        for r in rs:
            fh.write(json.dumps(r) + "\n")


def write_runs(path, rs):
    pq.write_table(pyarrow.table({
        "task_id": pyarrow.array([r["task_id"] for r in rs]),
        "run_id": pyarrow.array([0] * len(rs), pyarrow.int32()),
        "pending_at": pyarrow.array([dt.datetime.fromisoformat(r["pending_at"][:19]) for r in rs],
                                    pyarrow.timestamp("us")),
    }), path)


class TestCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.days = [(dt.date(2026, 8, 1) + dt.timedelta(days=k)).isoformat() for k in range(26)]
        self.full = rows(self.days)
        self.nd = os.path.join(self.tmp, "new.ndjson"); write_ndjson(self.nd, self.full)
        self.runs = os.path.join(self.tmp, "runs.parquet"); write_runs(self.runs, self.full)

    def run_cli(self, *extra):
        r = subprocess.run([sys.executable, os.path.join(HERE, "check-baseline-coverage.py"),
                            "--ndjson", self.nd, "--cohort", f"2026-08-27={self.runs}", *extra],
                           capture_output=True, text=True)
        return r.returncode, r.stdout

    def test_complete_coverage_is_promotable(self):
        rc, out = self.run_cli()
        self.assertEqual(rc, 0, out); self.assertIn("PROMOTABLE", out)

    def test_one_missing_train_row_is_not_promotable(self):
        # train window for as_of 08-27: [08-07, 08-22). Drop one row on 08-10.
        write_ndjson(self.nd, [r for r in self.full if r["task_id"] != "2026-08-10-t1"])
        rc, out = self.run_cli()
        self.assertEqual(rc, 1); self.assertIn("missing 1", out); self.assertIn("2026-08-10", out)

    def test_a_missing_holdout_row_does_not_count(self):
        write_ndjson(self.nd, [r for r in self.full if r["task_id"] != "2026-08-24-t1"])
        rc, _ = self.run_cli()
        self.assertEqual(rc, 0)

    def test_a_changed_reference_row_is_not_promotable(self):
        ref = os.path.join(self.tmp, "ref.ndjson")
        changed = [dict(r, bl_wait_p50=99.0) if r["task_id"] == "2026-08-05-t0" else r for r in self.full]
        write_ndjson(ref, changed)
        rc, out = self.run_cli("--reference", ref)
        self.assertEqual(rc, 1); self.assertIn("1 disagree", out)

    def test_rows_whose_history_touches_a_differing_exclusion_are_skipped(self):
        ref = os.path.join(self.tmp, "ref.ndjson")
        # Reference row on 08-05 differs, but 08-03 is excluded only in the new
        # list and lies inside 08-05's 7-day history: not comparable, so no failure.
        changed = [dict(r, bl_wait_p50=99.0) if r["task_id"] == "2026-08-05-t0" else r for r in self.full]
        write_ndjson(ref, changed)
        rc, out = self.run_cli("--reference", ref, "--exclude-dates", "2026-08-03")
        self.assertEqual(rc, 0, out); self.assertIn("2026-08-03", out)
        # The same differing date far from 08-05 does not shield it.
        rc, out = self.run_cli("--reference", ref, "--exclude-dates", "2026-08-20")
        self.assertEqual(rc, 1)

    def test_the_train_window_arithmetic_is_the_trainers(self):
        s, e = cbc.train_window(dt.datetime(2026, 8, 27), 5, 1, 14)
        self.assertEqual((s.date().isoformat(), e.date().isoformat()), ("2026-08-07", "2026-08-22"))


if __name__ == "__main__":
    unittest.main()
