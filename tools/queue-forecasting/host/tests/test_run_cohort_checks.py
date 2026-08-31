# The three pre-flight refusals in `research-experiments/run_cohort.py`.
#
# WHY THESE ARE WORTH A SUITE. This file is operator-owned code that runs at the
# top of EVERY experiment, inside a container nobody is watching, and its whole
# job is to turn a twenty-minute failure into a one-second one. A check that
# stops refusing is invisible: the run still fails, just expensively and with a
# traceback from three layers down. `check_window` in particular exists because
# that exact failure happened for real on 2026-08-31 -- the hazard config needs
# `train_start <= 2026-08-01` and the frozen extract's window started 08-07 --
# so the first case below is that incident, by its real dates.
#
# The module is loaded by exec rather than imported: it lives outside `host/`
# (it is copied into the agent's repo), it has no package, and its `main()`
# expects container mounts that do not exist here.
import datetime
import os
import pathlib
import types
import unittest

_HOST = pathlib.Path(__file__).resolve().parent.parent
_SOURCE = _HOST / "research-experiments" / "run_cohort.py"

# Narrow: the Series A extract. Wide: Series B. Real published windows.
NARROW = ("2026-08-07T00:00:00Z", "2026-08-27T00:00:00Z")
WIDE = ("2026-07-21T00:00:00Z", "2026-08-26T00:00:00Z")

# validation_days is the key that separates them: 1 for every quantile config,
# 7 for the hazard config, and that difference is exactly 6 days of train_start.
QUANTILE = {"holdout_days": 5, "validation_days": 1, "lookback_days": 14}
HAZARD = {"holdout_days": 5, "validation_days": 7, "lookback_days": 14}


def load(manifest):
    """The module with `manifest()` stubbed, and `main()` never invoked."""
    mod = types.ModuleType("run_cohort")
    source = _SOURCE.read_text().replace("raise SystemExit(main())", "pass")
    exec(compile(source, str(_SOURCE), "exec"), mod.__dict__)
    mod.manifest = lambda: manifest
    return mod


def window(train_start, as_of):
    return {"request": {"train_start": train_start, "as_of_date": as_of}}


class CheckWindow(unittest.TestCase):
    def refuse(self, extract, config):
        mod = load(window(*extract))
        with self.assertRaises(SystemExit) as caught:
            mod.check_window(dict(config))
        return str(caught.exception)

    def allow(self, extract, config):
        load(window(*extract)).check_window(dict(config))

    def test_the_2026_08_31_incident(self):
        """The real one, by its real dates: the hazard config against Series A.

        `extract_source._covers` reported these same two dates from inside a
        loaded dataframe. Both numbers appear in the message because an operator
        reading it needs to know which extract to reach for, not just that this
        one is wrong.
        """
        message = self.refuse(NARROW, HAZARD)
        self.assertIn("2026-08-01", message)
        self.assertIn("2026-08-07", message)

    def test_it_shows_the_arithmetic_that_produced_the_date(self):
        """A date with no derivation is a date nobody can argue with. The three
        window keys sum to the offset, and naming them is what tells a reader
        that `validation_days` is the term that moved."""
        message = self.refuse(NARROW, HAZARD)
        for fragment in ("holdout 5", "validation 7", "lookback 14", "26 days"):
            self.assertIn(fragment, message)

    def test_it_forbids_the_tempting_fix(self):
        """The cheap way out is editing `validation_days` down until the run
        starts, which silently turns the experiment into a different one. The
        message has to say so, because the agent reading it is the one holding
        the config."""
        message = self.refuse(NARROW, HAZARD)
        self.assertIn("DO NOT shrink", message)
        self.assertIn("OPERATOR", message)

    def test_the_combinations_that_must_run(self):
        for extract, config, label in (
                (NARROW, QUANTILE, "Series A + quantile"),
                (WIDE, HAZARD, "Series B + hazard"),
                (WIDE, QUANTILE, "Series B + quantile")):
            with self.subTest(label):
                self.allow(extract, config)

    def test_an_exactly_covering_window_is_allowed(self):
        """The boundary is `<=`, not `<`. An extract whose window starts on the
        very day the cohort needs is sufficient, and refusing it would send an
        operator to widen an extract that was already wide enough."""
        self.allow(("2026-08-01T00:00:00Z", "2026-08-27T00:00:00Z"), HAZARD)
        self.refuse(("2026-08-02T00:00:00Z", "2026-08-27T00:00:00Z"), HAZARD)

    def test_it_has_no_opinion_on_a_config_it_cannot_read(self):
        """Not this check's business to validate the config -- the trainer owns
        that. Substituting a default for a missing key would compute a
        train_start the trainer never computes, and then refuse a run that would
        have worked (or pass one that will not).

        `True` is here because `isinstance(True, int)` holds in python, and
        `validation_days: true` would otherwise sum as one day.
        """
        for config in ({}, {"holdout_days": 5},
                       dict(HAZARD, validation_days=None),
                       dict(HAZARD, validation_days=True),
                       dict(HAZARD, validation_days="7"),
                       dict(HAZARD, lookback_days=-1)):
            with self.subTest(repr(config)):
                self.allow(NARROW, config)

    def test_an_unreadable_manifest_is_not_a_refusal(self):
        """A manifest this cannot parse is a different fault with a different
        owner: `cohort_as_of` refuses a missing `as_of_date` by itself, and
        `_covers` still guards the real load. Refusing here would report a
        window problem for something that is not one."""
        for extract in (("garbage", "2026-08-27T00:00:00Z"),
                        ("2026-08-07T00:00:00Z", "nope"),
                        (None, None), ("", "")):
            with self.subTest(repr(extract)):
                self.allow(extract, HAZARD)
        load({}).check_window(dict(HAZARD))
        load({"request": {}}).check_window(dict(HAZARD))

    def test_a_naive_manifest_timestamp_is_treated_as_utc(self):
        """Every boundary in this system is a UTC day boundary. Comparing an
        aware datetime to a naive one raises TypeError, which would surface as a
        crash in the first second of every run rather than as a check."""
        self.allow(("2026-07-21T00:00:00", "2026-08-26T00:00:00"), HAZARD)
        self.refuse(("2026-08-07T00:00:00", "2026-08-27T00:00:00"), HAZARD)


