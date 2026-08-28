"""Phase 2b-1 Task 3: the extraction itself.

No database and no `pyarrow` here, by design: the orchestration is stdlib-only
and takes its session and its writer as arguments, so every ordering rule, every
refusal and every staging guarantee is exercised against a fake that can be made
to misbehave on demand. The live behaviour belongs to the privileged tasks.

**THE FAKE USES PRODUCTION TYPES.** Revision 1 of this file returned ISO strings
from the fake session, and psycopg returns `datetime`/`date`. That one divergence
hid a crash in the watermark merge that a production-shaped row would have found
immediately. A fake whose types are more convenient than the real thing's is not
a simplification, it is a hole in the coverage.

What is being defended, in order of how badly it fails:

  * D19 -- all six files come from ONE `REPEATABLE READ` snapshot.
  * D20 -- a published extract is immutable and there is exactly ONE artifact per
    `request_hash`, so a recorded result cannot silently acquire a different
    input.
  * D16 -- the extractor validates the request ITSELF. `qfd` is trusted code and
    the point of this domain is that its trust is not required.
  * D23 -- one extraction at a time, and bounded memory.
"""
import datetime
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "shared"))

import extract_spec                                            # noqa: E402
import extractor                                               # noqa: E402
import inventory                                               # noqa: E402

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 8, 5, tzinfo=UTC)


def ts(y, m, d, hh=0, mm=0):
    """A psycopg-shaped timestamp: an aware datetime, not a string."""
    return datetime.datetime(y, m, d, hh, mm, tzinfo=UTC)


# Production types throughout: `datetime` for timestamps, `date` for
# `sample_date`, `None` for a still-pending run's `started_at`.
ROWS = {
    "runs": [
        ("t1", 0, ts(2026, 6, 1), ts(2026, 6, 1, 0, 5), ts(2026, 6, 1, 0, 40),
         "completed", 300, 2100, "normal", 4, "tq/a", "sched", "name", "norm",
         3600, "fam", "{}"),
        ("t2", 0, ts(2026, 7, 31, 23, 0), None, ts(2026, 7, 31, 23, 30),
         "failed", 120, 1800, "high", 1, "tq/b", "sched", "name", "norm",
         3600, "fam", "{}"),
    ],
    "worker_counts": [("tq/a", ts(2026, 6, 1), 3, 2, 5),
                      ("tq/a", ts(2026, 7, 31, 22, 0), 4, 3, 6)],
    "worker_pools": [("tq/a", "gcp", "fxci-level1-gcp")],
    "throughput_runs": [
        ("tq/a", ts(2026, 6, 1, 0, 5), ts(2026, 6, 1, 0, 40), 300, 2100)],
    "qctx_runs": [
        ("t1", 0, ts(2026, 6, 1), ts(2026, 6, 1, 0, 5), ts(2026, 6, 1, 0, 40),
         "normal", "tq/a", "fam")],
    "daily_health": [
        (datetime.date(2026, 6, 1), False, False, False, False, False, False,
         False, False, False, False)],
}


class FakeSession:
    def __init__(self, *, write_refused_by="read_only", parallel="0",
                 read_only="on", rows=None, raise_on=None, empty=(),
                 batch_size=1):
        self.calls = []
        self.write_refused_by = write_refused_by
        self.settings = {"max_parallel_workers_per_gather": parallel,
                         "transaction_read_only": read_only}
        self.rows = dict(ROWS if rows is None else rows)
        self.raise_on = raise_on
        self.empty = set(empty)
        self.batch_size = batch_size
        self.closed = False

    def setting(self, name):
        self.calls.append(("setting", name))
        return self.settings[name]

    def attempt_write(self):
        """Mirrors the real contract: a REASON, or None if the write succeeded."""
        self.calls.append(("attempt_write", None))
        return self.write_refused_by

    def begin_snapshot(self):
        self.calls.append(("begin_snapshot", None))
        return ("2026-08-05T09:00:00Z", "1234:1240:1237")

    def query(self, name, sql, params):
        self.calls.append(("query", name))
        if self.raise_on == name:
            raise RuntimeError(f"boom in {name}")
        rows = [] if name in self.empty else list(self.rows[name])
        columns = inventory.DATASETS[name].columns

        def batches():
            for i in range(0, len(rows), self.batch_size):
                yield rows[i:i + self.batch_size]

        return columns, batches()

    def close(self):
        self.closed = True
        self.calls.append(("close", None))

    def ops(self, kind):
        return [n for k, n in self.calls if k == kind]


