# Tests for the metrics readout that travels beside a verdict.
#
# WHY THIS EXISTS AS A BOUNDARY AT ALL. The full verdict document is written into
# the evaluate run's `out/`, which is qfrun 2770 -- unreadable by the identity
# that submits experiments. That identity saw one word. A research loop cannot
# pick its next hypothesis from a word, so the numbers come back through the
# reply and are recorded as a pin. The pin is a string this service stores under
# a name other things read, so what gets stored is PROJECTED from the reply, not
# forwarded: these tests are mostly about what does NOT make it through.
import json
import os
import sys
import unittest
from unittest import mock

_HOST = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
# BOTH paths, stated here rather than inherited: `qfd` imports `baseline` from
# `host/shared`, and a test module that relies on a sibling having inserted that
# path passes in the suite and fails when run alone. `test_protocol.py` records
# the same lesson.
sys.path.insert(0, os.path.join(_HOST, "shared"))
sys.path.insert(0, os.path.join(_HOST, "dispatcher"))

import qfd                                                     # noqa: E402


def board(**metrics):
    return {"metrics": metrics,
            "consistency": {"days_required": 3, "days_passed": 4}}


# THE REAL SHAPE, copied from `contracts/wait_time.v1.json` by way of
# `verdict.decide`: `bar` is an OBJECT. An earlier version of this file invented
# `bar: 0.15`, and against that fixture `_scoreboard_pin` looked correct while
# refusing every scoreboard the evaluator actually produces.
MAE = {"value": 425.3, "baseline": 455.9, "measured": 0.0671,
       "bar": {"kind": "relative_improvement", "value": 0.15},
       "direction": "lower_is_better", "passed": False}
BAND = {"value": 0.887, "baseline": None, "measured": 0.887,
        "bar": {"kind": "band", "low": 0.85, "high": 0.95},
        "direction": "band", "passed": True}


