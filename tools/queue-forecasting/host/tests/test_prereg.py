# The pre-registration note format.
#
# WHAT THESE ARE REALLY GUARDING. The note is the only tamper-evident place a
# claim can live (it goes into `spec_json` at submit time and the store
# hash-chains it), which means every property below is load-bearing: a note
# `spec.py` rejects loses the whole experiment at submit, and a note that
# decodes as `registered` without a falsifiable claim is worse than no note --
# it makes the frontier report a prediction that was never made.
import os
import re
import sys
import unittest

HOST = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOST, "research-loop"))
sys.path.insert(0, os.path.join(HOST, "dispatcher"))

import prereg as P                                              # noqa: E402


class NoteBudget(unittest.TestCase):
    """The limit is duplicated from `spec.py` as a number, so it is checked."""

    def test_limit_matches_the_dispatcher(self):
        with open(os.path.join(HOST, "dispatcher", "spec.py")) as fh:
            source = fh.read()
        found = re.search(r"_NOTE_RE = re\.compile\(r\"\^\[\\x20-\\x7e\]"
                          r"\{0,(\d+)\}", source)
        self.assertIsNotNone(found, "spec.py's note regex changed shape;"
                                    " prereg.NOTE_MAX may now be wrong")
        self.assertEqual(int(found.group(1)), P.NOTE_MAX)

    def test_a_long_hypothesis_is_truncated_not_refused(self):
        note = P.encode("configs/wait_qctx_d_priority_flow.yaml", "mae",
                        "improve", "x" * 900, vs="eval-123")
        self.assertLessEqual(len(note), P.NOTE_MAX)
        self.assertTrue(P.is_valid_note(note))
        # The STRUCTURED fields survive; only the prose is cut.
        got = P.decode(note)
        self.assertEqual(got["bar"], "mae")
        self.assertEqual(got["direction"], "improve")
        self.assertEqual(got["vs"], "eval-123")
        self.assertTrue(got["hypothesis"].endswith("..."))

    def test_non_ascii_is_substituted_so_the_submit_still_succeeds(self):
        note = P.encode("configs/a.yaml", "mae", "improve",
                        "qctx sharpens the model — tail pays for it", vs="e0")
        self.assertTrue(P.is_valid_note(note))
        self.assertNotIn("—", note)


class Roundtrip(unittest.TestCase):
    def test_encode_decode(self):
        note = P.encode("configs/wait_qctx_d_priority_flow.yaml", "p90_miss_tail",
                        "hold", "dropping capacity should not cost the tail",
                        vs="probe-20260831T130111Z-51b862ebf4de-5568")
        got = P.decode(note)
        self.assertTrue(got["registered"])
        self.assertEqual(got["config"],
                         "configs/wait_qctx_d_priority_flow.yaml")
        self.assertEqual(got["bar"], "p90_miss_tail")
        self.assertEqual(got["direction"], "hold")
        self.assertEqual(got["hypothesis"],
                         "dropping capacity should not cost the tail")

    def test_cfg_stays_first_so_results_py_still_labels_the_column(self):
        # `results.py:_split_note` needs `cfg=<path> | ...` verbatim.
        note = P.encode("configs/a.yaml", "mae", "improve", "why", vs="e0")
        self.assertTrue(note.startswith("cfg=configs/a.yaml | "))

    def test_a_hypothesis_containing_the_separator_is_not_truncated(self):
        note = P.encode("configs/a.yaml", "mae", "improve",
                        "a | b | c", vs="e0")
        self.assertEqual(P.decode(note)["hypothesis"], "a | b | c")


class Refusals(unittest.TestCase):
    def test_a_bar_outside_the_contract_is_refused(self):
        for bad in ("tail", "p90", "MAE", ""):
            with self.assertRaises(P.PreregError):
                P.encode("configs/a.yaml", bad, "improve", "why", vs="e0")

    def test_a_direction_outside_the_two_is_refused(self):
        with self.assertRaises(P.PreregError):
            P.encode("configs/a.yaml", "mae", "maybe", "why", vs="e0")

    def test_an_empty_hypothesis_is_refused(self):
        # The bar records what was measured; only the hypothesis records what was
        # believed, and that is the part that cannot be reconstructed later.
        for blank in ("", "   ", "\n"):
            with self.assertRaises(P.PreregError):
                P.encode("configs/a.yaml", "mae", "improve", blank, vs="e0")


