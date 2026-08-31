# Input resolution for one experiment: which extract, baseline and contract.
#
# WHY THE FIXTURES ARE REAL. Every extract below is one the host actually
# published on 2026-08-31, with its real window, generation and column list,
# and the history counts are the real scored evaluations. The resolution rule
# is a judgement call encoded as a sort, so a synthetic fixture would only
# prove the sort runs -- these prove it reaches the same conclusions a human
# reached by hand, including the one that took two failed runs to find.
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import experiment as X                                          # noqa: E402

QCTX = ["task_id", "run_id", "pending_at", "started_at", "resolved_at",
        "priority_at_pending", "task_queue_id", "repo_family", "task_created"]
GEN1 = QCTX[:-1]                        # before `task_created` landed

# `qf extracts` on the host, 2026-08-31.
NARROW_GEN2 = dict(
    request_hash="bd29b39a" + "b" * 56, target="wait_time",
    train_start="2026-08-07T00:00:00Z", as_of_date="2026-08-27T00:00:00Z",
    generation=2, lookback_days=30, columns={"qctx_runs": QCTX},
    snapshot_start_ts="2026-08-30T15:02:09Z")
NARROW_GEN1 = dict(
    request_hash="cd467b4b" + "c" * 56, target="wait_time",
    train_start="2026-08-07T00:00:00Z", as_of_date="2026-08-27T00:00:00Z",
    generation=1, lookback_days=30, columns={"qctx_runs": GEN1},
    snapshot_start_ts="2026-08-29T12:00:00Z")
WIDE_GEN2 = dict(
    request_hash="c179c7f5" + "d" * 56, target="wait_time",
    train_start="2026-07-21T00:00:00Z", as_of_date="2026-08-26T00:00:00Z",
    generation=2, lookback_days=30, columns={"qctx_runs": QCTX},
    snapshot_start_ts="2026-08-30T18:00:00Z")
WIDE_GEN1 = dict(
    request_hash="8e94d833" + "e" * 56, target="wait_time",
    train_start="2026-07-21T00:00:00Z", as_of_date="2026-08-26T00:00:00Z",
    generation=1, lookback_days=30, columns={"qctx_runs": GEN1},
    snapshot_start_ts="2026-08-29T00:00:00Z")
ALL = [WIDE_GEN1, NARROW_GEN2, WIDE_GEN2, NARROW_GEN1]

BASELINE = dict(baseline_hash="e51a3210" + "f" * 56, broken=False,
                promoted_at="2026-08-28T00:00:00Z")
CONTRACT = dict(contract_hash="f740716d" + "a" * 56, target="wait_time",
                created_at="2026-08-28T00:00:00Z")

# 20d: every quantile wait config. 26d: the hazard config's validation_days: 7.
QUANTILE = dict(path="q.yaml", target="wait_time", model_type="lightgbm",
                qctx=True, holdout_days=5, validation_days=1,
                lookback_days=14, cohort_span_days=20)
HAZARD = dict(QUANTILE, path="h.yaml", model_type="discrete_hazard",
              validation_days=7, cohort_span_days=26)


def history(*hashes):
    return [{"pins": {"request_hash": h,
                      "baseline_hash": BASELINE["baseline_hash"],
                      "contract_hash": CONTRACT["contract_hash"]}}
            for h in hashes]


REAL_HISTORY = history(*([NARROW_GEN2["request_hash"]] * 3
                         + [NARROW_GEN1["request_hash"]] * 6))


