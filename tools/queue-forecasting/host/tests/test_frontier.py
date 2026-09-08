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
from unittest.mock import patch

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


def _make_journal(files, commit=True, then_edit=None):
    """A journal directory inside a real git repo, committed by plumbing.

    `git commit` IS REFUSED IN THE DEV CONTAINER, so the fixture uses
    write-tree/commit-tree/update-ref. `commit=False` leaves the files
    staged but unreachable from HEAD; `then_edit` rewrites a file in the
    WORKING TREE after the commit, which is the tamper the reader must
    ignore.

    Module-level so both `Recorded` and `Retirement` share one fixture rather
    than keeping two copies in step by hand.
    """
    import subprocess
    import tempfile
    root = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q", "-b", "main", root],
                   capture_output=True)
    d = os.path.join(root, "journal")
    os.makedirs(os.path.join(d, "escalations"), exist_ok=True)
    os.makedirs(os.path.join(d, "waivers"), exist_ok=True)
    for name, body in files.items():
        path = os.path.join(d, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(body)
    subprocess.run(["git", "-C", root, "add", "-A"], capture_output=True)
    if commit:
        def git(*args):
            # IDENTITY SUPPLIED, NOT INHERITED. `commit-tree` needs an author,
            # and left to itself it takes the ambient global git config -- so
            # this fixture passed on a developer box and failed everywhere
            # else with `Author identity unknown ... unable to auto-detect
            # email address (got 'dev@<container-id>.(none)')`. It cost 25
            # green tests the moment a container came back with a new hostname
            # and no global config. `test_tick.sh`'s commit probe already does
            # it this way; a hermetic fixture depends on nothing it did not set.
            done = subprocess.run(
                ["git", "-C", root,
                 "-c", "user.name=qf-fixture",
                 "-c", "user.email=qf-fixture@queue-forecasting.invalid",
                 *args],
                capture_output=True)
            assert done.returncode == 0, done.stderr
            return done.stdout.decode().strip()
        tree = git("write-tree")
        head = git("commit-tree", tree, "-m", "fixture")
        git("update-ref", "refs/heads/main", head)
    for name, body in (then_edit or {}).items():
        with open(os.path.join(d, name), "w") as fh:
            fh.write(body)
    return d


class Recorded(unittest.TestCase):
    """A result the loop already wrote up must stop matching action 1."""

    _journal = staticmethod(_make_journal)

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
                          commit=False)
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

    def test_a_run_id_in_escalation_kinds_md_does_not_count_as_recorded(self):
        # THE SELF-DEFEAT `waivers/` WAS GIVEN ITS OWN DIRECTORY TO AVOID,
        # reproduced for `escalation-kinds.md`: it lives in the journal root,
        # its format is `<stamp> <kind>`, but a human classifying escalations
        # by hand will very plausibly leave a trailing comment naming the run
        # for their own benefit -- and `_RUN_ID` is loose enough to scrape it
        # out of that comment. Before the fix, that made the run read as
        # RECORDED (not retired -- recorded, so the leader never picks it up
        # again) with no diagnostic anywhere. This is reachable by the file's
        # ordinary, intended use, not a crafted adversarial input.
        rid = "evaluate-20260904T090941Z-0a217f40da8f-6446"
        d = self._journal({"escalation-kinds.md":
                           f"20260904T101126Z rejection   # {rid}, tail run\n"})
        self.assertEqual(F.journaled_run_ids(d), set())

    def test_a_missing_journal_directory_is_not_an_error(self):
        self.assertEqual(F.journaled_run_ids("/nonexistent/journal"), set())

    def test_a_working_tree_edit_to_a_committed_entry_is_ignored(self):
        # THE TAMPER THIS CHANGE EXISTS TO STOP. The leader shares the uid, so
        # it can rewrite a committed entry; only HEAD counts.
        d = self._journal(
            {"20260831T140000Z.md": "wrote up eA-1\n"},
            then_edit={"20260831T140000Z.md":
                       "cites evaluate-20260904T090941Z-0a217f40da8f-6446\n"})
        self.assertEqual(F.journaled_run_ids(d), set())

    def test_a_staged_but_uncommitted_entry_does_not_count(self):
        d = self._journal({"20260831T140000Z.md":
                           "evaluate-20260904T090941Z-0a217f40da8f-6446\n"},
                          commit=False)
        self.assertEqual(F.journaled_run_ids(d), set())

    def test_a_committed_entry_in_the_journal_root_counts(self):
        rid = "evaluate-20260904T090941Z-0a217f40da8f-6446"
        d = self._journal({"20260831T140000Z.md": f"wrote up `{rid}` today\n"})
        self.assertEqual(F.journaled_run_ids(d), {rid})

    def test_blobs_resolve_from_a_journal_subdirectory(self):
        # REGRESSION FOR THE cat-file PATH TRAP: `ls-tree -C journal` prints
        # `escalations/x.md`, but `cat-file HEAD:escalations/x.md` resolves from
        # the REPO ROOT and fails. Blob oids have no prefix to get wrong.
        d = self._journal({"escalations/20260904T101126Z.md": "body\n"})
        blobs = F._committed_blobs(d, "escalations")
        self.assertEqual(blobs, {"20260904T101126Z.md": "body\n"})

    def test_no_repository_counts_nothing(self):
        import tempfile
        d = os.path.join(tempfile.mkdtemp(), "journal")
        os.makedirs(d)
        self.assertIsNone(F._committed_blobs(d))
        self.assertEqual(F.journaled_run_ids(d), set())


