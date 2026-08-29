"""Phase 2b-3 Task 13: the baseline set's identity.

A baseline has the same provenance problem as an extract -- same window, produced
twice, different content -- so it gets the same answer: immutable publication with
a content digest.

It has one thing an extract does not. `exclude_dates` (the Policy B filtered
baseline) affects the percentile HISTORY rather than the output rows, so it is
**not recoverable from the files**. It is declared by whoever promotes, and the
manifest has to say so: a manifest that presented a declared value as a derived
one would be the strongest-looking claim in the record and the weakest fact in it.

Pure and stdlib-only, so it runs anywhere the dispatcher's tests run.
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import baseline                                                # noqa: E402


class BaselineCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write_set(self, *, days=("2026-08-20", "2026-08-21"), rows=3,
                  extra=(), ndjson=None):
        """A plausible baseline directory: one aggregate NDJSON and per-day JSONs."""
        if ndjson is None:
            lines = []
            for i in range(rows):
                lines.append(json.dumps({
                    "task_id": f"t{i}", "run_id": 0,
                    "pending_at": f"2026-08-2{i % 3}T01:00:00Z",
                    "bl_wait_p50": 1.0, "bl_wait_p90": 2.0,
                    "bl_duration_p50": 3.0, "bl_duration_p90": 4.0}))
            ndjson = "\n".join(lines) + "\n"
        with open(os.path.join(self.dir, baseline.NDJSON_NAME), "w") as fh:
            fh.write(ndjson)
        for day in days:
            with open(os.path.join(self.dir, f"{day}.json"), "w") as fh:
                json.dump({"day": day}, fh)
        for name in extra:
            with open(os.path.join(self.dir, name), "w") as fh:
                fh.write("x")
        return self.dir


class TestTheFileSetIsClosedWorld(BaselineCase):
    """Named and counted, never a glob. A baseline directory accumulates per-day
    files over months, and "everything here" is not a description of anything."""

    def test_a_plausible_set_is_accepted(self):
        manifest = baseline.describe(self.write_set(), exclude_dates=[])
        self.assertEqual(sorted(manifest["files"]),
                         sorted([baseline.NDJSON_NAME, "2026-08-20.json",
                                 "2026-08-21.json"]))

    def test_a_missing_aggregate_ndjson_is_refused(self):
        self.write_set()
        os.unlink(os.path.join(self.dir, baseline.NDJSON_NAME))
        with self.assertRaises(baseline.BaselineError) as cm:
            baseline.describe(self.dir, exclude_dates=[])
        self.assertIn(baseline.NDJSON_NAME, str(cm.exception))

    def test_a_set_with_no_per_day_files_is_refused(self):
        with self.assertRaises(baseline.BaselineError) as cm:
            baseline.describe(self.write_set(days=()), exclude_dates=[])
        self.assertIn("per-day", str(cm.exception))

    def test_an_unrecognised_file_is_refused_by_name(self):
        # Not ignored. A stray file is either something that belongs in the
        # identity or something that should not be published, and both need a
        # human to say which.
        with self.assertRaises(baseline.BaselineError) as cm:
            baseline.describe(self.write_set(extra=("notes.txt",)),
                              exclude_dates=[])
        self.assertIn("notes.txt", str(cm.exception))

    def test_a_subdirectory_is_refused(self):
        self.write_set()
        os.makedirs(os.path.join(self.dir, "old"))
        with self.assertRaises(baseline.BaselineError):
            baseline.describe(self.dir, exclude_dates=[])

    def test_a_malformed_per_day_name_is_refused(self):
        with self.assertRaises(baseline.BaselineError) as cm:
            baseline.describe(self.write_set(extra=("2026-8-1.json",)),
                              exclude_dates=[])
        self.assertIn("2026-8-1.json", str(cm.exception))


class TestEverythingDerivableIsDerived(BaselineCase):
    def test_per_file_digests_are_of_the_bytes_on_disk(self):
        manifest = baseline.describe(self.write_set(), exclude_dates=[])
        for name, entry in manifest["files"].items():
            with self.subTest(name=name):
                with open(os.path.join(self.dir, name), "rb") as fh:
                    self.assertEqual(entry["sha256"],
                                     hashlib.sha256(fh.read()).hexdigest())

    def test_the_days_come_from_the_filenames(self):
        manifest = baseline.describe(
            self.write_set(days=("2026-08-22", "2026-08-20", "2026-08-21")),
            exclude_dates=[])
        self.assertEqual(manifest["days"],
                         ["2026-08-20", "2026-08-21", "2026-08-22"])

    def test_the_ndjson_row_count_is_counted(self):
        manifest = baseline.describe(self.write_set(rows=7), exclude_dates=[])
        self.assertEqual(manifest["ndjson_rows"], 7)

    def test_the_pending_range_comes_from_the_content(self):
        # The window the baseline actually covers, read out rather than declared:
        # a declared window can be wrong and nothing would notice.
        manifest = baseline.describe(self.write_set(rows=3), exclude_dates=[])
        self.assertEqual(manifest["pending_at_min"], "2026-08-20T01:00:00Z")
        self.assertEqual(manifest["pending_at_max"], "2026-08-22T01:00:00Z")

    def test_blank_lines_do_not_count_as_rows(self):
        manifest = baseline.describe(
            self.write_set(ndjson='{"task_id":"a","run_id":0,'
                                  '"pending_at":"2026-08-20T00:00:00Z"}\n\n\n'),
            exclude_dates=[])
        self.assertEqual(manifest["ndjson_rows"], 1)

    def test_an_unparseable_ndjson_line_is_refused(self):
        with self.assertRaises(baseline.BaselineError) as cm:
            baseline.describe(self.write_set(ndjson="{not json\n"),
                              exclude_dates=[])
        self.assertIn("line 1", str(cm.exception))

    def test_an_empty_ndjson_is_refused(self):
        # A baseline with no rows joins to nothing, and every residual would be
        # NaN -- which trains, and produces a number.
        with self.assertRaises(baseline.BaselineError):
            baseline.describe(self.write_set(ndjson="\n"), exclude_dates=[])


class TestExcludeDatesIsDeclaredNotMeasured(BaselineCase):
    """The one value that cannot be recovered from the artifact: it changes the
    percentile HISTORY the predictor used, not the rows it emitted."""

    def test_it_is_recorded(self):
        manifest = baseline.describe(self.write_set(),
                                     exclude_dates=["2026-07-04"])
        self.assertEqual(manifest["exclude_dates"], ["2026-07-04"])

    def test_the_manifest_says_it_is_declared(self):
        manifest = baseline.describe(self.write_set(), exclude_dates=[])
        self.assertIn("declared", manifest["exclude_dates_provenance"])

    def test_it_changes_the_identity(self):
        # Two baselines over the same window with different exclusions are
        # different baselines, and a comparison must not mix them.
        plain = baseline.describe(self.write_set(), exclude_dates=[])
        filtered = baseline.describe(self.write_set(),
                                     exclude_dates=["2026-07-04"])
        self.assertNotEqual(baseline.baseline_hash(plain),
                            baseline.baseline_hash(filtered))

    def test_the_order_does_not_change_the_identity(self):
        a = baseline.describe(self.write_set(),
                              exclude_dates=["2026-07-04", "2026-07-05"])
        b = baseline.describe(self.write_set(),
                              exclude_dates=["2026-07-05", "2026-07-04"])
        self.assertEqual(baseline.baseline_hash(a), baseline.baseline_hash(b))

    def test_a_malformed_date_is_refused(self):
        for bad in (["2026-7-4"], ["yesterday"], [20260704], [""], "2026-07-04"):
            with self.subTest(exclude=bad):
                with self.assertRaises(baseline.BaselineError):
                    baseline.describe(self.write_set(), exclude_dates=bad)


class TestTheIdentityIsAContentKey(BaselineCase):
    def test_identical_files_give_one_identity(self):
        # So promoting twice is a no-op rather than a second artifact.
        first = baseline.describe(self.write_set(), exclude_dates=[])
        second = baseline.describe(self.write_set(), exclude_dates=[])
        self.assertEqual(baseline.baseline_hash(first),
                         baseline.baseline_hash(second))

    def test_changed_content_gives_a_different_identity(self):
        first = baseline.describe(self.write_set(rows=3), exclude_dates=[])
        second = baseline.describe(self.write_set(rows=4), exclude_dates=[])
        self.assertNotEqual(baseline.baseline_hash(first),
                            baseline.baseline_hash(second))

    def test_an_added_day_gives_a_different_identity(self):
        first = baseline.describe(self.write_set(), exclude_dates=[])
        second = baseline.describe(
            self.write_set(days=("2026-08-20", "2026-08-21", "2026-08-22")),
            exclude_dates=[])
        self.assertNotEqual(baseline.baseline_hash(first),
                            baseline.baseline_hash(second))

    def test_the_hash_does_not_cover_itself(self):
        manifest = baseline.describe(self.write_set(), exclude_dates=[])
        manifest["baseline_hash"] = baseline.baseline_hash(manifest)
        body = {k: v for k, v in manifest.items() if k != "baseline_hash"}
        self.assertEqual(
            manifest["baseline_hash"],
            hashlib.sha256(json.dumps(body, sort_keys=True,
                                      separators=(",", ":")).encode()
                           ).hexdigest())

    def test_it_is_hex_sha256(self):
        h = baseline.baseline_hash(
            baseline.describe(self.write_set(), exclude_dates=[]))
        self.assertEqual(len(h), 64)
        int(h, 16)

    def test_the_promotion_timestamp_is_not_part_of_the_identity(self):
        # Otherwise promoting the same files twice would produce two artifacts,
        # which is the whole thing a content key exists to prevent.
        manifest = baseline.describe(self.write_set(), exclude_dates=[])
        self.assertNotIn("promoted_at", manifest)


if __name__ == "__main__":
    unittest.main()