class ChooseExtract(unittest.TestCase):
    def choose(self, config, extracts=None, hist=REAL_HISTORY):
        counts = X.usage_counts(hist)["extract"]
        return X.choose_extract(config, extracts or ALL, counts)

    def test_a_quantile_config_gets_the_incumbent_series(self):
        """Not the widest and not the newest: the one three scored runs used.

        The wide gen-2 extract can also serve this config, and picking it would
        produce a valid number comparable to nothing.
        """
        chosen, _, runners_up = self.choose(QUANTILE)
        self.assertEqual(chosen["request_hash"], NARROW_GEN2["request_hash"])
        self.assertEqual([e["request_hash"] for e in runners_up],
                         [WIDE_GEN2["request_hash"]])

    def test_the_hazard_config_gets_the_only_one_that_fits(self):
        """The 2026-08-31 failure, resolved instead of hit. Its 26-day cohort
        needs train_start 2026-08-01 and the incumbent window starts 08-07, so
        the wide extract is the only candidate -- and this is the case where
        the most-used extract is the WRONG answer."""
        chosen, rejected, runners_up = self.choose(HAZARD)
        self.assertEqual(chosen["request_hash"], WIDE_GEN2["request_hash"])
        self.assertEqual(runners_up, [])
        self.assertIn(NARROW_GEN2["request_hash"],
                      [e["request_hash"] for e, _ in rejected])

    def test_a_gen1_extract_is_refused_for_a_qctx_config(self):
        """`task_created` is not optional for a queue-context config, and the
        gen-1 extracts of both windows lack it. Same decision
        `run_cohort.check_qctx` makes, made before a container starts."""
        for extract in (WIDE_GEN1, NARROW_GEN1):
            reasons = X.extract_can_serve(QUANTILE, extract)
            self.assertTrue(any("task_created" in r for r in reasons), reasons)

    def test_a_gen1_extract_serves_a_config_that_does_not_ask_for_qctx(self):
        plain = dict(QUANTILE, qctx=False)
        self.assertEqual(X.extract_can_serve(plain, NARROW_GEN1), [])

    def test_usage_counts_beat_a_narrower_window(self):
        """The ordering, asserted directly: if freshness or narrowness came
        first, the incumbent would lose as soon as a tighter extract appeared.
        """
        tighter = dict(WIDE_GEN2, request_hash="0" * 64,
                       train_start="2026-08-08T00:00:00Z",
                       as_of_date="2026-08-27T00:00:00Z",
                       snapshot_start_ts="2026-08-31T00:00:00Z")
        chosen, _, _ = self.choose(QUANTILE, ALL + [tighter])
        self.assertEqual(chosen["request_hash"], NARROW_GEN2["request_hash"])

    def test_with_no_history_the_narrowest_wins(self):
        """A fresh deployment has no usage to count, and then "least data that
        can serve the config" is the only defensible tiebreak."""
        chosen, _, _ = self.choose(QUANTILE, ALL, hist=[])
        self.assertEqual(chosen["request_hash"], NARROW_GEN2["request_hash"])

    def test_a_failed_evaluation_is_not_usage(self):
        """`usage_counts` reads only SUCCEEDED evaluations -- `inventory`
        filters them -- so a pinned input on a run that produced no number must
        not pull the resolver toward it. Asserted at the counting level."""
        self.assertEqual(X.usage_counts([{"pins": {}}, {}, None or {}]),
                         {"extract": {}, "baseline": {}, "contract": {}})

    def test_a_target_mismatch_is_a_reason(self):
        other = dict(NARROW_GEN2, target="run_duration")
        self.assertTrue(any("target" in r
                            for r in X.extract_can_serve(QUANTILE, other)))

    def test_an_unreadable_window_is_a_reason_not_a_crash(self):
        """Overridden AND removed: merging `{}` leaves a valid extract, so the
        absent-key case has to drop the keys rather than blank them."""
        for bad in ({"train_start": None, "as_of_date": None},
                    {"train_start": "nope"},
                    {"as_of_date": ""}):
            with self.subTest(repr(bad)):
                self.assertTrue(
                    X.extract_can_serve(QUANTILE, dict(NARROW_GEN2, **bad)))
        stripped = {k: v for k, v in NARROW_GEN2.items()
                    if k not in ("train_start", "as_of_date")}
        self.assertTrue(X.extract_can_serve(QUANTILE, stripped))
        self.assertTrue(X.extract_can_serve(QUANTILE, {}))