class ScoreboardPin(unittest.TestCase):
    def test_a_normal_scoreboard_round_trips(self):
        text = qfd._scoreboard_pin(board(mae=dict(MAE)))
        got = json.loads(text)
        self.assertEqual(got["metrics"]["mae"], MAE)
        self.assertEqual(got["consistency"],
                         {"days_required": 3, "days_passed": 4})

    def test_it_is_compact_and_ordered(self):
        """Stored as one line, deterministically: a pin is compared between runs
        by whoever reads two of them."""
        text = qfd._scoreboard_pin(board(zeta=dict(MAE), alpha=dict(MAE)))
        self.assertNotIn("\n", text)
        self.assertNotIn(", ", text)
        self.assertLess(text.index("alpha"), text.index("zeta"))

    def test_a_band_bar_keeps_both_edges(self):
        got = json.loads(qfd._scoreboard_pin(board(p90_coverage=dict(BAND))))
        self.assertEqual(got["metrics"]["p90_coverage"]["bar"],
                         {"kind": "band", "low": 0.85, "high": 0.95})

    def test_a_bar_that_is_not_an_object_is_refused(self):
        """Not coerced. A bare `0.15` does not say whether it is an absolute
        threshold or a relative improvement, and guessing is how a scoreboard
        would report a bar the contract never set."""
        for bad in (0.15, "0.15", None, {}, {"kind": 1}, {"value": "x"},
                    {"value": float("inf")}):
            self.assertIsNone(qfd._scoreboard_pin(board(mae=dict(MAE,
                                                                 bar=bad))),
                              bad)

    def test_a_bucket_metric_keeps_its_bucket(self):
        spec = dict(MAE, bucket="30m+")
        got = json.loads(qfd._scoreboard_pin(board(p90_miss_tail=spec)))
        self.assertEqual(got["metrics"]["p90_miss_tail"]["bucket"], "30m+")

    def test_passed_must_be_a_real_bool(self):
        """THE FIELD THAT BECOMES A WORD. `"false"` and `1` are both truthy in
        python, so a generic type policy plus a truthy render printed PASS for a
        metric that had failed -- a validator whose output inverts the thing it
        describes."""
        for bad in ("false", "true", 1, 0, None, [], {}):
            self.assertIsNone(
                qfd._scoreboard_pin(board(mae=dict(MAE, passed=bad))), bad)

    def test_a_bar_with_no_kind_is_refused(self):
        """A bar printed without its kind is a number whose rule nobody can look
        up: `bar=0.15` does not say whether that is 15% better than baseline or
        an absolute 0.15."""
        self.assertIsNone(qfd._scoreboard_pin(
            board(mae=dict(MAE, bar={"value": 0.15}))))

    def test_an_unknown_bar_kind_is_refused(self):
        self.assertIsNone(qfd._scoreboard_pin(
            board(mae=dict(MAE, bar={"kind": "vibes", "value": 0.15}))))

    def test_the_kind_decides_the_bar_shape(self):
        """Same rule `contract.py` applies to the same object: `band` carries
        low/high, every other kind carries value. Crossing them is a typo that
        would otherwise print as a bar the contract never set."""
        for bad in ({"kind": "band", "value": 0.15},
                    {"kind": "band", "low": 0.85},
                    {"kind": "band", "low": 0.85, "high": 0.95,
                     "value": 0.15},
                    {"kind": "absolute", "low": 0.85, "high": 0.95},
                    {"kind": "absolute", "value": 0.3, "high": 0.95},
                    {"kind": "absolute"},
                    {"kind": "absolute", "value": 0.3, "extra": 1}):
            self.assertIsNone(
                qfd._scoreboard_pin(board(mae=dict(MAE, bar=bad))), bad)

    def test_a_direction_outside_the_contract_vocabulary_is_refused(self):
        """`direction` is a contract field and `contract.BAR_KINDS`/`DIRECTIONS`
        own what it may say, so this is checked against that list rather than
        against "is a string"."""
        for bad in ("lower", "LOWER_IS_BETTER", "", 1, True):
            self.assertIsNone(
                qfd._scoreboard_pin(board(mae=dict(MAE, direction=bad))), bad)

    def test_a_metric_missing_a_required_field_is_refused(self):
        for field in ("passed", "bar", "value", "measured"):
            spec = dict(MAE)
            del spec[field]
            self.assertIsNone(qfd._scoreboard_pin(board(mae=spec)), field)

    def test_a_null_baseline_is_kept_as_null(self):
        """A metric with no baseline is a real case (an absolute bar needs
        none), and it is different from a metric whose baseline was dropped."""
        got = json.loads(qfd._scoreboard_pin(board(mae=dict(MAE,
                                                            baseline=None))))
        self.assertIsNone(got["metrics"]["mae"]["baseline"])
        self.assertIn("baseline", got["metrics"]["mae"])

    def test_a_non_numeric_number_is_refused_not_dropped(self):
        """Dropping it would print a scoreboard missing one number, which reads
        as "this metric has no baseline" rather than as "something is wrong"."""
        for field in ("value", "baseline", "measured"):
            for bad in ("0.5", True, [], {}):
                self.assertIsNone(
                    qfd._scoreboard_pin(board(mae=dict(MAE, **{field: bad}))),
                    (field, bad))

    def test_a_bucket_must_look_like_a_bucket(self):
        for bad in ("", "x" * 64, 30, True, "a b"):
            self.assertIsNone(
                qfd._scoreboard_pin(board(mae=dict(MAE, bucket=bad))), bad)

    def test_unknown_fields_are_dropped_not_stored(self):
        """The reply is another service's object. A field nobody chose must not
        arrive in a record everybody cites."""
        got = json.loads(qfd._scoreboard_pin(
            board(mae=dict(MAE, note="hi", nested={"a": 1}))))
        self.assertEqual(got["metrics"]["mae"], MAE)

    def test_non_finite_numbers_are_refused(self):
        """`float('nan')` serialises to JSON that python reads back and nothing
        else does, so a reader in another language would see a parse error
        instead of a missing number."""
        for bad in (float("nan"), float("inf"), float("-inf")):
            self.assertIsNone(qfd._scoreboard_pin(board(mae=dict(MAE,
                                                                 value=bad))))

    def test_shapes_that_are_not_a_scoreboard(self):
        for bad in (None, "no-go", [], {}, {"metrics": None},
                    {"metrics": {}}, {"metrics": []},
                    {"metrics": {"mae": "0.5"}},
                    {"metrics": {"MAE": dict(MAE)}},        # name not lowercase
                    {"metrics": {"a" * 64: dict(MAE)}},     # name too long
                    {"metrics": {"mae": {}}}):              # no fields at all
            self.assertIsNone(qfd._scoreboard_pin(bad), bad)

    def test_too_many_metrics_is_refused(self):
        many = {f"m{i}": dict(MAE)
                for i in range(qfd.SCOREBOARD_MAX_METRICS + 1)}
        self.assertIsNone(qfd._scoreboard_pin({"metrics": many}))

    def test_an_oversized_scoreboard_is_refused(self):
        """The byte cap is a BACKSTOP, not the operative limit.

        The count cap and the per-field truncation together bound a worst-case
        scoreboard well under 8KB, so this drives the cap down to reach it rather
        than contriving a payload that does not occur. A limit no test can reach
        is a limit nobody knows is wrong.
        """
        many = {f"m{i}": dict(MAE, bucket="b" * 31)
                for i in range(qfd.SCOREBOARD_MAX_METRICS)}
        # Measured off the PIN, not off a pretty-printed dump: the pin is
        # written with compact separators, so a size taken from `json.dumps`
        # defaults is larger than the thing being capped and the cap never fires.
        size = len(qfd._scoreboard_pin({"metrics": many}).encode())
        with mock.patch.object(qfd, "SCOREBOARD_MAX_BYTES", size - 1):
            self.assertIsNone(qfd._scoreboard_pin({"metrics": many}))
        self.assertIsNotNone(qfd._scoreboard_pin({"metrics": many}))


    def test_missing_consistency_still_yields_the_metrics(self):
        """The metrics are the point; the day count is context. Losing the
        second must not lose the first."""
        got = json.loads(qfd._scoreboard_pin({"metrics": {"mae": dict(MAE)}}))
        self.assertNotIn("consistency", got)
        self.assertIn("mae", got["metrics"])

    def test_partial_consistency_keeps_what_it_has(self):
        got = json.loads(qfd._scoreboard_pin(
            {"metrics": {"mae": dict(MAE)},
             "consistency": {"days_passed": 4, "days_required": None}}))
        self.assertEqual(got["consistency"], {"days_passed": 4})

    def test_booleans_are_not_counted_as_day_numbers(self):
        """`isinstance(True, int)` is True in python, and `days_passed: true`
        would print as a day count."""
        got = json.loads(qfd._scoreboard_pin(
            {"metrics": {"mae": dict(MAE)},
             "consistency": {"days_passed": True, "days_required": 3}}))
        self.assertEqual(got["consistency"], {"days_required": 3})


if __name__ == "__main__":
    unittest.main()
