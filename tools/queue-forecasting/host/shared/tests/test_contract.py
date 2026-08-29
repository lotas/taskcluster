"""Phase 2c Task 17. `contract.py` -- the identity of an evaluation contract."""
import json
import os
import tempfile
import unittest

import contract


def a_contract(**over):
    base = {
        "schema": 1,
        "name": "wait_time_v1",
        "target": "wait_time",
        "baseline_hash": "a" * 64,
        "primary_slice": {"reason_resolved": ["completed"]},
        "metrics": {
            "mae": {"direction": "lower_is_better",
                    "bar": {"kind": "relative_improvement", "value": 0.15}},
        },
        "consistency": {"days_required": 3},
        "holdout_days": 5,
    }
    base.update(over)
    return base


class TestTheIdentityIsAContentKey(unittest.TestCase):
    def test_the_same_rule_hashes_the_same_however_it_was_written(self):
        # Key order and int-vs-float must not change the identity: two results
        # citing one hash are comparable BY CONSTRUCTION, and a hash that
        # changed with formatting would break that silently.
        one = a_contract(metrics={
            "mae": {"direction": "lower_is_better",
                    "bar": {"kind": "absolute", "value": 1}}})
        two = {k: one[k] for k in reversed(list(one))}
        # `1` and `1.0` are the same bar; `_need_number` normalises to float so
        # a contract written by hand and one round-tripped through JSON cannot
        # get different identities.
        two["metrics"] = {"mae": {"bar": {"value": 1.0, "kind": "absolute"},
                                  "direction": "lower_is_better"}}
        self.assertEqual(contract.contract_hash(one),
                         contract.contract_hash(two))

    def test_changing_a_bar_changes_the_identity(self):
        loose = a_contract(metrics={
            "mae": {"direction": "lower_is_better",
                    "bar": {"kind": "relative_improvement", "value": 0.05}}})
        self.assertNotEqual(contract.contract_hash(a_contract()),
                            contract.contract_hash(loose))

    def test_changing_the_baseline_changes_the_identity(self):
        # The point of pinning it: "improves by >=15% over baseline" is a
        # different rule when the baseline is different.
        self.assertNotEqual(
            contract.contract_hash(a_contract()),
            contract.contract_hash(a_contract(baseline_hash="b" * 64)))

    def test_changing_the_slice_changes_the_identity(self):
        other = a_contract(primary_slice={"reason_resolved":
                                          ["completed", "failed"]})
        self.assertNotEqual(contract.contract_hash(a_contract()),
                            contract.contract_hash(other))

    def test_the_note_is_inside_the_identity(self):
        # Unlike a baseline's promoted_at. A note is what a reader is TOLD the
        # rule means, so two contracts differing only in their note are two
        # different rules to anyone reading a verdict.
        self.assertNotEqual(contract.contract_hash(a_contract()),
                            contract.contract_hash(a_contract(note="tail gate")))

    def test_a_declared_hash_is_excluded_from_its_own_computation(self):
        body = a_contract()
        digest = contract.contract_hash(body)
        self.assertEqual(contract.contract_hash({**body,
                                                 "contract_hash": digest}),
                         digest)


class TestClosedWorld(unittest.TestCase):
    def test_an_unknown_top_level_key_is_refused(self):
        with self.assertRaises(contract.ContractError) as cm:
            contract.validate(a_contract(tolerance=0.01))
        self.assertIn("tolerance", str(cm.exception))

    def test_an_unknown_metric_key_is_refused(self):
        with self.assertRaises(contract.ContractError) as cm:
            contract.validate(a_contract(metrics={
                "mae": {"direction": "lower_is_better", "weight": 2,
                        "bar": {"kind": "absolute", "value": 1.0}}}))
        self.assertIn("weight", str(cm.exception))

    def test_an_unknown_bar_key_is_refused(self):
        with self.assertRaises(contract.ContractError):
            contract.validate(a_contract(metrics={
                "mae": {"direction": "lower_is_better",
                        "bar": {"kind": "absolute", "value": 1.0,
                                "unit": "seconds"}}}))

    def test_every_required_key_is_required(self):
        for key in ("schema", "name", "target", "baseline_hash",
                    "primary_slice", "metrics", "consistency", "holdout_days"):
            with self.subTest(missing=key):
                body = a_contract()
                del body[key]
                with self.assertRaises(contract.ContractError) as cm:
                    contract.validate(body)
                self.assertIn(key, str(cm.exception))


