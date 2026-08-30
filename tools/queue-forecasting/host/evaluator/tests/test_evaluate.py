"""Phase 2c Task 23: the evaluation, end to end, on real Parquet.

THE FIXTURE'S EXTRACT USES THE PRODUCTION SCHEMA, not a convenient subset. The
runs table is built from `extractor/inventory.DATASETS["runs"]` through
`extractor/parquet_writer.schema_for`, which is the same pair the privileged
extractor writes with. A fixture carrying only the five columns this code reads
would pass while the real file failed, and this project has paid for
"more convenient than production" fixtures three times already.

WHAT THESE TESTS CAN AND CANNOT SHOW. They exercise the real Parquet reader, the
real hashing, the real join and the real metric math on real files, so the shape,
the refusals and the self-consistency are genuinely verified. They CANNOT show
that the numbers reproduce a recorded walk-forward result -- that needs the
promoted baseline and a probe's own prediction set, and it is the acceptance run.
Said here rather than implied by a green suite.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import numpy as np
import pyarrow
import pyarrow.parquet

HERE = os.path.dirname(os.path.abspath(__file__))
EVALUATOR = os.path.dirname(HERE)
HOST = os.path.dirname(EVALUATOR)
sys.path.insert(0, EVALUATOR)
sys.path.insert(0, os.path.join(HOST, "shared"))
sys.path.insert(0, os.path.join(HOST, "extractor"))

import baseline as baseline_mod                                # noqa: E402
import contract as contract_mod                                # noqa: E402
import evaluate as ev                                          # noqa: E402
import extract_manifest as extract_manifest_mod                # noqa: E402
import extract_spec as extract_spec_mod                        # noqa: E402
import inventory as inventory_mod                              # noqa: E402
import parquet_writer as parquet_writer_mod                    # noqa: E402
import srcscan                                                 # noqa: E402

RUN_ID = "evaluate-20260829T101112Z-abcdef1-7"

# One row per bucket edge, five of each, so a per-day coverage of 0.9 is
# reachable. With four rows a day the only per-day coverages are 0, .25, .5, .75
# and 1.0 -- none of which is inside the [0.85, 0.95] band, so every day would
# fail consistency and the "go" case could not exist. The first fixture had
# exactly that bug.
BUCKET_TRUE = (30.0, 120.0, 600.0, 2400.0)
PER_BUCKET = 5
HOLDOUT = ("2026-08-20", "2026-08-21", "2026-08-22")
TRAINING = ("2026-08-18", "2026-08-19")


def _ts(day, second):
    return datetime.datetime.fromisoformat(day + "T00:00:00+00:00") \
        + datetime.timedelta(seconds=second)


class Row(dict):
    pass


def _times(value, factor):
    """A null `y_true` stays null in the baseline too. The first version
    multiplied it and raised a TypeError inside the fixture -- a fixture that
    cannot express the case the test is about does not test it."""
    return None if value is None else value * factor


def make_rows():
    """`(extract rows, prediction rows)` for a model that should pass."""
    extract, predictions = [], []
    n = 0
    for day in TRAINING + HOLDOUT:
        holdout = day in HOLDOUT
        for b, base in enumerate(BUCKET_TRUE):
            for k in range(PER_BUCKET):
                n += 1
                y = base + k
                row = Row(task_id=f"task{n:05d}", run_id=0,
                          pending_at=_ts(day, n),
                          reason_resolved="completed", y_true=y)
                extract.append(row)
                if not holdout:
                    continue
                # Two rows a day are deliberately UNCOVERED, both in the <1m
                # bucket, so aggregate coverage is 54/60 = 0.9 (inside the band)
                # and the 30m+ tail miss stays 0.
                p90 = y * 0.5 if (b == 0 and k < 2) else y * 1.5
                # DELIBERATELY NOT PERFECT. The first fixture set `p50 = y`
                # exactly, so `sum_abs_error` was 0.0 aggregate and per day --
                # which made every "recompute the counts" assertion compare 0 to
                # 0, and made a tamper test that zeroed that field a perturbation
                # that perturbed nothing. 2% keeps within_2x at 1.0 and the
                # relative MAE improvement at ~0.99, so the verdict is unchanged
                # and the numbers are non-degenerate.
                predictions.append(Row(task_id=row["task_id"], run_id=0,
                                       row_id=f"{row['task_id']}:0",
                                       p50=y * 1.02, p90_raw=p90))
    return extract, predictions


class Fixture:
    """A whole evaluable world on disk: extract, baseline, contract, staged
    predictions, and a `cfg` shaped like `service.Config`."""

    def __init__(self, *, target="wait_time", holdout_days=len(HOLDOUT),
                 days_required=2, slice_values=("completed",)):
        self.tmp = tempfile.mkdtemp()
        self.extracts = os.path.join(self.tmp, "extracts")
        self.baselines = os.path.join(self.tmp, "baselines")
        self.contracts = os.path.join(self.tmp, "contracts")
        self.eval_dir = os.path.join(self.tmp, "eval")
        for path in (self.extracts, self.baselines, self.contracts,
                     self.eval_dir):
            os.makedirs(path)
        self.target = target
        self.holdout_days = holdout_days
        self.days_required = days_required
        self.slice_values = list(slice_values)
        self.extract_rows, self.prediction_rows = make_rows()
        self.baseline_rows = None      # defaults to 3x the truth: a bad baseline
        self.run_id = RUN_ID

    # --- building -------------------------------------------------------
    def request(self):
        return dict(extract_spec_mod.validate(
            {"schema": extract_spec_mod.SCHEMA_VERSION, "target": self.target,
             "train_start": "2026-08-18T00:00:00Z",
             "as_of_date": "2026-08-23T00:00:00Z", "lookback_days": 7},
            now=datetime.datetime(2026, 8, 26, tzinfo=datetime.timezone.utc)))

    def write_extract(self):
        request = self.request()
        request_hash = extract_spec_mod.request_hash(request)
        directory = os.path.join(self.extracts, request_hash)
        os.makedirs(directory, exist_ok=True)
        dataset = inventory_mod.DATASETS["runs"]
        schema = parquet_writer_mod.schema_for(dataset.columns, dataset.types)
        target_column = extract_spec_mod.TARGET_COLUMNS[self.target]
        columns = {name: [] for name in dataset.columns}
        for row in self.extract_rows:
            for name in dataset.columns:
                if name == target_column:
                    columns[name].append(row["y_true"])
                elif name in row:
                    columns[name].append(row[name])
                else:
                    columns[name].append(None)
        table = pyarrow.table(
            [pyarrow.array(columns[name], schema.field(name).type)
             for name in dataset.columns], schema=schema)
        path = os.path.join(directory, dataset.file)
        pyarrow.parquet.write_table(table, path)
        manifest = {
            "schema": 1, "request": request, "request_hash": request_hash,
            "settlement_lag_s": 172800, "snapshot_start_ts": "2026-08-26",
            "snapshot": {"txid": "1"}, "watermark": {},
            "files": {"runs": {"file": dataset.file,
                               "sha256": ev.file_digest(path),
                               "rows": len(self.extract_rows),
                               "window": {}, "columns": list(dataset.columns)}},
        }
        # THE SHARED IMPLEMENTATION, which is the whole point of the module.
        # This used to be an inline `hashlib.sha256(json.dumps(..,
        # sort_keys=True))` -- DEFAULT separators, so `", "` and `": "`, so
        # different bytes and a different hash from what production writes. The
        # fixture was wrong and the suite was green, because nothing verified the
        # field. A fixture that computes an identity its own way is not a
        # fixture, it is a second implementation with no arbiter.
        manifest[extract_manifest_mod.HASH_FIELD] = \
            extract_manifest_mod.extract_hash(manifest)
        with open(os.path.join(directory, "MANIFEST.json"), "w") as fh:
            json.dump(manifest, fh, sort_keys=True, indent=2)
        self.request_hash = request_hash
        self.extract_dir = directory
        return directory

    def write_baseline(self):
        directory = os.path.join(self.baselines, "staging")
        os.makedirs(directory, exist_ok=True)
        p50_column, p90_column = ev.BASELINE_COLUMNS[self.target]
        rows = self.baseline_rows
        if rows is None:
            rows = [{"task_id": r["task_id"], "run_id": r["run_id"],
                     "pending_at": r["pending_at"].isoformat(),
                     p50_column: _times(r["y_true"], 3.0),
                     p90_column: _times(r["y_true"], 4.0)}
                    for r in self.extract_rows]
        with open(os.path.join(directory, baseline_mod.NDJSON_NAME), "w") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        for day in HOLDOUT:
            with open(os.path.join(directory, f"{day}.json"), "w") as fh:
                json.dump({"eval_date": day}, fh)
        manifest = baseline_mod.describe(directory, exclude_dates=[])
        manifest["baseline_hash"] = baseline_mod.baseline_hash(manifest)
        target = os.path.join(self.baselines, manifest["baseline_hash"])
        os.rename(directory, target)
        with open(os.path.join(target, "MANIFEST.json"), "w") as fh:
            json.dump(manifest, fh, sort_keys=True, indent=2)
        self.baseline_hash = manifest["baseline_hash"]
        self.baseline_dir = target
        return target

    def contract_body(self, **over):
        body = {
            "schema": 1, "name": "fixture", "target": self.target,
            "baseline_hash": self.baseline_hash,
            "primary_slice": {"reason_resolved": self.slice_values,
                              "anchor": "pending_at"},
            "metrics": {
                "mae": {"direction": "lower_is_better",
                        "bar": {"kind": "relative_improvement",
                                "value": 0.15}},
                "within_2x": {"direction": "higher_is_better",
                              "bar": {"kind": "absolute_improvement",
                                      "value": 0.05}},
                "p90_coverage": {"direction": "band",
                                 "bar": {"kind": "band", "low": 0.85,
                                         "high": 0.95}},
                "p90_miss_tail": {"direction": "lower_is_better",
                                  "bucket": "30m+",
                                  "bar": {"kind": "absolute", "value": 0.30}},
            },
            "consistency": {"days_required": self.days_required},
            "holdout_days": self.holdout_days,
        }
        body.update(over)
        return body

    def write_contract(self, **over):
        body = self.contract_body(**over)
        body["contract_hash"] = contract_mod.contract_hash(body)
        path = os.path.join(self.contracts, "fixture.json")
        with open(path, "w") as fh:
            json.dump(body, fh, sort_keys=True, indent=2)
        self.contract_hash = body["contract_hash"]
        return path

    def write_predictions(self, rows=None, *, run_id_type=pyarrow.int32(),
                          extra=None, columns=None):
        rows = self.prediction_rows if rows is None else rows
        inbox = os.path.join(self.eval_dir, self.run_id, "in")
        outbox = os.path.join(self.eval_dir, self.run_id, "out")
        os.makedirs(inbox, exist_ok=True)
        os.makedirs(outbox, exist_ok=True)
        data = {
            "task_id": pyarrow.array([r["task_id"] for r in rows],
                                     pyarrow.string()),
            "run_id": pyarrow.array([r["run_id"] for r in rows], run_id_type),
            "row_id": pyarrow.array([r["row_id"] for r in rows],
                                    pyarrow.string()),
            "p50": pyarrow.array([r["p50"] for r in rows], pyarrow.float64()),
            "p90_raw": pyarrow.array([r["p90_raw"] for r in rows],
                                     pyarrow.float64()),
        }
        if columns is not None:
            data = {k: v for k, v in data.items() if k in columns}
        if extra:
            for name, values in extra.items():
                data[name] = pyarrow.array(values)
        path = os.path.join(inbox, ev.PREDICTIONS_NAME)
        pyarrow.parquet.write_table(pyarrow.table(data), path)
        self.predictions_path = path
        return path

    def build(self, **contract_over):
        self.write_extract()
        self.write_baseline()
        self.write_contract(**contract_over)
        self.write_predictions()
        return self

    # --- running --------------------------------------------------------
    @property
    def cfg(self):
        class Cfg:
            pass
        cfg = Cfg()
        cfg.extracts_dir = self.extracts
        cfg.baselines_dir = self.baselines
        cfg.contracts_dir = self.contracts
        cfg.eval_dir = self.eval_dir
        return cfg

    def req(self, **over):
        out = {"op": "evaluate", "run_id": self.run_id,
               "contract": self.contract_hash,
               "request_hash": self.request_hash,
               "predictions_sha256": ev.file_digest(self.predictions_path),
               "baseline_hash": self.baseline_hash}
        out.update(over)
        return out

    def run(self, **over):
        return ev.evaluate(self.cfg, self.req(**over), "fixture.json")

    def verdict_document(self):
        with open(os.path.join(self.eval_dir, self.run_id, "out",
                               ev.VERDICT_NAME)) as fh:
            return json.load(fh)

    def eval_table(self):
        return pyarrow.parquet.read_table(
            os.path.join(self.eval_dir, self.run_id, "out", ev.EVAL_NAME))

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class EvaluateCase(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture().build()
        self.addCleanup(self.fx.close)

    def refusal(self, **over):
        with self.assertRaises(ev.EvaluateError) as cm:
            self.fx.run(**over)
        return cm.exception


class TestTheHappyPath(EvaluateCase):
    """THE CANARY. Without a passing evaluation, every refusal below could hold
    because the evaluator refuses everything."""

    def test_a_good_model_gets_a_go(self):
        reply = self.fx.run()
        self.assertEqual(reply["verdict"], "go")
        self.assertFalse(reply["reused"])
        self.assertEqual(len(reply["eval_hash"]), 64)
        self.assertEqual(reply["scored_n"], len(HOLDOUT) * len(BUCKET_TRUE)
                         * PER_BUCKET)
        self.assertEqual(reply["days"], list(HOLDOUT))

    def test_both_artifacts_are_published(self):
        self.fx.run()
        out = os.path.join(self.fx.eval_dir, self.fx.run_id, "out")
        for name in (ev.EVAL_NAME, ev.VERDICT_NAME):
            self.assertTrue(os.path.isfile(os.path.join(out, name)), name)
        # Nothing half-written left behind: publication is a rename.
        leftovers = [n for n in os.listdir(out) if n.endswith(".partial")]
        self.assertEqual(leftovers, [])

    def test_the_verdict_document_is_self_describing(self):
        self.fx.run()
        doc = self.fx.verdict_document()
        self.assertEqual(doc["schema"], ev.SCHEMA)
        self.assertEqual(doc["run_id"], self.fx.run_id)
        self.assertEqual(doc["contract"]["hash"], self.fx.contract_hash)
        self.assertEqual(doc["inputs"]["request_hash"], self.fx.request_hash)
        self.assertEqual(doc["inputs"]["baseline_hash"], self.fx.baseline_hash)
        # The per-row file's digest is INSIDE the verdict, so a verdict citing an
        # eval.parquet that has since changed is detectable.
        self.assertEqual(doc["inputs"]["eval_sha256"],
                         ev.file_digest(os.path.join(
                             self.fx.eval_dir, self.fx.run_id, "out",
                             ev.EVAL_NAME)))
        self.assertEqual(doc["eval_hash"], ev.eval_hash(doc))

    def test_the_verdict_hash_covers_the_numbers(self):
        self.fx.run()
        doc = self.fx.verdict_document()
        before = ev.eval_hash(doc)
        doc["model"]["aggregate"]["mae"]["sum_abs_error"] += 1.0
        self.assertNotEqual(ev.eval_hash(doc), before)

    def test_every_count_recomputes_from_the_per_row_file(self):
        """`eval.parquet` plus the contract has to reproduce `verdict.json`.
        That is the artifact's only purpose: a verdict nobody can recompute is a
        number to be believed rather than checked."""
        self.fx.run()
        doc = self.fx.verdict_document()
        table = self.fx.eval_table()
        y_true = np.array(table.column("y_true").to_pylist(), dtype=float)
        p50 = np.array(table.column("p50").to_pylist(), dtype=float)
        p90 = np.array(table.column("p90_raw").to_pylist(), dtype=float)
        agg = doc["model"]["aggregate"]
        self.assertEqual(agg["mae"]["eligible_n"], len(y_true))
        self.assertAlmostEqual(agg["mae"]["sum_abs_error"],
                               float(np.abs(p50 - y_true).sum()), places=6)
        ratio = np.maximum(p50 / y_true, y_true / p50)
        self.assertEqual(agg["within_2x"]["hit_n"], int((ratio <= 2).sum()))
        self.assertEqual(agg["p90_coverage"]["covered_n"],
                         int((y_true <= p90).sum()))

    def test_the_per_row_file_carries_the_baseline_too(self):
        # Without it the verdict's baseline numbers are unrecomputable, and the
        # relative bars are exactly the ones a reader will want to check.
        self.fx.run()
        table = self.fx.eval_table()
        for name in ("bl_p50", "bl_p90", "bl_abs_error", "bucket", "day",
                     "row_id", "target"):
            self.assertIn(name, table.column_names)
        bl = np.array(table.column("bl_p50").to_pylist(), dtype=float)
        y = np.array(table.column("y_true").to_pylist(), dtype=float)
        self.assertAlmostEqual(
            self.fx.verdict_document()["baseline"]["aggregate"]["mae"]
            ["sum_abs_error"], float(np.abs(bl - y).sum()), places=6)

    def test_the_two_sides_are_scored_over_one_population(self):
        # The property that makes a relative bar a comparison rather than a
        # coincidence.
        doc = (self.fx.run(), self.fx.verdict_document())[1]
        for metric in ("mae", "within_2x", "p90_coverage"):
            self.assertEqual(doc["model"]["aggregate"][metric]["eligible_n"],
                             doc["baseline"]["aggregate"][metric]["eligible_n"],
                             metric)

    def test_the_holdout_days_are_the_derived_ones(self):
        self.fx.run()
        days = self.fx.verdict_document()["days"]
        self.assertEqual(days["claimed"], list(HOLDOUT))
        # DERIVED from the extract's as_of_date, not recorded as an observation.
        self.assertEqual(days["required"], list(HOLDOUT))
        self.assertEqual(days["available_days"], len(TRAINING) + len(HOLDOUT))

    def test_the_derivation_matches_the_trainers_own_window(self):
        # `config.compute_windows`: hold_start = as_of_date - holdout_days,
        # hold_end = as_of_date, walked one calendar day at a time.
        self.assertEqual(
            ev.required_days("2026-08-23T00:00:00Z", len(HOLDOUT)),
            list(HOLDOUT))
        self.assertEqual(ev.required_days("2026-03-02T00:00:00Z", 3),
                         ["2026-02-27", "2026-02-28", "2026-03-01"])

    def test_the_training_window_is_not_required_to_be_predicted(self):
        # NC11 read literally would require it, and would be wrong: the extract
        # covers the training window and a probe predicts only the holdout.
        self.fx.run()
        self.assertEqual(self.fx.verdict_document()["rows"]["predicted_n"],
                         len(HOLDOUT) * len(BUCKET_TRUE) * PER_BUCKET)


class TestTheVerdictIsEarned(EvaluateCase):
    def test_a_bad_model_gets_a_no_go(self):
        # THE OTHER CANARY: a judge that only ever says "go" is not a judge.
        fx = Fixture()
        _extract, predictions = make_rows()
        for row in predictions:
            row["p50"] = row["p50"] * 50        # far outside 2x
        fx.prediction_rows = predictions
        fx.build()
        self.addCleanup(fx.close)
        reply = fx.run()
        self.assertEqual(reply["verdict"], "no-go")
        doc = fx.verdict_document()
        self.assertFalse(doc["metrics"]["mae"]["passed"])
        self.assertFalse(doc["metrics"]["within_2x"]["passed"])

    def test_the_improvement_is_measured_against_the_baseline(self):
        self.fx.run()
        mae = self.fx.verdict_document()["metrics"]["mae"]
        self.assertIsNotNone(mae["baseline"])
        self.assertGreater(mae["baseline"], mae["value"])
        self.assertAlmostEqual(mae["measured"],
                               (mae["baseline"] - mae["value"])
                               / abs(mae["baseline"]))


class TestIdentitiesAreRecomputed(EvaluateCase):
    def test_a_tampered_prediction_file_is_refused(self):
        # The digest is checked BEFORE the file is parsed.
        req = self.fx.req()
        with open(self.fx.predictions_path, "ab") as fh:
            fh.write(b"\x00")
        with self.assertRaises(ev.EvaluateError) as cm:
            ev.evaluate(self.fx.cfg, req, "fixture.json")
        self.assertIn("digests to", str(cm.exception))

    def test_a_tampered_runs_parquet_is_refused(self):
        path = os.path.join(self.fx.extract_dir, "runs.parquet")
        with open(path, "ab") as fh:
            fh.write(b"\x00")
        self.assertIn("not the one that was published",
                      str(self.refusal()))

    def test_an_edited_extract_manifest_is_refused(self):
        path = os.path.join(self.fx.extract_dir, "MANIFEST.json")
        with open(path) as fh:
            manifest = json.load(fh)
        manifest["request"]["as_of_date"] = "2026-09-01T00:00:00Z"
        with open(path, "w") as fh:
            json.dump(manifest, fh)
        self.assertIn("content key", str(self.refusal()))

    def test_a_manifest_field_outside_the_request_hash_is_still_covered(self):
        """`extract_hash` is the only thing that can catch this, and that is the
        point: the `as_of_date` edit above changes `request_hash` too, so it was
        passing on the older check and the new one was never exercised. A
        red-green pass is what said so -- removing the `extract_hash`
        verification left the suite green.

        `settlement_lag_s` is deliberately NOT in `request_hash` (adjusting an
        operational knob must not orphan every published extract), so it is
        exactly a field only the manifest's own content key covers.
        """
        path = os.path.join(self.fx.extract_dir, "MANIFEST.json")
        with open(path) as fh:
            manifest = json.load(fh)
        self.assertEqual(
            extract_spec_mod.request_hash(manifest["request"]),
            self.fx.request_hash,
            "the request hash must still verify, or this tests the wrong check")
        manifest["settlement_lag_s"] = manifest["settlement_lag_s"] + 1
        with open(path, "w") as fh:
            json.dump(manifest, fh)
        with self.assertRaises(ev.EvaluateError) as cm:
            self.fx.run()
        self.assertEqual(cm.exception.error_class, "extract_manifest_invalid")
        self.assertIn("content key", str(cm.exception))

    def test_the_fixture_builds_the_hash_the_way_production_does(self):
        # The review found the fixture computing `extract_hash` with default
        # `json.dumps` separators -- different bytes, wrong hash, and it passed
        # because nothing verified the field. Pinned so it cannot drift back.
        with open(os.path.join(self.fx.extract_dir, "MANIFEST.json")) as fh:
            manifest = json.load(fh)
        self.assertEqual(extract_manifest_mod.verify(manifest),
                         manifest["extract_hash"])
        with open(os.path.join(HOST, "extractor", "extractor.py")) as fh:
            self.assertIn("extract_manifest.extract_hash(manifest)", fh.read())

    def test_an_edited_baseline_manifest_is_refused(self):
        path = os.path.join(self.fx.baseline_dir, "MANIFEST.json")
        with open(path) as fh:
            manifest = json.load(fh)
        manifest["ndjson_rows"] = manifest["ndjson_rows"] + 1
        with open(path, "w") as fh:
            json.dump(manifest, fh)
        self.assertIn("hashes to", str(self.refusal()))

    def test_a_tampered_baseline_ndjson_is_refused(self):
        path = os.path.join(self.fx.baseline_dir, baseline_mod.NDJSON_NAME)
        with open(path, "a") as fh:
            fh.write('{"task_id":"x","run_id":0}\n')
        self.assertIn("not the one that was published", str(self.refusal()))

    def test_a_contract_edited_after_resolution_is_refused(self):
        path = os.path.join(self.fx.contracts, "fixture.json")
        with open(path) as fh:
            body = json.load(fh)
        body["metrics"]["mae"]["bar"]["value"] = 0.99
        with open(path, "w") as fh:
            json.dump(body, fh)
        # `contract.load` refuses it first: the declared hash no longer matches
        # the body. Either refusal is correct; what must not happen is judging.
        self.assertIn("contract", str(self.refusal()).lower())

    def test_a_missing_extract_names_the_reason(self):
        shutil.rmtree(self.fx.extract_dir)
        self.assertIn("has not published", str(self.refusal()))

    def test_a_missing_baseline_names_the_remedy(self):
        shutil.rmtree(self.fx.baseline_dir)
        self.assertIn("promote-baseline.sh", str(self.refusal()))


class TestTheContractAndTheRunMustAgree(EvaluateCase):
    def test_a_request_with_no_baseline_is_refused(self):
        e = self.refusal(baseline_hash=None)
        self.assertIn("carries no", str(e))

    def test_a_baseline_other_than_the_contract_s_is_refused(self):
        other = "b" * 64
        e = self.refusal(baseline_hash=other)
        self.assertIn("is not the bar that was agreed", str(e))

    def test_a_target_the_extract_was_not_requested_for_is_refused(self):
        # The extract carries BOTH duration columns, so this is about the pin
        # rather than a missing column -- and without the check the numbers come
        # out looking fine.
        fx = Fixture(target="wait_time")
        fx.write_extract()
        fx.write_baseline()
        fx.target = "run_duration"          # the contract, not the extract
        fx.write_contract()
        fx.write_predictions()
        self.addCleanup(fx.close)
        with self.assertRaises(ev.EvaluateError) as cm:
            fx.run()
        self.assertEqual(cm.exception.error_class, "extract_target_mismatch")

    def test_a_contract_hash_that_no_longer_matches_is_refused(self):
        e = self.refusal(contract="c" * 64)
        self.assertIn("this request named", str(e))


class TestTheRowSet(EvaluateCase):
    """NC11, through the real join."""

    def test_cherry_picking_inside_a_day_is_refused(self):
        fx = Fixture()
        _extract, predictions = make_rows()
        # Drop the worst-covered rows from one day: the exact gaming vector.
        fx.prediction_rows = [p for p in predictions
                              if not p["row_id"].endswith("00041:0")]
        fx.build()
        self.addCleanup(fx.close)
        with self.assertRaises(ev.EvaluateError) as cm:
            fx.run()
        self.assertEqual(cm.exception.error_class, "row_set_rejected")
        self.assertIn("omits", str(cm.exception))

    def test_a_row_id_disagreeing_with_its_keys_is_refused(self):
        fx = Fixture()
        _extract, predictions = make_rows()
        predictions[0]["row_id"] = "somethingelse:0"
        fx.prediction_rows = predictions
        fx.build()
        self.addCleanup(fx.close)
        with self.assertRaises(ev.EvaluateError) as cm:
            fx.run()
        self.assertEqual(cm.exception.error_class, "row_set_rejected")

    def test_predicting_a_row_outside_the_extract_is_refused(self):
        fx = Fixture()
        _extract, predictions = make_rows()
        predictions.append(Row(task_id="ghost", run_id=0, row_id="ghost:0",
                               p50=1.0, p90_raw=2.0))
        fx.prediction_rows = predictions
        fx.build()
        self.addCleanup(fx.close)
        with self.assertRaises(ev.EvaluateError) as cm:
            fx.run()
        self.assertEqual(cm.exception.error_class, "row_set_rejected")

    def test_a_day_set_other_than_the_derived_one_is_refused(self):
        """The days come from the extract's `as_of_date`, not from the candidate.

        The first version only required CONTIGUITY and recorded whether the block
        was the most recent one. That accepted an easier earlier holdout with
        `is_tail: false` sitting in the verdict where nothing gated on it.
        """
        fx = Fixture(holdout_days=2, days_required=1)
        _extract, predictions = make_rows()
        middle = {row["task_id"] for row in _extract
                  if row["pending_at"].date().isoformat() == HOLDOUT[1]}
        fx.prediction_rows = [p for p in predictions
                              if p["task_id"] not in middle]
        fx.build()
        self.addCleanup(fx.close)
        with self.assertRaises(ev.EvaluateError) as cm:
            fx.run()
        self.assertEqual(cm.exception.error_class, "row_set_rejected")
        self.assertIn("not the candidate's to choose", str(cm.exception))

    def test_an_earlier_holdout_block_is_refused_end_to_end(self):
        # The exact vector: a contiguous block, just not the required one.
        fx = Fixture(holdout_days=2, days_required=1)
        extract, _predictions = make_rows()
        # Predict the two TRAINING days instead of the holdout: contiguous,
        # complete within each day, and not the window the contract describes.
        wanted = [r for r in extract
                  if r["pending_at"].date().isoformat() in TRAINING]
        fx.prediction_rows = [
            Row(task_id=r["task_id"], run_id=0,
                row_id=f"{r['task_id']}:0", p50=r["y_true"],
                p90_raw=r["y_true"] * 1.5) for r in wanted]
        fx.build()
        self.addCleanup(fx.close)
        with self.assertRaises(ev.EvaluateError) as cm:
            fx.run()
        self.assertEqual(cm.exception.error_class, "row_set_rejected")
        self.assertIn(TRAINING[0], str(cm.exception))


class TestThePredictionContract(EvaluateCase):
    """Design §4.6, declared in `qfd.Runner.PREDICTION_COLUMNS` and enforced
    here, because `qfd` is stdlib-only and cannot read Parquet."""

    def test_an_extra_column_is_refused_by_name(self):
        self.fx.write_predictions(
            extra={"note": ["x"] * len(self.fx.prediction_rows)})
        e = self.refusal()
        self.assertIn("note", str(e))

    def test_a_missing_column_is_refused_by_name(self):
        self.fx.write_predictions(columns=("task_id", "run_id", "row_id",
                                           "p50"))
        self.assertIn("p90_raw", str(self.refusal()))

    def test_a_null_is_refused(self):
        rows = [dict(r) for r in self.fx.prediction_rows]
        rows[0]["p50"] = None
        self.fx.write_predictions(rows)
        self.assertIn("null", str(self.refusal()))

    def test_a_nan_prediction_is_refused(self):
        rows = [dict(r) for r in self.fx.prediction_rows]
        rows[0]["p50"] = float("nan")
        self.fx.write_predictions(rows)
        self.assertIn("non-finite", str(self.refusal()))

    def test_an_infinite_prediction_is_refused(self):
        rows = [dict(r) for r in self.fx.prediction_rows]
        rows[0]["p90_raw"] = float("inf")
        self.fx.write_predictions(rows)
        self.assertIn("non-finite", str(self.refusal()))

    def test_an_int64_run_id_is_accepted(self):
        # THE TYPE RULE THAT MUST NOT BE EXACT. §4.6 freezes `int32`, and a
        # candidate writing this file from pandas gets `int64` by default, so
        # exact equality would be a contract the usual tool cannot satisfy.
        self.fx.write_predictions(run_id_type=pyarrow.int64())
        self.assertEqual(self.fx.run()["verdict"], "go")

    def test_a_string_where_a_number_belongs_is_refused(self):
        rows = [dict(r) for r in self.fx.prediction_rows]
        inbox = os.path.dirname(self.fx.predictions_path)
        table = pyarrow.table({
            "task_id": pyarrow.array([r["task_id"] for r in rows]),
            "run_id": pyarrow.array([r["run_id"] for r in rows],
                                    pyarrow.int32()),
            "row_id": pyarrow.array([r["row_id"] for r in rows]),
            "p50": pyarrow.array([str(r["p50"]) for r in rows]),
            "p90_raw": pyarrow.array([r["p90_raw"] for r in rows],
                                     pyarrow.float64()),
        })
        pyarrow.parquet.write_table(
            table, os.path.join(inbox, ev.PREDICTIONS_NAME))
        self.assertIn("numeric", str(self.refusal()))

    def test_an_empty_prediction_set_is_refused(self):
        self.fx.write_predictions([])
        self.assertIn("no rows", str(self.refusal()))

    def test_a_non_parquet_file_is_refused_not_crashed(self):
        with open(self.fx.predictions_path, "wb") as fh:
            fh.write(b"not parquet at all")
        e = self.refusal()
        self.assertIn("not readable Parquet", str(e))
        self.assertIsInstance(e, ValueError)      # so the service relays it

    def test_the_ceiling_matches_its_stated_budget(self):
        """The ceiling is what bounds this module's memory, so the arithmetic
        beside it has to be the arithmetic. The first value was 20_000_000 --
        about 10.7 GB at the measured 536 MB per 1M rows, on a unit with
        `MemoryMax=4G`, reached BEFORE validation finishes. A ceiling above the
        memory limit is not a ceiling."""
        implied = (ev.MAX_PREDICTION_ROWS / 1_000_000
                   * ev.MAX_PREDICTION_MB_PER_1M_ROWS)
        self.assertLessEqual(implied, ev.MAX_PREDICTION_BUDGET_MB)
        # And the budget has to leave room on the unit it runs on.
        with open(os.path.join(EVALUATOR, "qf-eval.service")) as fh:
            unit = fh.read()
        limit = re.search(r"^MemoryMax=(\d+)G", unit, re.M)
        self.assertIsNotNone(limit, "the unit declares no MemoryMax")
        self.assertLess(ev.MAX_PREDICTION_BUDGET_MB,
                        int(limit.group(1)) * 1024 / 2,
                        "the prediction set may not claim half the unit's"
                        " memory: the extract scan and the baseline map run"
                        " alongside it")

    def test_the_row_ceiling_is_read_from_metadata(self):
        # A ceiling enforced after the read is not a ceiling.
        source = srcscan.code_only(
            open(os.path.join(EVALUATOR, "evaluate.py")).read())
        head = source.split("names = list(")[0]
        self.assertIn("MAX_PREDICTION_ROWS", head)
        self.assertIn("pf.metadata.num_rows", head)


class TestThePopulationAccounting(EvaluateCase):
    def test_a_row_outside_the_slice_is_counted_and_not_scored(self):
        fx = Fixture()
        extract, predictions = make_rows()
        # An `exception` row on a holdout day, predicted. It is outside the
        # contract's population, so completeness does not require it and the
        # metrics must not include it.
        victim = [r for r in extract
                  if r["pending_at"].date().isoformat() == HOLDOUT[0]][0]
        victim["reason_resolved"] = "exception"
        fx.extract_rows = extract
        fx.prediction_rows = predictions
        fx.build()
        self.addCleanup(fx.close)
        fx.run()
        doc = fx.verdict_document()
        self.assertEqual(doc["rows"]["out_of_slice_n"], 1)
        self.assertEqual(doc["rows"]["baseline_missing_n"], 0)
        self.assertEqual(doc["rows"]["scored_n"],
                         doc["rows"]["predicted_n"] - 1)

    def test_a_row_the_baseline_lacks_is_counted_and_dropped(self):
        fx = Fixture()
        extract, predictions = make_rows()
        fx.extract_rows = extract
        fx.prediction_rows = predictions
        fx.write_extract()
        p50_column, p90_column = ev.BASELINE_COLUMNS[fx.target]
        fx.baseline_rows = [
            {"task_id": r["task_id"], "run_id": r["run_id"],
             "pending_at": r["pending_at"].isoformat(),
             p50_column: (None if r["task_id"] == extract[-1]["task_id"]
                          else r["y_true"] * 3.0),
             p90_column: r["y_true"] * 4.0}
            for r in extract]
        fx.write_baseline()
        fx.write_contract()
        fx.write_predictions()
        self.addCleanup(fx.close)
        fx.run()
        doc = fx.verdict_document()
        self.assertEqual(doc["rows"]["baseline_missing_n"], 1)
        self.assertEqual(doc["rows"]["out_of_slice_n"], 0)
        # THE POINT: both sides still score the same rows.
        self.assertEqual(doc["model"]["aggregate"]["mae"]["eligible_n"],
                         doc["baseline"]["aggregate"]["mae"]["eligible_n"])

    def test_the_two_exclusions_do_not_double_count(self):
        # A row that is BOTH out of slice and missing from the baseline belongs
        # to one count. The first version subtracted one from the other.
        fx = Fixture()
        extract, predictions = make_rows()
        victim = [r for r in extract
                  if r["pending_at"].date().isoformat() == HOLDOUT[0]][0]
        victim["reason_resolved"] = "exception"
        fx.extract_rows = extract
        fx.prediction_rows = predictions
        fx.write_extract()
        p50_column, p90_column = ev.BASELINE_COLUMNS[fx.target]
        fx.baseline_rows = [
            {"task_id": r["task_id"], "run_id": r["run_id"],
             "pending_at": r["pending_at"].isoformat(),
             p50_column: (None if r["task_id"] == victim["task_id"]
                          else r["y_true"] * 3.0),
             p90_column: r["y_true"] * 4.0}
            for r in extract]
        fx.write_baseline()
        fx.write_contract()
        fx.write_predictions()
        self.addCleanup(fx.close)
        fx.run()
        doc = fx.verdict_document()
        self.assertEqual(doc["rows"]["out_of_slice_n"], 1)
        self.assertEqual(doc["rows"]["baseline_missing_n"], 0)
        self.assertEqual(doc["rows"]["scored_n"]
                         + doc["rows"]["out_of_slice_n"]
                         + doc["rows"]["baseline_missing_n"],
                         doc["rows"]["predicted_n"])

    def test_a_null_y_true_row_is_not_required_to_be_predicted(self):
        fx = Fixture()
        extract, predictions = make_rows()
        victim = [r for r in extract
                  if r["pending_at"].date().isoformat() == HOLDOUT[1]][0]
        victim["y_true"] = None
        fx.extract_rows = extract
        fx.prediction_rows = [p for p in predictions
                              if p["task_id"] != victim["task_id"]]
        fx.build()
        self.addCleanup(fx.close)
        self.assertEqual(fx.run()["verdict"], "go")

    def test_a_day_that_loses_every_row_is_refused(self):
        # A 2-of-3 rule applied to 2 days is not the rule that was agreed.
        fx = Fixture()
        extract, predictions = make_rows()
        for row in extract:
            if row["pending_at"].date().isoformat() == HOLDOUT[2]:
                row["reason_resolved"] = "exception"
        fx.extract_rows = extract
        fx.prediction_rows = predictions
        fx.build()
        self.addCleanup(fx.close)
        with self.assertRaises(ev.EvaluateError) as cm:
            fx.run()
        self.assertIn(HOLDOUT[2], str(cm.exception))


class TestPublicationIsIdempotent(EvaluateCase):
    def test_re_evaluating_the_same_inputs_reuses_the_verdict(self):
        first = self.fx.run()
        before = ev.file_digest(os.path.join(self.fx.eval_dir, self.fx.run_id,
                                             "out", ev.EVAL_NAME))
        second = self.fx.run()
        self.assertTrue(second["reused"])
        self.assertEqual(second["verdict"], first["verdict"])
        self.assertEqual(second["eval_hash"], first["eval_hash"])
        self.assertEqual(before, ev.file_digest(os.path.join(
            self.fx.eval_dir, self.fx.run_id, "out", ev.EVAL_NAME)))

    def test_different_inputs_under_one_run_id_are_refused(self):
        self.fx.run()
        rows = [dict(r) for r in self.fx.prediction_rows]
        rows[0]["p50"] = rows[0]["p50"] * 9
        self.fx.write_predictions(rows)
        with self.assertRaises(ev.EvaluateError) as cm:
            self.fx.run()
        self.assertEqual(cm.exception.error_class, "verdict_already_recorded")

    def test_a_missing_per_row_file_is_not_reusable(self):
        """Reuse returns somebody else's numbers as this run's answer, so what
        makes those numbers checkable has to still hold. Without this, deleting
        `eval.parquet` still returned `reused: true` -- a verdict with no
        evidence behind it, reported as a success."""
        self.fx.run()
        os.unlink(os.path.join(self.fx.eval_dir, self.fx.run_id, "out",
                               ev.EVAL_NAME))
        with self.assertRaises(ev.EvaluateError) as cm:
            self.fx.run()
        self.assertEqual(cm.exception.error_class, "eval_rows_missing")

    def test_an_altered_per_row_file_is_not_reusable(self):
        self.fx.run()
        path = os.path.join(self.fx.eval_dir, self.fx.run_id, "out",
                            ev.EVAL_NAME)
        with open(path, "ab") as fh:
            fh.write(b"\x00")
        with self.assertRaises(ev.EvaluateError) as cm:
            self.fx.run()
        self.assertEqual(cm.exception.error_class, "eval_rows_altered")

    def test_an_edited_verdict_body_is_not_reusable(self):
        # The document carries its own content key, so an edit that leaves the
        # inputs alone is still detectable -- and a flattering `verdict: go`
        # pasted into a `no-go` document is exactly that edit.
        self.fx.run()
        path = os.path.join(self.fx.eval_dir, self.fx.run_id, "out",
                            ev.VERDICT_NAME)
        with open(path) as fh:
            doc = json.load(fh)
        before = doc["model"]["aggregate"]["mae"]["sum_abs_error"]
        self.assertGreater(before, 0.0,
                           "the fixture must not make this edit a no-op")
        doc["model"]["aggregate"]["mae"]["sum_abs_error"] = 0.0
        with open(path, "w") as fh:
            json.dump(doc, fh)
        with self.assertRaises(ev.EvaluateError) as cm:
            self.fx.run()
        self.assertEqual(cm.exception.error_class, "verdict_body_altered")

    def test_the_identity_excludes_the_derived_digest(self):
        # `eval_sha256` is derived from a write that has not happened when the
        # comparison is made. The first version included it, so every
        # re-evaluation of identical inputs refused itself as a conflict -- and
        # the test above is what says so.
        self.assertNotIn("eval_sha256", ev.IDENTITY_INPUTS)


class TestTheStreamingProperty(EvaluateCase):
    def test_only_the_claimed_days_are_returned(self):
        """The extract covers five days; the scan must return the three claimed.
        This is the observable half of "memory is independent of the window": the
        row set the property is checked against is holdout-sized."""
        rows = ev.scan_runs(
            os.path.join(self.fx.extract_dir, "runs.parquet"),
            target_column="wait_duration_s", slice_values=("completed",),
            wanted_keys=np.array([f"{r['task_id']}:0"
                                  for r in self.fx.prediction_rows], dtype=str))
        self.assertEqual(sorted(set(rows["day"].tolist())), list(HOLDOUT))
        self.assertEqual(rows["row_id"].size,
                         len(HOLDOUT) * len(BUCKET_TRUE) * PER_BUCKET)

    def test_the_day_set_covers_the_whole_window_not_the_reduction(self):
        """The reduced row set is holdout-sized; `available_days` is not.

        This is the assertion that caught `check_day_block` being vacuous: with
        the day set derived from the reduction, the claimed days were a
        contiguous block of themselves and `is_tail` was always true.
        """
        rows = ev.scan_runs(
            os.path.join(self.fx.extract_dir, "runs.parquet"),
            target_column="wait_duration_s", slice_values=("completed",),
            wanted_keys=np.array([f"{r['task_id']}:0"
                                  for r in self.fx.prediction_rows], dtype=str))
        self.assertEqual(rows["available_days"],
                         sorted(TRAINING + HOLDOUT))
        self.assertEqual(sorted(set(rows["day"].tolist())), list(HOLDOUT))

    def test_the_scan_does_not_read_the_whole_table_at_once(self):
        source = srcscan.code_only(
            open(os.path.join(EVALUATOR, "evaluate.py")).read())
        # `iter_batches`, never `.read()`, inside the scan.
        scan = source.split("def scan_runs")[1].split("\ndef ")[0]
        self.assertIn("iter_batches", scan)
        self.assertNotIn("pf.read(", scan)

    def test_no_predicted_row_in_the_extract_is_refused(self):
        rows = [dict(r) for r in self.fx.prediction_rows]
        for row in rows:
            row["task_id"] = "absent" + row["task_id"]
            row["row_id"] = f"{row['task_id']}:0"
        self.fx.write_predictions(rows)
        self.assertIn("different data", str(self.refusal()))


class TestItJudgesAndDoesNotAct(unittest.TestCase):
    """D27. A judge that could act on its own finding is not a judge."""

    def setUp(self):
        with open(os.path.join(EVALUATOR, "evaluate.py")) as fh:
            self.code = srcscan.code_only(fh.read())

    def test_nothing_writes_outside_the_eval_directory(self):
        # TOKENISED, not line-stripped: this file's own docstring names the paths
        # it must not write to, and a `#`-only scan matched its own prose. That
        # was the ninth instance and `srcscan` is its fix.
        for forbidden in ("trainer/data/models", "docker", "psycopg",
                          "DATABASE_URL"):
            self.assertNotIn(forbidden, self.code, forbidden)

    def test_it_writes_only_under_the_eval_dir(self):
        for call in ("_atomic_write_bytes", "_atomic_write_table"):
            self.assertIn(call, self.code)
        # Every write target is built from `cfg.eval_dir`.
        self.assertIn("out_dir = os.path.join(run_dir,", self.code)
        self.assertIn("run_dir = os.path.join(cfg.eval_dir,", self.code)

    def test_no_path_comes_from_the_request(self):
        """Every request field that reaches a path must be one of the three
        regex-validated identifiers.

        Asserted STRUCTURALLY rather than by spelling: `code_only` blanks string
        literals, so `req["run_id"]` is blanked by design and a test looking for
        it was asserting against the scan's own contract. The raw source is the
        right input for a subscript key -- and the direction that matters here is
        the whitelist, which fails closed when a new field appears.
        """
        with open(os.path.join(EVALUATOR, "evaluate.py")) as fh:
            raw = fh.read()
        allowed = re.compile(
            r'req\["(run_id|request_hash|baseline_hash)"\]')
        joins = re.findall(r"os\.path\.join\(([^\n]*)", raw)
        self.assertTrue(joins)
        for call in joins:
            if "req[" in call or "req.get" in call:
                self.assertRegex(call, allowed,
                                 f"a path is built from an unvalidated request"
                                 f" field: {call}")

    def test_the_identifiers_that_reach_a_path_are_all_pattern_matched(self):
        # The whitelist above is only worth anything if `request.py` matches
        # each of those three against a pattern rather than merely typing them.
        with open(os.path.join(EVALUATOR, "request.py")) as fh:
            request_source = fh.read()
        self.assertIn("_RUN_ID_RE.match(run_id)", request_source)
        self.assertIn('_hash(raw["request_hash"]', request_source)
        self.assertIn("_hash(baseline,", request_source)


class TestTheSuiteRunsUnderTheRealClosure(unittest.TestCase):
    def test_the_venv_has_pyarrow_and_not_pandas(self):
        """D26: the per-row single pass is what makes this a different route
        from the trainer's, and pandas being absent is what keeps it one."""
        python = os.path.join(EVALUATOR, "env", ".venv", "bin", "python")
        if not os.path.exists(python):
            self.skipTest("the evaluator venv is not built here")
        p = subprocess.run(
            [python, "-c", "import pyarrow, numpy; import pandas"],
            capture_output=True, text=True, timeout=120)
        self.assertNotEqual(p.returncode, 0,
                            "pandas is importable in the evaluator's closure")
        self.assertIn("pandas", p.stderr)




