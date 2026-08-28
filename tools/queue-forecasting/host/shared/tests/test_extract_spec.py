"""Phase 2b-1 Task 1: the typed extraction request.

Written before `extract_spec.py`. Every case names the failure it prevents,
because a validator's test list is the only place the closed world is legible.

The rules under test come from `auto-research-phase2b-plan.md` D17 and D20:
unknown is refused rather than ignored; nothing a research config could
influence reaches a query; `as_of_date` is a completed UTC day boundary past a
settlement lag; and the request hash is what makes an extract identifiable, so
it must move when the request moves and not otherwise.
"""
import datetime
import unittest

import extract_spec


UTC = datetime.timezone.utc


def a_request(**over):
    """A valid request. Tests override one field so a failure names one cause."""
    base = {
        "schema": 1,
        "target": "wait_time",
        # 31 days: inside MAX_WINDOW_DAYS (60, lowered from 120 by measurement)
        # and close to the 36 days the largest promoted config actually needs.
        "train_start": "2026-07-01T00:00:00Z",
        "as_of_date": "2026-08-01T00:00:00Z",
        "lookback_days": 30,
    }
    base.update(over)
    for k, v in list(base.items()):
        if v is extract_spec.OMIT:
            del base[k]
    return base


# Far enough past `as_of_date` that the default settlement lag is satisfied.
NOW = datetime.datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


class _DEFAULT:
    """The helper's own sentinel. `None` cannot serve: one test passes None AS
    the request, and a `raw is None` default would quietly turn that case into
    a valid request and assert nothing."""


def validate(raw=_DEFAULT, *, now=NOW, lag=None):
    kw = {} if lag is None else {"settlement_lag_s": lag}
    return extract_spec.validate(a_request() if raw is _DEFAULT else raw,
                                 now=now, **kw)


class TestTheClosedWorldIsClosed(unittest.TestCase):
    def test_an_unknown_key_is_refused_not_ignored(self):
        # An ignored key is how `filters` arrives one day and nobody notices:
        # the request would validate, the extract would be narrowed, and the
        # audit record would show a request that never ran.
        with self.assertRaises(extract_spec.ExtractSpecError) as cm:
            validate(a_request(filters=["r.priority_at_pending = 'high'"]))
        self.assertIn("filters", str(cm.exception))

    def test_a_missing_required_key_is_named(self):
        for key in ("schema", "target", "train_start", "as_of_date",
                    "lookback_days"):
            with self.subTest(key=key):
                with self.assertRaises(extract_spec.ExtractSpecError) as cm:
                    validate(a_request(**{key: extract_spec.OMIT}))
                self.assertIn(key, str(cm.exception))

    def test_a_wrong_schema_is_refused(self):
        for bad in (0, 2, "1", None, True):
            with self.subTest(schema=bad):
                with self.assertRaises(extract_spec.ExtractSpecError):
                    validate(a_request(schema=bad))

    def test_the_request_must_be_an_object(self):
        for bad in ([], "x", 3, None):
            with self.subTest(raw=bad):
                with self.assertRaises(extract_spec.ExtractSpecError):
                    validate(bad)


class TestTheTargetIsAnEnumNotAColumnName(unittest.TestCase):
    """`_build_query` selects `f"r.{c.target_column} AS y"` — a column name from
    a research config. The whole point of D4 is that trusted code never does
    that, so the request carries a *target*, and the column is looked up here."""

    def test_both_real_targets_are_accepted(self):
        for target, column in (("wait_time", "wait_duration_s"),
                               ("run_duration", "run_duration_s")):
            with self.subTest(target=target):
                got = validate(a_request(target=target))
                self.assertEqual(got["target"], target)
                self.assertEqual(got["target_column"], column)

    def test_an_unknown_target_is_refused_by_name(self):
        # By name and with the allowed set, because "invalid target" sends the
        # caller to the source and a list sends them to the fix.
        with self.assertRaises(extract_spec.ExtractSpecError) as cm:
            validate(a_request(target="p90"))
        msg = str(cm.exception)
        self.assertIn("p90", msg)
        self.assertIn("wait_time", msg)
        self.assertIn("run_duration", msg)

    def test_a_column_name_is_not_a_target(self):
        # Someone will try this, because `target_column` is what the config
        # calls it. It must fail rather than work by coincidence.
        for bad in ("wait_duration_s", "run_duration_s", "y"):
            with self.subTest(target=bad):
                with self.assertRaises(extract_spec.ExtractSpecError):
                    validate(a_request(target=bad))

    def test_the_target_column_cannot_be_supplied_directly(self):
        with self.assertRaises(extract_spec.ExtractSpecError) as cm:
            validate(a_request(target_column="wait_duration_s"))
        self.assertIn("target_column", str(cm.exception))