class TestShapesThatEscapeATypedRefusal(unittest.TestCase):
    """The `extract_spec` P1, not repeated: `"x" in 5` is a TypeError and
    `isinstance(True, int)` is True, so a membership test before a type test and
    a bool where a number is expected both escape the refusal this file exists to
    make."""

    HOSTILE = (None, 5, 5.0, True, False, "", "x", [], [1], (), 0, -1,
               float("nan"), float("inf"))

    def test_no_input_shape_escapes_as_something_other_than_contract_error(self):
        for shape in self.HOSTILE:
            with self.subTest(shape=shape):
                with self.assertRaises(contract.ContractError):
                    contract.validate(shape)

    def test_no_field_shape_escapes(self):
        for field in ("schema", "name", "target", "baseline_hash",
                      "primary_slice", "metrics", "consistency",
                      "holdout_days", "note"):
            for shape in self.HOSTILE:
                with self.subTest(field=field, shape=shape):
                    try:
                        contract.validate(a_contract(**{field: shape}))
                    except contract.ContractError:
                        pass
                    # A shape that happens to be VALID is fine; anything else
                    # escaping is the defect.

    def test_a_boolean_bar_is_refused_rather_than_read_as_one(self):
        # `True` compares as 1.0, so a bar of True would judge every run.
        with self.assertRaises(contract.ContractError):
            contract.validate(a_contract(metrics={
                "mae": {"direction": "lower_is_better",
                        "bar": {"kind": "absolute", "value": True}}}))

    def test_a_non_finite_bar_is_refused(self):
        # Every comparison against NaN is False, so a NaN bar fails every run
        # while looking like a threshold. An infinite one decides every run by
        # its sign. Both are a judge that has stopped judging.
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(contract.ContractError) as cm:
                    contract.validate(a_contract(metrics={
                        "mae": {"direction": "lower_is_better",
                                "bar": {"kind": "absolute", "value": value}}}))
                self.assertIn("finite", str(cm.exception))

    def test_a_boolean_days_required_is_refused(self):
        with self.assertRaises(contract.ContractError):
            contract.validate(a_contract(consistency={"days_required": True}))


class TestRulesNothingCouldSatisfy(unittest.TestCase):
    """Each of these fails EVERY run, for a reason that has nothing to do with
    the model -- which is the worst kind of bug in a judge, because the output
    looks like a finding."""

    def test_requiring_more_days_than_the_holdout_has_is_refused(self):
        with self.assertRaises(contract.ContractError) as cm:
            contract.validate(a_contract(holdout_days=3,
                                         consistency={"days_required": 5}))
        self.assertIn("no run could ever satisfy it", str(cm.exception))

    def test_an_empty_band_is_refused(self):
        with self.assertRaises(contract.ContractError) as cm:
            contract.validate(a_contract(metrics={
                "p90_coverage": {"direction": "band",
                                 "bar": {"kind": "band", "low": 0.95,
                                         "high": 0.85}}}))
        self.assertIn("empty band", str(cm.exception))

    def test_an_empty_metric_set_is_refused(self):
        with self.assertRaises(contract.ContractError) as cm:
            contract.validate(a_contract(metrics={}))
        self.assertIn("judges nothing", str(cm.exception))

    def test_a_repeated_slice_value_is_refused(self):
        # It would double count every row in the repeated class.
        with self.assertRaises(contract.ContractError) as cm:
            contract.validate(a_contract(
                primary_slice={"reason_resolved": ["completed", "completed"]}))
        self.assertIn("double count", str(cm.exception))


class TestDirectionAndBarMustAgree(unittest.TestCase):
    """A coverage metric with a one-sided bar reads as though it checks
    calibration and does not: a model that never misses its p90 is not
    calibrated, it is inflated."""

    def test_a_band_direction_needs_a_band_bar(self):
        with self.assertRaises(contract.ContractError) as cm:
            contract.validate(a_contract(metrics={
                "p90_coverage": {"direction": "band",
                                 "bar": {"kind": "absolute", "value": 0.9}}}))
        self.assertIn("disagree", str(cm.exception))

    def test_a_band_bar_needs_a_band_direction(self):
        with self.assertRaises(contract.ContractError):
            contract.validate(a_contract(metrics={
                "mae": {"direction": "lower_is_better",
                        "bar": {"kind": "band", "low": 0.1, "high": 0.2}}}))

    def test_a_metric_with_no_direction_is_refused(self):
        # A number nobody can fail.
        with self.assertRaises(contract.ContractError):
            contract.validate(a_contract(metrics={
                "mae": {"bar": {"kind": "absolute", "value": 1.0}}}))

    def test_band_keys_are_refused_on_a_one_sided_bar(self):
        with self.assertRaises(contract.ContractError) as cm:
            contract.validate(a_contract(metrics={
                "mae": {"direction": "lower_is_better",
                        "bar": {"kind": "relative_improvement", "value": 0.15,
                                "low": 0.1}}}))
        self.assertIn("only meaningful for kind 'band'", str(cm.exception))


