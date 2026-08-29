# Tests for spec.py. No I/O. Run:
#   python3 -m unittest discover -s host/dispatcher/tests
#
# Each case names the failure it prevents. A job field is not a convenience:
# it is the only thing an untrusted caller controls, so a field that is
# accepted loosely is a hole in the boundary (design D12).
import datetime
import unittest

import os
import sys

# `host/shared` on the path: `spec.normalize` delegates the `extract` kind to
# `shared/extract_spec.py`, the one closed-world definition both privilege
# domains use (D16). Inline rather than in a shared helper module, because
# `tests/` is not a package and a helper only resolves under `unittest discover`
# -- a bootstrap that works under one invocation is worse than two copies.
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "shared"))

import spec                                                    # noqa: E402

SHA = "3f1c" + "0" * 36


def base(**over):
    d = {"schema": 1, "kind": "test", "source_sha": SHA}
    d.update(over)
    return d


class TestTopLevel(unittest.TestCase):
    def test_minimal_spec_normalizes_and_fills_kind_defaults(self):
        eff = spec.normalize(base())
        self.assertEqual(eff["timeout_s"], 1800)
        self.assertEqual(eff["mem_limit"], "4g")
        self.assertEqual(eff["cpus"], 4.0)
        # `trainer/tests`, because the worktree ROOT is the mount point and
        # qf-research keeps its suite one level down. The first live `--kind
        # test` submission without --path came back pytest exit 4 on the old
        # default.
        self.assertEqual(eff["args"]["paths"], ["trainer/tests"])
        self.assertEqual(eff["lane"], "light")   # derived, not supplied

    def test_unknown_top_level_key_is_refused(self):
        # The whole point of closed-world validation: a caller must not be able
        # to introduce a field a later version might honour.
        with self.assertRaises(spec.SpecError):
            spec.normalize(base(dockerfile="/home/research/evil"))

    def test_unknown_schema_version_is_refused(self):
        with self.assertRaises(spec.SpecError):
            spec.normalize(base(schema=2))

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(spec.SpecError):
            spec.normalize(base(kind="confirm"))  # arrives in 2d, not now

    def test_short_or_uppercase_sha_is_refused(self):
        for bad in ["3f1c", SHA.upper(), "z" * 40, "", None, 40 * "0" + "0"]:
            with self.assertRaises(spec.SpecError):
                spec.normalize(base(source_sha=bad))

    def test_branch_name_is_not_a_sha(self):
        # Pinning means a SHA. A ref would be mutable, which is the hole §7
        # exists to close.
        with self.assertRaises(spec.SpecError):
            spec.normalize(base(source_sha="feat/queue-forecasting"))


class TestLimits(unittest.TestCase):
    def test_mem_limit_above_host_ceiling_is_refused(self):
        with self.assertRaises(spec.SpecError):
            spec.normalize(base(mem_limit="32g"))

    def test_mem_limit_units_and_shape(self):
        self.assertEqual(spec.normalize(base(mem_limit="512m"))["mem_limit"], "512m")
        for bad in ["8G", "8gb", "8", "0g", "-8g", "8 g"]:
            with self.assertRaises(spec.SpecError):
                spec.normalize(base(mem_limit=bad))

    def test_timeout_ceiling_is_coupled_to_the_nightly_wait(self):
        # 3600 in 2a: the whole chain (timeout + build + build-lock wait +
        # handoff + setup/teardown) must fit inside JOB_HOLD_DEADLINE_S, which
        # must fit inside LOCK_WAIT_S (design D10a). Revision 6 set the deadline
        # to exactly TIMEOUT_MAX + BUILD_TIMEOUT_S, leaving nothing for either.
        # This assertion is here so the coupling breaks a test rather than a
        # night's training.
        self.assertEqual(spec.TIMEOUT_MAX, 3600)

    def test_timeout_and_cpu_ranges(self):
        for bad in [0, 59, 3601, "1800", 1800.5, True]:
            with self.assertRaises(spec.SpecError):
                spec.normalize(base(timeout_s=bad))
        for bad in [0, 0.1, 16, "4"]:
            with self.assertRaises(spec.SpecError):
                spec.normalize(base(cpus=bad))

    def test_bool_is_not_an_int(self):
        # In Python True == 1. A spec that accepts True for timeout_s would
        # store a nonsense record and pass every later type check.
        with self.assertRaises(spec.SpecError):
            spec.normalize(base(cpus=True))