class RecordingWriter:
    """A sink factory, so rows are streamed rather than materialised."""

    def __init__(self, fail_on=None):
        self.opened = []
        self.max_batch = 0
        self.fail_on = fail_on

    def open(self, path, columns):
        if self.fail_on and self.fail_on in os.path.basename(path):
            raise OSError(f"cannot write {path}")
        self.opened.append(path)
        return _Sink(path, columns, self)


class _Sink:
    def __init__(self, path, columns, parent):
        self.path = path
        self.columns = list(columns)
        self.parent = parent
        self.rows = []

    def write(self, batch):
        self.parent.max_batch = max(self.parent.max_batch, len(batch))
        self.rows.extend(batch)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if exc[0] is None:
            payload = json.dumps({"columns": self.columns,
                                  "rows": [list(r) for r in self.rows]},
                                 sort_keys=True, default=str).encode()
            with open(self.path, "wb") as fh:
                fh.write(payload)
        return False


RAW = {"schema": 1, "target": "wait_time",
       "train_start": "2026-06-01T00:00:00Z",
       "as_of_date": "2026-08-01T00:00:00Z", "lookback_days": 30}


def raw(**over):
    out = dict(RAW)
    out.update(over)
    return out


class ExtractorCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.session = FakeSession()
        self.writer = RecordingWriter()
        self.free_mb = 100_000

    def make(self, **over):
        kw = dict(
            root=self.root,
            session_factory=lambda: self.session,
            writer=self.writer,
            free_disk_mb=lambda path: self.free_mb,
            clock=lambda: NOW,
            settlement_lag_s=48 * 3600,
        )
        kw.update(over)
        return extractor.Extractor(**kw)

    def published(self, manifest):
        return os.path.join(self.root, manifest["request_hash"])


class TestTheExtractorValidatesForItself(ExtractorCase):
    """D16: `qfd` validates so a bad request is refused cheaply and legibly;
    the extractor validates because a caller is a caller. Revision 1 accepted a
    pre-validated mapping and hashed whatever it was given, which meant every
    bound in Task 1 -- including the 120-day scan ceiling -- was enforced only
    by the caller that the boundary exists to distrust."""

    def test_a_raw_request_is_accepted_and_normalised(self):
        manifest = self.make().run(raw())
        self.assertEqual(manifest["request"]["target_column"],
                         "wait_duration_s")
        self.assertEqual(manifest["request"]["generation"], 1)

    def test_an_invalid_request_is_refused_without_opening_a_session(self):
        for bad in (raw(target="p90"), raw(lookback_days=0),
                    raw(filters=["1=1"]),
                    raw(as_of_date="2026-08-01T06:00:00Z")):
            with self.subTest(request=bad):
                session = FakeSession()
                with self.assertRaises(extract_spec.ExtractSpecError):
                    self.make(session_factory=lambda: session).run(bad)
                self.assertEqual(session.calls, [])

    def test_forged_derived_bounds_are_refused_not_used(self):
        # The bypass: supply `ref_lower`/`window_lower` directly and the queries
        # would have used them. They are on the forbidden list, so a request
        # carrying them is refused by name.
        for field in ("ref_lower", "window_lower", "target_column"):
            with self.subTest(field=field):
                with self.assertRaises(extract_spec.ExtractSpecError) as cm:
                    self.make().run(raw(**{field: "2024-01-01T00:00:00Z"}))
                self.assertIn(field, str(cm.exception))

    def test_the_scan_ceiling_is_enforced_here_too(self):
        with self.assertRaises(extract_spec.ExtractSpecError) as cm:
            self.make().run(raw(train_start="2021-01-01T00:00:00Z"))
        self.assertIn(str(extract_spec.MAX_WINDOW_DAYS), str(cm.exception))

    def test_the_settlement_lag_is_the_extractors_own(self):
        # D17: the extractor holds the authoritative value. A caller cannot ask
        # for a shorter one, and the extractor's clock -- not the caller's -- is
        # what the window is measured against.
        strict = self.make(settlement_lag_s=30 * 24 * 3600)
        with self.assertRaises(extract_spec.ExtractSpecError) as cm:
            strict.run(raw())
        self.assertIn("settlement", str(cm.exception))

    def test_the_injected_clock_is_actually_used(self):
        # It was accepted and never read, which is how the lag check came to be
        # unenforced. A parameter nothing consults is a claim nothing backs.
        early = self.make(clock=lambda: datetime.datetime(2026, 8, 2,
                                                          tzinfo=UTC))
        with self.assertRaises(extract_spec.ExtractSpecError):
            early.run(raw())