class TestTheSliceIsMembershipNeverAnExpression(unittest.TestCase):
    """A predicate that is an expression is code, and this file is read by the
    process whose job is constraining the candidate."""

    def test_an_unknown_resolved_value_is_refused(self):
        with self.assertRaises(contract.ContractError) as cm:
            contract.validate(a_contract(
                primary_slice={"reason_resolved": ["completed'; DROP"]}))
        self.assertIn("not one of", str(cm.exception))

    def test_a_string_predicate_is_refused(self):
        with self.assertRaises(contract.ContractError):
            contract.validate(a_contract(
                primary_slice={"reason_resolved": "reason = 'completed'"}))

    def test_the_anchor_is_the_one_the_baselines_were_scored_on(self):
        # `--pending-eval-date`. Another anchor would compare against a cohort
        # the baseline never scored.
        with self.assertRaises(contract.ContractError) as cm:
            contract.validate(a_contract(
                primary_slice={"reason_resolved": ["completed"],
                               "anchor": "resolved_at"}))
        self.assertIn("pending_at", str(cm.exception))

    def test_the_slice_is_sorted_so_input_order_is_not_an_identity(self):
        one = contract.validate(a_contract(
            primary_slice={"reason_resolved": ["completed", "failed"]}))
        two = contract.validate(a_contract(
            primary_slice={"reason_resolved": ["failed", "completed"]}))
        self.assertEqual(one, two)


class TestLoadRehashesRatherThanTrusting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, body):
        path = os.path.join(self.tmp.name, "c.json")
        with open(path, "w") as fh:
            json.dump(body, fh)
        return path

    def test_a_self_describing_file_loads_and_its_hash_is_recomputed(self):
        body = a_contract()
        body["contract_hash"] = contract.contract_hash(body)
        loaded, digest = contract.load(self.write(body))
        self.assertEqual(digest, body["contract_hash"])
        self.assertNotIn("contract_hash", loaded)

    def test_a_file_edited_since_it_was_written_is_refused(self):
        body = a_contract()
        body["contract_hash"] = contract.contract_hash(body)
        body["metrics"]["mae"]["bar"]["value"] = 0.01   # leaves the hash alone
        with self.assertRaises(contract.ContractError) as cm:
            contract.load(self.write(body))
        self.assertIn("hashes to", str(cm.exception))

    def test_a_file_with_no_declared_hash_is_fine(self):
        # The hash is derivable; declaring it is a convenience for readers.
        _loaded, digest = contract.load(self.write(a_contract()))
        self.assertEqual(digest, contract.contract_hash(a_contract()))

    def test_a_missing_file_is_a_contract_error_not_an_oserror(self):
        with self.assertRaises(contract.ContractError) as cm:
            contract.load(os.path.join(self.tmp.name, "nope.json"))
        self.assertIn("cannot read", str(cm.exception))

    def test_a_file_that_is_not_json_is_a_contract_error(self):
        path = os.path.join(self.tmp.name, "bad.json")
        with open(path, "w") as fh:
            fh.write("{not json")
        with self.assertRaises(contract.ContractError) as cm:
            contract.load(path)
        self.assertIn("not JSON", str(cm.exception))


class TestItStaysImportableByQfd(unittest.TestCase):
    """`qfd` resolves a submitted contract hash and `qfd` is stdlib-only (D6).
    A dependency here would be discovered as an ImportError in the daemon."""

    def test_stdlib_only(self):
        import re as _re
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "contract.py")
        with open(path) as fh:
            source = fh.read()
        for name in _re.findall(r"^\s*(?:import|from)\s+([a-zA-Z_][\w.]*)",
                                source, _re.M):
            with self.subTest(module=name):
                self.assertIn(name.split(".")[0],
                              {"hashlib", "json", "re", "__future__"})

    def test_the_targets_match_what_an_extract_can_produce(self):
        # A contract judging a column the extract does not produce would
        # validate here and fail at the join.
        import extract_spec
        self.assertEqual(set(contract.TARGETS),
                         set(extract_spec.TARGET_COLUMNS))

    def test_the_error_is_not_a_dispatcher_type(self):
        # `shared` must not depend on `dispatcher`; the same decision, for the
        # same reason, as ExtractSpecError.
        self.assertTrue(issubclass(contract.ContractError, ValueError))
        for base in contract.ContractError.__mro__:
            self.assertNotEqual(base.__name__, "SpecError")


if __name__ == "__main__":
    unittest.main()
