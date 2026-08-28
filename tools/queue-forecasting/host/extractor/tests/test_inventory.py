"""Phase 2b-1 Task 2: the six queries, as data.

Written before `inventory.py`. **These tests are the regression test for D4
itself** -- the claim that a new table or column requires a human promotion --
so they assert the SQL at the level the hazard lives at: its text.

The hazard is not hypothetical. `trainer/src/data_loader.py` does both of these
today, and both are correct there and forbidden here:

    f"r.{c.target_column} AS y"                    # :199, a config's column name
    where = [..., *c.filters]                      # :240, a config's SQL
    query = f"SELECT ... WHERE {condition}"         # :527, a config's predicate

Run:  PYTHONPATH=. python3 -m unittest discover -s tests
"""
import datetime
import re
import unittest

import inventory


# D18's table, restated independently of the code. A column added to
# `inventory.py` without a design change fails here, and a column named in the
# design and missing from the code fails too. Restating it is the point: a test
# that imports the value it checks is checking nothing.
EXPECTED = {
    "runs": {
        "task_id", "run_id", "pending_at", "started_at", "resolved_at",
        "reason_resolved", "wait_duration_s", "run_duration_s",
        "priority_at_pending", "queue_pending", "task_queue_id",
        "scheduler_id", "metadata_name", "normalized_name", "max_run_time_s",
        "repo_family", "tags",
    },
    "worker_counts": {
        "task_queue_id", "sampled_at", "running_workers", "claimed_tasks",
        "existing_capacity",
    },
    "worker_pools": {"task_queue_id", "pool_kind", "provider_type"},
    "throughput_runs": {
        "task_queue_id", "started_at", "resolved_at", "wait_duration_s",
        "run_duration_s",
    },
    "qctx_runs": {
        "task_id", "run_id", "pending_at", "started_at", "resolved_at",
        "priority_at_pending", "task_queue_id", "repo_family",
    },
    "daily_health": {
        "sample_date", "is_anomalous",
        "flag_exception_spike", "flag_stuck_pending_spike",
        "flag_wait_p99_spike", "flag_volume_anomaly", "flag_low_completion",
        "flag_capacity_drop", "flag_capacity_spike", "flag_low_utilization",
        "flag_sampler_offline",
    },
}

# The five tables the extractor may read (plan §4 fact 10).
ALLOWED_TABLES = {
    "queue_forecast_task_runs", "queue_forecast_tasks",
    "queue_forecast_worker_counts", "queue_forecast_worker_pools",
    "queue_forecast_daily_health",
}

# Every bound parameter any query may use: the request's own window fields, the
# derived qctx floor, and the trusted trailing-lookback constant. Nothing else.
ALLOWED_PARAMS = {"train_start", "as_of_date", "ref_lower", "window_lower"}

_PLACEHOLDER = re.compile(r"%\(([a-z_]+)\)s")
_WRITES = ("insert", "update", "delete", "drop", "alter", "create", "grant",
           "revoke", "truncate", "copy", "merge", "call", "do")