class LegacyNotes(unittest.TestCase):
    """Every row scored before this existed must read as history, not violation."""

    def test_a_hand_typed_note_decodes_as_unregistered(self):
        got = P.decode("cfg=configs/wait_time.yaml | hazard on qctx_d features")
        self.assertFalse(got["registered"])
        self.assertEqual(got["config"], "configs/wait_time.yaml")
        self.assertEqual(got["hypothesis"], "hazard on qctx_d features")

    def test_free_text_with_no_cfg_decodes_as_unregistered(self):
        got = P.decode("first probe, checking the mount")
        self.assertFalse(got["registered"])
        self.assertEqual(got["hypothesis"], "first probe, checking the mount")

    def test_empty_and_none(self):
        for value in ("", None):
            got = P.decode(value)
            self.assertFalse(got["registered"])
            self.assertEqual(got["hypothesis"], "")

    def test_a_bar_without_a_direction_is_not_a_prediction(self):
        got = P.decode("cfg=configs/a.yaml | bar=mae | hyp=looks good")
        self.assertFalse(got["registered"])



class ConfigDigest(unittest.TestCase):
    def test_the_digest_is_over_raw_bytes(self):
        import tempfile
        d = tempfile.mkdtemp()
        a, b = os.path.join(d, "a.yaml"), os.path.join(d, "b.yaml")
        for path in (a, b):
            with open(path, "w") as fh:
                fh.write("target: wait_time\n")
        self.assertEqual(P.config_digest(a), P.config_digest(b))
        # A COMMENT CHANGE IS A DIFFERENT FILE, deliberately. Being too sensitive
        # costs a re-run; being too loose costs a wrong CONFIRMED.
        with open(b, "w") as fh:
            fh.write("target: wait_time  # tweaked\n")
        self.assertNotEqual(P.config_digest(a), P.config_digest(b))
        self.assertEqual(len(P.config_digest(a)), P.CFGH_LEN)

    def test_the_digest_survives_the_note_roundtrip(self):
        note = P.encode("configs/a.yaml", "mae", "improve", "why",
                        cfgh="abc123def456", vs="e0")
        self.assertEqual(P.decode(note)["cfgh"], "abc123def456")

    def test_a_note_without_a_digest_still_decodes_as_registered(self):
        # Legacy rows and hand-run experiments: registered, just not confirmable.
        note = P.encode("configs/a.yaml", "mae", "improve", "why", vs="e0")
        got = P.decode(note)
        self.assertTrue(got["registered"])
        self.assertEqual(got["cfgh"], "")


class Tolerance(unittest.TestCase):
    def test_zero_is_omitted_from_the_note(self):
        self.assertNotIn("tol=", P.encode("configs/a.yaml", "p90_miss_tail",
                                          "hold", "why", vs="e0"))

    def test_a_tolerance_roundtrips(self):
        note = P.encode("configs/a.yaml", "p90_miss_tail", "hold", "why",
                        vs="e0", tol=0.005)
        self.assertIn("tol=0.005", note)
        self.assertAlmostEqual(P.decode(note)["tol"], 0.005)

    def test_a_negative_tolerance_is_refused(self):
        # It would INVERT the claim: hold with tol=-0.05 demands an improvement
        # while reading as "no change".
        with self.assertRaises(P.PreregError):
            P.encode("configs/a.yaml", "p90_miss_tail", "hold", "why",
                     vs="e0", tol=-0.05)

    def test_a_non_numeric_tolerance_is_refused(self):
        with self.assertRaises(P.PreregError):
            P.encode("configs/a.yaml", "mae", "hold", "why", vs="e0",
                     tol="loose")

    def test_a_corrupted_tolerance_decodes_strict_not_permissive(self):
        got = P.decode("cfg=a | bar=mae | dir=hold | tol=wide | hyp=x")
        self.assertEqual(got["tol"], 0.0)

    def test_the_structured_fields_still_fit_the_budget(self):
        note = P.encode("configs/wait_hazard_qctx_d_priority_flow.yaml",
                        "p90_miss_tail", "hold", "x" * 900,
                        vs="probe-20260831T135844Z-18c7eb6ed0db-5632",
                        cfgh="abc123def456", tol=0.0042)
        self.assertLessEqual(len(note), P.NOTE_MAX)
        self.assertTrue(P.is_valid_note(note))
        got = P.decode(note)
        self.assertEqual(got["cfgh"], "abc123def456")
        self.assertAlmostEqual(got["tol"], 0.0042)
        self.assertEqual(got["vs"],
                         "probe-20260831T135844Z-18c7eb6ed0db-5632")