class RootEntryNaming(unittest.TestCase):
    """The journal root's contract -- every committed `.md` is a stamped
    entry or a name in `_NOT_ENTRIES` -- pinned by a fixture rather than by a
    check against the real journal, which lives on the host this loop runs
    on and is not shipped in this repository. `escalation-kinds.md` reached
    the root by exactly this gap: the hazard was reasoned through for
    `waivers/` and not re-applied to the next control file. This is the
    tripwire that catches a third one."""

    def test_a_third_control_file_is_flagged_as_unrecognized(self):
        names = ["20260831T140000Z.md", "PENDING.md", "escalation-kinds.md",
                 "notes.md"]
        self.assertEqual(F._unrecognized_root_names(names), ["notes.md"])

    def test_stamped_entries_and_known_control_files_are_not_flagged(self):
        names = ["20260831T140000Z.md", "20260904T101126Z.md",
                 "PENDING.md", "escalation-kinds.md"]
        self.assertEqual(F._unrecognized_root_names(names), [])


def _batch_record(oid, kind, content):
    """One `cat-file --batch` answer: `<oid> SP <type> SP <size> LF <bytes> LF`."""
    return (oid.encode() + b" " + kind + b" " + str(len(content)).encode()
            + b"\n" + content + b"\n")


class ParseBatch(unittest.TestCase):
    """`_parse_batch` in isolation -- no git, just the bytes `--batch` prints.

    Reached directly rather than through `_committed_blobs`+monkeypatching,
    because that indirection was exactly how the defect below went unnoticed: a
    parser only reachable by mocking a subprocess is not a parser anyone tests.
    """

    def test_a_well_formed_two_record_buffer_parses(self):
        buf = _batch_record("aaaa", b"blob", b"AB\n") + \
            _batch_record("bbbb", b"blob", b"CD")
        self.assertEqual(F._parse_batch(buf, ["a.md", "b.md"]),
                         {"a.md": "AB\n", "b.md": "CD"})

    def test_a_size_longer_than_the_remaining_bytes_returns_none_not_mixed_content(self):
        # THE REVIEWER'S REPRODUCTION. The header declares 4 content bytes but
        # only 3 are actually there before the next record starts with a `d` --
        # the unchecked parser spliced that byte in and returned
        # `{'a.md': 'AB\nd'}` instead of refusing. Confirmed against the old
        # (unguarded) slicing logic before this fix: it produced exactly that
        # string.
        buf = b"a1oid blob 4\nAB\nd2oid blob 1\nX\n"
        result = F._parse_batch(buf, ["a.md"])
        self.assertIsNone(result)
        self.assertNotEqual(result, {"a.md": "AB\nd"})

    def test_a_size_one_byte_short_so_the_trailing_newline_lands_on_content_returns_none(self):
        # Declared size 3 but the real content is 4 bytes ("ABCD"), so the byte
        # at the position the trailing newline must occupy is `D`, not `\n`.
        # This is the check that catches a wrong size even when the buffer has
        # PLENTY of bytes left -- the failure above is only "too few bytes",
        # this one is "framing landed in the wrong place".
        buf = b"oid blob 3\nABCD\n"
        self.assertIsNone(F._parse_batch(buf, ["a.md"]))

    def test_a_missing_object_header_with_two_fields_returns_none(self):
        # `--batch` prints `<oid> missing` (no size, no content) for an oid it
        # cannot find -- not the documented 3-field format.
        buf = b"deadbeefdeadbeef missing\n"
        self.assertIsNone(F._parse_batch(buf, ["a.md"]))

    def test_a_non_integer_size_returns_none(self):
        buf = b"oid blob notanumber\nX\n"
        self.assertIsNone(F._parse_batch(buf, ["a.md"]))

    def test_an_empty_buffer_with_names_expected_returns_none(self):
        self.assertIsNone(F._parse_batch(b"", ["a.md"]))

    def test_a_zero_length_blob_parses_as_the_empty_string(self):
        buf = _batch_record("aaaa", b"blob", b"")
        self.assertEqual(F._parse_batch(buf, ["empty.md"]), {"empty.md": ""})

    def test_a_negative_size_returns_none(self):
        # `int()` accepts a leading `-`, so this is not caught by the
        # non-integer check above; it needs its own guard.
        buf = b"oid blob -1\nX\n"
        self.assertIsNone(F._parse_batch(buf, ["a.md"]))


def _ls_entry(mode, kind, oid, path):
    """One `git ls-tree -z` record: `<mode> SP <type> SP <oid> TAB <path> NUL`.

    `path` is bytes, so a test can embed a literal tab or newline in a filename
    without Python's own string handling getting in the way.
    """
    return mode.encode() + b" " + kind + b" " + oid.encode() + b"\t" + path + b"\0"