class TheRefusal(unittest.TestCase):
    """When nothing published fits, the output has to be actionable -- this is
    the message that replaces an operator being handed a placeholder."""

    def message(self):
        counts = X.usage_counts(REAL_HISTORY)["extract"]
        with self.assertRaises(X.Refused) as caught:
            X.choose_extract(HAZARD, [NARROW_GEN2, NARROW_GEN1], counts)
        return str(caught.exception)

    def test_it_prints_a_command_with_no_placeholders(self):
        text = self.message()
        self.assertIn("qf extract --target wait_time", text)
        self.assertIn("--train-start 2026-08-01", text)
        self.assertIn("--as-of 2026-08-27", text)
        self.assertIn("--lookback-days 30", text)
        for placeholder in ("<", ">", "..."):
            self.assertNotIn(placeholder, text.split("qf extract")[1],
                             placeholder)

    def test_it_anchors_the_new_window_on_the_existing_series(self):
        """A new extract at a fresh as_of would be runnable and comparable to
        nothing. Anchoring on the incumbent's as_of keeps the holdout
        population identical, so only history widens."""
        self.assertIn("--as-of 2026-08-27", self.message())

    def test_it_says_why_each_candidate_lost(self):
        text = self.message()
        self.assertIn("2026-08-07", text)
        self.assertIn("this cohort needs 2026-08-01", text)
        self.assertIn("task_created", text)

    def test_it_names_the_decision_as_the_operators(self):
        self.assertIn("OPERATOR", self.message())

    def test_an_unpublished_lookback_is_flagged_not_invented(self):
        """Extracts published before 2026-08-31 carry no `lookback_days`, and
        it is part of `request_hash` -- so a wrong guess silently produces a
        different extract. The default is offered WITH a warning."""
        counts = {}
        older = {k: v for k, v in NARROW_GEN2.items() if k != "lookback_days"}
        with self.assertRaises(X.Refused) as caught:
            X.choose_extract(HAZARD, [older], counts)
        self.assertIn("lookback_days is not published", str(caught.exception))


class ChooseBaselineAndContract(unittest.TestCase):
    def test_a_broken_baseline_is_never_chosen(self):
        broken = dict(BASELINE, baseline_hash="1" * 64, broken=True,
                      promoted_at="2026-08-31T00:00:00Z")
        chosen = X.choose_baseline([broken, BASELINE], {})
        self.assertEqual(chosen["baseline_hash"], BASELINE["baseline_hash"])

    def test_all_broken_is_a_refusal_naming_the_operator_step(self):
        with self.assertRaises(X.Refused) as caught:
            X.choose_baseline([dict(BASELINE, broken=True)], {})
        self.assertIn("promote-baseline.sh", str(caught.exception))

    def test_usage_beats_recency_for_a_baseline_too(self):
        newer = dict(BASELINE, baseline_hash="2" * 64,
                     promoted_at="2026-08-31T00:00:00Z")
        counts = {BASELINE["baseline_hash"]: 9}
        self.assertEqual(
            X.choose_baseline([newer, BASELINE], counts)["baseline_hash"],
            BASELINE["baseline_hash"])

    def test_with_no_usage_the_newest_baseline_wins(self):
        newer = dict(BASELINE, baseline_hash="2" * 64,
                     promoted_at="2026-08-31T00:00:00Z")
        self.assertEqual(
            X.choose_baseline([BASELINE, newer], {})["baseline_hash"],
            newer["baseline_hash"])

    def test_a_contract_for_another_target_is_not_used(self):
        with self.assertRaises(X.Refused) as caught:
            X.choose_contract("wait_time",
                              [dict(CONTRACT, target="run_duration")], {})
        self.assertIn("instantiate-contract.sh", str(caught.exception))


class ReadConfig(unittest.TestCase):
    """The window keys come off real configs, and a key this cannot read with
    confidence must refuse rather than default."""

    REPO = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))

    def config(self, name):
        return os.path.join(self.REPO, "trainer", "configs", name)

    def test_the_two_real_configs_that_differ_only_in_validation_days(self):
        quantile = X.read_config(self.config("wait_qctx_d_priority_flow.yaml"))
        hazard = X.read_config(
            self.config("wait_hazard_qctx_d_priority_flow.yaml"))
        self.assertEqual(quantile["cohort_span_days"], 20)
        self.assertEqual(hazard["cohort_span_days"], 26)
        self.assertEqual(hazard["cohort_span_days"]
                         - quantile["cohort_span_days"], 6)
        for config in (quantile, hazard):
            self.assertTrue(config["qctx"])
            self.assertEqual(config["target"], "wait_time")
        self.assertEqual(hazard["model_type"], "discrete_hazard")

    def test_a_config_without_qctx_reads_as_false(self):
        config = X.read_config(self.config("wait_time_residual.yaml"))
        self.assertFalse(config["qctx"])

    def test_a_missing_window_key_refuses(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".yaml",
                                         delete=False) as fh:
            fh.write("target: wait_time\nholdout_days: 5\n")
            path = fh.name
        try:
            with self.assertRaises(X.Refused) as caught:
                X.read_config(path)
            self.assertIn("lookback_days", str(caught.exception))
            self.assertIn("validation_days", str(caught.exception))
        finally:
            os.unlink(path)


