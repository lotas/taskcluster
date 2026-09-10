# Tests for `host/contracts/ACTIVE`: which published contract is current.
#
# WHY THE SETTING EXISTS. `experiment.choose_contract` ranks published contracts
# by how many scored runs used them, and usage only grows -- so a contract
# published this morning has zero and the incumbent wins forever. Publishing v2
# could not cut an unattended loop over to it. `--contract` names one for one
# run; deleting v1 is NOT the alternative, because
# `research-loop/frontier.py:load_contract` reads v1's body to interpret the
# cohorts judged under v1, and the file is tracked so a deploy restores it.
#
# So the cutover is a line in a root-owned file that qfd READS and never writes,
# the same posture as the contracts directory beside it. What these tests are
# mostly about is what does NOT come back: an entry naming a hash that is not
# published would be a target with an active rule nothing can load.
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_HOST = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
# BOTH paths, for the reason `test_scoreboard.py` records: `qfd` imports
# `baseline` and `contract` from `host/shared`, and a module that relies on a
# sibling having inserted that path passes in the suite and fails alone.
sys.path.insert(0, os.path.join(_HOST, "shared"))
sys.path.insert(0, os.path.join(_HOST, "dispatcher"))

import contract as contract_mod                                # noqa: E402
import qfd                                                     # noqa: E402