class TestTheSnapshotDisciplineIsObserved(ExtractorCase):
    def test_all_six_datasets_are_queried(self):
        self.make().run(raw())
        self.assertEqual(set(self.session.ops("query")),
                         set(inventory.DATASETS))

    def test_the_snapshot_is_taken_before_the_first_query(self):
        self.make().run(raw())
        kinds = [k for k, _ in self.session.calls]
        self.assertLess(kinds.index("begin_snapshot"), kinds.index("query"))

    def test_the_transaction_is_begun_exactly_once(self):
        self.make().run(raw())
        self.assertEqual(self.session.calls.count(("begin_snapshot", None)), 1)

    def test_the_write_canary_runs_before_the_transaction(self):
        # A failed statement ABORTS a PostgreSQL transaction, so a deliberate
        # write must be attempted in its own transaction, before the snapshot.
        self.make().run(raw())
        kinds = [k for k, _ in self.session.calls]
        self.assertLess(kinds.index("attempt_write"),
                        kinds.index("begin_snapshot"))

    def test_a_write_that_SUCCEEDS_refuses_the_extraction(self):
        session = FakeSession(write_refused_by=None)
        with self.assertRaises(extractor.ExtractError) as cm:
            self.make(session_factory=lambda: session).run(raw())
        self.assertIn("succeeded", str(cm.exception).lower())

    def test_either_refusal_reason_is_accepted(self):
        # With a SELECT-only role a write is refused by PRIVILEGES, not by
        # read-onlyness, so insisting on one SQLSTATE would abort every
        # extraction on a correctly configured cluster. Both prove the thing
        # being asserted: this role cannot write.
        for reason in ("read_only", "insufficient_privilege"):
            with self.subTest(reason=reason):
                session = FakeSession(write_refused_by=reason)
                self.make(session_factory=lambda: session,
                          writer=RecordingWriter()).run(raw())
                shutil.rmtree(self.root)
                os.makedirs(self.root)

    def test_an_unexpected_canary_failure_is_not_read_as_a_refusal(self):
        # THE P2, and it went untested when it was first fixed: reverting the
        # check produced zero failures. A connection error, a missing table or a
        # statement timeout says nothing about whether this role can write, and
        # folding it into "refused" is how a control comes to pass for a reason
        # unrelated to what it asserts -- which is the failure mode the original
        # `except psycopg.Error: return False` had.
        for reason in ("unexpected sqlstate 08006: connection failure",
                       "unexpected sqlstate 42P01: no such table",
                       "unexpected sqlstate 57014: statement timeout",
                       "who knows"):
            with self.subTest(reason=reason):
                session = FakeSession(write_refused_by=reason)
                with self.assertRaises(extractor.ExtractError) as cm:
                    self.make(session_factory=lambda: session).run(raw())
                msg = str(cm.exception)
                self.assertIn("unexpected", msg.lower())
                # And it must say what it expected, or the operator is left
                # guessing which of two SQLSTATEs would have satisfied it.
                self.assertIn("read_only", msg)

    def test_an_unexpected_canary_failure_stops_before_any_query(self):
        session = FakeSession(write_refused_by="unexpected sqlstate 08006: x")
        with self.assertRaises(extractor.ExtractError):
            self.make(session_factory=lambda: session).run(raw())
        self.assertEqual(session.ops("query"), [])

    def test_transaction_read_only_is_checked_after_beginning(self):
        session = FakeSession(read_only="off")
        with self.assertRaises(extractor.ExtractError) as cm:
            self.make(session_factory=lambda: session).run(raw())
        self.assertIn("transaction_read_only", str(cm.exception))

    def test_parallel_workers_must_be_zero(self):
        session = FakeSession(parallel="4")
        with self.assertRaises(extractor.ExtractError) as cm:
            self.make(session_factory=lambda: session).run(raw())
        msg = str(cm.exception)
        self.assertIn("max_parallel_workers_per_gather", msg)
        self.assertIn("per process", msg)

    def test_the_parallel_setting_is_read_before_the_transaction_begins(self):
        # Ordering keeps the assertion honest: the session also SETs it
        # defensively, and a check after that would read its own answer.
        self.make().run(raw())
        kinds = [(k, n) for k, n in self.session.calls]
        read_at = kinds.index(("setting", "max_parallel_workers_per_gather"))
        began_at = [i for i, (k, _) in enumerate(kinds)
                    if k == "begin_snapshot"][0]
        self.assertLess(read_at, began_at)

    def test_the_parallel_check_happens_before_any_query(self):
        session = FakeSession(parallel="4")
        with self.assertRaises(extractor.ExtractError):
            self.make(session_factory=lambda: session).run(raw())
        self.assertEqual(session.ops("query"), [])

    def test_the_session_is_closed_even_when_a_query_raises(self):
        session = FakeSession(raise_on="qctx_runs")
        with self.assertRaises(Exception):
            self.make(session_factory=lambda: session).run(raw())
        self.assertTrue(session.closed)