class TheLineParserFallback(unittest.TestCase):
    """Exercised directly, because it only runs where PyYAML is absent -- which
    is the research user's interpreter, not this suite's."""

    def test_it_reads_the_real_configs_the_same_way_yaml_does(self):
        repo = ReadConfig.REPO
        for name in ("wait_qctx_d_priority_flow.yaml",
                     "wait_hazard_qctx_d_priority_flow.yaml",
                     "wait_time_residual.yaml",
                     "wait_time_residual_throughput_filtered_baseline.yaml"):
            path = os.path.join(repo, "trainer", "configs", name)
            with open(path) as fh:
                text = fh.read()
            with self.subTest(name):
                for key in X.WINDOW_KEYS:
                    self.assertEqual(X._scalar_int(text, key),
                                     X.read_config(path)[key], key)
                self.assertEqual(
                    X._nested_flag(text, "queue_context_features", "enabled"),
                    X.read_config(path)["qctx"])

    def test_a_nested_flag_does_not_match_under_another_parent(self):
        text = ("throughput_features:\n  enabled: true\n"
                "queue_context_features:\n  version: 1\n")
        self.assertFalse(X._nested_flag(text, "queue_context_features",
                                        "enabled"))
        self.assertTrue(X._nested_flag(text, "throughput_features", "enabled"))

    def test_a_duplicated_key_reads_as_unknown_not_as_the_first(self):
        """Two `holdout_days:` lines is a broken config, and picking one would
        resolve an extract for a window nobody wrote."""
        self.assertIsNone(X._scalar_int("holdout_days: 5\nholdout_days: 9\n",
                                        "holdout_days"))

    def test_a_commented_out_key_is_not_read(self):
        self.assertIsNone(X._scalar_int("# holdout_days: 5\n", "holdout_days"))

    def test_a_trailing_comment_is_tolerated(self):
        self.assertEqual(X._scalar_int("holdout_days: 5   # five\n",
                                       "holdout_days"), 5)


class Plan(unittest.TestCase):
    def resolved(self, config, extracts=None):
        return X.plan(config, extracts or ALL, [BASELINE], [CONTRACT],
                      REAL_HISTORY)

    def test_it_reports_the_cohorts_own_train_start(self):
        self.assertEqual(self.resolved(QUANTILE)["cohort_train_start"],
                         "2026-08-07")
        self.assertEqual(self.resolved(HAZARD)["cohort_train_start"],
                         "2026-07-31")

    def test_it_warns_when_the_chosen_extract_has_no_scored_runs(self):
        """The hazard config resolves to an extract nothing has been scored on,
        so its result compares to nothing until an anchor is run there. That
        has to be said, not inferred from a count."""
        text = X.render_plan(self.resolved(HAZARD))
        self.assertIn("comparable to NOTHING", text)
        self.assertEqual(self.resolved(HAZARD)["scored_runs_here"], 0)

    def test_it_says_so_when_the_extract_is_in_the_series(self):
        text = X.render_plan(self.resolved(QUANTILE))
        self.assertIn("3 scored run(s)", text)
        self.assertNotIn("comparable to NOTHING", text)

    def test_the_plan_names_every_hash_in_full(self):
        """Truncated hashes are what `qf` refuses; a plan a caller can act on
        has to carry the copyable value."""
        text = X.render_plan(self.resolved(QUANTILE))
        for full in (NARROW_GEN2["request_hash"], BASELINE["baseline_hash"],
                     CONTRACT["contract_hash"]):
            self.assertIn(full, text)


if __name__ == "__main__":
    unittest.main()