class TestLaneIsDerivedNotRequested(unittest.TestCase):
    # A caller-selected lane is a caller-selected concurrency limit. The first
    # revision let `test` ask for a lane while independently allowing 22g, so
    # two light jobs could admit 44g on a 29g host -- the exact failure NC8
    # exists to prevent (design D10).
    def test_lane_field_is_not_accepted_at_all(self):
        with self.assertRaises(spec.SpecError):
            spec.normalize(base(lane="heavy"))

    def test_small_memory_derives_light(self):
        self.assertEqual(spec.normalize(base(mem_limit="4g"))["lane"], "light")

    def test_memory_above_the_light_ceiling_derives_heavy(self):
        # This is how NC8 gets a heavy job before any heavy kind exists: by
        # exercising the real admission rule instead of a flag added for it.
        self.assertEqual(spec.normalize(base(mem_limit="8g"))["lane"], "heavy")

    def test_the_ceiling_is_inclusive_and_stated(self):
        self.assertEqual(spec.LIGHT_MEM_CEILING_MB, 4 * 1024)
        self.assertEqual(spec.normalize(base(mem_limit="4096m"))["lane"], "light")
        self.assertEqual(spec.normalize(base(mem_limit="4097m"))["lane"], "heavy")


class TestTestArgs(unittest.TestCase):
    def test_absolute_path_is_refused(self):
        with self.assertRaises(spec.SpecError):
            spec.normalize(base(args={"paths": ["/etc/passwd"]}))

    def test_parent_traversal_is_refused(self):
        for bad in ["../src", "tests/../../etc", "..", "tests/.."]:
            with self.assertRaises(spec.SpecError):
                spec.normalize(base(args={"paths": [bad]}))

    def test_node_id_selection_is_allowed(self):
        eff = spec.normalize(base(args={"paths": ["tests/test_model.py::test_fit"]}))
        self.assertEqual(eff["args"]["paths"], ["tests/test_model.py::test_fit"])

    def test_pytest_args_are_an_allowlist_not_a_pattern(self):
        self.assertEqual(
            spec.normalize(base(args={"pytest_args": ["-q", "--tb=short"]}))["args"]["pytest_args"],
            ["-q", "--tb=short"],
        )
        for bad in ["--pdb", "-pno:randomly", "-p", "--co", "--maxfail=99", "-q -x"]:
            with self.assertRaises(spec.SpecError):
                spec.normalize(base(args={"pytest_args": [bad]}))

    def test_duplicate_pytest_flag_is_refused(self):
        with self.assertRaises(spec.SpecError):
            spec.normalize(base(args={"pytest_args": ["-q", "-q"]}))

    def test_k_expression_shape(self):
        self.assertEqual(spec.normalize(base(args={"k": "hazard and not slow"}))["args"]["k"],
                         "hazard and not slow")
        for bad in ["`id`", "$(id)", "a" * 201, ";rm -rf /"]:
            with self.assertRaises(spec.SpecError):
                spec.normalize(base(args={"k": bad}))

    def test_too_many_paths_is_refused(self):
        with self.assertRaises(spec.SpecError):
            spec.normalize(base(args={"paths": [f"tests/t{i}.py" for i in range(21)]}))

    def test_selftest_takes_no_args(self):
        spec.normalize(base(kind="selftest", args={}))
        with self.assertRaises(spec.SpecError):
            spec.normalize(base(kind="selftest", args={"paths": ["tests"]}))


class TestNote(unittest.TestCase):
    def test_note_is_printable_and_bounded(self):
        spec.normalize(base(note="H-0031 baseline"))
        for bad in ["x" * 501, "line\nbreak", "bell\x07"]:
            with self.assertRaises(spec.SpecError):
                spec.normalize(base(note=bad))


