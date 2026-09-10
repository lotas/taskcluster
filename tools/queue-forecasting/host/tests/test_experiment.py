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
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import experiment as X                                          # noqa: E402
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dispatcher"))
import spec                                                      # noqa: E402


def can_commit():
    """Whether `git commit` works here at all.

    Some sandboxes refuse commits outright ("Commits are disabled in
    devtainer"). Skipped rather than deleted: these assertions are about git's
    behaviour with an UNCONFIGURED identity, which is exactly what broke on the
    host, so they have to keep running wherever they can run.
    """
    import shutil
    import subprocess
    import tempfile
    root = tempfile.mkdtemp()
    try:
        subprocess.run(["git", "init", "-q", "-b", "main", root],
                       capture_output=True)
        with open(os.path.join(root, "f"), "w") as fh:
            fh.write("x")
        subprocess.run(["git", "-C", root, "add", "-A"], capture_output=True)
        done = subprocess.run(["git", "-C", root, *X.GIT_IDENT, "commit",
                               "-m", "probe"], capture_output=True, text=True)
        return done.returncode == 0
    except OSError:
        return False
    finally:
        shutil.rmtree(root, ignore_errors=True)


CAN_COMMIT = can_commit()

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
                created_at="2026-08-28T00:00:00Z",
                # PINNED, because every published contract is: `baseline_hash`
                # is required by `shared/contract.py` and `qf contracts` only
                # lists files that validate. A fixture without it would be a
                # contract that cannot exist.
                baseline_hash=BASELINE["baseline_hash"])

# THE PAIR. `wait_time.v1.json` pins `baseline_hash`, and a v2 published
# against a newly promoted baseline pins the NEW one -- which is the whole
# reason the contract has to be resolved before the baseline.
BASELINE_V2 = dict(baseline_hash="7c0ffee0" + "d" * 56, broken=False,
                   promoted_at="2026-09-09T00:00:00Z")