class TestTheSixDatasetsAreTheSixDatasets(unittest.TestCase):
    def test_exactly_six(self):
        self.assertEqual(set(inventory.DATASETS), set(EXPECTED))

    def test_each_names_a_parquet_file(self):
        for name, ds in inventory.DATASETS.items():
            with self.subTest(name=name):
                self.assertEqual(ds.file, f"{name}.parquet")

    def test_the_declared_columns_match_the_design(self):
        for name, columns in EXPECTED.items():
            with self.subTest(name=name):
                self.assertEqual(set(inventory.DATASETS[name].columns), columns)

    def test_the_select_list_matches_the_declared_columns(self):
        # THE MANIFEST DESCRIBES THE FILE. `columns` is what the manifest
        # reports and the SELECT list is what the file will actually contain, so
        # a divergence means the manifest lies -- a column dropped from the SQL
        # would still be advertised, and a candidate would read a KeyError as
        # missing data.
        #
        # Parsing also enforces that the SELECT list is bare column names: an
        # expression or an alias does not round-trip through this comparison,
        # which is deliberate. `AS y` is exactly the shape D4 forbids.
        for name, ds in inventory.DATASETS.items():
            with self.subTest(name=name):
                body = ds.sql.strip()
                select_list = body[len("SELECT"):body.upper().index("FROM")]
                parsed = []
                for piece in select_list.split(","):
                    piece = piece.strip()
                    self.assertRegex(
                        piece, r"^(?:[a-z]\.)?[a-z_][a-z0-9_]*$",
                        f"{name} selects something that is not a bare column:"
                        f" {piece!r}")
                    parsed.append(piece.split(".")[-1])
                self.assertEqual(tuple(parsed), ds.columns)

    def test_the_whole_table_datasets_carry_no_predicate(self):
        # D17: the WHOLE `daily_health` row set is emitted, and the candidate
        # subsets it. A `WHERE is_anomalous = TRUE` here would narrow what a
        # candidate can filter on while passing every other test in this file --
        # verified by reintroducing exactly that and watching 30 tests pass.
        # `worker_pools` is held to the same rule: a time-filtered dimension
        # table silently drops pools that stopped being sampled.
        for name in ("worker_pools", "daily_health"):
            with self.subTest(name=name):
                self.assertNotIn("WHERE", inventory.DATASETS[name].sql.upper(),
                                 f"{name} is a whole-table read")

    def test_no_dataset_declares_a_duplicate_column(self):
        # A duplicate would make the Parquet schema ambiguous and the manifest's
        # column list disagree with the file it describes.
        for name, ds in inventory.DATASETS.items():
            with self.subTest(name=name):
                self.assertEqual(len(ds.columns), len(set(ds.columns)))


class TestNoIdentifierEverReachesTheSql(unittest.TestCase):
    """The D4 regression test. Everything here is a text assertion, because
    the failure being prevented is textual: a column name or a predicate
    arriving from outside this module."""

    def test_no_format_placeholders_or_fstring_residue(self):
        for name, ds in inventory.DATASETS.items():
            with self.subTest(name=name):
                self.assertNotIn("{", ds.sql, "brace: an f-string was edited in")
                self.assertNotIn("}", ds.sql)
                self.assertNotIn(".format(", ds.sql)
                self.assertNotIn("%s", ds.sql, "positional binding is unnamed,"
                                               " so it cannot be checked")

    def test_every_percent_belongs_to_a_named_placeholder(self):
        # The strong form: count every `%` in the text and require that all of
        # them are accounted for by `%(name)s` occurrences. A stray `%` is
        # either a LIKE pattern nobody declared or a format string in waiting.
        for name, ds in inventory.DATASETS.items():
            with self.subTest(name=name):
                placeholders = _PLACEHOLDER.findall(ds.sql)
                self.assertEqual(ds.sql.count("%"), len(placeholders),
                                 f"{name} has a % outside a named placeholder")

    def test_declared_params_match_the_placeholders_used(self):
        # Both directions. A declared-but-unused param means the caller binds a
        # value the query ignores; a used-but-undeclared one is a KeyError at
        # extraction time, i.e. in production rather than here.
        for name, ds in inventory.DATASETS.items():
            with self.subTest(name=name):
                used = set(_PLACEHOLDER.findall(ds.sql))
                self.assertEqual(used, set(ds.params))

    def test_no_param_is_outside_the_allowlist(self):
        for name, ds in inventory.DATASETS.items():
            with self.subTest(name=name):
                self.assertLessEqual(set(ds.params), ALLOWED_PARAMS)

    def test_no_query_mentions_a_config_concept(self):
        # These are the names the trainer's loader uses for the things a config
        # supplies. None of them has any business in trusted SQL.
        for name, ds in inventory.DATASETS.items():
            for forbidden in ("filters", "target_column", "flag_subset",
                              "config", "c.", "AS y"):
                with self.subTest(name=name, forbidden=forbidden):
                    self.assertNotIn(forbidden, ds.sql)

    def test_the_target_does_not_influence_any_query(self):
        # D18: `runs` carries BOTH duration columns under their own names, so
        # the target is a candidate-side rename and no query varies with it.
        # If a query ever did vary, two extracts with the same `request_hash`
        # minus the target would differ, and reuse (D20) would be wrong.
        for name, ds in inventory.DATASETS.items():
            with self.subTest(name=name):
                self.assertNotIn("target", ds.sql.lower())

    def test_runs_selects_both_targets_and_no_column_called_y(self):
        runs = inventory.DATASETS["runs"]
        self.assertIn("wait_duration_s", runs.columns)
        self.assertIn("run_duration_s", runs.columns)
        self.assertNotIn("y", runs.columns)
        self.assertNotIn(" y\n", runs.sql)
        self.assertNotIn(" y,", runs.sql)