class TestHash(unittest.TestCase):
    def test_hash_is_over_the_effective_spec_not_the_input(self):
        # Two inputs that mean the same job must record the same hash, or the
        # audit record is faithful to typing rather than to what ran.
        a = spec.spec_hash(spec.normalize(base()))
        b = spec.spec_hash(spec.normalize(base(timeout_s=1800,
                                               mem_limit="4g", cpus=4.0,
                                               args={"paths":
                                                     ["trainer/tests"]})))
        self.assertEqual(a, b)

    def test_key_order_does_not_change_the_hash(self):
        raw = {"source_sha": SHA, "kind": "test", "schema": 1}
        self.assertEqual(spec.spec_hash(spec.normalize(raw)),
                         spec.spec_hash(spec.normalize(base())))

    def test_different_memory_changes_the_hash_and_the_lane(self):
        big = spec.normalize(base(mem_limit="8g"))
        self.assertNotEqual(spec.spec_hash(spec.normalize(base())),
                            spec.spec_hash(big))
        self.assertEqual(big["lane"], "heavy")


class TestTrailingNewlineIsNotAnAnchorHole(unittest.TestCase):
    # Deviation from the plan text, revision 12: every field regex was anchored
    # with `$`, which in Python also matches immediately BEFORE a trailing
    # newline. So "<40 hex>\n" satisfied ^[0-9a-f]{40}$ and became a git argv
    # element; "4g\n" became a --memory flag; and `note` accepted the newline
    # its own error message forbids. The anchor is \Z, which means end of
    # string and nothing else. These cases fail on `$` and pass on `\Z`.
    def test_trailing_newline_is_refused_in_every_string_field(self):
        for field, payload in [
            ("source_sha", base(source_sha=SHA + "\n")),
            ("mem_limit", base(mem_limit="4g\n")),
            ("note", base(note="ok\n")),
            ("args.paths", base(args={"paths": ["tests\n"]})),
            ("args.k", base(args={"k": "hazard\n"})),
        ]:
            with self.subTest(field=field), self.assertRaises(spec.SpecError):
                spec.normalize(payload)


if __name__ == "__main__":
    unittest.main()


class TestTheExtractKind(unittest.TestCase):
    """Phase 2b-1 Task 5. An extraction is a job so that it gets the state
    machine, the event chain and `qf status` -- but it runs no container, no
    image build and no worktree, and it has no commit.

    `source_sha` is `NOT NULL` in a schema with sixteen hundred live rows, so
    rather than migrate it, an extract job stores its **request_hash** there: the
    column's role is "the immutable identity of what this job ran", which for a
    test is a commit and for an extraction is the request. `source_ref` is set to
    a literal that makes it unmistakable the value is not a commit, because a
    reader who assumed it was would join on it.
    """

    def a_raw(self, **over):
        args = {"target": "wait_time",
                "train_start": "2026-07-01T00:00:00Z",
                "as_of_date": "2026-08-01T00:00:00Z",
                "lookback_days": 30}
        args.update(over.pop("args", {}))
        raw = {"schema": 1, "kind": "extract", "args": args}
        raw.update(over)
        return raw

    def validate(self, raw=None, **kw):
        kw.setdefault("now", datetime.datetime(2026, 8, 5,
                                               tzinfo=datetime.timezone.utc))
        kw.setdefault("settlement_lag_s", 48 * 3600)
        return spec.normalize(self.a_raw() if raw is None else raw, **kw)

    def test_it_needs_no_source_sha_from_the_caller(self):
        # The caller cannot know the request hash, and requiring a commit would
        # record a dependency the extract does not have.
        effective = self.validate()
        self.assertEqual(len(effective["source_sha"]), 64)
        self.assertEqual(effective["source_ref"], spec.EXTRACT_SOURCE_REF)

    def test_the_source_sha_is_the_request_hash(self):
        import extract_spec
        effective = self.validate()
        request = extract_spec.validate(
            {"schema": 1, **self.a_raw()["args"]},
            now=datetime.datetime(2026, 8, 5, tzinfo=datetime.timezone.utc),
            settlement_lag_s=48 * 3600)
        self.assertEqual(effective["source_sha"],
                         extract_spec.request_hash(request))

    def test_a_supplied_source_sha_is_refused(self):
        # Not ignored: a caller that thought it was choosing the identity of the
        # extract would be wrong, and silently.
        with self.assertRaises(spec.SpecError) as cm:
            self.validate(self.a_raw(source_sha="a" * 40))
        self.assertIn("source_sha", str(cm.exception))

    def test_the_effective_spec_carries_the_normalised_request(self):
        effective = self.validate()
        self.assertEqual(effective["args"]["target_column"], "wait_duration_s")
        self.assertEqual(effective["args"]["generation"], 1)
        self.assertIn("window_lower", effective["args"])

    def test_an_invalid_request_is_refused_here_too(self):
        # qfd validates so a bad request is refused cheaply and legibly at submit
        # time. The extractor validates again because a caller is a caller (D16).
        for bad in ({"target": "p90"}, {"lookback_days": 0},
                    {"as_of_date": "2026-08-01T06:00:00Z"}):
            with self.subTest(bad=bad):
                with self.assertRaises(spec.SpecError):
                    self.validate(self.a_raw(args=bad))

    def test_it_lands_in_the_light_lane_and_takes_no_mutex(self):
        # An extraction runs in another process entirely. It occupies a slot so
        # the dispatcher's bookkeeping stays honest, and nothing more -- putting
        # it in the heavy lane would let a read block the nightly.
        effective = self.validate()
        self.assertEqual(effective["lane"], "light")
        self.assertLessEqual(spec.mem_mb(effective["mem_limit"]),
                             spec.LIGHT_MEM_CEILING_MB)

    def test_its_timeout_covers_a_measured_extraction(self):
        # The first real extraction took 688s for 36 days, and the ceiling is 60
        # days. A timeout under that would kill work the extractor completed.
        effective = self.validate()
        self.assertGreaterEqual(effective["timeout_s"], 1800)

    def test_the_clock_and_lag_are_required_for_this_kind(self):
        # They are what make the settlement rule enforceable. Defaulting them
        # would make qfd's copy of the rule silently absent.
        with self.assertRaises(spec.SpecError):
            spec.normalize(self.a_raw())

    def test_other_kinds_still_require_a_real_sha(self):
        with self.assertRaises(spec.SpecError):
            spec.normalize({"schema": 1, "kind": "test"})
        ok = spec.normalize({"schema": 1, "kind": "test", "source_sha": "b" * 40})
        self.assertEqual(ok["source_sha"], "b" * 40)
        self.assertIsNone(ok.get("source_ref"))