class ToleranceInjection(unittest.TestCase):
    """A malformed structured field must INVALIDATE, never widen."""

    def test_infinite_tolerance_is_refused_at_encode(self):
        for bad in (float("inf"), float("-inf"), float("nan")):
            with self.assertRaises(P.PreregError):
                P.encode("configs/a.yaml", "p90_miss_tail", "hold", "why",
                         vs="e0", tol=bad)

    def test_an_injected_negative_tolerance_is_not_flipped_positive(self):
        # `abs()` used to turn this into a PERMISSIVE +10.
        got = P.decode("cfg=a.yaml | bar=mae | dir=hold | vs=e0 | tol=-10 | hyp=x")
        self.assertEqual(got["tol"], 0.0)
        self.assertFalse(got["registered"])
        self.assertIn("negative", got["tol_error"])

    def test_an_injected_infinite_tolerance_invalidates_the_note(self):
        for bad in ("inf", "-inf", "nan", "Infinity", "1e400"):
            got = P.decode(f"cfg=a.yaml | bar=mae | dir=hold | vs=e0"
                           f" | tol={bad} | hyp=x")
            self.assertFalse(got["registered"], bad)
            self.assertTrue(got["tol_error"], bad)
            self.assertEqual(got["tol"], 0.0, bad)

    def test_an_unreadable_tolerance_invalidates_rather_than_defaulting(self):
        got = P.decode("cfg=a.yaml | bar=mae | dir=hold | vs=e0 | tol=wide | hyp=x")
        self.assertFalse(got["registered"])
        self.assertEqual(got["tol"], 0.0)

    def test_an_absent_tolerance_is_fine_and_strict(self):
        got = P.decode("cfg=a.yaml | bar=mae | dir=hold | vs=e0 | hyp=x")
        self.assertTrue(got["registered"])
        self.assertEqual(got["tol"], 0.0)
        self.assertEqual(got["tol_error"], "")


class ReferenceRun(unittest.TestCase):
    def test_a_prereg_without_vs_or_reference_is_refused(self):
        with self.assertRaises(P.PreregError) as caught:
            P.encode("configs/a.yaml", "mae", "improve", "why")
        self.assertIn("--vs", str(caught.exception))

    def test_reference_and_vs_are_mutually_exclusive(self):
        with self.assertRaises(P.PreregError):
            P.encode("configs/a.yaml", "mae", "improve", "why", vs="e0",
                     reference=True)

    def test_a_reference_run_roundtrips_and_is_registered(self):
        note = P.encode("configs/a.yaml", "mae", "improve",
                        "establishes the series", reference=True)
        got = P.decode(note)
        self.assertTrue(got["registered"])
        self.assertTrue(got["reference"])
        self.assertEqual(got["vs"], "")

    def test_a_normal_run_is_not_a_reference(self):
        got = P.decode(P.encode("configs/a.yaml", "mae", "improve", "why",
                                vs="e0"))
        self.assertFalse(got["reference"])