class TestRowsAreStreamedNotMaterialised(ExtractorCase):
    """A `fetchall()` plus a per-column Python list plus a file-sized row group
    put several multiples of the extract's size in memory at once. With a 4 GiB
    output allowance on a host whose disk floor is already contested, that is a
    second resource bound nobody declared."""

    def test_the_extractor_never_holds_a_whole_dataset(self):
        session = FakeSession(batch_size=1)
        writer = RecordingWriter()
        self.make(session_factory=lambda: session, writer=writer).run(raw())
        # The largest batch handed to the sink is the session's batch size, not
        # the dataset size. `runs` has two rows; a materialising extractor would
        # show 2.
        self.assertEqual(writer.max_batch, 1)

    def test_row_counts_are_accumulated_from_batches(self):
        manifest = self.make(
            session_factory=lambda: FakeSession(batch_size=1)).run(raw())
        for name, entry in manifest["files"].items():
            with self.subTest(name=name):
                self.assertEqual(entry["rows"], len(ROWS[name]))

    def test_the_batch_size_does_not_change_the_result(self):
        first = self.make(
            session_factory=lambda: FakeSession(batch_size=1)).run(raw())
        shutil.rmtree(self.root)
        os.makedirs(self.root)
        second = self.make(
            session_factory=lambda: FakeSession(batch_size=100),
            writer=RecordingWriter()).run(raw())
        self.assertEqual(first["watermark"], second["watermark"])
        self.assertEqual({k: v["rows"] for k, v in first["files"].items()},
                         {k: v["rows"] for k, v in second["files"].items()})