class TestTheNcSuiteAgreesWithThisModule(EvaluateCase):
    """NC11 on the host and this module have to agree about two things: that its
    mutation snippets actually mutate, and that the tokens it greps for are ones
    the system emits.

    THE GROUP WAS RESTRUCTURED AFTER A REVIEW, and the history is the point. It
    used to mutate `<run>/out/predictions.parquet` and expect `row_set_rejected`.
    That worked because the relay staged from `out/` -- an un-digested directory
    whose bytes can change after a run finishes -- so the control passed BECAUSE
    OF the defect it should have found. Post-hoc mutation now hits the digest
    binding first (`evaluate_input_missing`, raised by `qfd`), which is correct,
    and means the row-set property needs a candidate that legitimately emits a
    bad set rather than a file edited behind one.
    """

    SUITE = os.path.join(HOST, "nc-suite-phase2.sh")

    def nc11(self):
        with open(self.SUITE) as fh:
            body = fh.read()
        return body[body.index("nc11() {"):body.index("\nnc10() {")]

    def snippets(self):
        """The MUTATION snippets, which are the ones that write the file back.

        NC11 also carries a verification snippet (clause (e) recomputes the
        verdict's own hash), and a regex over every `-c` body picked it up and
        fed it a Parquet file, which is how this test first failed. Selected by
        what they DO rather than by where they sit, so reordering the clauses
        does not silently change the set.
        """
        bodies = re.findall(r'"\$py" -c \'\n(.*?)\n\' ', self.nc11(), re.S)
        found = [b for b in bodies if "pq.write_table" in b]
        # A FLOOR, not an exact count. Zero would mean the extraction stopped
        # working and every subTest below would vacuously pass.
        self.assertGreaterEqual(len(found), 2,
                                f"NC11's mutation snippets were not found"
                                f" ({len(bodies)} script(s) seen)")
        return found

    def test_every_mutation_snippet_actually_mutates(self):
        for i, source in enumerate(self.snippets()):
            with self.subTest(snippet=i):
                fx = Fixture().build()
                self.addCleanup(fx.close)
                before = ev.file_digest(fx.predictions_path)
                script = os.path.join(fx.tmp, f"snip{i}.py")
                with open(script, "w") as fh:
                    fh.write(source + "\n")
                p = subprocess.run(
                    [os.path.join(EVALUATOR, "env", ".venv", "bin", "python"),
                     script, fx.predictions_path],
                    capture_output=True, text=True, timeout=180)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertNotEqual(before, ev.file_digest(fx.predictions_path),
                                    f"snippet {i} did not change the file")

    def test_a_dropped_row_is_refused_by_this_module_too(self):
        """The suite's mutation is caught by `qfd`'s digest check before this
        module sees it. Run directly, it must ALSO be refused here -- otherwise
        the two layers disagree about what an unscorable set is, and removing the
        outer one silently would leave nothing."""
        source = self.snippets()[0]
        script = os.path.join(self.fx.tmp, "drop.py")
        with open(script, "w") as fh:
            fh.write(source + "\n")
        p = subprocess.run(
            [os.path.join(EVALUATOR, "env", ".venv", "bin", "python"),
             script, self.fx.predictions_path],
            capture_output=True, text=True, timeout=180)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with self.assertRaises(ev.EvaluateError) as cm:
            # The declared digest is refreshed, so the digest control is out of
            # the way and the ROW SET is what is under test.
            ev.evaluate(self.fx.cfg, self.fx.req(), "fixture.json")
        self.assertEqual(cm.exception.error_class, "row_set_rejected")

    def test_every_class_the_suite_matches_is_one_something_emits(self):
        nc11 = self.nc11()
        with open(os.path.join(HOST, "dispatcher", "qfd.py")) as fh:
            qfd_source = fh.read()
        with open(os.path.join(EVALUATOR, "evaluate.py")) as fh:
            eval_source = fh.read()
        pattern = re.search(r'_ERROR_CLASS_RE = re\.compile\(r"([^"]+)"\)',
                            qfd_source)
        self.assertIsNotNone(pattern)
        # The tokens NC11 asserts on, taken from the clause bodies rather than
        # listed here, so adding a clause with a typo is caught.
        #
        # TWO SHAPES, because the clause bodies have had two. NC11 used to
        # compare against the literal `"FAILED <class> -"`; the suite then moved
        # that comparison into `nc11_refused_as "$result" <class>`, and this
        # extraction went to zero -- which is exactly what the assertion below
        # exists to catch, and did. Both are matched now so that reverting either
        # way does not silently empty the check again.
        matched = set(re.findall(r'"FAILED ([a-z][a-z0-9_]+) ', nc11))
        matched |= set(re.findall(
            r'nc11_refused_as\s+"\$\w+"\s+([a-z][a-z0-9_]+)', nc11))
        self.assertTrue(matched, "NC11 matches on no error class at all")
        for klass in sorted(matched):
            with self.subTest(error_class=klass):
                self.assertRegex(klass, pattern.group(1))
                emitted = (f'"{klass}"' in qfd_source
                           or f'"{klass}"' in eval_source)
                self.assertTrue(emitted,
                                f"nothing emits {klass}, so the clause matching"
                                f" it can never pass")

    def test_the_classes_this_module_emits_are_all_valid_tokens(self):
        with open(os.path.join(EVALUATOR, "evaluate.py")) as fh:
            classes = set(re.findall(r'error_class="([^"]+)"', fh.read()))
        with open(os.path.join(HOST, "dispatcher", "qfd.py")) as fh:
            pattern = re.search(r'_ERROR_CLASS_RE = re\.compile\(r"([^"]+)"\)',
                                fh.read())
        self.assertGreaterEqual(len(classes), 5)
        for klass in sorted(classes):
            with self.subTest(error_class=klass):
                # A class `qfd` would reject becomes an opaque default, so the
                # refusal this module wrote to be read would not reach the record.
                self.assertRegex(klass, pattern.group(1))


if __name__ == "__main__":
    # AT THE END. Two copies of this had drifted into the middle of the file as
    # classes were appended below them, so `python tests/test_evaluate.py` ran
    # only the classes above the first one and reported OK. `discover` imports
    # the whole module, so the gap was invisible.
    unittest.main()