class TestTimestampsAreUtcDayBoundaries(unittest.TestCase):
    def test_a_naive_timestamp_is_refused(self):
        with self.assertRaises(extract_spec.ExtractSpecError) as cm:
            validate(a_request(as_of_date="2026-08-01T00:00:00"))
        self.assertIn("UTC", str(cm.exception))

    def test_a_non_utc_offset_is_refused(self):
        # Not converted. A window expressed in a local zone is a window whose
        # day boundaries depend on where the caller was sitting.
        for bad in ("2026-08-01T00:00:00+02:00", "2026-08-01T00:00:00-07:00"):
            with self.subTest(ts=bad):
                with self.assertRaises(extract_spec.ExtractSpecError):
                    validate(a_request(as_of_date=bad))

    def test_a_mid_day_boundary_is_refused(self):
        # D20: `as_of_date` must be a completed UTC day boundary. The rest of the
        # system speaks in days -- `daily_health` is keyed by `sample_date` --
        # so a window ending at 13:47 is one nothing else can describe.
        with self.assertRaises(extract_spec.ExtractSpecError) as cm:
            validate(a_request(as_of_date="2026-08-01T13:47:00Z"))
        self.assertIn("boundary", str(cm.exception))

    def test_train_start_is_held_to_the_same_rule(self):
        with self.assertRaises(extract_spec.ExtractSpecError):
            validate(a_request(train_start="2026-06-01T06:00:00Z"))

    def test_a_nonsense_date_is_refused(self):
        for bad in ("2026-13-01T00:00:00Z", "2026-02-30T00:00:00Z",
                    "not-a-date", "", "20260801T000000Z"):
            with self.subTest(ts=bad):
                with self.assertRaises(extract_spec.ExtractSpecError):
                    validate(a_request(as_of_date=bad))

    def test_a_non_string_timestamp_is_refused(self):
        for bad in (0, 1786000000, None, True, ["2026-08-01T00:00:00Z"]):
            with self.subTest(ts=bad):
                with self.assertRaises(extract_spec.ExtractSpecError):
                    validate(a_request(as_of_date=bad))

    def test_an_empty_or_inverted_window_is_refused(self):
        for start, end in (("2026-08-01T00:00:00Z", "2026-08-01T00:00:00Z"),
                           ("2026-08-02T00:00:00Z", "2026-08-01T00:00:00Z")):
            with self.subTest(start=start):
                with self.assertRaises(extract_spec.ExtractSpecError) as cm:
                    validate(a_request(train_start=start, as_of_date=end))
                self.assertIn("train_start", str(cm.exception))


class TestTheSettlementLagIsTrustedConfigNotARequestField(unittest.TestCase):
    """D17. A caller that could choose its own lag could choose zero, and the
    completed-boundary rule would buy nothing."""

    def test_it_cannot_be_supplied_in_the_request(self):
        with self.assertRaises(extract_spec.ExtractSpecError) as cm:
            validate(a_request(settlement_lag_s=0))
        self.assertIn("settlement_lag_s", str(cm.exception))

    def test_an_as_of_date_inside_the_lag_is_refused(self):
        # The window ends at 2026-08-01T00:00Z; with a 48h lag it is not
        # extractable until 2026-08-03T00:00Z.
        with self.assertRaises(extract_spec.ExtractSpecError) as cm:
            validate(now=datetime.datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
                     lag=48 * 3600)
        msg = str(cm.exception)
        self.assertIn("settlement", msg)
        # The lag in force, so the caller can tell a policy from a bug.
        self.assertTrue("48" in msg or "172800" in msg, msg)

    def test_it_is_accepted_once_the_lag_has_passed(self):
        got = validate(now=datetime.datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
                       lag=48 * 3600)
        self.assertEqual(got["as_of_date"], "2026-08-01T00:00:00Z")

    def test_a_future_window_is_refused_whatever_the_lag(self):
        with self.assertRaises(extract_spec.ExtractSpecError):
            validate(now=datetime.datetime(2026, 7, 1, 0, 0, tzinfo=UTC),
                     lag=0)

    def test_it_is_not_part_of_the_request_hash(self):
        # D20 records the lag in the MANIFEST as provenance. If it were hashed
        # into the request, changing an operational knob would silently orphan
        # every published extract and re-extract the entire history.
        a = validate(now=datetime.datetime(2026, 8, 5, tzinfo=UTC), lag=3600)
        b = validate(now=datetime.datetime(2026, 8, 5, tzinfo=UTC),
                     lag=72 * 3600)
        self.assertEqual(extract_spec.request_hash(a),
                         extract_spec.request_hash(b))
        self.assertNotIn("settlement_lag_s", a)