class TestTheProbeKind(unittest.TestCase):
    """Phase 2b-2 Task 9. A probe runs agent-authored code against a frozen
    extract, so unlike `extract` it DOES take a real commit -- and unlike `test`
    it is confined to `research/experiments/`.

    It names its extract and the extract must ALREADY EXIST. A probe that
    triggered an eleven-minute extraction would put a surprise inside a job
    somebody expected to be quick, and reuse already makes "extract once, probe
    often" cheap."""

    EXTRACT = "a" * 64

    def a_raw(self, **over):
        args = {"path": "research/experiments/cohort.py", "extract": self.EXTRACT}
        args.update(over.pop("args", {}))
        raw = {"schema": 1, "kind": "probe", "source_sha": "b" * 40,
               "args": args}
        raw.update(over)
        return raw

    def test_a_valid_probe_normalises(self):
        eff = spec.normalize(self.a_raw())
        self.assertEqual(eff["kind"], "probe")
        self.assertEqual(eff["args"]["path"], "research/experiments/cohort.py")
        self.assertEqual(eff["args"]["extract"], self.EXTRACT)

    def test_it_requires_a_real_commit(self):
        # It runs code, so the record must say WHICH code. This is the opposite
        # of `extract`, whose identity is its request.
        with self.assertRaises(spec.SpecError):
            raw = self.a_raw()
            del raw["source_sha"]
            spec.normalize(raw)

    def test_a_relative_path_outside_the_prefix_names_the_prefix(self):
        for bad in ("trainer/tests/test_x.py", "research/x.py",
                    "trainer/src/model.py"):
            with self.subTest(path=bad):
                with self.assertRaises(spec.SpecError) as cm:
                    spec.normalize(self.a_raw(args={"path": bad}))
                self.assertIn("research/experiments", str(cm.exception))

    def test_an_escaping_path_is_refused_for_the_more_precise_reason(self):
        # These are refused BEFORE the prefix check, and the earlier message is
        # the better one: "must be a relative path" and "escapes the worktree"
        # say what is wrong, where "must be under research/experiments" would
        # invite someone to prepend the prefix to an absolute path.
        for bad, expected in (("/etc/passwd", "relative path"),
                              ("../research/experiments/x.py", "relative path"),
                              ("research/experiments/../../trainer/src/m.py",
                               "escapes the worktree")):
            with self.subTest(path=bad):
                with self.assertRaises(spec.SpecError) as cm:
                    spec.normalize(self.a_raw(args={"path": bad}))
                self.assertIn(expected, str(cm.exception))

    def test_the_path_must_be_a_python_file(self):
        # The entrypoint runs it with the venv interpreter, so a directory or a
        # shell script would fail inside the container rather than here.
        for bad in ("research/experiments/", "research/experiments/x.sh",
                    "research/experiments/x"):
            with self.subTest(path=bad):
                with self.assertRaises(spec.SpecError):
                    spec.normalize(self.a_raw(args={"path": bad}))

    def test_exactly_one_path(self):
        # A probe is one script, not a set of test paths: `paths` would invite
        # the pytest shape and there is no pytest here.
        with self.assertRaises(spec.SpecError):
            spec.normalize(self.a_raw(args={"paths": ["research/experiments/x.py"]}))

    def test_the_extract_must_look_like_a_request_hash(self):
        for bad in ("", "short", "A" * 64, "g" * 64, 7, None):
            with self.subTest(extract=bad):
                with self.assertRaises(spec.SpecError) as cm:
                    spec.normalize(self.a_raw(args={"extract": bad}))
                self.assertIn("extract", str(cm.exception))

    def test_the_extract_is_required(self):
        # A probe with no extract is a probe with no data, and it would fail
        # inside the container with a FileNotFoundError about /extract.
        raw = self.a_raw()
        del raw["args"]["extract"]
        with self.assertRaises(spec.SpecError):
            spec.normalize(raw)

    def test_an_unknown_arg_is_refused(self):
        with self.assertRaises(spec.SpecError):
            spec.normalize(self.a_raw(args={"pytest_args": ["-q"]}))

    def test_it_is_heavy_by_derivation_not_by_request(self):
        # A cohort trains, so it competes with the nightly for the same host --
        # and the lane is DERIVED from memory (D10), never requested. The default
        # is above the light ceiling, which is correct rather than incidental.
        eff = spec.normalize(self.a_raw())
        self.assertEqual(eff["lane"], "heavy")
        self.assertGreater(spec.mem_mb(eff["mem_limit"]),
                           spec.LIGHT_MEM_CEILING_MB)

    def test_a_small_probe_may_still_be_light(self):
        # Nothing about the KIND forces heavy; the memory does. A probe that only
        # reads the manifest has no business holding the training mutex.
        eff = spec.normalize(self.a_raw(mem_limit="1g"))
        self.assertEqual(eff["lane"], "light")

    def test_its_timeout_covers_a_training_run(self):
        eff = spec.normalize(self.a_raw())
        self.assertGreaterEqual(eff["timeout_s"], 1800)