class HypothesisCannotShadowAField(unittest.TestCase):
    """The hypothesis is agent-authored free text. It must not be able to
    become a structured field.

    THE HOLE THIS CLOSES. `encode` omits `tol` when it is zero and `vs` on a
    reference run, and `decode` used to key-parse every ` | ` chunk including
    the hypothesis -- so `--note "safe | tol=5"` decoded to a tolerance of 5
    against a pre-registered 0. That is the unrefutable-claim hole the tolerance
    validation exists to close, reached through the one field the leader writes
    freely."""

    def test_an_injected_tolerance_in_the_hypothesis_is_inert(self):
        note = P.encode("configs/a.yaml", "p90_miss_tail", "hold",
                        "dropping capacity is safe | tol=5", vs="e1",
                        cfgh="a" * 12)
        got = P.decode(note)
        self.assertEqual(got["tol"], 0.0)
        self.assertTrue(got["registered"])
        self.assertEqual(got["hypothesis"], "dropping capacity is safe | tol=5")

    def test_an_injected_vs_on_a_reference_run_is_inert(self):
        note = P.encode("configs/a.yaml", "mae", "improve",
                        "first run of the series | vs=e9", reference=True)
        got = P.decode(note)
        self.assertEqual(got["vs"], "")
        self.assertTrue(got["reference"])

    def test_an_injected_digest_in_the_hypothesis_is_inert(self):
        # Would let one config masquerade as another across cohorts.
        note = P.encode("configs/a.yaml", "mae", "improve",
                        "beats it | cfgh=deadbeefdead", vs="e1",
                        cfgh="a" * 12)
        self.assertEqual(P.decode(note)["cfgh"], "a" * 12)

    def test_an_injected_bar_in_the_hypothesis_is_inert(self):
        note = P.encode("configs/a.yaml", "p90_miss_tail", "hold",
                        "safe | bar=mae | dir=improve", vs="e1")
        got = P.decode(note)
        self.assertEqual(got["bar"], "p90_miss_tail")
        self.assertEqual(got["direction"], "hold")

    def test_a_structured_key_after_hyp_never_counts(self):
        # Hand-crafted: everything after `hyp=` is hypothesis, so this note
        # names no bar and is therefore not a pre-registration at all.
        got = P.decode("cfg=a.yaml | hyp=x | bar=mae | dir=hold | vs=e1")
        self.assertFalse(got["registered"])
        self.assertEqual(got["bar"], "")

    def test_the_hypothesis_roundtrips_verbatim(self):
        for text in ("a | b | c", "why? because x=1 | y=2",
                     "cfg=other.yaml is better", "100% | done"):
            note = P.encode("configs/a.yaml", "mae", "improve", text, vs="e1")
            self.assertEqual(P.decode(note)["hypothesis"], text, text)


class StructuredFieldsCannotForge(unittest.TestCase):
    """The separator is refused in every structured value, not just the tail.

    Stopping the parse at `hyp=` closed the hypothesis route, but the fields
    BEFORE it were still `_clean`ed only -- and `decode` takes the FIRST
    occurrence of each key, so a separator in an earlier field injects a value
    that WINS over the genuine one. `configs/a | cfgh=deadbeefdead` shadowed the
    real digest, which is enough to make two different files confirm each other.
    """

    def test_a_pipe_in_the_config_path_is_refused(self):
        with self.assertRaises(P.PreregError) as caught:
            P.encode("configs/a | cfgh=deadbeefdead", "mae", "improve", "x",
                     vs="e1", cfgh="a" * 12)
        self.assertIn("pipe", str(caught.exception))

    def test_a_pipe_in_vs_is_refused(self):
        with self.assertRaises(P.PreregError):
            P.encode("configs/a.yaml", "mae", "improve", "x",
                     vs="e1 | cfgh=deadbeefdead", cfgh="a" * 12)

    def test_a_pipe_in_the_digest_is_refused(self):
        with self.assertRaises(P.PreregError):
            P.encode("configs/a.yaml", "mae", "improve", "x", vs="e1",
                     cfgh="aaa | vs=e9")

    def test_the_genuine_digest_survives_a_hypothesis_that_names_one(self):
        note = P.encode("configs/a.yaml", "mae", "improve",
                        "x | cfgh=deadbeefdead", vs="e1", cfgh="a" * 12)
        self.assertEqual(P.decode(note)["cfgh"], "a" * 12)

    def test_ordinary_paths_and_ids_are_unaffected(self):
        note = P.encode("configs/wait_time_residual_throughput_filtered.yaml",
                        "mae", "improve", "why",
                        vs="probe-20260831T130111Z-51b862ebf4de-5568",
                        cfgh="0123456789ab")
        got = P.decode(note)
        self.assertTrue(got["registered"])
        self.assertEqual(got["cfgh"], "0123456789ab")


if __name__ == "__main__":
    unittest.main()