class ParseLsTree(unittest.TestCase):
    """`_parse_ls_tree` in isolation -- no git, just the bytes `ls-tree -z` prints.

    Extracted for the same reason `_parse_batch` was: framing logic reachable
    only by mocking a subprocess is logic nothing tests, and the reviewer's
    confidence that this side handles adversarial filenames rested on
    inspection alone until now.
    """

    def test_a_well_formed_two_entry_listing_parses(self):
        stdout = (_ls_entry("100644", b"blob", "aaaa", b"a.md")
                  + _ls_entry("100644", b"blob", "bbbb", b"b.md"))
        self.assertEqual(F._parse_ls_tree(stdout, None),
                         (["aaaa", "bbbb"], ["a.md", "b.md"]))

    def test_subdir_none_excludes_an_escalations_entry(self):
        stdout = (_ls_entry("100644", b"blob", "aaaa", b"a.md")
                  + _ls_entry("100644", b"blob", "bbbb", b"escalations/b.md"))
        self.assertEqual(F._parse_ls_tree(stdout, None), (["aaaa"], ["a.md"]))

    def test_subdir_escalations_includes_only_its_own_direct_entries(self):
        stdout = (_ls_entry("100644", b"blob", "aaaa", b"x.md")
                  + _ls_entry("100644", b"blob", "bbbb", b"escalations/x.md")
                  + _ls_entry("100644", b"blob", "cccc",
                             b"escalations/deeper/x.md"))
        self.assertEqual(F._parse_ls_tree(stdout, "escalations"),
                         (["bbbb"], ["x.md"]))

    def test_a_non_md_entry_is_excluded(self):
        stdout = _ls_entry("100644", b"blob", "aaaa", b"a.txt")
        self.assertEqual(F._parse_ls_tree(stdout, None), ([], []))

    def test_a_tree_entry_is_excluded(self):
        stdout = _ls_entry("040000", b"tree", "aaaa", b"escalations")
        self.assertEqual(F._parse_ls_tree(stdout, None), ([], []))

    def test_a_record_with_no_tab_returns_none(self):
        stdout = b"100644 blob aaaa a.md\0"
        self.assertIsNone(F._parse_ls_tree(stdout, None))

    def test_a_header_without_three_fields_returns_none(self):
        stdout = b"100644 aaaa\ta.md\0"
        self.assertIsNone(F._parse_ls_tree(stdout, None))

    def test_a_literal_tab_inside_the_filename_does_not_desynchronise_the_split(self):
        # `partition(b"\t")` splits on the FIRST tab only, so a tab embedded in
        # the filename itself must stay part of the path rather than truncating
        # it or being read as a second field.
        stdout = _ls_entry("100644", b"blob", "aaaa", b"we\tird.md")
        self.assertEqual(F._parse_ls_tree(stdout, None), (["aaaa"], ["we\tird.md"]))

    def test_a_literal_newline_inside_the_filename_does_not_desynchronise_the_split(self):
        # `-z` NUL-delimits ENTRIES, so a newline byte inside the path is just
        # more path, never a record boundary.
        stdout = _ls_entry("100644", b"blob", "aaaa", b"we\nird.md")
        self.assertEqual(F._parse_ls_tree(stdout, None), (["aaaa"], ["we\nird.md"]))


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


ESC_TARGET = "evaluate-20260904T090941Z-0a217f40da8f-6446"
OTHER = "evaluate-20260903T101010Z-1111111111aa-1"


def escalation(target, kind="rejection", extra=""):
    return (f"# Retry\n\n**Target run:** {target}\n\n{extra}\n"
            "## NOT RECORDED — the copilot did not agree\n\n"
            f"Escalation kind: {kind}\n\n```\nreason\n```\n")


