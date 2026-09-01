# The progress rollup, and the confirm gate.
#
# WHY THE FIXTURES ARE REAL. The three rows below are the three scored
# experiments of 2026-08-30/31 with their real measured values, real probe ids
# and real pass/fail pattern (reference misses MAE and within_2x but passes the
# tail; both qctx variants pass three bars and miss the tail). The contract is
# the real `contracts/wait_time.v1.json`, read off disk exactly as the code
# reads it. A synthetic fixture would prove the grouping runs; these prove it
# reaches the conclusions the experiment queue reached by hand -- including the
# one that matters most, that qctx is PROMISING and not CONFIRMED.
import os
import sys
import unittest

HOST = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HOST, "research-loop"))

import frontier as F                                            # noqa: E402
import prereg as P                                              # noqa: E402

CONTRACT = "f740716d32b8ddef20bd2e42ede873fd0b59486f752c8d077293ebc440997173"
BASELINE = "e51a321057ca884977edc357c3c2c254dcefb01ed700f9009f5d92b412ec9a27"
EXTRACT_A = "22bcaf4f474a0000"          # as_of 2026-08-27, the current trio
EXTRACT_B = "c179c7f5b9610000"          # as_of 2026-08-26 -- ONE DAY apart
EXTRACT_C = "cd467b4b00000000"          # as_of 2026-07-27 -- a real second cohort

CONTRACTS = {"contracts": [{"contract_hash": CONTRACT,
                            "file": "wait_time.v1.json"}],
             "dir": os.path.join(HOST, "contracts")}

EXTRACTS = {"extracts": [
    {"request_hash": EXTRACT_A, "as_of_date": "2026-08-27", "target": "wait_time"},
    {"request_hash": EXTRACT_B, "as_of_date": "2026-08-26", "target": "wait_time"},
    {"request_hash": EXTRACT_C, "as_of_date": "2026-07-27", "target": "wait_time"},
]}


CFGH_A = "aaaaaaaaaaaa"
CFGH_B = "bbbbbbbbbbbb"


def row(evaluation, probe, config, metrics, passed, note=None,
        extract=EXTRACT_A, when="2026-08-31 13:01"):
    return {"evaluation": evaluation, "probe": probe, "when": when,
            "verdict": "no-go", "extract": extract, "baseline": BASELINE,
            "contract": CONTRACT, "metrics": dict(metrics),
            "passed": dict(passed),
            "note": note if note is not None else f"cfg={config}"}


# THE SCOREBOARD'S `measured`, NOT THE UNDERLYING METRIC -- and the distinction
# is the whole reason these fixtures were wrong before. `verdict.py:48-60`:
#
#   mae            relative_improvement -> the improvement FRACTION (higher good)
#   within_2x      absolute_improvement -> improvement in POINTS   (higher good)
#   p90_coverage   band                 -> the raw coverage
#   p90_miss_tail  absolute             -> the raw miss rate       (lower good)
#
# The earlier fixtures put absolute seconds in `mae` (225.1, 171.6). That is the
# `value` field, not `measured` -- and because it happened to agree with a
# hardcoded `RANK["mae"] = "lower"`, 43 tests passed over an inverted comparison.
# These are the real numbers from the first live tick, 2026-09-01, series
# bd29b39a.
REFERENCE_M = {"mae": 0.04038, "within_2x": 0.04417,
               "p90_coverage": 0.8901, "p90_miss_tail": 0.29457}
REFERENCE_P = {"mae": False, "within_2x": False,
               "p90_coverage": True, "p90_miss_tail": True}
QCTX_M = {"mae": 0.26856, "within_2x": 0.09506,
          "p90_coverage": 0.8821, "p90_miss_tail": 0.31078}
QCTX_P = {"mae": True, "within_2x": True,
          "p90_coverage": True, "p90_miss_tail": False}
QCTX_D_M = {"mae": 0.25853, "within_2x": 0.08680,
            "p90_coverage": 0.8870, "p90_miss_tail": 0.30425}
QCTX_D_P = dict(QCTX_P)

REF_PROBE = "probe-20260830T202842Z-4a2ae967d664-5418"
CFG_REF = "configs/wait_time_residual_throughput_filtered_baseline.yaml"
CFG_QCTX = "configs/wait_time_residual_throughput_filtered_baseline_qctx.yaml"
CFG_QCTX_D = "configs/wait_qctx_d_priority_flow.yaml"


def _only(report):
    """The single config group in a report, by whatever name it was keyed under.

    The key carries the baseline and contract now (they are part of a
    confirmation's identity), so tests assert on the group rather than on the
    spelling of its name.
    """
    keys = list(report["configs"])
    assert len(keys) == 1, keys
    return keys[0]


T = sys.modules[__name__]        # the fixtures above, referenced by name below
T_SWEEP = QCTX_P