class CheckTargetAndQctx(unittest.TestCase):
    """The two checks that shipped without tests. Same contract as the above:
    refuse from the manifest, in the first second."""

    def test_a_target_mismatch_is_refused(self):
        mod = load({"request": {"target": "run_duration"}})
        with self.assertRaises(SystemExit) as caught:
            mod.check_target("wait_time")
        self.assertIn("run_duration", str(caught.exception))
        self.assertIn("wait_time", str(caught.exception))

    def test_a_matching_target_passes(self):
        load({"request": {"target": "wait_time"}}).check_target("wait_time")

    def test_qctx_without_task_created_is_refused(self):
        mod = load({"files": {"qctx_runs": {"columns": ["task_id",
                                                        "pending_at"]}}})
        with self.assertRaises(SystemExit) as caught:
            mod.check_qctx({"queue_context_features": {"enabled": True}})
        self.assertIn("task_created", str(caught.exception))
        # The remedy is the non-obvious part: `request_hash` does not cover the
        # column list, so re-requesting the same window is a cache hit.
        self.assertIn("generation", str(caught.exception))

    def test_qctx_with_task_created_passes(self):
        load({"files": {"qctx_runs": {"columns": ["task_id",
                                                  "task_created"]}}}).check_qctx(
            {"queue_context_features": {"enabled": True}})

    def test_a_config_not_asking_for_qctx_is_never_refused(self):
        """Checked against an extract that could not serve it: the check must
        key on the config, not on the extract."""
        mod = load({"files": {"qctx_runs": {"columns": []}}})
        for config in ({}, {"queue_context_features": {}},
                       {"queue_context_features": {"enabled": False}},
                       {"queue_context_features": None}):
            with self.subTest(repr(config)):
                mod.check_qctx(config)


class TheCheckIsWired(unittest.TestCase):
    def test_main_calls_all_three(self):
        """A check nothing calls is a check that does not run. Asserted on the
        source because `main()` needs container mounts to reach the call site.
        """
        source = _SOURCE.read_text()
        body = source[source.index("def main()"):]
        for call in ("check_target(", "check_qctx(", "check_window("):
            self.assertIn(call, body, call)


if __name__ == "__main__":
    unittest.main()