class ActiveCase(unittest.TestCase):
    """A real contracts directory with one real, validating contract in it.

    Real rather than stubbed because the published set is what an ACTIVE entry
    is checked against: a fixture that returned any hash as published would let
    every refusal below pass for the wrong reason.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(self._clean)
        self.hash_v1 = self.write_contract("wait_time.v1.json")
        self.hash_v2 = self.write_contract(
            "wait_time.v2.json", metrics={
                "mae": {"direction": "lower_is_better",
                        "bar": {"kind": "relative_improvement",
                                "value": 0.20}}})
        # THE CANARY. Without it every "omitted" assertion below could be
        # passing because the resolver publishes nothing at all.
        self.assertEqual(sorted(self.published()),
                         sorted([self.hash_v1, self.hash_v2]))

    def _clean(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_contract(self, name, **over):
        body = {"schema": 1, "name": name.split(".")[0], "target": "wait_time",
                "baseline_hash": "a" * 64,
                "primary_slice": {"reason_resolved": ["completed"]},
                "metrics": {"mae": {"direction": "lower_is_better",
                                    "bar": {"kind": "relative_improvement",
                                            "value": 0.15}}},
                "consistency": {"days_required": 3}, "holdout_days": 5}
        body.update(over)
        body = contract_mod.validate(body)
        body["contract_hash"] = contract_mod.contract_hash(body)
        with open(os.path.join(self.dir, name), "w") as fh:
            json.dump(body, fh)
        return body["contract_hash"]

    def published(self):
        disp = qfd.Dispatcher.__new__(qfd.Dispatcher)
        disp.cfg = mock.Mock(contracts_dir=self.dir)
        return disp.available_contracts()

    def write_active(self, text):
        with open(os.path.join(self.dir, "ACTIVE"), "w") as fh:
            fh.write(text)

    def read(self, text=None):
        """`(active, unresolved, rendered-errors)`, with the dispatcher's logger
        captured so an omission can be shown to be LOUD."""
        if text is not None:
            self.write_active(text)
        with mock.patch.object(qfd, "log") as log:
            got = qfd.read_active_contracts(self.dir, self.published())
        # RENDERED, not str()'d. `log.error` is called with %-style lazy
        # arguments, so the assembled sentence -- which is what an operator
        # reads -- only exists after the substitution. Asserting on the repr
        # of the call would pass on a message whose placeholders never filled.
        errors = "\n".join(call.args[0] % call.args[1:]
                            for call in log.error.call_args_list)
        return got[0], got[1], errors


class TestReadingActive(ActiveCase):
    def test_a_valid_entry_resolves(self):
        got, stuck, errors = self.read(f"wait_time {self.hash_v2}\n")
        self.assertEqual(got, {"wait_time": self.hash_v2})
        self.assertEqual(errors, "")

    def test_comments_and_blank_lines_and_missing_newline(self):
        got, stuck, errors = self.read(
            "# the active contract per target\n"
            "\n"
            f"wait_time {self.hash_v1}   # cut over 2026-09-10\n"
            "   \n"
            "# run_duration is still chosen by usage\n")
        self.assertEqual(got, {"wait_time": self.hash_v1})
        self.assertEqual(errors, "")

    def test_no_file_is_the_default_not_an_error(self):
        """Every target chosen by usage, which is what happened before this
        setting existed. An absent file must not be a fault."""
        got, stuck, errors = self.read()
        self.assertEqual(got, {})
        self.assertEqual(errors, "")

    def test_an_unset_contracts_dir_resolves_nothing(self):
        self.assertEqual(qfd.read_active_contracts("", {}), ({}, {}))

    def test_an_unknown_target_is_omitted_and_named(self):
        got, stuck, errors = self.read(f"wait_tim {self.hash_v1}\n")
        self.assertEqual(got, {})
        self.assertIn("line 1", errors)
        self.assertIn("wait_tim", errors)
        self.assertIn("unknown target", errors)

    def test_a_hash_that_is_not_64_hex_is_omitted_and_named(self):
        for bad in (self.hash_v1[:12], self.hash_v1.upper(),
                    "z" * 64, self.hash_v1 + "a"):
            got, stuck, errors = self.read(f"wait_time {bad}\n")
            self.assertEqual(got, {}, bad)
            self.assertIn("not a 64-hex", errors)

    def test_a_hash_that_is_not_published_is_UNRESOLVED_not_dropped(self):
        """THE ONE THAT MATTERS MOST, and the one the first version got wrong.

        A well-formed line naming an unpublished contract is a DEPLOYMENT fault,
        not a typo: ACTIVE committed without the new `.json`, or a `.json` the
        service cannot read. Dropping it made `active` empty, the resolver read
        that as "no setting", ranked by usage, and the loop kept running v1
        while ACTIVE said v2 and the tool that wrote it said "activated". So it
        comes back separately, and `experiment.py` refuses on it.
        """
        got, stuck, errors = self.read(f"wait_time {'9' * 64}\n")
        self.assertEqual(got, {}, "it must NOT be offered as active")
        self.assertEqual(stuck, {"wait_time": "9" * 64})
        self.assertIn("is not published", errors)
        self.assertIn("999999999999", errors)
        # The error names both causes, because from here they are
        # indistinguishable and an operator has to check both.
        self.assertIn("0644", errors)

    def test_an_unreadable_contract_file_lands_in_unresolved(self):
        """The mode-0600 trigger, reproduced rather than described.
        `contract.load` cannot read it, `available_contracts` omits it, and the
        ACTIVE line then names a hash nothing publishes -- which must refuse,
        not rank."""
        os.chmod(os.path.join(self.dir, "wait_time.v2.json"), 0o000)
        self.addCleanup(os.chmod,
                        os.path.join(self.dir, "wait_time.v2.json"), 0o644)
        if os.access(os.path.join(self.dir, "wait_time.v2.json"), os.R_OK):
            self.skipTest("running as a uid that ignores file modes (root)")
        got, stuck, errors = self.read(f"wait_time {self.hash_v2}\n")
        self.assertEqual(got, {})
        self.assertEqual(stuck, {"wait_time": self.hash_v2})

    def test_a_malformed_line_is_omitted_and_named(self):
        for bad in ("wait_time", f"wait_time {self.hash_v1} extra", "="):
            got, stuck, errors = self.read(bad + "\n")
            self.assertEqual(got, {}, bad)
            self.assertIn("<target> <contract_hash>", errors)

    def test_a_duplicate_target_keeps_the_first_and_says_so(self):
        got, stuck, errors = self.read(f"wait_time {self.hash_v1}\n"
                                       f"wait_time {self.hash_v2}\n")
        self.assertEqual(got, {"wait_time": self.hash_v1})
        self.assertEqual(stuck, {})
        self.assertIn("line 2", errors)
        self.assertIn("already set", errors)

    def test_a_duplicate_cannot_resolve_past_an_unresolved_first_line(self):
        """First wins across BOTH dicts. If the duplicate check only looked at
        the resolved ones, a second line would quietly become the answer while
        the operator's first line sat unresolved -- and the refusal that first
        line should have caused would never happen."""
        got, stuck, errors = self.read(f"wait_time {'9' * 64}\n"
                                       f"wait_time {self.hash_v2}\n")
        self.assertEqual(got, {})
        self.assertEqual(stuck, {"wait_time": "9" * 64})
        self.assertIn("line 2", errors)

    def test_one_bad_line_does_not_lose_the_good_one(self):
        got, stuck, errors = self.read(f"wait_tim {self.hash_v1}\n"
                                f"wait_time {self.hash_v2}\n")
        self.assertEqual(got, {"wait_time": self.hash_v2})
        self.assertIn("line 1", errors)

    def test_the_file_is_not_offered_as_a_contract(self):
        """It sits in the contracts directory and is not a contract.
        `available_contracts` filters on `.json`, and this pins that."""
        self.write_active(f"wait_time {self.hash_v1}\n")
        self.assertEqual(sorted(self.published()),
                         sorted([self.hash_v1, self.hash_v2]))

    def test_it_is_read_fresh_on_every_call(self):
        """An operator edits this between requests. A cached answer would serve
        the previous decision, which is the whole failure this replaces."""
        self.assertEqual(self.read(f"wait_time {self.hash_v1}\n")[0],
                         {"wait_time": self.hash_v1})
        self.assertEqual(self.read(f"wait_time {self.hash_v2}\n")[0],
                         {"wait_time": self.hash_v2})
        os.unlink(os.path.join(self.dir, "ACTIVE"))
        self.assertEqual(self.read()[:2], ({}, {}))

    def test_every_target_the_validator_knows_is_accepted(self):
        """The target vocabulary is `contract_mod.TARGETS` and not a second
        list here: a target this file rejected but a contract could declare
        would be a rule that can be published and never activated."""
        digest = self.write_contract("run_duration.v1.json",
                                     target="run_duration")
        lines = "".join(f"{t} {digest if t == 'run_duration' else self.hash_v1}\n"
                        for t in contract_mod.TARGETS)
        got, stuck, errors = self.read(lines)
        self.assertEqual(sorted(got), sorted(contract_mod.TARGETS))
        self.assertEqual(errors, "")


class TestTheContractsOp(ActiveCase):
    """`_op_contracts` is what `experiment.py` actually reads."""

    def reply(self):
        disp = qfd.Dispatcher.__new__(qfd.Dispatcher)
        disp.cfg = mock.Mock(contracts_dir=self.dir)
        with mock.patch.object(qfd, "log"):
            return disp._op_contracts({}, 0)

    def test_the_reply_carries_the_unresolved_entries_separately(self):
        """A resolver must be able to tell "no setting" from "a setting I could
        not resolve": one ranks by usage, the other refuses."""
        self.write_active(f"wait_time {'9' * 64}\n")
        reply = self.reply()
        self.assertEqual(reply["active"], {})
        self.assertEqual(reply["active_unresolved"], {"wait_time": "9" * 64})

    def test_the_reply_carries_the_target_vocabulary(self):
        """So `qf` can say "no ACTIVE entry for run_duration" without a second
        copy of `contract_mod.TARGETS` that would drift."""
        self.assertEqual(self.reply()["targets"], list(contract_mod.TARGETS))

    def test_the_reply_carries_the_active_setting(self):
        self.write_active(f"wait_time {self.hash_v2}\n")
        reply = self.reply()
        self.assertEqual(reply["active"], {"wait_time": self.hash_v2})
        self.assertEqual(reply["dir"], self.dir)
        self.assertEqual(
            sorted(row["contract_hash"] for row in reply["contracts"]),
            sorted([self.hash_v1, self.hash_v2]),
            "activating one contract must not unpublish the others: the"
            " frontier reads old bodies to interpret old cohorts")

    def test_the_reply_carries_empty_dicts_when_there_is_no_file(self):
        """Keys that are always present, so a reader never has to distinguish
        "no setting" from "an older dispatcher"."""
        self.assertEqual(self.reply()["active"], {})
        self.assertEqual(self.reply()["active_unresolved"], {})

    def test_a_malformed_entry_does_not_reach_the_reply_at_all(self):
        """Unlike an unpublished hash: a line that names nothing cannot be a
        deployment fault, so there is nothing for a resolver to refuse over."""
        self.write_active("wait_tim not-a-hash\n")
        reply = self.reply()
        self.assertEqual(reply["active"], {})
        self.assertEqual(reply["active_unresolved"], {})


if __name__ == "__main__":
    unittest.main()