class TestLookbackDaysIsBoundedAndIsAnInt(unittest.TestCase):
    """The one config value that reaches a window bound (D17). Unbounded is a
    full-history scan -- the docstring in `load_task_runs_for_queue_context`
    records a confirmed multi-TB read profile."""

    def test_the_bounds_hold(self):
        for good in (1, 30, 120):
            with self.subTest(n=good):
                self.assertEqual(
                    validate(a_request(lookback_days=good))["lookback_days"],
                    good)

    def test_out_of_range_is_refused(self):
        for bad in (0, -1, 121, 10000):
            with self.subTest(n=bad):
                with self.assertRaises(extract_spec.ExtractSpecError) as cm:
                    validate(a_request(lookback_days=bad))
                self.assertIn("lookback_days", str(cm.exception))

    def test_a_bool_is_not_an_int(self):
        # `isinstance(True, int)` is True, so this needs its own rejection or
        # `lookback_days: true` becomes a one-day lookback.
        for bad in (True, False):
            with self.subTest(n=bad):
                with self.assertRaises(extract_spec.ExtractSpecError):
                    validate(a_request(lookback_days=bad))

    def test_a_string_or_float_is_refused(self):
        for bad in ("30", 30.0, 30.5, None):
            with self.subTest(n=bad):
                with self.assertRaises(extract_spec.ExtractSpecError):
                    validate(a_request(lookback_days=bad))

    def test_ref_lower_is_derived_from_window_lower_not_train_start(self):
        # THE P1 FIX. `ref_lower` used to be `train_start - lookback_days`,
        # which is 90 minutes LATER than the trainer's floor -- see
        # TestTheDerivedBoundsSupersetTheTrainersWindows for why that made the
        # extract a subset.
        got = validate(a_request(train_start="2026-07-01T00:00:00Z",
                                 lookback_days=30))
        # Worked example: 2026-07-01 - 24h = 2026-06-30, - 30d = 2026-05-31.
        self.assertEqual(got["window_lower"], "2026-06-30T00:00:00Z")
        self.assertEqual(got["ref_lower"], "2026-05-31T00:00:00Z")
        # And the relation, so a wrong literal above cannot pass by agreeing
        # with a wrong implementation. Three literals in this file have now been
        # miscomputed by hand; the relation is what actually pins the rule.
        self.assertEqual(
            _parse(got["ref_lower"]),
            _parse(got["window_lower"]) - datetime.timedelta(days=30))
        self.assertEqual(
            _parse(got["window_lower"]),
            _parse(got["train_start"]) - datetime.timedelta(
                minutes=extract_spec.WINDOW_LOOKBACK_MINUTES))

    def test_ref_lower_cannot_be_supplied(self):
        with self.assertRaises(extract_spec.ExtractSpecError) as cm:
            validate(a_request(ref_lower="2024-01-01T00:00:00Z"))
        self.assertIn("ref_lower", str(cm.exception))

    def test_window_lower_cannot_be_supplied(self):
        with self.assertRaises(extract_spec.ExtractSpecError) as cm:
            validate(a_request(window_lower="2024-01-01T00:00:00Z"))
        self.assertIn("window_lower", str(cm.exception))


