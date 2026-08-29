"""Phase 2c Task 19. The evaluation request: IDs and hashes, never paths."""
import unittest

import request


def a_request(**over):
    base = {"op": "evaluate",
            "run_id": "probe-20260829T123756Z-9d54e39271d7-4290",
            "contract": "c" * 64,
            "request_hash": "e" * 64,
            "predictions_sha256": "d" * 64}
    base.update(over)
    return base


class TestNoPathCanCrossTheWire(unittest.TestCase):
    """`qfd` is in the `docker` group, which is root-equivalent (D5). A path on
    the wire would make the peer check the only thing standing between the widest
    domain in the system and the narrowest one's whole filesystem."""

    def test_the_accepted_shape_carries_no_path_like_field(self):
        req = request.validate(a_request())
        for key, value in req.items():
            with self.subTest(key=key):
                self.assertNotIn("/", str(value))

    def test_a_path_field_is_refused_by_name(self):
        for extra in ("predictions_path", "extract_dir", "baseline_dir",
                      "contract_path", "out_dir"):
            with self.subTest(field=extra):
                with self.assertRaises(request.RequestError) as cm:
                    request.validate(a_request(**{extra: "/tmp/x"}))
                self.assertIn(extra, str(cm.exception))

    def test_a_contract_name_is_not_accepted_in_place_of_a_hash(self):
        # A name would let the caller choose which rule judges it.
        with self.assertRaises(request.RequestError) as cm:
            request.validate(a_request(contract="wait_time.v1.json"))
        self.assertIn("contract_hash", str(cm.exception))


class TestTheRunIdBecomesADirectoryName(unittest.TestCase):
    """So it is MATCHED, not sanitised. "Contains no separator" is necessary and
    not sufficient: `..x` contains none either."""

    def test_a_real_run_id_is_accepted(self):
        for good in ("probe-20260829T123756Z-9d54e39271d7-4290",
                     "extract-20260829T000000Z-abcdef012345-1",
                     "test-20260101T000000Z-0000000-999999"):
            with self.subTest(run_id=good):
                self.assertEqual(request.validate(a_request(run_id=good))
                                 ["run_id"], good)

    def test_traversal_and_absolute_paths_are_refused(self):
        for bad in ("..", "../../etc/passwd", "/etc/passwd", ".", "..x",
                    "probe-20260829T123756Z-9d54e39271d7-4290/../x",
                    "probe-20260829T123756Z-9d54e39271d7-4290\x00",
                    "probe-20260829T123756Z-9d54e39271d7-4290 ",
                    "", "probe", "PROBE-20260829T123756Z-9d54e39271d7-1"):
            with self.subTest(run_id=bad):
                with self.assertRaises(request.RequestError):
                    request.validate(a_request(run_id=bad))

    def test_a_run_id_that_is_not_a_string_is_refused(self):
        for bad in (5, None, True, [], {}, ["probe-x"]):
            with self.subTest(run_id=bad):
                with self.assertRaises(request.RequestError):
                    request.validate(a_request(run_id=bad))


class TestClosedWorldAndTypes(unittest.TestCase):
    HOSTILE = (None, 5, 5.0, True, False, "", "x", [], {}, ["a" * 64],
               "A" * 64, "a" * 63, "a" * 65, "g" * 64)

    def test_every_hash_field_refuses_every_hostile_shape(self):
        for field in ("contract", "request_hash", "predictions_sha256",
                      "baseline_hash"):
            for shape in self.HOSTILE:
                # `baseline_hash: null` is the one legitimate exception: the
                # field is OPTIONAL, and an explicit null means absent (see
                # TestTheBaselineIsOptionalHere). Excluded here rather than
                # loosening the validator to satisfy an over-broad sweep.
                if field == "baseline_hash" and shape is None:
                    continue
                with self.subTest(field=field, shape=shape):
                    with self.assertRaises(request.RequestError):
                        request.validate(a_request(**{field: shape}))

    def test_a_non_object_request_is_refused(self):
        for bad in (None, 5, "evaluate", [], ["op"]):
            with self.subTest(raw=bad):
                with self.assertRaises(request.RequestError):
                    request.validate(bad)

    def test_the_op_must_be_evaluate(self):
        for bad in ("ping", "extract", "EVALUATE", "", None, 5):
            with self.subTest(op=bad):
                with self.assertRaises(request.RequestError):
                    request.validate(a_request(op=bad))

    def test_every_required_field_is_required(self):
        for field in ("op", "run_id", "contract", "request_hash",
                      "predictions_sha256"):
            with self.subTest(field=field):
                body = a_request()
                del body[field]
                with self.assertRaises(request.RequestError) as cm:
                    request.validate(body)
                self.assertIn(field, str(cm.exception))

    def test_an_unknown_field_is_refused_by_name(self):
        with self.assertRaises(request.RequestError) as cm:
            request.validate(a_request(force=True))
        self.assertIn("force", str(cm.exception))

    def test_a_flood_of_fields_is_bounded(self):
        body = a_request()
        body.update({f"k{i}": i for i in range(64)})
        with self.assertRaises(request.RequestError):
            request.validate(body)


class TestTheBaselineIsOptionalHere(unittest.TestCase):
    def test_absent_is_accepted_and_stays_absent(self):
        req = request.validate(a_request())
        self.assertNotIn("baseline_hash", req)

    def test_present_is_carried_through(self):
        req = request.validate(a_request(baseline_hash="f" * 64))
        self.assertEqual(req["baseline_hash"], "f" * 64)

    def test_an_explicit_null_means_absent(self):
        req = request.validate(a_request(baseline_hash=None))
        self.assertNotIn("baseline_hash", req)


if __name__ == "__main__":
    unittest.main()