class TestTheProbeEntrypoint(unittest.TestCase):
    def test_it_runs_the_script_with_the_venv_interpreter(self):
        import sandbox
        eff = spec.normalize({"schema": 1, "kind": "probe",
                              "source_sha": "b" * 40,
                              "args": {"path": "research/experiments/cohort.py",
                                       "extract": "a" * 64}})
        argv = sandbox.entrypoint_for(eff)
        # ABSOLUTE, built from SRC_DEST: a relative path resolves against the
        # image's WORKDIR, which is a Dockerfile in another repository.
        self.assertEqual(argv, [sandbox.VENV_PYTHON,
                                sandbox.SRC_DEST
                                + "/research/experiments/cohort.py"])

    def test_the_extract_is_not_passed_as_an_argument(self):
        # It is mounted at a fixed path. A path passed as an argument is a path
        # something has to validate twice, and the second validator is inside
        # untrusted code.
        import sandbox
        eff = spec.normalize({"schema": 1, "kind": "probe",
                              "source_sha": "b" * 40,
                              "args": {"path": "research/experiments/cohort.py",
                                       "extract": "a" * 64}})
        argv = sandbox.entrypoint_for(eff)
        self.assertFalse(any("a" * 64 in part for part in argv))
        self.assertFalse(any(part.startswith("/extract") for part in argv))