class TestGenerationIsHowReExtractionIsDeliberate(unittest.TestCase):
    """D20: a published extract is immutable, so incorporating late data means
    asking for a new artifact rather than rewriting one."""

    def test_it_defaults_to_one(self):
        self.assertEqual(validate()["generation"], 1)

    def test_bumping_it_changes_the_request_hash(self):
        one = validate(a_request(generation=1))
        two = validate(a_request(generation=2))
        self.assertNotEqual(extract_spec.request_hash(one),
                            extract_spec.request_hash(two))

    def test_zero_and_negative_are_refused(self):
        for bad in (0, -1):
            with self.subTest(n=bad):
                with self.assertRaises(extract_spec.ExtractSpecError):
                    validate(a_request(generation=bad))

    def test_a_bool_or_string_is_refused(self):
        for bad in (True, "2", 2.0, None):
            with self.subTest(n=bad):
                with self.assertRaises(extract_spec.ExtractSpecError):
                    validate(a_request(generation=bad))

    def test_it_is_bounded(self):
        # Not because a large generation is dangerous, but because an unbounded
        # integer field in a closed-world validator is an inconsistency someone
        # will later read as permission.
        with self.assertRaises(extract_spec.ExtractSpecError):
            validate(a_request(generation=10**6))


class TestTheRequestHashIdentifiesTheRequestAndNothingElse(unittest.TestCase):
    def test_it_is_stable_under_key_order_and_whitespace(self):
        forward = a_request()
        backward = dict(reversed(list(forward.items())))
        self.assertEqual(
            extract_spec.request_hash(validate(forward)),
            extract_spec.request_hash(validate(backward)))

    def test_every_field_moves_it(self):
        base = extract_spec.request_hash(validate())
        for field, value in (("target", "run_duration"),
                             ("train_start", "2026-06-02T00:00:00Z"),
                             ("as_of_date", "2026-07-31T00:00:00Z"),
                             ("lookback_days", 31),
                             ("generation", 2)):
            with self.subTest(field=field):
                other = extract_spec.request_hash(
                    validate(a_request(**{field: value})))
                self.assertNotEqual(base, other,
                                    f"{field} does not affect request_hash, so "
                                    f"two different extracts would collide")

    def test_it_is_hex_sha256(self):
        h = extract_spec.request_hash(validate())
        self.assertEqual(len(h), 64)
        int(h, 16)

    def test_the_defaults_are_in_the_hash(self):
        # The effective request is what runs, so it is what must be hashed
        # (design D12). An omitted `generation` and an explicit `generation: 1`
        # are the same request and must not produce two extracts.
        self.assertEqual(
            extract_spec.request_hash(validate(a_request())),
            extract_spec.request_hash(validate(a_request(generation=1))))


class TestAValidatedRequestCannotBeWidenedLater(unittest.TestCase):
    """A later stage that could add a key to a validated request would be
    adding it *after* the only code that knows what is allowed."""

    def test_it_is_not_mutable(self):
        got = validate()
        with self.assertRaises(TypeError):
            got["filters"] = ["1=1"]
        with self.assertRaises(TypeError):
            got["lookback_days"] = 9999

    def test_it_still_serialises_canonically(self):
        got = validate()
        self.assertIsInstance(extract_spec.canonical(got), bytes)
        self.assertEqual(extract_spec.canonical(got),
                         extract_spec.canonical(dict(got)))

    def test_a_mutable_copy_is_available_for_callers_that_need_one(self):
        got = validate()
        copy = dict(got)
        copy["scratch"] = 1
        self.assertNotIn("scratch", got)


class TestTheEffectiveRequestNamesEverythingThatRuns(unittest.TestCase):
    def test_it_carries_exactly_the_documented_fields(self):
        # Pinned so a field added to the code without a design change fails
        # here, and vice versa -- the same rule Task 2 applies to the column
        # inventory.
        self.assertEqual(set(validate()), {
            "schema", "target", "target_column", "train_start", "as_of_date",
            "lookback_days", "window_lower", "ref_lower", "generation",
        })

    def test_no_field_carries_sql_or_a_path(self):
        # A closed-world request has no field a shell or a query could splice.
        # If one ever appears, this fails before it reaches `inventory.py`.
        for key, value in validate().items():
            with self.subTest(key=key):
                if isinstance(value, str):
                    for forbidden in ("'", '"', ";", "--", "/*", "%", "/",
                                      "\\", "\n"):
                        self.assertNotIn(forbidden, value)


if __name__ == "__main__":
    unittest.main()


# The trainer's own prefixes, spelled out rather than imported, because these
# are the numbers the extract must never be later than. From
# `trainer/src/data_loader.py`:
#
#   qctx:          load_task_runs_for_queue_context(c, w.train_start - 90m, ...)
#                  and inside it, ref_lower = window_start - lookback_days
#   worker_counts: load_worker_counts(c, w.train_start - 30m, ...) and inside it
#                  fetch_from = train_start - 90m, so the real floor is -120m
#   throughput:    train_start - (max(windows_minutes) + 30)m = -90m today
TRAINER_QCTX_PREFIX_MIN = 90
TRAINER_WORKER_COUNTS_PREFIX_MIN = 120
TRAINER_THROUGHPUT_PREFIX_MIN = 90