class TestEveryQueryIsAReadOfAnAllowedTable(unittest.TestCase):
    def test_each_is_a_single_select(self):
        for name, ds in inventory.DATASETS.items():
            with self.subTest(name=name):
                body = ds.sql.strip()
                self.assertTrue(body.upper().startswith("SELECT"), body[:40])
                # No statement stacking. A trailing semicolon is allowed; one in
                # the middle is a second statement.
                self.assertEqual(body.rstrip(";").count(";"), 0)

    def test_no_write_verb_appears(self):
        # Tokenised on `[a-z_]+`, NOT `[a-z]+`. The first version split on
        # underscores, so `flag_capacity_drop` produced the token `drop` and
        # this test failed against correct SQL. A scan for dangerous words has
        # to agree with the language about what a word is.
        for name, ds in inventory.DATASETS.items():
            words = set(re.findall(r"[a-z_]+", ds.sql.lower()))
            for verb in _WRITES:
                with self.subTest(name=name, verb=verb):
                    self.assertNotIn(verb, words)

    def test_only_the_five_allowed_tables_are_named(self):
        for name, ds in inventory.DATASETS.items():
            with self.subTest(name=name):
                named = set(re.findall(r"queue_forecast_[a-z_]+", ds.sql))
                self.assertTrue(named, "no table named at all")
                self.assertLessEqual(named, ALLOWED_TABLES)

    def test_the_union_of_tables_is_the_grant_surface(self):
        # If this shrinks, the recommended `forecast_extract` role (plan Task 6)
        # should shrink with it; if it grows, that growth is the human promotion
        # D4 describes and must not pass unnoticed.
        named = set()
        for ds in inventory.DATASETS.values():
            named |= set(re.findall(r"queue_forecast_[a-z_]+", ds.sql))
        self.assertEqual(named, ALLOWED_TABLES)


class TestTheWindowsAreTheOnesTheTrainerWouldHaveUsed(unittest.TestCase):
    """The extract must be a SUPERSET of what each of the trainer's six queries
    would have returned, or a candidate reproducing a past result silently gets
    fewer rows than the result was computed from."""

    def test_the_two_whole_table_datasets_take_no_parameters(self):
        # `worker_pools` (~650 rows) and `daily_health` are whole-table reads.
        # A window on either would narrow what a candidate can filter on.
        for name in ("worker_pools", "daily_health"):
            with self.subTest(name=name):
                self.assertEqual(inventory.DATASETS[name].params, ())

    def test_the_windowed_datasets_bound_both_ends(self):
        for name in ("runs", "worker_counts", "throughput_runs", "qctx_runs"):
            with self.subTest(name=name):
                self.assertIn("as_of_date", inventory.DATASETS[name].params)

    def test_the_window_lower_bound_is_used_by_every_dataset_that_needs_it(self):
        for name in ("worker_counts", "throughput_runs"):
            with self.subTest(name=name):
                self.assertIn("window_lower",
                              inventory.DATASETS[name].params)

    def test_qctx_floors_both_sides_of_its_join(self):
        # The docstring records that without the tasks-side floor this join
        # scans the full history of an ever-growing table -- a confirmed
        # multi-TB read profile. Both floors use the same derived bound.
        sql = inventory.DATASETS["qctx_runs"].sql
        self.assertIn("t.task_created", sql)
        self.assertEqual(sql.count("%(ref_lower)s"), 2)

    def test_throughput_keeps_the_trainers_inclusive_upper_bound(self):
        # `load_task_runs_for_throughput` uses `resolved_at <= window_end`
        # while `_build_query` uses `pending_at < as_of_date`. Copying each
        # faithfully matters more than making them consistent: an exclusive
        # bound here would drop rows resolving exactly at the boundary, and
        # `as_of_date` is midnight, where a boundary row is unremarkable.
        sql = inventory.DATASETS["throughput_runs"].sql
        self.assertRegex(sql, r"resolved_at\s*<=\s*%\(as_of_date\)s")

    def test_runs_keeps_the_trainers_exclusive_upper_bound(self):
        sql = inventory.DATASETS["runs"].sql
        self.assertRegex(sql, r"pending_at\s*<\s*%\(as_of_date\)s")
        self.assertNotRegex(sql, r"pending_at\s*<=\s*%\(as_of_date\)s")