class Retirement(unittest.TestCase):
    """Counting rejection episodes per target -- the input to task 3's
    `recorded|unrecorded|retired` state. Each test below is one load-bearing
    decision in `escalation_targets`, including two the quality review found:
    CRLF line endings and ambiguous target lines both used to make a rejection
    vanish -- counted toward neither a run nor `unclassified` -- rather than
    fail loudly or safely."""

    def test_one_rejection_is_counted_with_its_path(self):
        d = _make_journal({"escalations/20260904T101126Z.md":
                           escalation(ESC_TARGET)})
        targets, problems = F.escalation_targets(d)
        self.assertEqual(targets[ESC_TARGET]["rejections"], 1)
        self.assertEqual(problems["unclassified"], [])
        self.assertEqual(targets[ESC_TARGET]["latest"],
                         "escalations/20260904T101126Z.md")

    def test_a_file_naming_the_run_three_times_counts_once(self):
        d = _make_journal({"escalations/20260904T101126Z.md":
                           escalation(ESC_TARGET,
                                     extra=f"{ESC_TARGET} {ESC_TARGET}")})
        targets, _ = F.escalation_targets(d)
        self.assertEqual(targets[ESC_TARGET]["rejections"], 1)

    def test_a_comparator_in_evidence_is_never_a_target(self):
        d = _make_journal({"escalations/20260904T101126Z.md":
                           escalation(ESC_TARGET, extra=f"vs {OTHER}")})
        targets, _ = F.escalation_targets(d)
        self.assertNotIn(OTHER, targets)

    def test_a_probe_id_target_counts_nothing(self):
        d = _make_journal({"escalations/20260904T101126Z.md":
                           escalation(REF_PROBE)})
        targets, _ = F.escalation_targets(d)
        self.assertEqual(targets, {})

    def test_verifier_failure_does_not_count(self):
        d = _make_journal({"escalations/20260904T101126Z.md":
                           escalation(ESC_TARGET, kind="verifier-failure")})
        targets, problems = F.escalation_targets(d)
        self.assertEqual(targets, {})
        self.assertEqual(problems["unclassified"], [])

    def test_a_missing_kind_line_is_unknown_and_names_the_file(self):
        # NAMED, NOT JUST COUNTED (quality review): an operator reading "1
        # escalation(s)" still has to go find which one; the basename is the
        # whole fix.
        body = escalation(ESC_TARGET).replace("Escalation kind: rejection\n",
                                              "")
        d = _make_journal({"escalations/20260904T101126Z.md": body})
        targets, problems = F.escalation_targets(d)
        self.assertEqual(targets, {})
        self.assertEqual(problems["unclassified"], ["20260904T101126Z.md"])

    def test_the_migration_record_classifies_a_historical_file(self):
        body = escalation(ESC_TARGET).replace("Escalation kind: rejection\n",
                                              "")
        d = _make_journal({"escalations/20260904T101126Z.md": body,
                           "escalation-kinds.md":
                               "20260904T101126Z rejection\n"})
        targets, problems = F.escalation_targets(d)
        self.assertEqual(problems["unclassified"], [])
        self.assertEqual(targets[ESC_TARGET]["rejections"], 1)

    def test_the_kind_override_still_works_even_though_journaled_run_ids_skips_the_file(self):
        # NOT FIXED BY BREAKING THE FEATURE. `journaled_run_ids` now skips
        # `escalation-kinds.md` by name so a run id in it cannot read as
        # recorded (see `Recorded`'s equivalent test) -- but the file is still
        # a real input to `escalation_targets`, which reads it through its own
        # `_committed_blobs(journal_dir)` call, untouched by that skip. Both
        # must hold at once: invisible to `journaled_run_ids`, still load-
        # bearing for `escalation_targets`.
        body = escalation(ESC_TARGET).replace("Escalation kind: rejection\n",
                                              "")
        d = _make_journal({"escalations/20260904T101126Z.md": body,
                           "escalation-kinds.md":
                               "20260904T101126Z rejection\n"})
        self.assertEqual(F.journaled_run_ids(d), set())
        targets, problems = F.escalation_targets(d)
        self.assertEqual(problems["unclassified"], [])
        self.assertEqual(targets[ESC_TARGET]["rejections"], 1)

    def test_a_waiver_starts_a_new_episode_rather_than_subtracting(self):
        d = _make_journal({
            "escalations/20260901T101126Z.md": escalation(ESC_TARGET),
            "escalations/20260902T101126Z.md": escalation(ESC_TARGET),
            "escalations/20260903T101126Z.md": escalation(ESC_TARGET),
            "waivers/20260905T000000Z.md":
                f"**Waiver:** {ESC_TARGET}\n",
        })
        targets, _ = F.escalation_targets(d)
        # Presence with rejections: 0 is as valid a shape as absence --
        # retirement compares against a threshold, not against presence.
        self.assertEqual(targets.get(ESC_TARGET, {}).get("rejections", 0), 0)

    def test_a_rejection_after_a_waiver_counts_again(self):
        d = _make_journal({
            "escalations/20260901T101126Z.md": escalation(ESC_TARGET),
            "waivers/20260905T000000Z.md":
                f"**Waiver:** {ESC_TARGET}\n",
            "escalations/20260906T101126Z.md": escalation(ESC_TARGET),
        })
        targets, _ = F.escalation_targets(d)
        self.assertEqual(targets[ESC_TARGET]["rejections"], 1)
        self.assertEqual(targets[ESC_TARGET]["latest"],
                         "escalations/20260906T101126Z.md")

    def test_a_waiver_with_the_same_stamp_as_an_escalation_waives_it(self):
        # THE BOUNDARY, PINNED ON PURPOSE. The design says an escalation counts
        # only when committed AFTER the latest waiver -- a stamp EQUAL to the
        # waiver's is not after it, so this must still read as waived. Without
        # this test, "equal excluded" is only true by accident of `<=` vs `<`
        # in the implementation.
        stamp = "20260905T000000Z"
        d = _make_journal({
            f"escalations/{stamp}.md": escalation(ESC_TARGET),
            f"waivers/{stamp}.md": f"**Waiver:** {ESC_TARGET}\n",
        })
        targets, _ = F.escalation_targets(d)
        self.assertEqual(targets.get(ESC_TARGET, {}).get("rejections", 0), 0)

    def test_a_waiver_does_not_make_the_run_recorded(self):
        d = _make_journal({"waivers/20260905T000000Z.md":
                           f"**Waiver:** {ESC_TARGET}\n"})
        self.assertEqual(F.journaled_run_ids(d), set())

    def test_an_unreadable_journal_yields_no_targets(self):
        import tempfile
        d = os.path.join(tempfile.mkdtemp(), "journal")
        os.makedirs(d)
        self.assertEqual(F.escalation_targets(d),
                         ({}, {"unclassified": [], "unparseable_waivers": [],
                               "misdated_waivers": []}))

    def test_an_override_wins_over_the_files_own_kind_line(self):
        # PRECEDENCE, PINNED. The file's own line says `verifier-failure`; the
        # migration record says `rejection`. The record must win -- it exists
        # to CORRECT a classification, not just to fill in one that is
        # missing, so a file that already has a line is not exempt from it.
        d = _make_journal({"escalations/20260904T101126Z.md":
                           escalation(ESC_TARGET, kind="verifier-failure"),
                           "escalation-kinds.md":
                               "20260904T101126Z rejection\n"})
        targets, problems = F.escalation_targets(d)
        self.assertEqual(targets[ESC_TARGET]["rejections"], 1)
        self.assertEqual(problems["unclassified"], [])

    def test_a_misnamed_waiver_revives_nothing_and_names_the_file(self):
        # THE ONE FILE TYPE WHOSE JOB IS REVIVAL gets no silent failure: a
        # waiver file whose name does not start with a stamp cannot set a
        # floor, so it must not revive `ESC_TARGET`'S count -- but unlike the
        # old behaviour, that failure is now visible in `unparseable_waivers`,
        # BY NAME (basename, not directory-prefixed -- `render()` states the
        # directory once for the whole list).
        d = _make_journal({
            "escalations/20260901T101126Z.md": escalation(ESC_TARGET),
            "escalations/20260902T101126Z.md": escalation(ESC_TARGET),
            "escalations/20260903T101126Z.md": escalation(ESC_TARGET),
            "waivers/not-a-stamp.md": f"**Waiver:** {ESC_TARGET}\n",
        })
        targets, problems = F.escalation_targets(d)
        self.assertEqual(targets[ESC_TARGET]["rejections"], 3)
        self.assertEqual(problems["unparseable_waivers"], ["not-a-stamp.md"])

    def test_a_waiver_stamped_far_ahead_of_the_journal_revives_nothing(self):
        # THE CRITICAL DEFECT, FOUND BY AN INDEPENDENT REVIEW. `_stamp_of`
        # validates a FILENAME SHAPE, not an instant -- so a typo'd year (or a
        # box with a skewed clock) put `floor[rid]` above every stamp the loop
        # would produce for a year, and `stamp <= floor` then waived EVERY
        # episode: the row stayed `unrecorded` (so still selectable AND still
        # suppressible), same-target suppression stopped the drift streak
        # advancing, every usable verdict reset the verifier-failure counter,
        # and retirement could never reach its threshold. The 2026-09-04 hourly
        # livelock with BOTH brakes off, from one mistyped filename, silently.
        #
        # A WAIVER THIS FAR AHEAD OF THE JOURNAL'S OWN NEWEST COMMITTED FILE IS
        # NOT BELIEVED AT ALL -- it waives nothing, exactly like a waiver with
        # no parseable stamp, and it is named for an operator to re-stamp.
        d = _make_journal({
            "escalations/20260901T101126Z.md": escalation(ESC_TARGET),
            "escalations/20260902T101126Z.md": escalation(ESC_TARGET),
            "waivers/20270908T000000Z.md": f"**Waiver:** {ESC_TARGET}\n",
        })
        targets, problems = F.escalation_targets(d)
        self.assertEqual(targets[ESC_TARGET]["rejections"], 2)
        self.assertEqual(problems["misdated_waivers"],
                         ["20270908T000000Z.md"])
        self.assertEqual(problems["unparseable_waivers"], [])

    def test_a_future_dated_waiver_leaves_retirement_reachable(self):
        # THE CONSEQUENCE THAT MADE IT CRITICAL, end to end through `build()`:
        # rejections committed AFTER a bad waiver must still retire the run, or
        # the loop re-narrates it hourly with nothing able to stop it.
        #
        # THIS IS ALSO THE ASSERTION A PER-ID CLAMP CANNOT PASS. Clamping
        # `floor[rid]` down to "the newest escalation committed for this id"
        # looks like it spends only episodes that exist, but the ceiling is
        # recomputed from whatever is committed AT READ TIME: each new
        # escalation raises the ceiling to its own stamp, and `<=` then waives
        # it. Count 0 forever -- worse than the un-clamped defect, which at
        # least expires when the typo'd year arrives.
        d = _make_journal({
            "escalations/20260901T101126Z.md": escalation(ESC_TARGET),
            "waivers/20270908T000000Z.md": f"**Waiver:** {ESC_TARGET}\n",
            "escalations/20260909T101126Z.md": escalation(ESC_TARGET),
            "escalations/20260910T101126Z.md": escalation(ESC_TARGET),
        })
        targets, _ = F.escalation_targets(d)
        self.assertEqual(targets[ESC_TARGET]["rejections"], 3)
        self.assertEqual(targets[ESC_TARGET]["latest"],
                         "escalations/20260910T101126Z.md")
        rows = [row(ESC_TARGET, REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        report = F.build(rows, EXTRACTS, CONTRACTS, escalations=targets,
                         retire_after_n=2)
        self.assertEqual(report["series"][0]["rows"][0]["handling"], "retired")

    def test_a_stamp_that_names_no_real_instant_revives_nothing(self):
        # `_STAMP` IS A SHAPE: `20261345T996000Z` matches it and sorts above
        # every real 2026 stamp, so a fat-fingered filename is the same hole as
        # a typo'd year without ever looking like a date. Parsed, not just
        # matched -- and an unparseable instant is reported, never believed.
        d = _make_journal({
            "escalations/20260901T101126Z.md": escalation(ESC_TARGET),
            "escalations/20260902T101126Z.md": escalation(ESC_TARGET),
            "waivers/20261345T996000Z.md": f"**Waiver:** {ESC_TARGET}\n",
        })
        targets, problems = F.escalation_targets(d)
        self.assertEqual(targets[ESC_TARGET]["rejections"], 2)
        self.assertEqual(problems["misdated_waivers"],
                         ["20261345T996000Z.md"])

    def test_a_freshly_granted_waiver_is_not_reported_for_postdating_its_escalations(self):
        # WHY THE ALARM IS NOT "NEWER THAN THE NEWEST ESCALATION FOR THIS ID".
        # Every real waiver postdates the rejection it answers -- a human reads
        # the escalation and then writes the waiver -- so that predicate fires
        # on EVERY legitimate revival, and on a successful one (the run is
        # written up and never escalated again) it fires forever. A report line
        # that is always present for correct operation is not a signal. The
        # predicate is instead "ahead of the journal's newest committed file by
        # more than `_WAIVER_LEAD`", which a real grant never is.
        d = _make_journal({
            "20260908T090000Z.md": "# tick\n",
            "escalations/20260908T080000Z.md": escalation(ESC_TARGET),
            "waivers/20260908T100000Z.md": f"**Waiver:** {ESC_TARGET}\n",
        })
        targets, problems = F.escalation_targets(d)
        self.assertEqual(targets.get(ESC_TARGET, {}).get("rejections", 0), 0)
        self.assertEqual(problems["misdated_waivers"], [])

    def test_a_waiver_for_a_run_with_no_escalations_waives_only_the_future(self):
        # THE DECISION, PINNED. A waiver naming a run with NO committed
        # escalation (a wrong id, or one written pre-emptively) has nothing to
        # waive, so it waives nothing and is NOT reported: a redundant waiver is
        # not evidence of a bad stamp. Its floor is unobservable except against
        # LATER escalations, and `_WAIVER_LEAD` is what bounds how much of the
        # future that floor can eat -- without a bound this is the typo'd-year
        # hole again, one run id over, and with one it is at most the slack a
        # real grant needs.
        d = _make_journal({
            "20260908T090000Z.md": "# tick\n",
            "waivers/20260908T100000Z.md": f"**Waiver:** {ESC_TARGET}\n",
        })
        targets, problems = F.escalation_targets(d)
        self.assertEqual(targets, {})
        self.assertEqual(problems["misdated_waivers"], [])
        # AND THE NEXT REJECTION STILL COUNTS IN FULL: the floor is a window
        # over what came before it, so a rejection stamped after the waiver is
        # episode one of the new window rather than a waived leftover.
        d = _make_journal({
            "20260908T090000Z.md": "# tick\n",
            "waivers/20260908T100000Z.md": f"**Waiver:** {ESC_TARGET}\n",
            "escalations/20260908T110000Z.md": escalation(ESC_TARGET),
        })
        targets, _ = F.escalation_targets(d)
        self.assertEqual(targets[ESC_TARGET]["rejections"], 1)

    def test_a_waiver_in_a_journal_with_no_committed_stamps_is_not_reported(self):
        # THE "CANNOT JUDGE" BRANCH. With no committed entry and no escalation,
        # the journal has no clock of its own to measure the waiver against --
        # and nothing to waive either, so believing the stamp costs nothing and
        # calling it misdated would be an accusation this journal cannot
        # support. Same convention as the rest of the file: an absence is
        # "unknown", never "wrong".
        d = _make_journal({"waivers/20270908T000000Z.md":
                           f"**Waiver:** {ESC_TARGET}\n"})
        targets, problems = F.escalation_targets(d)
        self.assertEqual(targets, {})
        self.assertEqual(problems["misdated_waivers"], [])

    def test_crlf_line_endings_do_not_hide_a_rejection(self):
        # THE REVIEWER'S REPRODUCTION. `_TARGET` and `_KIND` are anchored with
        # `$` under `re.M`, which stops before `\n` but not before a `\r` a
        # CRLF-saved file leaves in front of it -- so before normalization,
        # `target_of` returned `""`, the loop `continue`d before the kind was
        # even classified, and the escalation counted toward NEITHER `targets`
        # NOR `unclassified`: an invisible failure, unlike every other
        # "cannot answer" path in this file.
        body = escalation(ESC_TARGET).replace("\n", "\r\n")
        d = _make_journal({"escalations/20260904T101126Z.md": body})
        targets, problems = F.escalation_targets(d)
        self.assertEqual(targets[ESC_TARGET]["rejections"], 1)
        self.assertEqual(problems["unclassified"], [])

    def test_two_identical_target_lines_still_count_as_one_episode(self):
        # A file quoting its OWN target line (an evidence block echoing the
        # header, say) is harmless: both matches agree, so this resolves
        # exactly like a single mention.
        extra = f"**Target run:** {ESC_TARGET}\n"
        d = _make_journal({"escalations/20260904T101126Z.md":
                           escalation(ESC_TARGET, extra=extra)})
        targets, _ = F.escalation_targets(d)
        self.assertEqual(targets[ESC_TARGET]["rejections"], 1)

    def test_two_disagreeing_target_lines_retire_nothing(self):
        # AMBIGUOUS, SO REFUSED -- not resolved by position, which an
        # agent-written file does not guarantee. Neither id is retired on;
        # the drift shows up as this run staying unretired, which is the safe
        # direction, never as the wrong run being retired.
        extra = f"**Target run:** {OTHER}\n"
        d = _make_journal({"escalations/20260904T101126Z.md":
                           escalation(ESC_TARGET, extra=extra)})
        targets, _ = F.escalation_targets(d)
        self.assertEqual(targets, {})

    def test_a_quoted_prior_target_before_the_real_one_is_not_picked_by_position(self):
        # THE REVIEWER'S REPRODUCTION. A rejection quoting a PREVIOUS entry's
        # `**Target run:**` line inside a fenced evidence block, with the
        # real target line later in the file -- `.search` alone would resolve
        # to the quoted (wrong) one. Since the two disagree, this must retire
        # NEITHER id: not the quoted one (never its target) and not
        # `ESC_TARGET` (the rejection could not be attributed to it either).
        body = (f"# Retry\n\nEvidence:\n```\n**Target run:** {OTHER}\n```\n\n"
                f"**Target run:** {ESC_TARGET}\n\n"
                "## NOT RECORDED — the copilot did not agree\n\n"
                "Escalation kind: rejection\n\n```\nreason\n```\n")
        d = _make_journal({"escalations/20260904T101126Z.md": body})
        targets, _ = F.escalation_targets(d)
        self.assertNotIn(ESC_TARGET, targets)
        self.assertNotIn(OTHER, targets)


class RetirementState(unittest.TestCase):
    """`build()`'s `recorded|unrecorded|retired` state per row -- task 3. This is
    the consumer of `escalation_targets`' counts: two rejections retire a run
    that has not been written up, one does not, `recorded` always wins, and an
    absent record reads exactly like `rejections: 0` (see the note above
    `escalation_targets` about why an id with only unclassified escalations has
    no record at all)."""

    def test_two_rejections_retire_a_row(self):
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        esc = {"eA": {"rejections": 2, "latest": "escalations/z.md"}}
        report = F.build(rows, EXTRACTS, CONTRACTS, escalations=esc)
        r = report["series"][0]["rows"][0]
        self.assertEqual(r["handling"], "retired")
        self.assertEqual(r["rejections"], 2)
        self.assertEqual(r["escalation_latest"], "escalations/z.md")
        self.assertEqual(report["health"]["unrecorded_runs"], 0)
        self.assertEqual(report["health"]["retired_runs"], 1)

    def test_one_rejection_does_not_retire(self):
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        esc = {"eA": {"rejections": 1, "latest": "escalations/z.md"}}
        report = F.build(rows, EXTRACTS, CONTRACTS, escalations=esc)
        self.assertEqual(report["series"][0]["rows"][0]["handling"],
                         "unrecorded")
        self.assertEqual(report["health"]["unrecorded_runs"], 1)

    def test_a_row_with_no_escalation_record_is_unrecorded(self):
        # ABSENT AND `rejections: 0` ARE THE SAME THING (see Task 2): only a
        # counted rejection allocates a record, so a run whose only escalations
        # were verifier outages has none at all.
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        report = F.build(rows, EXTRACTS, CONTRACTS, escalations={})
        self.assertEqual(report["series"][0]["rows"][0]["handling"],
                         "unrecorded")
        self.assertEqual(report["series"][0]["rows"][0]["rejections"], 0)

    def test_recorded_beats_retired(self):
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        esc = {"eA": {"rejections": 9, "latest": "escalations/z.md"}}
        report = F.build(rows, EXTRACTS, CONTRACTS, journaled={"eA"},
                         escalations=esc)
        self.assertEqual(report["series"][0]["rows"][0]["handling"], "recorded")

    def test_the_env_var_path_rejects_a_bad_threshold_end_to_end(self):
        # THE ONE TEST OF THE `os.environ` PATH. `build()` is otherwise pure --
        # `retire_after_n` lets every other test pass the threshold directly --
        # but the systemd unit configures this via the environment, so that path
        # still needs coverage, exercised all the way through `build()` (default
        # `retire_after_n=None` reads it via `retire_after()`).
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        for bad in ("0", "-1", "x", ""):
            with self.subTest(bad=bad):
                with patch.dict(os.environ, {"QF_FRONTIER_RETIRE_AFTER": bad}):
                    with self.assertRaises(F.FrontierError):
                        F.build(rows, EXTRACTS, CONTRACTS)

    def test_a_valid_threshold_is_honoured(self):
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        esc = {"eA": {"rejections": 2, "latest": "escalations/z.md"}}
        report = F.build(rows, EXTRACTS, CONTRACTS, escalations=esc,
                         retire_after_n=3)
        self.assertEqual(report["series"][0]["rows"][0]["handling"],
                         "unrecorded")

    def test_the_prereg_fields_reach_the_rows(self):
        note = P.encode(CFG_REF, "p90_miss_tail", "hold",
                        "the categorical helps the tail", vs="eB",
                        cfgh=CFGH_A, tol=0.0011238)
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P,
                    note=note)]
        r = F.build(rows, EXTRACTS, CONTRACTS)["series"][0]["rows"][0]
        self.assertEqual(r["config_digest"], CFGH_A)
        self.assertEqual(r["vs"], "eB")
        self.assertAlmostEqual(r["tol"], 0.0011238)

    def test_a_misdated_waiver_reaches_health_and_the_json(self):
        # THE NEW KEY TRAVELS THE SAME ROUTE AS THE OLD ONES: names in
        # `problems` (so the report can say WHICH file), an int in `health` (so
        # the JSON shape stays int-valued). A clamped-or-ignored waiver that
        # reached neither would be the silence this whole counter exists to end.
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        report = F.build(rows, EXTRACTS, CONTRACTS,
                         problems={"misdated_waivers":
                                   ["20270908T000000Z.md"]})
        self.assertEqual(report["health"]["misdated_waivers"], 1)
        self.assertEqual(report["problems"]["misdated_waivers"],
                         ["20270908T000000Z.md"])

    def test_the_problems_dict_reaches_health(self):
        # `problems` HOLDS NAMES NOW (basenames), not counts -- `build()` is
        # the one place that turns them into the ints `health` carries, via
        # `len(...)`, so the JSON shape of `health` itself does not change.
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        report = F.build(rows, EXTRACTS, CONTRACTS,
                         problems={"unclassified": ["a.md", "b.md", "c.md"],
                                  "unparseable_waivers": ["not-a-stamp.md"]})
        self.assertEqual(report["health"]["unclassified_escalations"], 3)
        self.assertEqual(report["health"]["unparseable_waivers"], 1)