class TestTheWatermarkHandlesProductionTypes(ExtractorCase):
    """THE P1. Revision 1 kept the first value native and compared later ones
    with `str(value)`, so the second batch raised
    `TypeError: '>' not supported between instances of 'str' and
    'datetime.datetime'`. The fake returned ISO strings, so nothing failed."""

    def test_datetimes_across_batches_do_not_crash(self):
        # `worker_counts` has two rows with real datetimes, and a batch size of
        # 1 forces the merge path that used to compare a str with a datetime.
        manifest = self.make(
            session_factory=lambda: FakeSession(batch_size=1)).run(raw())
        self.assertEqual(manifest["watermark"]["sampled_at"],
                         "2026-07-31T22:00:00+00:00")

    def test_the_maximum_is_computed_on_native_values(self):
        manifest = self.make(
            session_factory=lambda: FakeSession(batch_size=1)).run(raw())
        self.assertEqual(manifest["watermark"]["pending_at"],
                         "2026-07-31T23:00:00+00:00")
        self.assertEqual(manifest["watermark"]["resolved_at"],
                         "2026-07-31T23:30:00+00:00")

    def test_a_date_column_survives(self):
        manifest = self.make().run(raw())
        self.assertEqual(manifest["watermark"]["sample_date"], "2026-06-01")

    def test_nulls_are_ignored_rather_than_propagated(self):
        # `started_at` is NULL for a still-pending run. A max() that propagated
        # None would erase the watermark for the column, and a missing watermark
        # reads as "nothing was extracted" rather than "some rows are open".
        manifest = self.make().run(raw())
        self.assertIn("pending_at", manifest["watermark"])
        self.assertIn("resolved_at", manifest["watermark"])

    def test_every_value_in_the_manifest_is_a_string(self):
        # The manifest is JSON and is hashed; a datetime would not serialise and
        # a per-driver repr would make the hash depend on the driver.
        manifest = self.make().run(raw())
        for key, value in manifest["watermark"].items():
            with self.subTest(key=key):
                self.assertIsInstance(value, str)


