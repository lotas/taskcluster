# `experiment.experiment_note` -- the gate between a CLI invocation and the
# immutable note that becomes the pre-registration.
#
# WHY IT NEEDED ITS OWN FILE. `test_tick.sh` stubs `experiment.py` entirely, so
# nothing exercised this function: the tick tests prove the tick calls something,
# and `test_prereg.py` proves the format is sound, but the mapping between them --
# which flags are mandatory under `QF_REQUIRE_PREREG`, where the digest comes
# from, and what happens to a bad tolerance -- was untested. That mapping is
# where an unattended run silently becomes unjudgeable.
import os
import sys
import tempfile
import unittest

HOST = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HOST)
sys.path.insert(0, os.path.join(HOST, "research-loop"))

import experiment as X                                          # noqa: E402
import prereg as P                                              # noqa: E402


class Args:
    """The subset of `argparse.Namespace` that `experiment_note` reads."""

    def __init__(self, **kw):
        self.config = "configs/a.yaml"
        self.note = "queue context should cut central error"
        self.bar = "mae"
        self.dir = "improve"
        self.vs = "probe-20260831T130111Z-51b862ebf4de-5568"
        self.tol = 0.0
        self.reference_run = False
        self.workspace_resolved = None
        self.__dict__.update(kw)


class NoteBuilding(unittest.TestCase):
    def setUp(self):
        # A workspace whose `trainer/configs/a.yaml` exists, because the digest
        # is taken from the file on disk rather than from the config name.
        self.ws = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.ws, "trainer", "configs"))
        self.cfg = os.path.join(self.ws, "trainer", "configs", "a.yaml")
        with open(self.cfg, "w") as fh:
            fh.write("target: wait_time\nholdout_days: 5\n")
        self._prev = os.environ.get("QF_REQUIRE_PREREG")
        os.environ["QF_REQUIRE_PREREG"] = "1"

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("QF_REQUIRE_PREREG", None)
        else:
            os.environ["QF_REQUIRE_PREREG"] = self._prev

    def note(self, **kw):
        return X.experiment_note(Args(workspace_resolved=self.ws, **kw))

    # ---- the digest -----------------------------------------------------
    def test_the_digest_comes_from_the_file_on_disk(self):
        got = P.decode(self.note())
        self.assertEqual(got["cfgh"], P.config_digest(self.cfg))
        self.assertTrue(got["registered"])

    def test_editing_the_config_changes_the_note(self):
        first = P.decode(self.note())["cfgh"]
        with open(self.cfg, "a") as fh:
            fh.write("min_data_in_leaf: 100\n")
        self.assertNotEqual(P.decode(self.note())["cfgh"], first)

    def test_a_missing_config_file_is_refused_before_anything_is_pushed(self):
        os.remove(self.cfg)
        with self.assertRaises(X.Refused) as caught:
            self.note()
        self.assertIn("digest", str(caught.exception))

    # ---- mandatory fields under QF_REQUIRE_PREREG ----------------------
    def test_no_bar_is_refused(self):
        with self.assertRaises(X.Refused) as caught:
            self.note(bar=None)
        self.assertIn("QF_REQUIRE_PREREG", str(caught.exception))

    def test_no_vs_is_refused(self):
        # THE CASE THAT COST A FULL TRAINING CYCLE: this used to submit happily
        # and score a number `frontier.py` could only mark unjudgeable.
        with self.assertRaises(X.Refused) as caught:
            self.note(vs=None)
        self.assertIn("--vs", str(caught.exception))

    def test_no_note_is_refused(self):
        with self.assertRaises(X.Refused) as caught:
            self.note(note=None)
        self.assertIn("--note", str(caught.exception))

    def test_reference_run_replaces_vs(self):
        got = P.decode(self.note(vs=None, reference_run=True))
        self.assertTrue(got["registered"])
        self.assertTrue(got["reference"])

    def test_reference_run_with_vs_is_refused(self):
        with self.assertRaises(X.Refused):
            self.note(reference_run=True)

    # ---- tolerance -----------------------------------------------------
    def test_a_tolerance_reaches_the_note(self):
        got = P.decode(self.note(bar="p90_miss_tail", dir="hold", tol=0.004))
        self.assertAlmostEqual(got["tol"], 0.004)

    def test_a_negative_tolerance_is_refused(self):
        with self.assertRaises(X.Refused):
            self.note(bar="p90_miss_tail", dir="hold", tol=-0.01)

    def test_a_non_finite_tolerance_is_refused(self):
        for bad in (float("inf"), float("nan")):
            with self.assertRaises(X.Refused):
                self.note(bar="p90_miss_tail", dir="hold", tol=bad)

    # ---- the note is submittable ---------------------------------------
    def test_the_note_is_always_within_the_dispatcher_limit(self):
        long_note = self.note(note="x" * 900)
        self.assertTrue(P.is_valid_note(long_note))
        self.assertLessEqual(len(long_note), P.NOTE_MAX)

    # ---- the unrequired path -------------------------------------------
    def test_without_the_env_var_a_bare_run_keeps_the_old_shape(self):
        os.environ.pop("QF_REQUIRE_PREREG", None)
        note = self.note(bar=None)
        self.assertTrue(note.startswith("cfg=configs/a.yaml | "))
        self.assertFalse(P.decode(note)["registered"])

    def test_without_the_env_var_a_partial_prereg_is_still_refused(self):
        # An operator who supplies --bar has opted in, and half a
        # pre-registration is the unfalsifiable case, not a lighter one.
        os.environ.pop("QF_REQUIRE_PREREG", None)
        with self.assertRaises(X.Refused):
            self.note(vs=None)


if __name__ == "__main__":
    unittest.main()
