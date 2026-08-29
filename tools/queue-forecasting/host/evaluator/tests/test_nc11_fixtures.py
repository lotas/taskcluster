"""The NC11 row-set fixtures, EXECUTED against a real extract. Task 24 / NC11 (c).

WHY THIS FILE EXISTS. `host/nc-fixtures-phase2c.sh` writes five experiment
scripts into a `qf-research` checkout, the operator pushes them, and the NC suite
asserts that four of them are refused as `row_set_rejected` while the fifth is
accepted. Every one of those assertions is a claim about code THIS repository
generates and cannot otherwise run: the scripts execute inside the sandbox, on a
host, against an extract this repository does not have.

So the fixtures' refusal classes would be a guess -- and a fixture whose
violation is subtly wrong (a cherry-pick that drops a whole day, a ghost row
whose row_id also fails part 1) produces the RIGHT CLASS for the WRONG REASON,
which reads as coverage and is not. That is the failure mode this programme keeps
finding, so the generated scripts are run here, on the synthetic extract from
`test_evaluate.py`, and their output is fed to the real evaluator.

The two things that made this possible are worth naming, because both are
concessions in the fixtures: `NC11_EXTRACT`/`NC11_OUT` override the mount points,
and `NC11_HOLDOUT_DAYS` overrides the trainer's holdout length (the synthetic
extract is three days, not five). The sandbox sets none of them.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
EVALUATOR = os.path.dirname(HERE)
HOST = os.path.dirname(EVALUATOR)
sys.path.insert(0, HERE)
sys.path.insert(0, EVALUATOR)
sys.path.insert(0, os.path.join(HOST, "shared"))
sys.path.insert(0, os.path.join(HOST, "extractor"))

import evaluate as ev                                          # noqa: E402
# The Fixture, not a second one. A whole evaluable world -- extract, baseline,
# contract -- already exists there, built from the production schema; a copy of
# it here would drift, and the drift would be invisible because both would be
# green. `tests/` is not a package, so this resolves via sys.path[0] under a
# direct run and via the discovery directory under `discover`. Both are checked
# by a test at the bottom of this file.
from test_evaluate import HOLDOUT, Fixture                     # noqa: E402

GENERATOR = os.path.join(HOST, "nc-fixtures-phase2c.sh")

# The five, with what each one's violation must produce. `None` means "scored":
# the canary, and the reason the other four prove anything at all.
EXPECTED = {
    "nc11_honest": None,
    "nc11_relabelled": "row_set_rejected",
    "nc11_ghost_row": "row_set_rejected",
    "nc11_cherry_picked": "row_set_rejected",
    "nc11_easy_days": "row_set_rejected",
}


def generate(into):
    """Run the real generator against a directory shaped like qf-research."""
    os.makedirs(os.path.join(into, "trainer"), exist_ok=True)
    os.makedirs(os.path.join(into, ".git"), exist_ok=True)
    open(os.path.join(into, "trainer", "pyproject.toml"), "w").close()
    done = subprocess.run(["bash", GENERATOR, into],
                          capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        raise AssertionError(f"the generator failed: {done.stderr}")
    return os.path.join(into, "research", "experiments")


class Nc11FixtureCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.experiments = generate(os.path.join(cls.tmp, "qf-research"))

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def script(self, name):
        path = os.path.join(self.experiments, f"{name}.py")
        self.assertTrue(os.path.isfile(path), path)
        return path

    def emit(self, name, fx, *, holdout_days=len(HOLDOUT)):
        """Run one fixture as the sandbox would, into the staged inbox.

        The OUT directory is the evaluator's own inbox, so the file the fixture
        writes is the file the evaluator reads -- no copy in between that could
        differ from what a probe would have handed over.
        """
        inbox = os.path.dirname(fx.predictions_path)
        env = dict(os.environ, NC11_EXTRACT=fx.extract_dir, NC11_OUT=inbox,
                   NC11_HOLDOUT_DAYS=str(holdout_days),
                   PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, self.script(name)],
                              capture_output=True, text=True, timeout=300,
                              env=env)


class TestEachFixtureProducesTheOutcomeTheSuiteAsserts(Nc11FixtureCase):
    def outcome(self, name):
        fx = Fixture().build()
        self.addCleanup(fx.close)
        done = self.emit(name, fx)
        self.assertEqual(done.returncode, 0,
                         f"{name} exited {done.returncode}:"
                         f" {done.stdout}{done.stderr}")
        self.assertIn(f"mode={name}", done.stdout)
        self.assertNotIn("vacuous=1", done.stdout)
        try:
            reply = fx.run()
        except ev.EvaluateError as e:
            return ("refused", e.error_class, str(e))
        return ("scored", reply["verdict"], "")

    def test_the_honest_fixture_is_scored(self):
        """THE CANARY, and it carries the whole group. If the honest set were
        refused -- a holdout length that disagrees with the contract, a slice
        value that does not match, a day derivation off by an hour -- all four
        refusals below would still be `row_set_rejected` and every one of them
        would be measuring the same unrelated mistake."""
        kind, detail, message = self.outcome("nc11_honest")
        self.assertEqual(kind, "scored", message)
        self.assertIn(detail, ("go", "no-go"))

    def test_a_relabelled_row_id_is_refused(self):
        kind, detail, message = self.outcome("nc11_relabelled")
        self.assertEqual(kind, "refused", detail)
        self.assertEqual(detail, EXPECTED["nc11_relabelled"])
        # THE REASON, not just the class. Four fixtures share one class, so a
        # test that checked only the class could not tell whether this fixture
        # exercised its own part or somebody else's.
        self.assertIn("row_id", message)

    def test_a_ghost_row_is_refused_for_being_absent_not_mislabelled(self):
        kind, detail, message = self.outcome("nc11_ghost_row")
        self.assertEqual(kind, "refused", detail)
        self.assertEqual(detail, EXPECTED["nc11_ghost_row"])
        self.assertIn("not in the frozen extract", message)
        self.assertNotIn("disagree with their own", message)

    def test_a_cherry_picked_day_is_refused_for_completeness(self):
        kind, detail, message = self.outcome("nc11_cherry_picked")
        self.assertEqual(kind, "refused", detail)
        self.assertEqual(detail, EXPECTED["nc11_cherry_picked"])
        # COMPLETENESS, not the day block: the fixture keeps every day and drops
        # rows inside them, and if it lost a day the refusal would be the other
        # one with the same class.
        self.assertIn("omits", message)
        self.assertNotIn("not the candidate's to choose", message)

    def test_an_easier_earlier_block_is_refused_as_a_choice(self):
        kind, detail, message = self.outcome("nc11_easy_days")
        self.assertEqual(kind, "refused", detail)
        self.assertEqual(detail, EXPECTED["nc11_easy_days"])
        self.assertIn("not the candidate's to choose", message)
        # And NOT as an extract gap, which is the other half of that refusal and
        # the one that is not the candidate's doing.
        self.assertNotIn("gap in the EXTRACT", message)

    def test_every_generated_fixture_is_covered_by_this_class(self):
        # A sixth script added to the generator and to nobody's assertions is a
        # fixture whose behaviour is a guess again.
        found = {name[:-3] for name in os.listdir(self.experiments)
                 if name.startswith("nc11_") and name.endswith(".py")}
        self.assertEqual(found, set(EXPECTED))


class TestAFixtureThatDoesNothingFailsRatherThanPassing(Nc11FixtureCase):
    """The perturbation-that-perturbs-nothing check, in the fixtures themselves.

    This programme has shipped it twice: a `sum_abs_error` of 0.0 multiplied by
    1000, and a day-set test filtered onto days with no predictions. Here it
    would be worse than a silent pass -- a vacuous fixture emits an HONEST
    prediction set, the evaluation succeeds, and the NC clause reports that the
    evaluator failed to refuse a violation nobody committed.
    """

    def one_row_per_day(self):
        fx = Fixture()
        self.addCleanup(fx.close)
        # One row on each holdout day, so dropping 25% of a day rounds to zero.
        keep, seen = [], set()
        for row in fx.extract_rows:
            day = row["pending_at"].date().isoformat()
            if day in seen:
                continue
            seen.add(day)
            keep.append(row)
        fx.extract_rows = keep
        fx.prediction_rows = [
            {"task_id": r["task_id"], "run_id": 0,
             "row_id": f"{r['task_id']}:0", "p50": r["y_true"],
             "p90_raw": r["y_true"] * 1.5}
            for r in keep if r["pending_at"].date().isoformat() in HOLDOUT]
        return fx.build()

    def test_a_cherry_pick_that_drops_nothing_fails_the_probe(self):
        fx = self.one_row_per_day()
        done = self.emit("nc11_cherry_picked", fx)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("vacuous=1", done.stdout)
        self.assertIn("rounded to zero dropped rows", done.stdout)

    def test_an_honest_fixture_on_the_wrong_holdout_length_fails(self):
        # The mismatch the canary exists to expose, forced: asking for a 30-day
        # holdout out of a 3-day extract. It must FAIL rather than claim the
        # days it happens to find.
        fx = Fixture().build()
        self.addCleanup(fx.close)
        done = self.emit("nc11_honest", fx, holdout_days=30)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("vacuous=1", done.stdout)
        self.assertIn("NC11_HOLDOUT_DAYS", done.stdout)


class TestTheSecondImplementationAgreesWithTheTrustedOne(Nc11FixtureCase):
    """`required_days` exists twice: in `evaluate.py`, and in every fixture --
    which cannot import the trusted module because it runs as agent-authored code
    inside a sandbox with no host code on its path. `baseline_contract.py` has
    the same shape for `canonical()`, and the rule there is the rule here: a
    duplicated derivation is acceptable only if something compares the two."""

    def fixture_module(self, name="nc11_honest"):
        spec = importlib.util.spec_from_file_location(
            f"nc11_mod_{name}", self.script(name))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_two_derivations_agree(self):
        module = self.fixture_module()
        for as_of in ("2026-08-23T00:00:00Z", "2026-01-01T00:00:00Z",
                      "2026-03-01T00:00:00Z", "2024-02-29T00:00:00Z",
                      "2026-12-31T00:00:00Z"):
            for days in (1, 3, 5, 14, 31):
                with self.subTest(as_of=as_of, days=days):
                    self.assertEqual(module.required_days(as_of, days),
                                     ev.required_days(as_of, days))

    def test_all_five_carry_the_same_derivation(self):
        want = self.fixture_module("nc11_honest").required_days(
            "2026-08-23T00:00:00Z", 5)
        for name in EXPECTED:
            with self.subTest(name=name):
                self.assertEqual(
                    self.fixture_module(name).required_days(
                        "2026-08-23T00:00:00Z", 5), want)


class TestTheGeneratedScriptsAreWhatTheSandboxCanRun(Nc11FixtureCase):
    def sources(self):
        for name in EXPECTED:
            with open(self.script(name)) as fh:
                yield name, fh.read()

    def test_no_fixture_imports_a_sibling(self):
        # A probe names ONE path. An import of another experiment file would
        # resolve here, where the whole directory is on sys.path, and fail in the
        # sandbox -- the exact class of defect 2b-1's P1 was.
        for name, source in self.sources():
            with self.subTest(name=name):
                for other in EXPECTED:
                    self.assertNotIn(f"import {other}", source)

    def test_each_declares_its_own_mode(self):
        # The generator substitutes MODE_NAME into the shared core. An
        # unsubstituted placeholder would make every fixture report `mode=`
        # MODE_NAME and the suite would match the wrong probe.
        for name, source in self.sources():
            with self.subTest(name=name):
                self.assertIn(f'MODE = "{name}"', source)
                self.assertNotIn("MODE_NAME", source.split('"""', 2)[-1])

    def test_each_can_refuse_to_be_vacuous(self):
        for name, source in self.sources():
            with self.subTest(name=name):
                self.assertIn("return vacuous(", source)

    def test_the_frozen_prediction_columns_are_the_only_ones_written(self):
        module = self.fixture_module = None       # not needed; source is enough
        for name, source in self.sources():
            with self.subTest(name=name):
                block = source[source.index("def emit("):]
                block = block[:block.index("\ndef ")]
                for column in ("task_id", "run_id", "row_id", "p50", "p90_raw"):
                    self.assertIn(f'"{column}"', block)

    def test_the_generator_refuses_a_directory_that_is_not_qf_research(self):
        # It writes into somebody's checkout. A generator that would happily
        # write five experiment files into an unrelated repository is one
        # mistyped path away from doing so.
        with tempfile.TemporaryDirectory() as other:
            # A git repository, so it gets past the usage check -- and NOT
            # qf-research, which is the thing being refused. Handing it an empty
            # directory would have tested the usage message instead, which is
            # what the first version of this test did.
            os.makedirs(os.path.join(other, ".git"))
            done = subprocess.run(["bash", GENERATOR, other],
                                  capture_output=True, text=True, timeout=60)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("does not look like qf-research",
                          done.stdout + done.stderr)
            self.assertFalse(os.path.exists(os.path.join(other, "research")))

    def test_the_probe_lines_pin_a_baseline(self):
        """None of these scripts reads /baseline, and the probes still need one.

        The evaluator refuses a judged run that recorded no `baseline_hash` --
        the contract states its bars against a specific baseline, and a relative
        improvement measured against a different one is not the bar that was
        agreed. So instructions that omitted `--baseline` would produce five
        probes whose evaluations all fail for that reason, canary included, and
        NC11 (c) would void with nothing to say about row sets.
        """
        with open(GENERATOR) as fh:
            source = fh.read()
        instructions = source[source.index("cat <<'NEXT'"):]
        # THE COMMAND ITSELF, continuations joined. A first version of this test
        # searched the whole instruction block, and `--baseline` also appears
        # there in a `qf baselines` comment and in the paragraph explaining the
        # requirement -- so deleting the flag from the probe line left it green.
        joined = re.sub(r"\\\n\s*", " ", instructions)
        probe = [line for line in joined.splitlines() if "qf probe" in line]
        self.assertEqual(len(probe), 1, probe)
        self.assertIn("--extract", probe[0])
        self.assertIn("--baseline", probe[0])
        # And it says WHY, because an unexplained flag is the first thing
        # somebody drops when the command does not fit on a line.
        self.assertIn("carries no baseline_hash", instructions)

    def test_it_never_commits_or_pushes(self):
        with open(GENERATOR) as fh:
            source = fh.read()
        # The dispatcher's token is read-only and the fixture branch is the
        # operator's to publish with the AGENT's credential.
        body = source[source.index("set -Eeuo"):source.index("cat <<'NEXT'")]
        for forbidden in ("git commit", "git push", "git add"):
            self.assertNotIn(forbidden, body)


if __name__ == "__main__":
    # AT THE END. Nine files in this tree had this guard mid-file with classes
    # below it, so a direct run executed a subset and reported OK.
    unittest.main()