class TestBindingsAreBuiltFromAValidatedRequestOnly(unittest.TestCase):
    def test_bindings_cover_every_declared_param(self):
        request = {
            "train_start": "2026-06-01T00:00:00Z",
            "as_of_date": "2026-08-01T00:00:00Z",
            "ref_lower": "2026-05-01T00:00:00Z",
            "window_lower": "2026-05-31T00:00:00Z",
        }
        for name, ds in inventory.DATASETS.items():
            with self.subTest(name=name):
                bound = inventory.bindings(name, request)
                self.assertEqual(set(bound), set(ds.params))

    def test_an_unknown_dataset_is_refused(self):
        with self.assertRaises(KeyError):
            inventory.bindings("secrets", {})

    def test_a_request_missing_a_window_field_fails_loudly(self):
        # Not silently None: a None bound into a timestamp comparison makes
        # every row fail the predicate, which reads as "the window was empty".
        with self.assertRaises(KeyError):
            inventory.bindings("runs", {"train_start": "2026-06-01T00:00:00Z"})

    def test_the_qctx_overlap_predicate_uses_window_lower(self):
        # THE P1 FIX, asserted in the SQL. Comparing a reference run's exit
        # against `train_start` drops runs that exited during the trainer's
        # 90-minute prefix -- a subset, silently.
        sql = inventory.DATASETS["qctx_runs"].sql
        self.assertIn("%(window_lower)s", sql)
        self.assertNotIn("%(train_start)s", sql)

    def test_qctx_does_not_bind_train_start_but_runs_still_does(self):
        # A first version of this test banned `train_start` outright and failed
        # against correct code. `runs` SHOULD bind it: the training window is
        # `pending_at >= train_start` by definition, exactly as `_build_query`
        # has it, and widening that would hand the candidate rows outside its own
        # window to filter back out.
        #
        # The rule is narrower than "never bind train_start": a REFERENCE window
        # needs the shifted bound, a training window does not.
        self.assertIn("train_start", inventory.DATASETS["runs"].params)
        self.assertNotIn("train_start", inventory.DATASETS["qctx_runs"].params)

    def test_bindings_does_no_arithmetic(self):
        request = {
            "train_start": "2026-06-01T00:00:00Z",
            "as_of_date": "2026-08-01T00:00:00Z",
            "ref_lower": "2026-05-01T00:00:00Z",
            "window_lower": "2026-05-31T00:00:00Z",
        }
        # `window_lower` is supplied by the request, not computed here.
        bound = inventory.bindings("worker_counts", request)
        self.assertEqual(bound["window_lower"], request["window_lower"])
        return
        # Asserted against the CONSTANT, not against a copied-out number: the
        # first version of this test hardcoded the 90-minute figure the trainer
        # uses and failed once the trusted constant was widened to 24h. The
        # derivation is what this test is for; the constant's *value* is held to
        # the superset requirement by
        # test_the_trailing_lookback_supersets_every_config_in_the_tree.
        expected = (datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
                    - datetime.timedelta(
                        minutes=inventory.TRAILING_LOOKBACK_MINUTES))
        self.assertEqual(bound["trailing_lower"],
                         expected.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertNotEqual(bound["trailing_lower"], request["train_start"])

    def test_bindings_never_return_a_value_from_outside_the_request(self):
        request = {
            "train_start": "2026-06-01T00:00:00Z",
            "as_of_date": "2026-08-01T00:00:00Z",
            "ref_lower": "2026-05-01T00:00:00Z",
            "window_lower": "2026-05-31T00:00:00Z",
            "target": "wait_time",
            "target_column": "wait_duration_s",
            "generation": 3,
        }
        for name in inventory.DATASETS:
            with self.subTest(name=name):
                bound = inventory.bindings(name, request)
                self.assertNotIn("target", bound)
                self.assertNotIn("target_column", bound)
                self.assertNotIn("generation", bound)


class TestTheWatermarkColumnsAreDeclared(unittest.TestCase):
    """D19's watermark is computed from the rows written, so which columns it
    reads has to be data rather than a guess at extraction time."""

    def test_every_dataset_declares_its_watermark_columns(self):
        for name, ds in inventory.DATASETS.items():
            with self.subTest(name=name):
                self.assertIsInstance(ds.watermark_columns, tuple)

    def test_each_watermark_column_is_one_of_that_datasets_columns(self):
        for name, ds in inventory.DATASETS.items():
            for col in ds.watermark_columns:
                with self.subTest(name=name, col=col):
                    self.assertIn(col, ds.columns)

    def test_the_run_datasets_watermark_on_both_pending_and_resolution(self):
        # D19 names "the maximum pending_at and resolution timestamp included".
        # Watermarking on only one would miss exactly the late-arrival case the
        # watermark exists to record.
        self.assertEqual(set(inventory.DATASETS["runs"].watermark_columns),
                         {"pending_at", "resolved_at"})


if __name__ == "__main__":
    unittest.main()


class TestTheInventoryAndTheValidatorCannotDrift(unittest.TestCase):
    """`inventory.py` declares which request fields each query binds;
    `extract_spec.py` decides which fields a validated request has. Nothing
    else checks that the two agree, and a disagreement is a KeyError at
    extraction time -- in production, against a real database, after a
    snapshot has been opened.

    The TEST reaches across the two domains; the CODE does not. `inventory` must
    not import from the dispatcher (it is the extractor's module) and the
    dispatcher must not import the extractor's SQL. A test is the right place
    for a cross-domain assertion precisely because it is not shipped as a
    dependency.
    """

    @classmethod
    def setUpClass(cls):
        import os
        import sys
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sys.path.insert(0, os.path.join(os.path.dirname(here), "dispatcher"))
        import extract_spec
        cls.extract_spec = extract_spec
        cls.request = extract_spec.validate(
            {
                "schema": 1,
                "target": "wait_time",
                "train_start": "2026-06-01T00:00:00Z",
                "as_of_date": "2026-08-01T00:00:00Z",
                "lookback_days": 30,
            },
            now=datetime.datetime(2026, 8, 5, tzinfo=datetime.timezone.utc),
        )

    def test_every_declared_param_exists_in_a_validated_request(self):
        for name, ds in inventory.DATASETS.items():
            for param in ds.params:
                with self.subTest(dataset=name, param=param):
                    self.assertIn(param, self.request)

    def test_binding_a_real_validated_request_works_for_all_six(self):
        for name, ds in inventory.DATASETS.items():
            with self.subTest(name=name):
                bound = inventory.bindings(name, self.request)
                self.assertEqual(set(bound), set(ds.params))
                for value in bound.values():
                    self.assertIsInstance(value, str)

    def test_the_derived_bounds_are_ordered_as_the_sql_assumes(self):
        # Every query's predicates only make sense if these hold, and they are
        # cheap to assert once against a real validated request.
        r = self.request
        self.assertLess(r["ref_lower"], r["window_lower"])
        self.assertLess(r["window_lower"], r["train_start"])
        self.assertLess(r["train_start"], r["as_of_date"])