class RetiredRendering(unittest.TestCase):
    """Task 4: the report's human/agent-facing surfaces agree with `handling`.
    `health["unrecorded_runs"]` alone is not what the leader steers on -- the
    per-series table, the shortlist and a visible RETIRED line all have to say
    the same thing, or retirement is not an escape from the livelock."""

    def test_a_retired_row_is_named_in_the_report_and_not_offered(self):
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        esc = {"eA": {"rejections": 2,
                      "latest": "escalations/20260904T111002Z.md"}}
        text = F.render(F.build(rows, EXTRACTS, CONTRACTS, escalations=esc))
        # NOT `RETIRED UNRECORDED:` -- quality review, item 1: that label
        # reused the exact word (`Unrecorded scored runs:`, action 1's "a
        # finished run is unrecorded") that names the bucket the leader is
        # supposed to act on, which is exactly the ambiguity the new
        # tick-prompt.md paragraph has to spend a sentence overriding.
        self.assertIn("RETIRED: eA", text)
        self.assertIn("escalations/20260904T111002Z.md", text)
        self.assertIn("| retired |", text)
        self.assertIn("Unrecorded scored runs: 0.", text)

    def test_a_retired_row_is_not_in_the_needs_writing_up_table(self):
        # THE LEADER'S SHORTLIST. A retired row listed here is the livelock
        # again, whatever the counter says.
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        esc = {"eA": {"rejections": 2, "latest": "escalations/z.md"}}
        text = F.render(F.build(rows, EXTRACTS, CONTRACTS, escalations=esc))
        self.assertNotIn("needs writing up", text)

    def test_an_unrecorded_row_is_still_offered(self):
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        text = F.render(F.build(rows, EXTRACTS, CONTRACTS))
        self.assertIn("needs writing up", text)
        self.assertIn("| NO |", text)

    def test_a_recorded_row_renders_yes(self):
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        text = F.render(F.build(rows, EXTRACTS, CONTRACTS, journaled={"eA"}))
        self.assertIn("| yes |", text)

    def test_a_mixed_series_lists_only_the_unrecorded_row(self):
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P),
                row("eB", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        esc = {"eA": {"rejections": 2, "latest": "escalations/z.md"}}
        text = F.render(F.build(rows, EXTRACTS, CONTRACTS, escalations=esc))
        self.assertIn("needs writing up", text)
        self.assertIn("| eB |", text)
        # NOT AMBIGUOUS: the shortlist is the only table anywhere in this
        # report that prints a bare evaluation id as a cell -- the per-series
        # "written up" table keys its rows on `when`/`config`, never on the
        # evaluation id, and the RETIRED line uses `RETIRED: eA` (no leading
        # pipe). So "| eA | " can only ever come from the shortlist, and this
        # assertion is exactly "eA is not on it".
        self.assertNotIn("| eA | ", text)
        self.assertIn("RETIRED: eA", text)

    def test_unclassified_escalations_are_named_not_just_counted(self):
        # Quality review item 2: a count told an operator "3 escalation(s), go
        # find them"; the basenames are the fix.
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        text = F.render(F.build(rows, EXTRACTS, CONTRACTS,
                                problems={"unclassified":
                                          ["a.md", "b.md", "c.md"]}))
        self.assertIn("UNCLASSIFIED ESCALATIONS 3 escalation(s) in"
                     " escalations/: a.md, b.md, c.md", text)

    def test_unclassified_escalations_are_capped_at_five_with_a_tally(self):
        # A LARGE BACKLOG MUST NOT SWAMP THE REPORT -- the fix for "you don't
        # know which file" is not allowed to become "the report is now one
        # line per file in the backlog".
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        names = [f"{n:02d}.md" for n in range(7)]
        text = F.render(F.build(rows, EXTRACTS, CONTRACTS,
                                problems={"unclassified": names}))
        self.assertIn("UNCLASSIFIED ESCALATIONS 7 escalation(s) in"
                     " escalations/: 00.md, 01.md, 02.md, 03.md, 04.md,"
                     " and 2 more", text)

    def test_an_unparseable_waiver_is_named_not_just_counted(self):
        # A MISNAMED WAIVER REVIVES NOTHING, and a waiver is the one file whose
        # whole purpose is operator-initiated revival -- so silence here is the
        # worst possible place for it. The basename is the fix, not the count.
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        text = F.render(F.build(rows, EXTRACTS, CONTRACTS,
                                problems={"unparseable_waivers":
                                          ["not-a-stamp.md"]}))
        self.assertIn("UNPARSEABLE WAIVERS 1 file(s) in waivers/:"
                     " not-a-stamp.md", text)

    def test_a_misdated_waiver_is_named_in_the_report(self):
        # A WAIVER THE READER REFUSED TO BELIEVE MUST SAY SO BY NAME. The
        # operator who typed the year is the only person who can fix it, and the
        # only surface they read is this report -- clamping or ignoring the file
        # quietly would leave a retired row un-revived with no stated reason,
        # which is the 2.5-day silence of 2026-09-04 in miniature.
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        text = F.render(F.build(rows, EXTRACTS, CONTRACTS,
                                problems={"misdated_waivers":
                                          ["20270908T000000Z.md"]}))
        self.assertIn("MISDATED WAIVERS 1 file(s) in waivers/:"
                      " 20270908T000000Z.md", text)

    def test_an_unmodeled_handling_renders_visibly_instead_of_raising(self):
        # `render()` has no surrounding try/except, and this report is what a
        # human reads when the loop is STUCK -- exactly the moment a `KeyError`
        # costing the whole report (no table, no health summary, nothing on
        # stdout) is most expensive. `handling` is code-assigned from a closed
        # set inside `build()`, so reaching this at all means a later task
        # added a state and forgot to teach `_HANDLING_CELL` about it; the
        # report must still print, with the bad value visible for a grep.
        rows = [row("eA", REF_PROBE, CFG_REF, REFERENCE_M, REFERENCE_P)]
        report = F.build(rows, EXTRACTS, CONTRACTS)
        report["series"][0]["rows"][0]["handling"] = "quarantined"
        text = F.render(report)          # must not raise
        self.assertIn("?quarantined?", text)


if __name__ == "__main__":
    unittest.main()