CONTRACT_V1 = dict(CONTRACT, name="wait_time_v1")
CONTRACT_V2 = dict(contract_hash="3ab19c2e" + "b" * 56, target="wait_time",
                   name="wait_time_v2", created_at="2026-09-09T00:00:00Z",
                   baseline_hash=BASELINE_V2["baseline_hash"])

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
        self.assertIn("--lookback-days 30", text)

    def test_the_dates_are_in_the_form_qf_accepts(self):
        """`qf` refuses a bare date: "train_start must look like
        2026-08-01T00:00:00Z". A generated command that needs editing is a
        placeholder with extra steps -- this shipped once and cost two
        attempts."""
        text = self.message()
        self.assertIn("--train-start 2026-08-01T00:00:00Z", text)
        self.assertIn("--as-of 2026-08-27T00:00:00Z", text)
        for flag in ("--train-start", "--as-of"):
            value = text.split(flag, 1)[1].split()[0]
            self.assertRegex(value, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
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


class ChooseContract(unittest.TestCase):
    """There is no baseline RANKING to test any more, and that is the fix.

    `choose_baseline` -- most-used, not-broken, newest as a tiebreak -- was
    deleted with the defect. A baseline is not a thing this resolver picks: the
    contract pins it, so what used to be four ranking tests is now
    `ContractDecidesTheBaseline`.
    """

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


class NamedExtract(unittest.TestCase):
    """`--extract`: the declared exit from the usage-count ordering.

    The gap it closes is not hypothetical. `choose_extract` ranks by scored-run
    usage count first, so the extract a config family has already used wins for
    that family forever -- and the only thing a family blocked on "needs a
    second non-overlapping cohort" needs is the extract usage count will never
    pick. Three PROMISING configs sat on that from 2026-09-08 to 2026-09-10,
    with the loop re-deriving the blocker every tick.
    """

    def named(self, config, request_hash, extracts=None):
        return X.named_extract(config, extracts or ALL, request_hash)

    def test_it_returns_the_named_extract_over_the_incumbent(self):
        """The whole point: usage count says NARROW_GEN2 (3 scored runs), and
        naming WIDE_GEN2 has to beat that, not lose to it."""
        chosen, _, runners_up = self.named(QUANTILE, WIDE_GEN2["request_hash"])
        self.assertEqual(chosen["request_hash"], WIDE_GEN2["request_hash"])
        self.assertIn(NARROW_GEN2["request_hash"],
                      [e["request_hash"] for e in runners_up])

    def test_a_prefix_is_refused_even_when_it_is_unambiguous(self):
        """`qf probe --extract` takes any unique 8+ hex prefix; this does not,
        and the difference is deliberate. Naming a cohort no usage count
        endorses is exactly when a prefix landing on a near-miss would produce
        a number that belongs to no series and reads as if it did."""
        with self.assertRaises(X.Refused) as caught:
            self.named(QUANTILE, WIDE_GEN2["request_hash"][:12])
        self.assertIn("full 64-hex", str(caught.exception))

    def test_an_unpublished_hash_is_refused_and_the_real_ones_listed(self):
        with self.assertRaises(X.Refused) as caught:
            self.named(QUANTILE, "9" * 64)
        message = str(caught.exception)
        self.assertIn("no published extract", message)
        self.assertIn(WIDE_GEN2["request_hash"][:12], message)

    def test_naming_overrides_the_ranking_and_not_the_requirements(self):
        """The hazard config's 26-day cohort needs train_start 2026-08-01 and
        NARROW_GEN2 starts 08-07. An override that skipped `extract_can_serve`
        would submit a probe that cannot train, which is the 2026-08-31
        failure with a flag on it."""
        with self.assertRaises(X.Refused) as caught:
            self.named(HAZARD, NARROW_GEN2["request_hash"])
        message = str(caught.exception)
        self.assertIn("cannot serve", message)
        self.assertIn("2026-08-01", message)

    def test_a_gen1_extract_is_refused_for_a_qctx_config(self):
        with self.assertRaises(X.Refused) as caught:
            self.named(QUANTILE, WIDE_GEN1["request_hash"])
        self.assertIn("task_created", str(caught.exception))

    def test_plan_counts_scored_runs_on_the_NAMED_extract(self):
        """The count must follow the extract that will actually be probed. If
        it kept reporting the incumbent's 3, the plan would promise
        comparability the run does not have -- the one thing an override must
        never do quietly."""
        resolved = X.plan(QUANTILE, ALL, [BASELINE], [CONTRACT], REAL_HISTORY,
                          named=WIDE_GEN2["request_hash"])
        self.assertEqual(resolved["extract"]["request_hash"],
                         WIDE_GEN2["request_hash"])
        self.assertEqual(resolved["scored_runs_here"], 0)
        self.assertTrue(resolved["extract_named"])

    def test_the_plan_says_it_was_named_and_still_warns(self):
        text = X.render_plan(
            X.plan(QUANTILE, ALL, [BASELINE], [CONTRACT], REAL_HISTORY,
                   named=WIDE_GEN2["request_hash"]))
        self.assertIn("NAMED with --extract", text)
        self.assertIn("comparable to NOTHING", text)
        self.assertIn(WIDE_GEN2["request_hash"], text)

    def test_the_ordinary_path_is_unchanged_and_says_it_was_not_named(self):
        resolved = X.plan(QUANTILE, ALL, [BASELINE], [CONTRACT], REAL_HISTORY)
        self.assertEqual(resolved["extract"]["request_hash"],
                         NARROW_GEN2["request_hash"])
        self.assertFalse(resolved["extract_named"])
        self.assertNotIn("NAMED with --extract", X.render_plan(resolved))

    def test_both_plan_and_run_accept_the_flag(self):
        """Asserted through `--help`, which is how the agent discovers it.

        On `plan` as well as `run`, because reading what a named cohort
        resolves to must not cost a probe -- and the 2026-09-10 escalation
        established the blocker by pasting exactly this output, so the help
        text IS the interface here.
        """
        import subprocess
        for command in ("plan", "run"):
            done = subprocess.run(
                [sys.executable, X.__file__, command, "--help"],
                capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("--extract", done.stdout)


class ContractDecidesTheBaseline(unittest.TestCase):
    """A contract and its baseline are one pair, not two rankings.

    THE ROLLOUT BLOCKER THIS PINS. `plan` used to rank baselines and contracts
    independently, each by scored-run usage. A contract PINS `baseline_hash`
    (`shared/contract.py:_REQUIRED`) and `evaluator/evaluate.py` refuses to
    judge a run whose recorded baseline is not the one its contract names, so
    the moment an operator published a contract pinned to a NEWLY promoted
    baseline, the resolver paired it with the old most-used baseline and every
    evaluation would have been refused -- after the probe was spent.

    Both counts below are usage as it would really stand: v1 has ten scored
    evaluations behind it, v2 has none, because it was published this morning.
    """

    # TEN scored evaluations under v1 and the OLD baseline, and TWELVE under
    # another target's contract against the NEW baseline. Built so the two
    # rankings DISAGREE: `choose_contract` says v1 for wait_time, while
    # `choose_baseline` -- which never filtered by target -- says BASELINE_V2.
    # A resolver that ranks them separately therefore pairs v1 with the
    # baseline v1 does not name, and that pair is unjudgeable.
    HISTORY = (
        [{"pins": {"request_hash": NARROW_GEN2["request_hash"],
                   "baseline_hash": BASELINE["baseline_hash"],
                   "contract_hash": CONTRACT_V1["contract_hash"]}}] * 10
        + [{"pins": {"request_hash": WIDE_GEN2["request_hash"],
                     "baseline_hash": BASELINE_V2["baseline_hash"],
                     "contract_hash": "d" * 64}}] * 12)

    BASELINES = [BASELINE, BASELINE_V2]
    CONTRACTS = [CONTRACT_V1, CONTRACT_V2]

    def resolve(self, contract_hash=None, baselines=None, contracts=None):
        return X.plan(QUANTILE, ALL,
                      self.BASELINES if baselines is None else baselines,
                      self.CONTRACTS if contracts is None else contracts,
                      self.HISTORY, contract_hash=contract_hash)

    def test_the_default_picks_the_used_contract_and_ITS_baseline(self):
        """Usage still decides the contract. What must NOT happen is the
        baseline being decided separately -- here the two disagree, because
        BASELINE_V2 is newer and unused while BASELINE is older and used, so a
        second ranking could land on either for reasons of its own."""
        counts = X.usage_counts(self.HISTORY)
        self.assertGreater(counts["baseline"][BASELINE_V2["baseline_hash"]],
                           counts["baseline"][BASELINE["baseline_hash"]],
                           "fixture must make usage point at the WRONG"
                           " baseline for the contract usage chooses")
        resolved = self.resolve()
        self.assertEqual(resolved["contract"]["contract_hash"],
                         CONTRACT_V1["contract_hash"])
        self.assertEqual(resolved["baseline"]["baseline_hash"],
                         CONTRACT_V1["baseline_hash"])
        self.assertNotEqual(resolved["baseline"]["baseline_hash"],
                            BASELINE_V2["baseline_hash"])
        self.assertFalse(resolved["contract_named"])

    def test_naming_v2_moves_the_baseline_with_it(self):
        """The pairing is the point: naming the contract must not leave the run
        training against the baseline v2 does not name, which is exactly the
        arrangement `evaluate.py` refuses to judge."""
        resolved = self.resolve(contract_hash=CONTRACT_V2["contract_hash"])
        self.assertEqual(resolved["contract"]["contract_hash"],
                         CONTRACT_V2["contract_hash"])
        self.assertEqual(resolved["baseline"]["baseline_hash"],
                         BASELINE_V2["baseline_hash"])
        self.assertTrue(resolved["contract_named"])

    def test_a_new_contract_is_otherwise_unreachable(self):
        """Without the flag, publishing v2 could never cut the loop over:
        usage only grows, so v1 wins forever. This is the same trap `--extract`
        was added for on 2026-09-10."""
        self.assertEqual(self.resolve()["contract"]["contract_hash"],
                         CONTRACT_V1["contract_hash"])
        self.assertEqual(
            self.resolve(contract_hash=CONTRACT_V2["contract_hash"])
            ["contract"]["contract_hash"],
            CONTRACT_V2["contract_hash"])

    def test_a_pinned_baseline_that_is_not_published_is_refused(self):
        """Refused HERE, naming both hashes, rather than resolving to something
        the evaluator will reject twenty minutes later."""
        with self.assertRaises(X.Refused) as caught:
            self.resolve(contract_hash=CONTRACT_V2["contract_hash"],
                         baselines=[BASELINE])
        message = str(caught.exception)
        self.assertIn(CONTRACT_V2["contract_hash"][:12], message)
        self.assertIn(BASELINE_V2["baseline_hash"][:12], message)
        self.assertIn("promote-baseline.sh", message)

    def test_a_pinned_baseline_flagged_broken_is_refused(self):
        with self.assertRaises(X.Refused) as caught:
            self.resolve(contract_hash=CONTRACT_V2["contract_hash"],
                         baselines=[BASELINE,
                                    dict(BASELINE_V2, broken=True)])
        message = str(caught.exception)
        self.assertIn("broken", message)
        self.assertIn(BASELINE_V2["baseline_hash"][:12], message)

    def test_a_prefix_is_refused_even_when_it_is_unambiguous(self):
        """Same reasoning as `--extract`: naming a rule no usage count endorses
        is exactly when a prefix landing on a near-miss would judge the run by
        a rule nobody named."""
        with self.assertRaises(X.Refused) as caught:
            self.resolve(contract_hash=CONTRACT_V2["contract_hash"][:12])
        self.assertIn("full 64-hex", str(caught.exception))

    def test_an_unpublished_contract_hash_is_refused_and_the_real_ones_listed(self):
        with self.assertRaises(X.Refused) as caught:
            self.resolve(contract_hash="9" * 64)
        message = str(caught.exception)
        self.assertIn("no published contract", message)
        self.assertIn(CONTRACT_V1["contract_hash"][:12], message)

    def test_a_named_contract_for_another_target_is_refused(self):
        """The override moves the RANKING, not the config. A run_duration
        contract cannot judge a wait_time run, and the evaluator would refuse
        it later anyway -- after the probe was spent."""
        other = dict(CONTRACT_V2, target="run_duration")
        with self.assertRaises(X.Refused) as caught:
            self.resolve(contract_hash=other["contract_hash"],
                         contracts=[CONTRACT_V1, other])
        message = str(caught.exception)
        self.assertIn("run_duration", message)
        self.assertIn("wait_time", message)

    def test_a_row_with_no_pinned_baseline_is_a_READ_failure_and_refuses(self):
        """`baseline_hash` is required by `shared/contract.py`, and
        `qfd.available_contracts` only lists files that validate -- so a listed
        row without one cannot mean "a contract with no baseline". It can only
        mean this process could not read the body, which is a permissions or
        path problem and must say so. Falling back to the most-used baseline
        would rebuild the defect and make `render_plan` claim "pinned by that
        contract" about a baseline no contract named."""
        unread = {"contract_hash": CONTRACT_V1["contract_hash"],
                  "file": "wait_time.v1.json",
                  "target": "wait_time",
                  "body_unreadable": "/srv/qf/contracts/wait_time.v1.json:"
                                     " Permission denied"}
        with self.assertRaises(X.Refused) as caught:
            X.plan(QUANTILE, ALL, self.BASELINES, [unread], self.HISTORY)
        message = str(caught.exception)
        self.assertIn(CONTRACT_V1["contract_hash"][:12], message)
        self.assertIn("Permission denied", message)
        self.assertIn("readable by the identity", message)

    def test_the_plan_says_the_baseline_came_from_the_contract(self):
        text = X.render_plan(
            self.resolve(contract_hash=CONTRACT_V2["contract_hash"]))
        self.assertIn("NAMED with --contract", text)
        self.assertIn(CONTRACT_V2["contract_hash"], text)
        self.assertIn(BASELINE_V2["baseline_hash"], text)
        self.assertIn("pinned by that contract", text)

    def test_both_plan_and_run_accept_the_flag(self):
        """Through `--help`, which is how the agent discovers it -- and on
        `plan` too, because reading what a named contract resolves to must not
        cost a probe."""
        import subprocess
        for command in ("plan", "run"):
            done = subprocess.run(
                [sys.executable, X.__file__, command, "--help"],
                capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("--contract", done.stdout)


class ActiveContractBeatsUsage(unittest.TestCase):
    """`host/contracts/ACTIVE` is how a cutover survives more than one run.

    THE BLOCKER THIS CLOSES. `choose_contract` ranks by scored-run usage and
    usage only grows, so v1 -- with ten evaluations behind it -- wins forever
    and a v2 published this morning is unreachable unattended. `--contract`
    was the answer for ONE invocation, which a loop that runs itself cannot
    use. The documented alternative, deleting `wait_time.v1.json`, is wrong on
    two counts: `research-loop/frontier.py:load_contract` reads v1's BODY to
    interpret the cohorts judged under v1, and the file is tracked so a deploy
    restores it. So every contract stays published and a setting says which is
    current.

    Precedence asserted here, strongest first: `--contract`, ACTIVE, usage.
    """

    HISTORY = ContractDecidesTheBaseline.HISTORY
    BASELINES = [BASELINE, BASELINE_V2]
    CONTRACTS = [CONTRACT_V1, CONTRACT_V2]
    ACTIVE_V2 = {"wait_time": CONTRACT_V2["contract_hash"]}

    def resolve(self, active=None, contract_hash=None, contracts=None,
                active_unresolved=None):
        return X.plan(QUANTILE, ALL, self.BASELINES,
                      self.CONTRACTS if contracts is None else contracts,
                      self.HISTORY, active=active,
                      active_unresolved=active_unresolved,
                      contract_hash=contract_hash)

    def test_the_fixture_makes_usage_choose_v1(self):
        """THE CANARY. Ten scored runs under v1 and none under v2, so every
        assertion below is about ACTIVE overcoming a real incumbent rather
        than about v2 winning a ranking it would have won anyway."""
        counts = X.usage_counts(self.HISTORY)["contract"]
        self.assertEqual(counts.get(CONTRACT_V2["contract_hash"], 0), 0)
        self.assertEqual(counts[CONTRACT_V1["contract_hash"]], 10)
        self.assertEqual(self.resolve()["contract"]["contract_hash"],
                         CONTRACT_V1["contract_hash"])

    def test_active_beats_usage_and_moves_the_baseline_with_it(self):
        """The pairing still holds: a contract PINS its baseline, so activating
        v2 has to move the run onto BASELINE_V2 -- pairing v2 with the
        most-used baseline is the arrangement `evaluate.py` refuses to judge."""
        resolved = self.resolve(active=self.ACTIVE_V2)
        self.assertEqual(resolved["contract"]["contract_hash"],
                         CONTRACT_V2["contract_hash"])
        self.assertEqual(resolved["baseline"]["baseline_hash"],
                         BASELINE_V2["baseline_hash"])
        self.assertTrue(resolved["contract_active"])
        self.assertFalse(resolved["contract_named"])

    def test_a_named_contract_beats_the_active_setting(self):
        """A per-run override a persistent setting could veto would be an
        override in name only -- and `plan` says the flag overrode a live
        cutover rather than letting it look like the ordinary case."""
        resolved = self.resolve(active=self.ACTIVE_V2,
                                contract_hash=CONTRACT_V1["contract_hash"])
        self.assertEqual(resolved["contract"]["contract_hash"],
                         CONTRACT_V1["contract_hash"])
        self.assertTrue(resolved["contract_named"])
        self.assertFalse(resolved["contract_active"])
        text = X.render_plan(resolved)
        self.assertIn("OVERRODE the ACTIVE setting", text)
        self.assertIn(CONTRACT_V2["contract_hash"][:12], text)

    def test_an_entry_for_another_target_does_not_apply(self):
        """`{target: hash}`, keyed by target, so a run_duration cutover must
        leave a wait_time config exactly where it was."""
        resolved = self.resolve(
            active={"run_duration": CONTRACT_V2["contract_hash"]})
        self.assertEqual(resolved["contract"]["contract_hash"],
                         CONTRACT_V1["contract_hash"])
        self.assertFalse(resolved["contract_active"])

    def test_active_naming_an_unpublished_contract_is_REFUSED(self):
        """Not a fallback to ranking. A silent fallback means the operator
        wrote a setting, the loop ignored it, and every result afterwards was
        judged by the rule they meant to replace."""
        with self.assertRaises(X.Refused) as caught:
            self.resolve(active={"wait_time": "9" * 64})
        message = str(caught.exception)
        self.assertIn("ACTIVE", message)
        self.assertIn("999999999999", message)
        self.assertIn(CONTRACT_V1["contract_hash"][:12], message)
        self.assertIn("qf contracts", message)

    def test_active_naming_another_targets_contract_is_REFUSED(self):
        """A published hash under the wrong target key. The dispatcher checks
        that the hash is published; only here is the TARGET known, so this is
        the only place the mismatch can be caught."""
        other = dict(CONTRACT_V2, target="run_duration")
        with self.assertRaises(X.Refused) as caught:
            self.resolve(active={"wait_time": other["contract_hash"]},
                         contracts=[CONTRACT_V1, other])
        self.assertIn("ACTIVE", str(caught.exception))

    def test_an_UNRESOLVED_active_entry_is_refused_not_ranked(self):
        """THE BLOCKER. `qfd` reports a well-formed ACTIVE line naming nothing
        published under `active_unresolved` instead of dropping it, and this is
        the half that makes that reporting matter. Ranking by usage here means
        the operator committed a cutover, the tool said "activated", and every
        experiment afterwards ran under the rule they meant to replace."""
        with self.assertRaises(X.Refused) as caught:
            self.resolve(active_unresolved={"wait_time": "9" * 64})
        message = str(caught.exception)
        self.assertIn("ACTIVE", message)
        self.assertIn("999999999999", message)
        # BOTH CAUSES NAMED, because from here they are indistinguishable and
        # an operator has to check both.
        self.assertIn("deployed", message)
        self.assertIn("0644", message)

    def test_an_unresolved_entry_for_another_target_does_not_refuse(self):
        """Keyed by target like `active`: a stuck run_duration cutover must not
        stop wait_time work."""
        resolved = self.resolve(
            active_unresolved={"run_duration": "9" * 64})
        self.assertEqual(resolved["contract"]["contract_hash"],
                         CONTRACT_V1["contract_hash"])

    def test_an_unresolved_entry_outranks_the_nothing_published_refusal(self):
        """When the contracts directory resolves to nothing, BOTH refusals
        apply and only this one names the setting the operator wrote -- "no
        published contract for wait_time" would send them to publish a
        contract they already published."""
        with self.assertRaises(X.Refused) as caught:
            self.resolve(contracts=[],
                         active_unresolved={"wait_time": "9" * 64})
        self.assertIn("ACTIVE", str(caught.exception))

    def test_no_active_setting_leaves_the_ranking_untouched(self):
        """Every spelling of "not set" is the pre-existing behaviour, because
        that is the state the deployment is in until a commit changes it."""
        for active in (None, {}, {"wait_time": None}, {"wait_time": ""}):
            resolved = self.resolve(active=active)
            self.assertEqual(resolved["contract"]["contract_hash"],
                             CONTRACT_V1["contract_hash"], repr(active))
            self.assertFalse(resolved["contract_active"], repr(active))

    def test_the_plan_states_which_mode_chose_the_contract(self):
        """An operator cannot tell a setting they control from a usage count
        that will keep choosing the incumbent unless the plan says so."""
        chosen = X.render_plan(self.resolve())
        self.assertIn("no ACTIVE contract for wait_time: chosen by usage",
                      chosen)
        self.assertNotIn("ACTIVE (host/contracts/ACTIVE)", chosen)

        active = X.render_plan(self.resolve(active=self.ACTIVE_V2))
        self.assertIn("ACTIVE (host/contracts/ACTIVE)", active)
        self.assertNotIn("chosen by usage", active)

        # AND NOT BOTH. With `--contract` and no ACTIVE entry the contract line
        # already says "(NAMED with --contract)", and "chosen by usage" beside
        # it states two different origins for one contract.
        named = X.render_plan(
            self.resolve(contract_hash=CONTRACT_V2["contract_hash"]))
        self.assertIn("NAMED with --contract", named)
        self.assertNotIn("chosen by usage", named)

    def test_inventory_keeps_the_setting_from_the_contracts_reply(self):
        """`qf contracts --json` carries `active` beside `contracts`, and a
        resolver that dropped it would rank by usage on a host that HAD cut
        over -- the failure being fixed, reintroduced one layer up."""
        replies = {
            "extracts": {"extracts": []},
            "baselines": {"baselines": []},
            "contracts": {"contracts": [], "dir": None,
                          "active": self.ACTIVE_V2,
                          "active_unresolved": {"run_duration": "9" * 64}},
            "list": {"jobs": []},
        }

        def fake_qf(*args, **kw):
            return True, replies[args[0]]

        with mock.patch.object(X, "qf", fake_qf):
            inv = X.inventory(limit=5)
        self.assertEqual(inv["active"], self.ACTIVE_V2)
        self.assertEqual(inv["active_unresolved"], {"run_duration": "9" * 64})

    def test_inventory_defaults_the_setting_when_the_reply_omits_it(self):
        """An older dispatcher, or one whose contracts directory has no ACTIVE
        file. `plan` must get a dict, not a None it would have to guard."""
        replies = {
            "extracts": {"extracts": []},
            "baselines": {"baselines": []},
            "contracts": {"contracts": [], "dir": None},
            "list": {"jobs": []},
        }
        with mock.patch.object(X, "qf", lambda *a, **k: (True, replies[a[0]])):
            inv = X.inventory(limit=5)
        self.assertEqual(inv["active"], {})
        self.assertEqual(inv["active_unresolved"], {})


class ContractRowsAreEnriched(unittest.TestCase):
    """`qf contracts` returns only `{contract_hash, file}` and the directory.

    Every field the resolver decides on -- `target`, and now `baseline_hash` --
    lives in the file, so `inventory` has to read it. `choose_contract` already
    filtered on `target`, and with `target` never present that filter passed
    everything: a `run_duration` contract was a candidate for a `wait_time`
    config.
    """

    def build(self, bodies):
        import json, shutil, tempfile
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        for name, body in bodies.items():
            with open(os.path.join(root, name), "w") as fh:
                fh.write(body if isinstance(body, str) else json.dumps(body))
        return root

    def test_target_name_and_baseline_hash_come_off_the_file(self):
        root = self.build({"wait_time.v2.json": {
            "name": "wait_time_v2", "target": "wait_time",
            "baseline_hash": BASELINE_V2["baseline_hash"]}})
        rows = X.read_contract_bodies(
            [{"contract_hash": CONTRACT_V2["contract_hash"],
              "file": "wait_time.v2.json"}], root)
        self.assertEqual(rows[0]["target"], "wait_time")
        self.assertEqual(rows[0]["name"], "wait_time_v2")
        self.assertEqual(rows[0]["baseline_hash"],
                         BASELINE_V2["baseline_hash"])
        # The row it came in as is not mutated, and the hash is untouched.
        self.assertEqual(rows[0]["contract_hash"],
                         CONTRACT_V2["contract_hash"])

    def test_an_enriched_run_duration_contract_is_no_longer_a_candidate(self):
        root = self.build({"run_duration.v1.json": {
            "name": "run_duration_v1", "target": "run_duration",
            "baseline_hash": BASELINE["baseline_hash"]}})
        rows = X.read_contract_bodies(
            [{"contract_hash": CONTRACT_V2["contract_hash"],
              "file": "run_duration.v1.json"}], root)
        with self.assertRaises(X.Refused):
            X.choose_contract("wait_time", rows, {})

    def test_an_unreadable_or_unparseable_file_is_marked_not_dropped(self):
        """Kept in the listing so `plan` can say WHY, and left unenriched so
        `baseline_for_contract` refuses rather than guesses. A row that
        vanishes is a refusal an operator cannot explain from what was
        printed."""
        root = self.build({"broken.json": "{not json"})
        rows = X.read_contract_bodies(
            [{"contract_hash": "a" * 64, "file": "broken.json"},
             {"contract_hash": "b" * 64, "file": "absent.json"}], root)
        self.assertEqual([r["contract_hash"] for r in rows],
                         ["a" * 64, "b" * 64])
        for row in rows:
            self.assertIsNone(row.get("baseline_hash"))
            self.assertIn("broken.json" if row["contract_hash"][0] == "a"
                          else "absent.json", row["body_unreadable"])

    def test_no_directory_does_not_resolve_the_file_against_the_CWD(self):
        """`os.path.join("", "wait_time.v1.json")` is a RELATIVE path. Without
        the guard, a listing that came back with no `dir` reads whatever file
        of that name is sitting in the working directory -- so a decoy in a
        checkout would decide which baseline a run trains against."""
        import os as _os
        root = self.build({"wait_time.v1.json": {
            "name": "decoy", "target": "wait_time",
            "baseline_hash": "e" * 64}})
        here = _os.getcwd()
        self.addCleanup(_os.chdir, here)
        _os.chdir(root)
        rows = X.read_contract_bodies(
            [{"contract_hash": CONTRACT_V1["contract_hash"],
              "file": "wait_time.v1.json"}], None)
        self.assertIsNone(rows[0].get("baseline_hash"))
        self.assertIsNone(rows[0].get("name"))
        self.assertIn("no directory", rows[0]["body_unreadable"])

    def test_a_row_naming_no_file_is_marked_before_any_open(self):
        rows = X.read_contract_bodies(
            [{"contract_hash": "a" * 64}], self.build({}))
        self.assertIn("names no file", rows[0]["body_unreadable"])

    def test_a_file_whose_own_hash_disagrees_is_not_used(self):
        """The file carries its own `contract_hash`; if it is not this row's,
        the file moved or was rewritten after qfd listed it. Enriching would
        attach one contract's baseline to another contract's hash, which is the
        mispairing in a form nothing downstream could detect."""
        root = self.build({"wait_time.v1.json": {
            "contract_hash": CONTRACT_V2["contract_hash"],
            "name": "wait_time_v2", "target": "wait_time",
            "baseline_hash": BASELINE_V2["baseline_hash"]}})
        rows = X.read_contract_bodies(
            [{"contract_hash": CONTRACT_V1["contract_hash"],
              "file": "wait_time.v1.json"}], root)
        self.assertIsNone(rows[0].get("baseline_hash"))
        self.assertIn(CONTRACT_V2["contract_hash"][:12],
                      rows[0]["body_unreadable"])

    def test_a_file_carrying_its_own_matching_hash_still_enriches(self):
        root = self.build({"wait_time.v2.json": {
            "contract_hash": CONTRACT_V2["contract_hash"],
            "name": "wait_time_v2", "target": "wait_time",
            "baseline_hash": BASELINE_V2["baseline_hash"]}})
        rows = X.read_contract_bodies(
            [{"contract_hash": CONTRACT_V2["contract_hash"],
              "file": "wait_time.v2.json"}], root)
        self.assertEqual(rows[0]["baseline_hash"],
                         BASELINE_V2["baseline_hash"])
        self.assertNotIn("body_unreadable", rows[0])


class TrainerDrift(unittest.TestCase):
    """Whether the agent's checkout trains the code that is deployed.

    Nothing syncs the trusted trainer into the research user's workspace --
    `first-probe.sh` syncs the OPERATOR's -- so a workspace can sit behind
    deployed code indefinitely, and every result it produces gets attributed to
    the wrong thing. Reported rather than refused: under this loop an edit to
    `trainer/` IS the experiment.
    """

    def build(self, files):
        import tempfile
        root = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, root)
        for relative, body in files.items():
            path = os.path.join(root, relative)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(body)
        return root

    TRUSTED = {"src/train.py": "print(1)\n",
               "configs/a.yaml": "target: wait_time\n",
               "src/__pycache__/train.cpython.pyc": "junk",
               "data/big.parquet": "junk"}

    def test_identical_trees_have_no_drift(self):
        trusted = self.build(self.TRUSTED)
        workspace = self.build({f"trainer/{k}": v
                                for k, v in self.TRUSTED.items()})
        self.assertEqual(X.trainer_drift(workspace, trusted), [])

    def test_an_edited_file_is_named(self):
        trusted = self.build(self.TRUSTED)
        changed = dict(self.TRUSTED, **{"configs/a.yaml": "target: other\n"})
        workspace = self.build({f"trainer/{k}": v for k, v in changed.items()})
        self.assertEqual(X.trainer_drift(workspace, trusted),
                         ["configs/a.yaml"])

    def test_a_missing_file_says_missing(self):
        """Different from an edit: a file the workspace never got is a stale
        checkout, and an edit is somebody's experiment."""
        trusted = self.build(self.TRUSTED)
        workspace = self.build({"trainer/src/train.py": "print(1)\n"})
        self.assertIn("configs/a.yaml (MISSING)",
                      X.trainer_drift(workspace, trusted))

    def test_caches_and_data_are_not_compared(self):
        """`__pycache__` and `data/` differ constantly and mean nothing, so
        including them would make the check always fire and get ignored."""
        trusted = self.build(self.TRUSTED)
        workspace = self.build({"trainer/src/train.py": "print(1)\n",
                                "trainer/configs/a.yaml": "target: wait_time\n",
                                "trainer/data/big.parquet": "COMPLETELY OTHER"})
        self.assertEqual(X.trainer_drift(workspace, trusted), [])

    def test_no_trusted_tree_is_none_not_empty(self):
        """`[]` means "verified identical" and `None` means "could not check".
        Collapsing them would report an unreadable mirror as agreement."""
        self.assertIsNone(X.trainer_drift(self.build({}), "/nonexistent"))


class SyncTrainer(unittest.TestCase):
    """Provisioning the agent's checkout from the mirror.

    Nothing did this: `first-probe.sh` syncs the OPERATOR's worktree, so
    `/home/research/qf-research` sat with no `run_cohort.py` at all and `run`
    failed on an errno two minutes into a session.
    """

    def build(self, files):
        import shutil, tempfile
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        for relative, body in files.items():
            path = os.path.join(root, relative)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(body)
        return root

    def trees(self, workspace_files=None):
        trainer = self.build({"src/train.py": "print(1)\n",
                              "configs/a.yaml": "target: wait_time\n",
                              "scripts/prep.py": "pass\n",
                              "__pycache__/x.pyc": "junk",
                              "data/big.parquet": "junk"})
        research = self.build({"run_cohort.py": 'CONFIG = "configs/a.yaml"\n',
                               "AGENTS.md": "# rules\n"})
        return self.build(workspace_files or {}), trainer, research

    def test_an_empty_workspace_gets_everything(self):
        workspace, trainer, research = self.trees()
        written = X.sync_trainer(workspace, trainer, research)
        self.assertIn("trainer/src/train.py", written)
        self.assertIn("trainer/configs/a.yaml", written)
        self.assertIn("research/experiments/run_cohort.py", written)
        self.assertIn("AGENTS.md", written)
        for relative in written:
            self.assertTrue(os.path.isfile(os.path.join(workspace, relative)),
                            relative)

    def test_caches_and_data_are_not_ported(self):
        """Copying `data/` would move gigabytes of parquet into the agent's git
        repo, and `__pycache__` would be committed."""
        workspace, trainer, research = self.trees()
        written = X.sync_trainer(workspace, trainer, research)
        self.assertFalse([w for w in written
                          if "__pycache__" in w or "/data/" in w])

    def test_a_second_sync_writes_nothing(self):
        """Idempotent, and it has to REPORT nothing rather than rewrite
        identical bytes: `run` commits whatever changed, so a sync that always
        touches files would put an empty experiment in the history."""
        workspace, trainer, research = self.trees()
        X.sync_trainer(workspace, trainer, research)
        self.assertEqual(X.sync_trainer(workspace, trainer, research), [])

    def test_it_does_not_delete_what_the_mirror_does_not_name(self):
        """A file under trainer/ that exists only in the workspace may be the
        agent's work in progress, and nothing here can tell that from a
        leftover. Same rule `first-probe.sh` states for the operator's tree."""
        workspace, trainer, research = self.trees(
            {"trainer/src/my_experiment.py": "# the agent's work\n"})
        X.sync_trainer(workspace, trainer, research)
        survivor = os.path.join(workspace, "trainer", "src", "my_experiment.py")
        self.assertTrue(os.path.isfile(survivor))

    def test_it_overwrites_an_agent_edit_to_an_operator_owned_file(self):
        """`run_cohort.py` and `AGENTS.md` are the loop's rules, not the
        agent's to change -- so these are ported over, unlike trainer files."""
        workspace, trainer, research = self.trees(
            {"AGENTS.md": "# I rewrote the rules\n"})
        X.sync_trainer(workspace, trainer, research)
        with open(os.path.join(workspace, "AGENTS.md")) as fh:
            self.assertEqual(fh.read(), "# rules\n")

    def test_dry_run_writes_nothing_but_reports_everything(self):
        workspace, trainer, research = self.trees()
        written = X.sync_trainer(workspace, trainer, research, apply=False)
        self.assertTrue(written)
        self.assertFalse(os.path.exists(os.path.join(workspace, "AGENTS.md")))

    def test_an_incomplete_mirror_refuses(self):
        """Silently skipping a missing `run_cohort.py` would produce a
        workspace that looks synced and cannot run."""
        workspace, trainer, _ = self.trees()
        empty = self.build({})
        with self.assertRaises(X.Refused) as caught:
            X.sync_trainer(workspace, trainer, empty)
        self.assertIn("mirror-refresh", str(caught.exception))


@unittest.skipUnless(CAN_COMMIT, "this environment refuses `git commit`")
class CommitAndPush(unittest.TestCase):
    """Committing into a checkout whose account has no git identity.

    Against a REAL local repository with a real remote, because both bugs here
    were git's behaviour rather than this code's logic: an account with no
    GECOS name makes git refuse with "empty ident name", and a sync that
    reported success without committing left the files matching the mirror
    while the commit did not.
    """

    def repo(self):
        import shutil, subprocess, tempfile
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        remote, work = os.path.join(root, "remote.git"), os.path.join(root, "w")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", remote],
                       check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", work], check=True)
        # No user.name/user.email set ANYWHERE in this repo: that is the state
        # the research account is actually in.
        subprocess.run(["git", "-C", work, "remote", "add", "origin", remote],
                       check=True)
        with open(os.path.join(work, "seed"), "w") as fh:
            fh.write("x\n")
        subprocess.run(["git", "-C", work, "add", "-A"], check=True)
        subprocess.run(["git", "-C", work, *X.GIT_IDENT, "commit", "-q",
                        "-m", "seed"], check=True)
        subprocess.run(["git", "-C", work, "push", "-q", "-u", "origin",
                        "main"], check=True)
        return work

    def test_it_commits_without_any_configured_identity(self):
        work = self.repo()
        with open(os.path.join(work, "new"), "w") as fh:
            fh.write("y\n")
        sha = X.commit_and_push(work, "an experiment")
        self.assertRegex(sha, r"^[0-9a-f]{40}$")

    def test_the_commit_carries_the_machine_identity(self):
        """Attributable, and not a person: these are machine commits, and the
        default must not silently borrow the operator's name."""
        import subprocess
        work = self.repo()
        with open(os.path.join(work, "new"), "w") as fh:
            fh.write("y\n")
        X.commit_and_push(work, "an experiment")
        author = subprocess.run(["git", "-C", work, "log", "-1",
                                 "--format=%an <%ae>"], capture_output=True,
                                text=True).stdout.strip()
        self.assertEqual(author, f"{X.GIT_NAME} <{X.GIT_EMAIL}>")

    def test_a_clean_tree_still_returns_head(self):
        """`sync` calls this even when it ported nothing, so a clean tree is a
        normal state and not an error."""
        work = self.repo()
        self.assertRegex(X.commit_and_push(work, "nothing"), r"^[0-9a-f]{40}$")

    def test_files_copied_before_a_failed_commit_are_still_committed(self):
        """The retry path. The first sync copied 17 files and died on the
        identity; `sync_trainer` then reports nothing to port, so if `cmd_sync`
        returned early the files would never reach a commit and a probe would
        train the old tree while everything reported success."""
        work = self.repo()
        trainer = self.build_tree({"src/train.py": "new\n"})
        research = self.build_tree({"run_cohort.py": "CONFIG = \"a\"\n",
                                    "AGENTS.md": "# rules\n"})
        first = X.sync_trainer(work, trainer, research)
        self.assertTrue(first)                       # copied
        second = X.sync_trainer(work, trainer, research)
        self.assertEqual(second, [])                 # nothing left to port
        import subprocess
        dirty = subprocess.run(["git", "-C", work, "status", "--porcelain"],
                               capture_output=True, text=True).stdout
        self.assertTrue(dirty.strip())                # but uncommitted
        X.commit_and_push(work, "sync")
        dirty = subprocess.run(["git", "-C", work, "status", "--porcelain"],
                               capture_output=True, text=True).stdout
        self.assertFalse(dirty.strip())

    def build_tree(self, files):
        import shutil, tempfile
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        for relative, body in files.items():
            path = os.path.join(root, relative)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(body)
        return root


class PushDiagnosis(unittest.TestCase):
    """Connectivity and credentials fail similarly and have opposite fixes.

    The real 2026-08-31 output is the first case: it was reported as a
    credential problem and it was egress.
    """

    def test_the_real_egress_failure_is_named_as_egress(self):
        real = ("fatal: unable to access"
                " 'https://github.com/lotas/qf-research/': Failed to connect"
                " to github.com port 443 after 5 ms: Could not connect to"
                " server")
        fix = X.push_fix(real)
        self.assertIn("EGRESS", fix)
        self.assertIn("~/.profile", fix)
        self.assertNotIn("credential store", fix)

    def test_a_missing_credential_is_named_as_one(self):
        for output in ("fatal: Authentication failed for 'https://...'",
                       "fatal: could not read Username for"
                       " 'https://github.com': terminal prompts disabled",
                       "remote: Permission to lotas/qf-research.git denied"):
            with self.subTest(output[:40]):
                fix = X.push_fix(output)
                self.assertIn("CREDENTIAL", fix)
                self.assertNotIn("EGRESS", fix)

    def test_dns_failure_is_egress_too(self):
        self.assertIn("EGRESS", X.push_fix("fatal: Could not resolve host:"
                                           " github.com"))

    def test_an_unrecognised_failure_does_not_guess(self):
        """Naming the wrong cause is worse than naming none: it sends somebody
        to rotate a credential that was fine."""
        fix = X.push_fix("fatal: something nobody has seen before")
        self.assertNotIn("EGRESS", fix)
        self.assertNotIn("CREDENTIAL", fix)
        self.assertIn("opposite fixes", fix)

    def test_empty_output_does_not_crash(self):
        for empty in (None, "", "   "):
            self.assertTrue(X.push_fix(empty))


class TestClientWaitOutlastsTheJob(unittest.TestCase):
    """The `qf --wait` timeout must exceed what the dispatcher will let a job run.

    These two numbers live in different privilege domains and looked unrelated
    until they collided: `--timeout` was 5400 while `spec.TIMEOUT_MAX` was 3600,
    which was fine, and raising the ceiling to 5400 made them EQUAL. A probe
    that ran to its ceiling then outlived the client waiting on it, so the host
    finished the work and `experiment.py` never submitted the evaluation.
    """

    def _default_timeout(self):
        # READ FROM THE SOURCE, because the parser is built inside `main()` and
        # there is no seam to ask it. Restating the number here instead would
        # make this test pass by agreeing with itself. The assert below is the
        # guard: if the line is ever reshaped, this fails loudly rather than
        # quietly checking nothing.
        import re
        src = open(X.__file__).read()
        m = re.search(r'add_argument\("--timeout", type=int, default=(\d+)\)', src)
        self.assertIsNotNone(m, "could not find --timeout's default in experiment.py")
        return int(m.group(1))

    def test_client_wait_exceeds_the_probe_execution_ceiling(self):
        self.assertGreater(self._default_timeout(), spec.TIMEOUT_MAX)

    def test_client_wait_covers_the_whole_job_hold_deadline(self):
        # Not just the execution ceiling: a job may also spend BUILD_TIMEOUT_S
        # and the build-lock wait before it starts running, and the dispatcher's
        # own guarantee is the hold deadline rather than the timeout.
        self.assertGreaterEqual(self._default_timeout(), 9600)


if __name__ == "__main__":
    unittest.main()