def _parse(ts):
    return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC)


class TestTheDerivedBoundsSupersetTheTrainersWindows(unittest.TestCase):
    """THE P1 REGRESSION. The extract must be a superset of what each of the
    trainer's queries would have returned, or a candidate reproducing a past
    result silently trains on fewer rows than the result was computed from.

    The first version derived `ref_lower = train_start - lookback_days` and
    tested pending overlap against `train_start`. The trainer passes
    `window_start = train_start - 90m` and derives `ref_lower` from *that*, so
    both bounds were 90 minutes too late -- a subset in two independent ways.
    """

    def test_window_lower_precedes_every_prefix_the_trainer_uses(self):
        got = validate()
        train_start = _parse(got["train_start"])
        window_lower = _parse(got["window_lower"])
        for label, minutes in (
                ("qctx", TRAINER_QCTX_PREFIX_MIN),
                ("worker_counts", TRAINER_WORKER_COUNTS_PREFIX_MIN),
                ("throughput", TRAINER_THROUGHPUT_PREFIX_MIN)):
            with self.subTest(query=label):
                self.assertLessEqual(
                    window_lower,
                    train_start - datetime.timedelta(minutes=minutes),
                    f"the extract starts after the trainer's {label} window,"
                    f" so it is a subset")

    def test_ref_lower_precedes_the_trainers_qctx_floor(self):
        # The exact arithmetic of the bug: the trainer's floor is
        # (train_start - 90m) - lookback_days.
        for lookback in (1, 30, 120):
            with self.subTest(lookback_days=lookback):
                got = validate(a_request(lookback_days=lookback))
                trainer_floor = (
                    _parse(got["train_start"])
                    - datetime.timedelta(minutes=TRAINER_QCTX_PREFIX_MIN)
                    - datetime.timedelta(days=lookback))
                self.assertLessEqual(_parse(got["ref_lower"]), trainer_floor)

    def test_a_run_crossing_into_the_window_is_inside_the_extract(self):
        # The concrete case the old bounds dropped: a reference run that went
        # pending 100 minutes before `train_start` and was STILL pending at
        # `train_start`. It affects queue-context features for the first rows of
        # the window, and it sat outside both old bounds.
        got = validate()
        train_start = _parse(got["train_start"])
        pending_at = train_start - datetime.timedelta(minutes=100)
        exited_at = train_start + datetime.timedelta(minutes=5)

        # inside the qctx floor ...
        self.assertGreaterEqual(pending_at, _parse(got["ref_lower"]))
        # ... below the upper bound ...
        self.assertLess(pending_at, _parse(got["as_of_date"]))
        # ... and it survives the overlap predicate, which compares the run's
        # exit against window_lower rather than train_start.
        self.assertGreater(exited_at, _parse(got["window_lower"]))

    def test_the_lookback_constant_supersets_every_config_in_the_tree(self):
        # Moved here from the extractor's suite: the constant lives with the
        # derivation now, so the assertion about its value does too.
        self.assertGreaterEqual(extract_spec.WINDOW_LOOKBACK_MINUTES,
                                TRAINER_WORKER_COUNTS_PREFIX_MIN)


class TestTheWindowSpanIsBounded(unittest.TestCase):
    """`lookback_days` was bounded and the window itself was not, so an
    authorised caller could ask for 2010..2026 and get the full-history scan the
    lookback bound exists to prevent. The bound on a part is not a bound on the
    whole."""

    def test_the_largest_promoted_config_still_fits(self):
        # `run_duration.yaml`: lookback 30 + validation 1 + holdout 5 = 36 days.
        got = validate(a_request(train_start="2026-06-26T00:00:00Z",
                                 as_of_date="2026-08-01T00:00:00Z"))
        self.assertEqual(got["train_start"], "2026-06-26T00:00:00Z")

    def test_a_span_at_the_ceiling_is_accepted(self):
        start = (_parse("2026-08-01T00:00:00Z")
                 - datetime.timedelta(days=extract_spec.MAX_WINDOW_DAYS))
        validate(a_request(train_start=start.strftime("%Y-%m-%dT%H:%M:%SZ")))

    def test_a_span_past_the_ceiling_is_refused_and_names_the_bound(self):
        start = (_parse("2026-08-01T00:00:00Z")
                 - datetime.timedelta(days=extract_spec.MAX_WINDOW_DAYS + 1))
        with self.assertRaises(extract_spec.ExtractSpecError) as cm:
            validate(a_request(train_start=start.strftime("%Y-%m-%dT%H:%M:%SZ")))
        msg = str(cm.exception)
        self.assertIn(str(extract_spec.MAX_WINDOW_DAYS), msg)

    def test_a_multi_year_window_is_refused(self):
        with self.assertRaises(extract_spec.ExtractSpecError):
            validate(a_request(train_start="2021-01-01T00:00:00Z"))