class TestTheManifestDescribesWhatWasActuallyWritten(ExtractorCase):
    def test_the_digest_is_of_the_bytes_on_disk(self):
        manifest = self.make().run(raw())
        for name, entry in manifest["files"].items():
            with self.subTest(name=name):
                path = os.path.join(self.published(manifest),
                                    inventory.DATASETS[name].file)
                with open(path, "rb") as fh:
                    self.assertEqual(entry["sha256"],
                                     hashlib.sha256(fh.read()).hexdigest())

    def test_each_file_records_the_window_it_was_queried_with(self):
        manifest = self.make().run(raw())
        request = manifest["request"]
        self.assertEqual(manifest["files"]["runs"]["window"],
                         {"train_start": request["train_start"],
                          "as_of_date": request["as_of_date"]})
        self.assertEqual(manifest["files"]["worker_pools"]["window"], {})

    def test_the_manifest_records_the_settlement_lag_in_force(self):
        manifest = self.make(settlement_lag_s=7200).run(raw(
            as_of_date="2026-08-04T00:00:00Z",
            train_start="2026-07-05T00:00:00Z"))
        self.assertEqual(manifest["settlement_lag_s"], 7200)

    def test_the_manifest_records_the_snapshot_not_just_a_txid(self):
        manifest = self.make().run(raw())
        self.assertEqual(manifest["snapshot"], "1234:1240:1237")
        self.assertEqual(manifest["snapshot_start_ts"], "2026-08-05T09:00:00Z")

    def test_the_extract_hash_covers_the_manifest_but_not_itself(self):
        manifest = self.make().run(raw())
        body = {k: v for k, v in manifest.items() if k != "extract_hash"}
        expected = hashlib.sha256(json.dumps(
            body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(manifest["extract_hash"], expected)

    def test_the_manifest_is_on_disk_and_matches_what_was_returned(self):
        manifest = self.make().run(raw())
        with open(os.path.join(self.published(manifest),
                               "MANIFEST.json")) as fh:
            self.assertEqual(json.load(fh), manifest)


class TestAnEmptyDatasetIsARefusal(ExtractorCase):
    def test_an_empty_file_refuses_and_names_the_dataset(self):
        for name in ("runs", "worker_pools", "daily_health"):
            with self.subTest(name=name):
                session = FakeSession(empty={name})
                with self.assertRaises(extractor.ExtractError) as cm:
                    self.make(session_factory=lambda: session).run(raw())
                self.assertIn(name, str(cm.exception))

    def test_nothing_is_published_when_a_dataset_is_empty(self):
        session = FakeSession(empty={"daily_health"})
        with self.assertRaises(extractor.ExtractError):
            self.make(session_factory=lambda: session).run(raw())
        self.assertEqual(
            [d for d in os.listdir(self.root) if not d.startswith(".")], [])


class TestStagingIsCleanedUpOnEveryFailurePath(ExtractorCase):
    def _staging_entries(self):
        staging = os.path.join(self.root, ".staging")
        return os.listdir(staging) if os.path.isdir(staging) else []

    def test_a_raising_query_leaves_no_staging(self):
        session = FakeSession(raise_on="throughput_runs")
        with self.assertRaises(Exception):
            self.make(session_factory=lambda: session).run(raw())
        self.assertEqual(self._staging_entries(), [])

    def test_a_raising_writer_leaves_no_staging(self):
        with self.assertRaises(Exception):
            self.make(writer=RecordingWriter(fail_on="qctx")).run(raw())
        self.assertEqual(self._staging_entries(), [])

    def test_a_refusal_leaves_no_staging(self):
        session = FakeSession(empty={"runs"})
        with self.assertRaises(extractor.ExtractError):
            self.make(session_factory=lambda: session).run(raw())
        self.assertEqual(self._staging_entries(), [])

    def test_a_successful_run_leaves_no_staging(self):
        self.make().run(raw())
        self.assertEqual(self._staging_entries(), [])

    def test_the_published_directory_holds_exactly_the_seven_files(self):
        manifest = self.make().run(raw())
        expected = {ds.file for ds in inventory.DATASETS.values()}
        expected.add("MANIFEST.json")
        self.assertEqual(set(os.listdir(self.published(manifest))), expected)


class TestThereIsExactlyOneArtifactPerRequest(ExtractorCase):
    """D20's first-publication rule, and the reason publication is keyed by
    `request_hash` rather than by `extract_hash` with a side index.

    Revision 1 renamed staging to `<extract_hash>/` and THEN wrote an index
    entry. Two steps, so a crash between them left the artifact published and
    undiscoverable -- and the retry took a NEW snapshot, got a different
    `extract_hash`, and published a SECOND artifact for the same request. One
    atomic rename removes the window entirely: the artifact and its
    discoverability are the same act."""

    def test_the_artifact_is_named_by_the_request(self):
        manifest = self.make().run(raw())
        self.assertTrue(os.path.isdir(
            os.path.join(self.root, manifest["request_hash"])))

    def test_a_repeat_request_is_served_without_querying(self):
        first = self.make().run(raw())
        session = FakeSession()
        second = self.make(session_factory=lambda: session).run(raw())
        self.assertEqual(first, second)
        self.assertEqual(session.calls, [],
                         "a reuse hit opened a session, so it was not a hit")

    def test_a_second_run_after_the_data_moved_still_yields_one_artifact(self):
        # The fault that produced two directories for one request hash: publish,
        # then run again when the data has moved. Reuse must win, and the
        # published artifact must be the ORIGINAL one.
        manifest = self.make().run(raw())
        rows = dict(ROWS)
        rows["worker_counts"] = [("tq/a", ts(2026, 8, 1), 9, 9, 9)]
        again = self.make(
            session_factory=lambda: FakeSession(rows=rows),
            writer=RecordingWriter()).run(raw())
        self.assertEqual(again["extract_hash"], manifest["extract_hash"])
        published = [d for d in os.listdir(self.root)
                     if not d.startswith(".")]
        self.assertEqual(len(published), 1, published)

    def test_a_forced_second_extraction_is_refused_not_overwritten(self):
        manifest = self.make().run(raw())
        path = os.path.join(self.published(manifest), "runs.parquet")
        with open(path, "rb") as fh:
            before = fh.read()
        with self.assertRaises(extractor.ExtractError) as cm:
            self.make().run(raw(), force=True)
        self.assertIn("immutable", str(cm.exception).lower())
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_bumping_generation_publishes_a_separate_artifact(self):
        first = self.make().run(raw())
        second = self.make(writer=RecordingWriter()).run(raw(generation=2))
        self.assertNotEqual(first["request_hash"], second["request_hash"])
        self.assertTrue(os.path.isdir(self.published(first)))
        self.assertTrue(os.path.isdir(self.published(second)))

    def test_the_watermark_does_not_participate_in_the_reuse_decision(self):
        # Behavioural, not textual: rewrite the stored watermark to something
        # absurd and require that reuse still resolves without a session.
        manifest = self.make().run(raw())
        path = os.path.join(self.published(manifest), "MANIFEST.json")
        with open(path) as fh:
            stored = json.load(fh)
        stored["watermark"] = {"pending_at": "1999-01-01T00:00:00Z"}
        with open(path, "w") as fh:
            json.dump(stored, fh)
        session = FakeSession()
        again = self.make(session_factory=lambda: session).run(raw())
        self.assertEqual(session.calls, [])
        self.assertEqual(again["watermark"],
                         {"pending_at": "1999-01-01T00:00:00Z"})

    def test_reuse_is_a_lookup_not_a_scan(self):
        manifest = self.make().run(raw())
        self.assertEqual(
            extractor.published_dir(self.root, manifest["request_hash"]),
            self.published(manifest))
        self.assertIsNone(extractor.published_dir(self.root, "0" * 64))

    def test_a_directory_without_a_manifest_is_not_a_publication(self):
        # Only a complete artifact counts. A bare directory -- however it came to
        # exist -- must not be served as a reuse hit, or a caller would get an
        # extract with no files in it.
        os.makedirs(os.path.join(self.root, "a" * 64))
        self.assertIsNone(extractor.published_dir(self.root, "a" * 64))


class TestOnlyOneExtractionRunsAtATime(ExtractorCase):
    def test_a_second_concurrent_extraction_is_refused(self):
        first = self.make()
        with first.hold_the_lock():
            with self.assertRaises(extractor.ExtractError) as cm:
                self.make().run(raw())
        self.assertIn("already", str(cm.exception).lower())

    def test_the_lock_is_released_after_a_failure(self):
        session = FakeSession(empty={"runs"})
        with self.assertRaises(extractor.ExtractError):
            self.make(session_factory=lambda: session).run(raw())
        self.make().run(raw())


class TestAdmissionCoversEveryClaimOnTheDisk(ExtractorCase):
    def test_it_refuses_below_the_floor_and_names_the_three_components(self):
        with self.assertRaises(extractor.ExtractError) as cm:
            self.make(free_disk_mb=lambda p: 1000).run(raw())
        msg = str(cm.exception).lower()
        for token in ("floor", "temp", "output"):
            with self.subTest(token=token):
                self.assertIn(token, msg)

    def test_it_checks_before_opening_a_session(self):
        session = FakeSession()
        with self.assertRaises(extractor.ExtractError):
            self.make(free_disk_mb=lambda p: 1000,
                      session_factory=lambda: session).run(raw())
        self.assertEqual(session.calls, [])

    def test_the_requirement_is_the_sum_of_the_three(self):
        self.assertEqual(
            extractor.required_disk_mb(floor_mb=20480, temp_mb=20480,
                                       output_mb=4096),
            20480 + 20480 + 4096)


class TestTheRunIsLegibleInTheJournal(ExtractorCase):
    def test_it_logs_a_start_and_a_publish_with_a_duration(self):
        with self.assertLogs(extractor.log, level="INFO") as captured:
            manifest = self.make().run(raw())
        blob = "\n".join(captured.output)
        self.assertIn(manifest["request_hash"][:12], blob)
        self.assertIn(manifest["extract_hash"][:12], blob)
        self.assertRegex(blob, r"\d+\.\d+s|\d+s")

    def test_a_reuse_hit_says_so(self):
        self.make().run(raw())
        with self.assertLogs(extractor.log, level="INFO") as captured:
            self.make().run(raw())
        self.assertIn("reuse", "\n".join(captured.output).lower())


if __name__ == "__main__":
    unittest.main()