class Series(unittest.TestCase):
    def test_rows_from_different_extracts_are_different_series(self):
        rows = [row("e1", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P),
                row("e2", "p2", CFG_REF, REFERENCE_M, REFERENCE_P,
                    extract=EXTRACT_C)]
        report = F.build(rows, EXTRACTS, CONTRACTS)
        self.assertEqual(report["health"]["series_count"], 2)

    def test_the_holdout_window_comes_from_as_of_minus_holdout_days(self):
        rows = [row("e1", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        report = F.build(rows, EXTRACTS, CONTRACTS)
        # The real contract carries `holdout_days: 5`.
        self.assertEqual(report["series"][0]["holdout"],
                         ["2026-08-22", "2026-08-27"])

    def test_an_unknown_extract_leaves_the_window_unknown(self):
        rows = [row("e1", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P,
                    extract="deadbeef")]
        report = F.build(rows, EXTRACTS, CONTRACTS)
        self.assertIsNone(report["series"][0]["holdout"])

    def test_frontier_ranks_by_the_contract_not_by_recency(self):
        rows = [row("e1", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P),
                row("e2", "p-qctx", CFG_QCTX, QCTX_M, QCTX_P)]
        front = F.build(rows, EXTRACTS, CONTRACTS)["series"][0]["frontier"]
        # mae's `measured` is an IMPROVEMENT, so the BIGGER number wins. The old
        # hardcoded rank made this the reference's 0.04038 -- naming the config
        # that FAILED the bar as the series best while a passing row sat beside
        # it, which is exactly what the first live tick reported.
        self.assertEqual(front["mae"]["value"], 0.26856)
        self.assertEqual(front["mae"]["config"], CFG_QCTX)
        self.assertEqual(front["within_2x"]["value"], 0.09506)
        # The tail is an `absolute` bar, so `measured` is the raw miss rate and
        # lower really is better -- the reference holds it.
        self.assertEqual(front["p90_miss_tail"]["value"], 0.29457)
        self.assertEqual(front["p90_miss_tail"]["config"], CFG_REF)

    def test_the_ranks_come_from_the_contract(self):
        import json
        with open(os.path.join(HOST, "contracts",
                               "wait_time.v1.json")) as fh:
            contract = json.load(fh)
        self.assertEqual(F.metric_ranks(contract),
                         {"mae": "higher", "within_2x": "higher",
                          "p90_coverage": "band", "p90_miss_tail": "lower"})

    def test_an_unreadable_contract_leaves_the_series_unordered(self):
        rows = [row("e1", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P),
                row("e2", "p-qctx", CFG_QCTX, QCTX_M, QCTX_P)]
        report = F.build(rows, EXTRACTS, {"contracts": [], "dir": "/nonexistent"})
        entry = report["series"][0]
        self.assertFalse(entry["ordered"])
        # First-seen stands; no direction is invented.
        self.assertEqual(entry["frontier"]["mae"]["value"], 0.04038)
        text = F.render(report)
        self.assertIn("could not be read", text)
        self.assertIn("unordered", text)


class ConfirmGate(unittest.TestCase):
    """A win has to repeat on data it was not selected on."""

    def test_one_series_is_promising_never_confirmed(self):
        rows = [row("e2", "p-qctx", CFG_QCTX, QCTX_M, QCTX_P)]
        # Force a clean sweep so the gate, not the bars, is what is being tested.
        rows[0]["passed"] = {k: True for k in QCTX_P}
        report = F.build(rows, EXTRACTS, CONTRACTS)
        self.assertEqual(report["configs"][_only(report)]["status"], "PROMISING")
        self.assertEqual(report["configs"][_only(report)]["independent_cohorts"], 1)

    def test_two_overlapping_cohorts_are_still_one(self):
        # EXTRACT_A as_of 2026-08-27 and EXTRACT_B as_of 2026-08-26 share four of
        # five holdout days. This is exactly the re-run the experiment queue
        # proposed as an "independent" check, and it is not one.
        sweep = {k: True for k in QCTX_P}
        rows = [row("e1", "p1", CFG_QCTX, QCTX_M, sweep, extract=EXTRACT_A),
                row("e2", "p2", CFG_QCTX, QCTX_M, sweep, extract=EXTRACT_B)]
        report = F.build(rows, EXTRACTS, CONTRACTS)
        self.assertEqual(report["configs"][_only(report)]["independent_cohorts"], 1)
        self.assertEqual(report["configs"][_only(report)]["status"], "PROMISING")

    def test_two_non_overlapping_cohorts_confirm(self):
        # Carries a digest, because identity is (path, contents) -- see
        # `ConfigIdentity`. Without one this is PROMISING however many cohorts
        # agree, which is the case `test_a_row_with_no_digest_can_never_confirm`
        # covers.
        sweep = {k: True for k in QCTX_P}
        note = P.encode(CFG_QCTX, "mae", "improve", "clears every bar",
                        reference=True, cfgh=CFGH_A)
        rows = [row("e1", "p1", CFG_QCTX, QCTX_M, sweep, extract=EXTRACT_A,
                    note=note),
                row("e2", "p2", CFG_QCTX, QCTX_M, sweep, extract=EXTRACT_C,
                    note=note)]
        report = F.build(rows, EXTRACTS, CONTRACTS)
        info = report["configs"][_only(report)]
        self.assertEqual(info["independent_cohorts"], 2)
        self.assertEqual(info["status"], "CONFIRMED")

    def test_an_unknown_window_cannot_confirm(self):
        # Digest supplied, so this isolates the WINDOW being unknown.
        sweep = {k: True for k in QCTX_P}
        note = P.encode(CFG_QCTX, "mae", "improve", "x", reference=True,
                        cfgh=CFGH_A)
        rows = [row("e1", "p1", CFG_QCTX, QCTX_M, sweep, extract=EXTRACT_A,
                    note=note),
                row("e2", "p2", CFG_QCTX, QCTX_M, sweep, extract="nosuch",
                    note=note)]
        report = F.build(rows, EXTRACTS, CONTRACTS)
        info = report["configs"][_only(report)]
        self.assertEqual(info["independent_cohorts"], 1)
        self.assertEqual(info["status"], "PROMISING")

    def test_a_config_that_misses_a_bar_never_appears(self):
        rows = [row("e2", "p-qctx", CFG_QCTX, QCTX_M, QCTX_P)]   # tail fails
        report = F.build(rows, EXTRACTS, CONTRACTS)
        self.assertEqual(report["configs"], {})

    def test_a_row_with_no_metrics_is_not_a_clean_sweep(self):
        # `all()` over an empty dict is True; an unscored row must not read as a
        # config that cleared every bar.
        rows = [row("e9", "p9", CFG_QCTX, {}, {})]
        report = F.build(rows, EXTRACTS, CONTRACTS)
        self.assertEqual(report["configs"], {})


class Claims(unittest.TestCase):
    def test_improve_is_judged_against_vs_not_against_the_bar(self):
        # The reference improved MAE 4% against the baseline and still MISSES the
        # 15% bar. Judged against the bar this would read as a broken claim;
        # judged against `vs` -- the discipline the queue runs on -- it is kept.
        # `worse` means a SMALLER improvement, mae's `measured` being a delta.
        worse = dict(REFERENCE_M, mae=0.01)
        rows = [row("e0", "p0", CFG_REF, worse, REFERENCE_P),
                row("e1", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P,
                    note=P.encode(CFG_REF, "mae", "improve",
                                  "promoted reference beats the older run",
                                  vs="e0"))]
        report = F.build(rows, EXTRACTS, CONTRACTS)
        claims = {r["evaluation"]: r["claim"]
                  for r in report["series"][0]["rows"]}
        self.assertEqual(claims["e1"], "kept")

    def test_hold_is_judgeable_on_a_bar_that_is_already_failing(self):
        # qctx_d claims "dropping capacity does not cost the tail". The tail bar
        # FAILS in both rows, so a pass/fail reading would call this broken
        # forever. Status-equality is what makes the claim answerable.
        rows = [row("e2", "p-qctx", CFG_QCTX, QCTX_M, QCTX_P),
                row("e3", "p-qctx-d", CFG_QCTX_D, QCTX_D_M, QCTX_D_P,
                    note=P.encode(CFG_QCTX_D, "p90_miss_tail", "hold",
                                  "dropping capacity should not cost the tail",
                                  vs="e2"))]
        report = F.build(rows, EXTRACTS, CONTRACTS)
        claims = {r["evaluation"]: r["claim"]
                  for r in report["series"][0]["rows"]}
        self.assertEqual(claims["e3"], "kept")

    def test_hold_breaks_when_the_bar_flips_from_pass_to_fail(self):
        rows = [row("e1", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P),
                row("e2", "p-qctx", CFG_QCTX, QCTX_M, QCTX_P,
                    note=P.encode(CFG_QCTX, "p90_miss_tail", "hold",
                                  "qctx will not cost the tail", vs="e1"))]
        report = F.build(rows, EXTRACTS, CONTRACTS)
        claims = {r["evaluation"]: r["claim"]
                  for r in report["series"][0]["rows"]}
        # The reference PASSES the tail and qctx does not: this is the real
        # 2026-08-31 result, and the claim is broken.
        self.assertEqual(claims["e2"], "broken")

    def test_a_vs_in_another_series_is_refused(self):
        rows = [row("e1", "p1", CFG_REF, REFERENCE_M, REFERENCE_P,
                    extract=EXTRACT_C),
                row("e2", "p2", CFG_QCTX, QCTX_M, QCTX_P, extract=EXTRACT_A,
                    note=P.encode(CFG_QCTX, "mae", "improve",
                                  "beats the reference", vs="e1"))]
        report = F.build(rows, EXTRACTS, CONTRACTS)
        claims = {r["evaluation"]: r["claim"]
                  for entry in report["series"] for r in entry["rows"]}
        self.assertEqual(claims["e2"], "unjudgeable: vs is another series")

    def test_a_missing_vs_is_unjudgeable_not_kept(self):
        # `encode` now REFUSES to write this, but the frontier still has to read
        # it: a note can reach here hand-typed, or written before --vs was
        # required. Built as a raw string for exactly that reason.
        rows = [row("e2", "p2", CFG_QCTX, QCTX_M, QCTX_P,
                    note=f"cfg={CFG_QCTX} | bar=mae | dir=improve"
                         f" | hyp=it will win")]
        report = F.build(rows, EXTRACTS, CONTRACTS)
        self.assertEqual(report["series"][0]["rows"][0]["claim"],
                         "unjudgeable: no vs")

    def test_vs_may_name_the_probe_rather_than_the_evaluation(self):
        rows = [row("e1", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P),
                row("e2", "p-qctx", CFG_QCTX, QCTX_M, QCTX_P,
                    note=P.encode(CFG_QCTX, "mae", "improve",
                                  "qctx beats the reference", vs=REF_PROBE))]
        report = F.build(rows, EXTRACTS, CONTRACTS)
        claims = {r["evaluation"]: r["claim"]
                  for r in report["series"][0]["rows"]}
        self.assertEqual(claims["e2"], "kept")

    def test_vs_may_point_forward_in_the_list(self):
        # `results.sh` is oldest-first, but nothing guarantees a reference is
        # earlier in the list than the row citing it.
        rows = [row("e2", "p-qctx", CFG_QCTX, QCTX_M, QCTX_P,
                    note=P.encode(CFG_QCTX, "mae", "improve", "beats it",
                                  vs="e1")),
                row("e1", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        report = F.build(rows, EXTRACTS, CONTRACTS)
        claims = {r["evaluation"]: r["claim"]
                  for r in report["series"][0]["rows"]}
        self.assertEqual(claims["e2"], "kept")

    def test_legacy_rows_are_unregistered_and_not_counted_as_broken(self):
        rows = [row("e1", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P),
                row("e2", "p-qctx", CFG_QCTX, QCTX_M, QCTX_P)]
        report = F.build(rows, EXTRACTS, CONTRACTS)
        self.assertEqual(report["health"]["pre_registered"], 0)
        self.assertEqual(report["health"]["claims_broken"], 0)
        self.assertEqual(report["health"]["claims_kept"], 0)


class Rendering(unittest.TestCase):
    def test_the_report_renders_and_names_the_gate(self):
        sweep = {k: True for k in QCTX_P}
        rows = [row("e1", "p1", CFG_QCTX, QCTX_M, sweep)]
        text = F.render(F.build(rows, EXTRACTS, CONTRACTS))
        self.assertIn("PROMISING", text)
        self.assertIn("non-overlapping", text)

    def test_it_warns_when_claims_cannot_come_out_false(self):
        rows = [row("e2", "p2", CFG_QCTX, QCTX_M, QCTX_P,
                    note="cfg=%s | bar=mae | dir=improve | hyp=no ref" % CFG_QCTX)]
        text = F.render(F.build(rows, EXTRACTS, CONTRACTS))
        self.assertIn("unjudgeable", text)
        self.assertIn("WARNING", text)

    def test_an_empty_history_renders(self):
        text = F.render(F.build([], EXTRACTS, CONTRACTS))
        self.assertIn("None yet.", text)



class ConfigIdentity(unittest.TestCase):
    """A confirmation must be about a FILE, not about a path the agent owns."""

    def test_the_same_path_with_different_contents_does_not_confirm(self):
        # The agent may edit `configs/x.yaml` between two cohorts. The second
        # cohort exists to CHECK the first, so one label over two different files
        # is the exact shape of a false confirmation.
        sweep = {k: True for k in T_SWEEP}
        rows = [row("e1", "p1", T.CFG_QCTX, QCTX_M, sweep, extract=EXTRACT_A,
                    note=P.encode(T.CFG_QCTX, "mae", "improve", "one",
                                  reference=True, cfgh=CFGH_A)),
                row("e2", "p2", T.CFG_QCTX, QCTX_M, sweep, extract=EXTRACT_C,
                    note=P.encode(T.CFG_QCTX, "mae", "improve", "two",
                                  reference=True, cfgh=CFGH_B))]
        report = F.build(rows, EXTRACTS, CONTRACTS)
        statuses = {k: v["status"] for k, v in report["configs"].items()}
        self.assertEqual(sorted(statuses.values()), ["PROMISING", "PROMISING"])
        self.assertEqual(len(statuses), 2, statuses)

    def test_the_same_digest_across_two_cohorts_confirms(self):
        sweep = {k: True for k in T_SWEEP}
        rows = [row("e1", "p1", T.CFG_QCTX, QCTX_M, sweep, extract=EXTRACT_A,
                    note=P.encode(T.CFG_QCTX, "mae", "improve", "one",
                                  reference=True, cfgh=CFGH_A)),
                row("e2", "p2", T.CFG_QCTX, QCTX_M, sweep, extract=EXTRACT_C,
                    note=P.encode(T.CFG_QCTX, "mae", "improve", "two",
                                  reference=True, cfgh=CFGH_A))]
        report = F.build(rows, EXTRACTS, CONTRACTS)
        info = report["configs"][_only(report)]
        self.assertEqual(info["status"], "CONFIRMED")

    def test_a_row_with_no_digest_can_never_confirm(self):
        # Legacy rows: "probably the same config" is not what CONFIRMED asserts.
        sweep = {k: True for k in T_SWEEP}
        rows = [row("e1", "p1", T.CFG_QCTX, QCTX_M, sweep, extract=EXTRACT_A),
                row("e2", "p2", T.CFG_QCTX, QCTX_M, sweep, extract=EXTRACT_C)]
        report = F.build(rows, EXTRACTS, CONTRACTS)
        info = report["configs"][_only(report)]
        self.assertEqual(info["independent_cohorts"], 2)
        self.assertEqual(info["status"], "PROMISING")
        self.assertIn("digest", info["blocked_by"])


class HoldSemantics(unittest.TestCase):
    """`hold` means "did not get worse", numerically."""

    def _pair(self, ref_tail, mine_tail, tol=0.0):
        ref_p = dict(QCTX_P, p90_miss_tail=ref_tail < 0.30)
        mine_p = dict(QCTX_P, p90_miss_tail=mine_tail < 0.30)
        rows = [row("e1", "p1", T.CFG_QCTX,
                    dict(QCTX_M, p90_miss_tail=ref_tail), ref_p),
                row("e2", "p2", T.CFG_QCTX_D,
                    dict(QCTX_D_M, p90_miss_tail=mine_tail), mine_p,
                    note=P.encode(T.CFG_QCTX_D, "p90_miss_tail", "hold",
                                  "should not cost the tail", vs="e1",
                                  tol=tol))]
        report = F.build(rows, EXTRACTS, CONTRACTS)
        return {r["evaluation"]: r["claim"]
                for r in report["series"][0]["rows"]}["e2"]

    def test_a_catastrophic_regression_between_two_failures_is_broken(self):
        # THE BUG THIS REPLACES: both 0.304 and 0.900 miss the 0.30 bar, so
        # status-equality called this "kept".
        self.assertEqual(self._pair(0.3042, 0.9000), "broken")

    def test_a_small_improvement_between_two_failures_is_kept(self):
        self.assertEqual(self._pair(0.3108, 0.3042), "kept")

    def test_an_improvement_from_fail_to_pass_is_kept_not_broken(self):
        # Status-equality called this "broken", which is absurd for a claim that
        # only asserted the bar would not get worse.
        self.assertEqual(self._pair(0.3108, 0.2900), "kept")

    def test_a_regression_from_pass_to_fail_is_broken(self):
        self.assertEqual(self._pair(0.2946, 0.3108), "broken")

    def test_a_pre_registered_tolerance_admits_exactly_that_much(self):
        self.assertEqual(self._pair(0.3000, 0.3040, tol=0.005), "kept")
        self.assertEqual(self._pair(0.3000, 0.3060, tol=0.005), "broken")

    def test_the_default_tolerance_is_strict(self):
        self.assertEqual(self._pair(0.3000, 0.3001), "broken")

    def test_hold_on_a_band_metric_only_breaks_on_leaving_the_band(self):
        rows = [row("e1", "p1", T.CFG_QCTX, QCTX_M,
                    dict(QCTX_P, p90_coverage=True)),
                row("e2", "p2", T.CFG_QCTX_D, QCTX_D_M,
                    dict(QCTX_P, p90_coverage=False),
                    note=P.encode(T.CFG_QCTX_D, "p90_coverage", "hold",
                                  "stays calibrated", vs="e1"))]
        claims = {r["evaluation"]: r["claim"]
                  for r in F.build(rows, EXTRACTS,
                                   CONTRACTS)["series"][0]["rows"]}
        self.assertEqual(claims["e2"], "broken")


class Recorded(unittest.TestCase):
    """A result the loop already wrote up must stop matching action 1."""

    def test_a_run_cited_by_a_journal_entry_is_recorded(self):
        rows = [row("evaluate-20260831T130111Z-51b862ebf4de-5568",
                    "probe-20260831T130111Z-51b862ebf4de-5568",
                    T.CFG_QCTX, QCTX_M, QCTX_P)]
        journaled = {"probe-20260831T130111Z-51b862ebf4de-5568"}
        report = F.build(rows, EXTRACTS, CONTRACTS, journaled=journaled)
        self.assertTrue(report["series"][0]["rows"][0]["recorded"])
        self.assertEqual(report["health"]["unrecorded_runs"], 0)

    def test_an_uncited_run_stays_unrecorded(self):
        rows = [row("e1", "p1", T.CFG_QCTX, QCTX_M, QCTX_P)]
        report = F.build(rows, EXTRACTS, CONTRACTS, journaled=set())
        self.assertFalse(report["series"][0]["rows"][0]["recorded"])
        self.assertEqual(report["health"]["unrecorded_runs"], 1)
        self.assertIn("needs writing up", F.render(report))

    def _journal(self, files, track=True):
        """A journal directory inside a real git repo.

        A REAL REPO, because only tracked files count now -- `git add` is enough
        (no commit needed), which is why this works even where commits are
        refused.
        """
        import subprocess
        import tempfile
        root = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", "-b", "main", root],
                       capture_output=True)
        d = os.path.join(root, "journal")
        os.makedirs(os.path.join(d, "escalations"))
        for name, body in files.items():
            path = os.path.join(d, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(body)
        if track:
            subprocess.run(["git", "-C", root, "add", "-A"],
                           capture_output=True)
        return d

    def test_run_ids_are_scraped_out_of_prose_and_tables(self):
        d = self._journal({"20260831T140000Z.md":
                           "The run `probe-20260831T130111Z-51b862ebf4de-5568`"
                           " shows\n"
                           "| evaluate-20260830T202842Z-4a2ae967d664-5418 |"
                           " x |\n"})
        found = F.journaled_run_ids(d)
        self.assertIn("probe-20260831T130111Z-51b862ebf4de-5568", found)
        self.assertIn("evaluate-20260830T202842Z-4a2ae967d664-5418", found)

    def test_an_untracked_file_cannot_retire_a_result(self):
        # The leader shares the uid that owns this directory. Dropping a file
        # here must not mark a run "written up" -- nothing verified it, nothing
        # committed it, and retiring a result is what an unverified file must not
        # be able to do.
        d = self._journal({"fake.md":
                           "probe-20260831T130111Z-51b862ebf4de-5568 is fine"},
                          track=False)
        self.assertEqual(F.journaled_run_ids(d), set())

    def test_outside_a_repository_nothing_counts(self):
        import tempfile
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "a.md"), "w") as fh:
            fh.write("probe-20260831T130111Z-51b862ebf4de-5568\n")
        # Noisy and safe: every run reads as unrecorded rather than as retired.
        self.assertEqual(F.journaled_run_ids(d), set())

    def test_escalations_do_not_count_as_recorded(self):
        # An escalated entry was REJECTED, so the run it describes is still
        # unwritten. Counting it would let a failed verification silently retire
        # the result it was about. Tracked on purpose: even committed, it must
        # not count.
        d = self._journal({"escalations/20260831T140000Z.md":
                           "probe-20260831T130111Z-51b862ebf4de-5568 was great"})
        self.assertEqual(F.journaled_run_ids(d), set())

    def test_pending_does_not_count_as_recorded(self):
        d = self._journal({"PENDING.md":
                           "probe-20260831T130111Z-51b862ebf4de-5568"})
        self.assertEqual(F.journaled_run_ids(d), set())

    def test_a_missing_journal_directory_is_not_an_error(self):
        self.assertEqual(F.journaled_run_ids("/nonexistent/journal"), set())


class ConfirmationInputs(unittest.TestCase):
    """A confirmation may vary the COHORT and nothing else."""

    def _cleared(self, contract_b=CONTRACT, baseline_b=BASELINE):
        sweep = {k: True for k in QCTX_P}
        note = P.encode(CFG_QCTX, "mae", "improve", "x", reference=True,
                        cfgh=CFGH_A)
        a = row("e1", "p1", CFG_QCTX, QCTX_M, sweep, extract=EXTRACT_A,
                note=note)
        b = row("e2", "p2", CFG_QCTX, QCTX_M, sweep, extract=EXTRACT_C,
                note=note)
        b["contract"] = contract_b
        b["baseline"] = baseline_b
        return F.build([a, b], EXTRACTS, CONTRACTS)

    def test_the_same_inputs_on_two_cohorts_confirm(self):
        report = self._cleared()
        self.assertEqual(len(report["configs"]), 1)
        self.assertEqual(report["configs"][_only(report)]["status"],
                         "CONFIRMED")

    def test_a_different_contract_does_not_confirm(self):
        # A different contract is a different QUESTION, so clearing both is not
        # one result repeated -- it is two results.
        report = self._cleared(contract_b="d" * 64)
        self.assertEqual(len(report["configs"]), 2)
        for info in report["configs"].values():
            self.assertEqual(info["status"], "PROMISING")
            # <= 1, not == 1: the unknown contract has no readable
            # `holdout_days`, so that group's window is unknown and contributes
            # nothing. Either way it is not a confirmation.
            self.assertLessEqual(info["independent_cohorts"], 1)

    def test_a_different_baseline_does_not_confirm(self):
        # A different baseline is a different thing to have beaten.
        report = self._cleared(baseline_b="d" * 64)
        self.assertEqual(len(report["configs"]), 2)
        for info in report["configs"].values():
            self.assertEqual(info["status"], "PROMISING")

    def test_two_groups_differing_only_in_contract_do_not_collide(self):
        report = self._cleared(contract_b="d" * 64)
        self.assertEqual(len(set(report["configs"])), 2)


class MalformedPrereg(unittest.TestCase):
    def test_an_injected_tolerance_is_not_counted_as_pre_registered(self):
        rows = [row("e1", "p1", CFG_QCTX, QCTX_M, QCTX_P),
                row("e2", "p2", CFG_QCTX_D, QCTX_D_M, QCTX_D_P,
                    note=f"cfg={CFG_QCTX_D} | bar=p90_miss_tail | dir=hold"
                         f" | vs=e1 | tol=inf | hyp=cannot lose")]
        report = F.build(rows, EXTRACTS, CONTRACTS)
        self.assertEqual(report["health"]["pre_registered"], 0)
        self.assertEqual(report["health"]["malformed_preregs"], 1)
        claims = {r["evaluation"]: r["claim"]
                  for r in report["series"][0]["rows"]}
        self.assertEqual(claims["e2"], "unregistered")
        self.assertIn("malformed", F.render(report))


class ReferenceRows(unittest.TestCase):
    def test_a_reference_row_is_neither_kept_nor_unjudgeable(self):
        rows = [row("e1", "p1", CFG_REF, REFERENCE_M, REFERENCE_P,
                    note=P.encode(CFG_REF, "mae", "improve",
                                  "establishes the series", reference=True))]
        report = F.build(rows, EXTRACTS, CONTRACTS)
        self.assertEqual(report["series"][0]["rows"][0]["claim"], "reference")
        self.assertEqual(report["health"]["reference_runs"], 1)
        self.assertEqual(report["health"]["claims_kept"], 0)
        self.assertEqual(report["health"]["claims_unjudgeable"], 0)


class BandHold(unittest.TestCase):
    """`hold` on a band metric must still detect getting worse."""

    def _pair(self, ref_cov, mine_cov, tol=0.0):
        # The real contract's band is 0.85-0.95.
        ref_p = dict(QCTX_P, p90_coverage=0.85 <= ref_cov <= 0.95)
        mine_p = dict(QCTX_P, p90_coverage=0.85 <= mine_cov <= 0.95)
        rows = [row("e1", "p1", CFG_QCTX,
                    dict(QCTX_M, p90_coverage=ref_cov), ref_p),
                row("e2", "p2", CFG_QCTX_D,
                    dict(QCTX_D_M, p90_coverage=mine_cov), mine_p,
                    note=P.encode(CFG_QCTX_D, "p90_coverage", "hold",
                                  "stays calibrated", vs="e1", tol=tol))]
        report = F.build(rows, EXTRACTS, CONTRACTS)
        return {r["evaluation"]: r["claim"]
                for r in report["series"][0]["rows"]}["e2"]

    def test_a_collapse_between_two_failures_is_broken(self):
        # THE BUG: both 0.84 and 0.01 are outside the 0.85-0.95 band, so a
        # pass/fail reading called this "kept".
        self.assertEqual(self._pair(0.84, 0.01), "broken")

    def test_moving_closer_to_the_band_while_still_outside_is_kept(self):
        self.assertEqual(self._pair(0.70, 0.84), "kept")

    def test_leaving_the_band_is_broken(self):
        self.assertEqual(self._pair(0.89, 0.70), "broken")

    def test_staying_inside_the_band_is_kept(self):
        self.assertEqual(self._pair(0.89, 0.86), "kept")

    def test_entering_the_band_is_kept(self):
        self.assertEqual(self._pair(0.70, 0.89), "kept")

    def test_a_tolerance_applies_outside_the_band_too(self):
        self.assertEqual(self._pair(0.84, 0.835, tol=0.01), "kept")
        self.assertEqual(self._pair(0.84, 0.820, tol=0.01), "broken")

    def test_without_readable_bounds_two_failures_are_unjudgeable(self):
        # No contract body -> no band edges -> no ordering. Unjudgeable, never
        # automatically kept.
        rows = [row("e1", "p1", CFG_QCTX, dict(QCTX_M, p90_coverage=0.84),
                    dict(QCTX_P, p90_coverage=False)),
                row("e2", "p2", CFG_QCTX_D, dict(QCTX_D_M, p90_coverage=0.01),
                    dict(QCTX_P, p90_coverage=False),
                    note=P.encode(CFG_QCTX_D, "p90_coverage", "hold", "x",
                                  vs="e1"))]
        report = F.build(rows, EXTRACTS, {"contracts": [], "dir": "/nonexistent"})
        claims = {r["evaluation"]: r["claim"]
                  for r in report["series"][0]["rows"]}
        self.assertIn("unjudgeable", claims["e2"])


class VsResolution(unittest.TestCase):
    def test_a_probe_evaluated_under_two_contracts_resolves_in_its_own_series(self):
        # The same probe scored under contracts A and B. A single-valued index
        # let the later row win, so a same-series claim citing that probe was
        # refused as cross-series.
        shared_probe = "probe-20260830T202842Z-4a2ae967d664-5418"
        a = row("eA", shared_probe, CFG_REF, REFERENCE_M, REFERENCE_P)
        b = row("eB", shared_probe, CFG_REF, REFERENCE_M, REFERENCE_P)
        b["contract"] = "d" * 64
        mine = row("eC", "p-qctx", CFG_QCTX, QCTX_M, QCTX_P,
                   note=P.encode(CFG_QCTX, "mae", "improve",
                                 "beats the reference", vs=shared_probe))
        report = F.build([a, b, mine], EXTRACTS, CONTRACTS)
        claims = {r["evaluation"]: r["claim"]
                  for entry in report["series"] for r in entry["rows"]}
        self.assertEqual(claims["eC"], "kept")

    def test_a_genuinely_cross_series_vs_is_still_refused(self):
        a = row("eA", "pA", CFG_REF, REFERENCE_M, REFERENCE_P,
                extract=EXTRACT_C)
        mine = row("eC", "pC", CFG_QCTX, QCTX_M, QCTX_P, extract=EXTRACT_A,
                   note=P.encode(CFG_QCTX, "mae", "improve", "beats it",
                                 vs="eA"))
        report = F.build([a, mine], EXTRACTS, CONTRACTS)
        claims = {r["evaluation"]: r["claim"]
                  for entry in report["series"] for r in entry["rows"]}
        self.assertEqual(claims["eC"], "unjudgeable: vs is another series")


class MaeIsAnImprovementNotAQuantity(unittest.TestCase):
    """Regression for the defect the first live tick found.

    `frontier.py` hardcoded `RANK["mae"] = "lower"`, reading the contract's
    `direction: lower_is_better` as a statement about the SCOREBOARD value. It is
    a statement about MAE the quantity; the scoreboard stores
    `baseline - value / baseline`, an improvement, so higher is better. Every mae
    comparison in the frontier was inverted, and -- because pre-registrations live
    in an immutable note -- any `--bar mae` claim written before the fix would
    read backwards permanently.
    """

    def _claim(self, ref_mae, mine_mae, direction, tol=0.0):
        rows = [row("e1", "p1", CFG_REF, dict(REFERENCE_M, mae=ref_mae),
                    dict(REFERENCE_P, mae=ref_mae >= 0.15)),
                row("e2", "p2", CFG_QCTX, dict(QCTX_M, mae=mine_mae),
                    dict(QCTX_P, mae=mine_mae >= 0.15),
                    note=P.encode(CFG_QCTX, "mae", direction, "qctx cuts error",
                                  vs="e1", tol=tol))]
        report = F.build(rows, EXTRACTS, CONTRACTS)
        return {r["evaluation"]: r["claim"]
                for r in report["series"][0]["rows"]}["e2"]

    def test_a_real_improvement_is_kept(self):
        # 4.0% -> 26.9%, the actual qctx result. Reported `broken` before.
        self.assertEqual(self._claim(0.04038, 0.26856, "improve"), "kept")

    def test_a_regression_is_broken(self):
        self.assertEqual(self._claim(0.26856, 0.04038, "improve"), "broken")

    def test_hold_is_not_satisfied_by_a_collapse(self):
        # A 22.8-point MAE regression pre-registered as `hold` reported `kept`
        # before, which is the same inversion seen from the other side.
        self.assertEqual(self._claim(0.26856, 0.04038, "hold"), "broken")

    def test_hold_accepts_an_equal_or_better_improvement(self):
        self.assertEqual(self._claim(0.25853, 0.26856, "hold"), "kept")
        self.assertEqual(self._claim(0.25853, 0.25853, "hold"), "kept")

    def test_hold_tolerance_applies_in_the_improvement_direction(self):
        # `tol` is how much WORSE it may get: a smaller improvement.
        self.assertEqual(self._claim(0.26856, 0.26000, "hold", tol=0.01), "kept")
        self.assertEqual(self._claim(0.26856, 0.25000, "hold", tol=0.01),
                         "broken")

    def test_within_2x_is_also_an_improvement_and_ranks_higher(self):
        rows = [row("e1", "p1", CFG_REF, REFERENCE_M, REFERENCE_P),
                row("e2", "p2", CFG_QCTX, QCTX_M, QCTX_P)]
        front = F.build(rows, EXTRACTS, CONTRACTS)["series"][0]["frontier"]
        self.assertEqual(front["within_2x"]["value"], 0.09506)
        self.assertEqual(front["within_2x"]["config"], CFG_QCTX)


class EvidenceForStructuralClaims(unittest.TestCase):
    """The copilot must be able to check claims about probes, not just metrics.

    Action-1 write-ups are mostly about the probe/evaluation relationship -- how
    many evaluations re-score one probe, which probes carry no scoreboard, how
    many distinct models the rows collapse to. The first live entry was escalated
    over such a count (claimed six re-evaluations, actual three), and the copilot
    could not have checked it: the JSON carried no probe ids.
    """

    def test_the_probe_id_reaches_the_report(self):
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        entry = F.build(rows, EXTRACTS, CONTRACTS)["series"][0]
        self.assertEqual(entry["rows"][0]["probe"], REF_PROBE)

    def test_two_evaluations_of_one_probe_are_both_visible(self):
        # This is the shape the escalated count was about: the same probe scored
        # twice, once without a scoreboard.
        rows = [row("eA", REF_PROBE, CFG_REF, {}, {}),
                row("eB", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        entry = F.build(rows, EXTRACTS, CONTRACTS)["series"][0]
        probes = [r["probe"] for r in entry["rows"]]
        self.assertEqual(probes, [REF_PROBE, REF_PROBE])
        unscored = [r for r in entry["rows"] if not r["metrics"]]
        self.assertEqual(len(unscored), 1)
        self.assertEqual(unscored[0]["evaluation"], "eA")


if __name__ == "__main__":
    unittest.main()