class TestNoValidJsonShapeEscapesTheTypedRefusal(unittest.TestCase):
    """A validator that raises TypeError has not refused the request, it has
    crashed on it: the caller gets a traceback instead of a reason, and the
    dispatcher's refusal path never runs."""

    def test_an_unhashable_target_is_a_named_refusal(self):
        # `target not in TARGET_COLUMNS` raises "unhashable type: 'list'" if the
        # membership test runs before the type check.
        for bad in ([], ["wait_time"], {}, {"a": 1}, set()):
            with self.subTest(target=type(bad).__name__):
                with self.assertRaises(extract_spec.ExtractSpecError) as cm:
                    validate(a_request(target=bad))
                self.assertIn("target", str(cm.exception))

    def test_an_unrepresentable_date_is_a_named_refusal(self):
        # `datetime(1,1,1) - timedelta(days=30)` raises OverflowError, which
        # escaped as a crash rather than a refusal.
        for bad in ("0001-01-01T00:00:00Z", "0002-01-01T00:00:00Z"):
            with self.subTest(train_start=bad):
                with self.assertRaises(extract_spec.ExtractSpecError) as cm:
                    validate(a_request(train_start=bad))
                self.assertIn("train_start", str(cm.exception))

    def test_a_date_before_the_data_exists_is_refused(self):
        with self.assertRaises(extract_spec.ExtractSpecError) as cm:
            validate(a_request(train_start="2015-01-01T00:00:00Z"))
        self.assertIn("2020", str(cm.exception))

    def test_no_input_produces_an_exception_outside_the_family(self):
        # A sweep, because the two found by hand were both "a shape JSON permits
        # that the validator's own operators do not".
        shapes = [None, True, 1, 1.5, "", "x", [], {}, [[]], {"a": {}},
                  float("nan"), float("inf"), -1, 10 ** 30]
        for field in ("schema", "target", "train_start", "as_of_date",
                      "lookback_days", "generation"):
            for shape in shapes:
                with self.subTest(field=field, shape=repr(shape)):
                    try:
                        validate(a_request(**{field: shape}))
                    except extract_spec.ExtractSpecError:
                        pass
                    except Exception as e:            # noqa: BLE001
                        self.fail(f"{field}={shape!r} raised"
                                  f" {type(e).__name__}: {e}")


class TestTheWindowCeilingIsWhatMeasurementSupports(unittest.TestCase):
    """`MAX_WINDOW_DAYS` was 120, chosen for scan safety at 3.3x the largest
    promoted config, with no knowledge of runtime. The first real extraction
    measured the `runs` statement at 8 minutes for 36 days against a 30-minute
    per-statement `statement_timeout`, which puts 120 days at 89% of the budget.
    """

    def test_the_ceiling_is_the_measured_one(self):
        self.assertEqual(extract_spec.MAX_WINDOW_DAYS, 60)

    def test_the_ceiling_still_covers_the_largest_promoted_config(self):
        # `run_duration.yaml`: lookback 30 + validation 1 + holdout 5 = 36 days.
        # A ceiling below that would break the thing 2b exists to reproduce.
        self.assertGreaterEqual(extract_spec.MAX_WINDOW_DAYS, 36)

    def test_the_binding_statement_stays_under_half_the_timeout(self):
        # 8 minutes measured for 36 days, straight-line, against 30 minutes. The
        # margin is for volume GROWTH: those 8 minutes are today's row count, and
        # a ceiling sitting at 67% now is over 100% later, silently.
        minutes = 8.0 / 36 * extract_spec.MAX_WINDOW_DAYS
        self.assertLess(minutes, 15.0,
                        f"the runs statement would take ~{minutes:.0f} min of a"
                        f" 30 min statement_timeout at the ceiling")
