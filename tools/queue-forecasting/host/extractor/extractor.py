"""The extraction itself: one snapshot, six files, a manifest. Task 3.

STDLIB ONLY, AND THAT IS THE POINT. The real work needs `psycopg` and
`pyarrow`, and both are reached through arguments -- a `session_factory` and a
`writer` -- rather than imported here. So every ordering rule, every refusal and
every staging guarantee in this module is exercised by tests that need no
database, no extractor environment, and no privileges. The concrete
implementations live in `pg.py` and `parquet_writer.py` and are wired in by the
service entry point (Task 4).

The five things this module exists to make true:

  * **One snapshot** (D19). Six files read from one `REPEATABLE READ` read-only
    transaction, or two of them can straddle a collector write and nothing in
    the record will show it.
  * **One artifact per request** (D20). Publication is a SINGLE atomic rename
    into a directory named by `request_hash`. Revision 1 renamed into
    `<extract_hash>/` and then wrote a side index, and a crash between the two
    left the artifact published but undiscoverable -- so the retry took a fresh
    snapshot and published a SECOND artifact for the same request. There is now
    no "between the two".
  * **The extractor validates for itself** (D16). `run` takes a RAW request and
    calls `extract_spec.validate` with its own clock and its own settlement lag.
    Revision 1 accepted a pre-validated mapping, which meant every bound in
    Task 1 -- including the scan ceiling -- was enforced only by the caller the
    boundary exists to distrust.
  * **Bounded memory** (D23). Rows arrive in batches and go straight to the
    sink; nothing here holds a dataset.
  * **Nothing partial under a real name.** Staging plus `rename()`.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import logging
import os
import shutil
import time

import extract_spec
import inventory

log = logging.getLogger("qf-extract")

STAGING = ".staging"
MANIFEST = "MANIFEST.json"

# The three claims on the same filesystem (D23). The floor is the dispatcher's
# and is not ours to spend; the temp allowance is what one PostgreSQL backend may
# spill; the output is this extract.
DEFAULT_FLOOR_MB = 20 * 1024
DEFAULT_TEMP_MB = 20 * 1024
DEFAULT_OUTPUT_MB = 4 * 1024

# Reasons `attempt_write` may give for a write having been refused. BOTH count,
# and the reason that is missing from this set is "no reason": a write that
# SUCCEEDS is the failure.
#
# `insufficient_privilege` is here because the role holds SELECT and nothing
# else, so on a correctly configured cluster a write is refused by the GRANT
# rather than by read-onlyness -- and the grant is the stronger of the two
# controls. Insisting on the read-only SQLSTATE alone would abort every
# extraction on exactly the hosts that are configured properly.
WRITE_REFUSAL_REASONS = frozenset({"read_only", "insufficient_privilege"})


class ExtractError(Exception):
    """A refusal. The message is shown to the caller and is expected to say what
    to do about it."""


def required_disk_mb(*, floor_mb=DEFAULT_FLOOR_MB, temp_mb=DEFAULT_TEMP_MB,
                     output_mb=DEFAULT_OUTPUT_MB):
    return floor_mb + temp_mb + output_mb


def published_dir(root, request_hash):
    """The published artifact directory for a request, or None.

    A LOOKUP, and deliberately unable to reach anything else: D20 demoted the
    watermark to provenance precisely because comparing it was the tempting and
    wrong way to decide reuse.

    A directory only counts once its manifest is there. The manifest is inside
    the atomically renamed staging directory, so its presence and the artifact's
    are the same fact -- but a directory created any other way must not be served
    as a hit, or a caller would receive an extract with no files in it.
    """
    path = os.path.join(root, request_hash)
    return path if os.path.isfile(os.path.join(path, MANIFEST)) else None


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def _stringify(value):
    """One representation for the manifest, whatever the driver returned.

    `isoformat()` for dates and datetimes, `str` otherwise. The manifest is JSON
    and is hashed, so a `datetime` would not serialise at all and a per-driver
    repr would make `extract_hash` depend on the driver version.
    """
    iso = getattr(value, "isoformat", None)
    return iso() if callable(iso) else str(value)


class _Watermark:
    """Running maxima over NATIVE values.

    Revision 1 stored the first value native and compared later ones against
    `str(value)`, which raised
    `TypeError: '>' not supported between instances of 'str' and
    'datetime.datetime'` on the second batch. The fake session returned ISO
    strings, so the suite never saw it. Compare natively, stringify once, at the
    end.

    Nulls are skipped rather than propagated: `started_at` is NULL for a
    still-pending run, and a maximum that became None would erase the watermark
    for the whole column -- which reads as "nothing was extracted" rather than
    "some rows are still open".
    """

    def __init__(self):
        self._max = {}

    def update(self, columns, watermark_columns, batch):
        for column in watermark_columns:
            index = columns.index(column)
            for row in batch:
                value = row[index]
                if value is None:
                    continue
                current = self._max.get(column)
                if current is None or value > current:
                    self._max[column] = value

    def as_manifest(self):
        return {k: _stringify(v) for k, v in sorted(self._max.items())}


class Extractor:
    def __init__(self, *, root, session_factory, writer, free_disk_mb, clock,
                 settlement_lag_s, floor_mb=DEFAULT_FLOOR_MB,
                 temp_mb=DEFAULT_TEMP_MB, output_mb=DEFAULT_OUTPUT_MB):
        self.root = root
        self.session_factory = session_factory
        self.writer = writer
        self.free_disk_mb = free_disk_mb
        self.clock = clock
        self.settlement_lag_s = settlement_lag_s
        self.floor_mb = floor_mb
        self.temp_mb = temp_mb
        self.output_mb = output_mb

    # --- the single-extraction lock ---------------------------------------
    @contextlib.contextmanager
    def hold_the_lock(self):
        """Non-blocking, and a refusal rather than a queue.

        Blocking would turn "the host is busy" into "the request hung", which a
        caller cannot distinguish from a broken extractor.
        """
        os.makedirs(self.root, exist_ok=True)
        path = os.path.join(self.root, ".extract.lock")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise ExtractError(
                        "an extraction is already running; only one runs at a"
                        " time because the PostgreSQL temp allowance is"
                        " per process and two would each spend all of it"
                    ) from None
                raise
            yield
        finally:
            # Released by closing the descriptor: flock ownership is per open
            # file description. On EVERY path, including a refusal, or the next
            # extraction is rejected for concurrency that already finished.
            os.close(fd)

    # --- admission --------------------------------------------------------
    def _check_disk(self):
        need = required_disk_mb(floor_mb=self.floor_mb, temp_mb=self.temp_mb,
                                output_mb=self.output_mb)
        free = self.free_disk_mb(self.root)
        if free is not None and free < need:
            raise ExtractError(
                f"{free}MiB free, and an extraction needs about {need}MiB:"
                f" the dispatcher's {self.floor_mb}MiB admission floor, which is"
                f" not ours to spend, plus {self.temp_mb}MiB of PostgreSQL temp"
                f" allowance, plus {self.output_mb}MiB of extract output."
                f" Note that queue-forecasting_pgdata spills temp files during"
                f" large queries and can double transiently -- wait for it"
                f" rather than lowering the floor."
            )

    # --- the run ----------------------------------------------------------
    def run(self, raw_request, *, force=False):
        """Validate, then extract or reuse. Takes a RAW request (D16).

        Validation happens HERE, with this process's clock and this process's
        settlement lag, because the whole point of a separate privilege domain is
        that the caller's diligence is not part of the guarantee. `qfd` also
        validates, so a bad request is refused at submit time with a legible
        message -- but that is a convenience, not the control.
        """
        request = extract_spec.validate(
            raw_request, now=self.clock(),
            settlement_lag_s=self.settlement_lag_s)
        request_hash = extract_spec.request_hash(request)

        existing = published_dir(self.root, request_hash)
        if existing is not None:
            return self._reuse(request_hash, existing, force=force)

        with self.hold_the_lock():
            # Re-checked inside the lock: two callers can both miss the check
            # above, and the loser must not proceed to publish over the winner.
            existing = published_dir(self.root, request_hash)
            if existing is not None:
                return self._reuse(request_hash, existing, force=force)
            self._check_disk()
            return self._extract(request, request_hash)

    def _reuse(self, request_hash, path, *, force):
        if force:
            # THE IMMUTABILITY RULE, enforced by the code that could break it.
            # `force` cannot buy re-extraction: incorporating late data means a
            # new `generation`, which is a new request and therefore a separate
            # artifact, so nothing that cited the old one changes.
            raise ExtractError(
                f"{request_hash[:12]} is already published and a published"
                f" extract is immutable. To incorporate late data, bump"
                f" `generation` or move `as_of_date`; both produce a new request"
                f" and a separate artifact."
            )
        with open(os.path.join(path, MANIFEST)) as fh:
            manifest = json.load(fh)
        log.info("reuse %s -> extract %s (published, not re-extracted)",
                 request_hash[:12], manifest.get("extract_hash", "?")[:12])
        return manifest

    def _extract(self, request, request_hash):
        started = time.time()
        log.info("extract %s: target=%s window=%s..%s generation=%s",
                 request_hash[:12], request["target"], request["train_start"],
                 request["as_of_date"], request["generation"])

        staging = os.path.join(self.root, STAGING,
                               f"{request_hash}.{os.getpid()}")
        os.makedirs(staging, exist_ok=False)
        published = False
        try:
            manifest = self._extract_into(staging, request, request_hash)
            target = os.path.join(self.root, request_hash)
            # ONE ATOMIC ACT. The artifact and its discoverability are the same
            # rename, so there is no window in which one exists without the
            # other and therefore no retry that can publish a second artifact.
            try:
                os.rename(staging, target)
                published = True
            except OSError as e:
                if e.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                    raise
                # Published while we held the lock -- only reachable across
                # hosts sharing the directory. Theirs wins; ours is discarded.
                log.info("extract %s: published concurrently, keeping theirs",
                         request_hash[:12])
                return self._reuse(request_hash, target, force=False)
            log.info("extract %s: published extract %s in %.1fs"
                     " (%d files, watermark %s)",
                     request_hash[:12], manifest["extract_hash"][:12],
                     time.time() - started, len(manifest["files"]),
                     manifest["watermark"])
            return manifest
        finally:
            # EVERY path, including a refusal and including a signal. A staging
            # directory left behind is not merely litter: the next run of the
            # same request finds `makedirs(exist_ok=False)` occupied and fails
            # for a reason unrelated to its own problem.
            if not published:
                shutil.rmtree(staging, ignore_errors=True)

    def _extract_into(self, staging, request, request_hash):
        session = self.session_factory()
        try:
            # BEFORE the transaction, both of them.
            #
            # The parallel-worker check first because it is cheapest and because
            # a wrong answer here invalidates the temp bound for everything that
            # follows. It reads the INHERITED value: `begin_snapshot` also sets
            # it defensively, and a check after that would be reading its own
            # answer -- a canary that cannot fail.
            parallel = session.setting("max_parallel_workers_per_gather")
            if str(parallel) != "0":
                raise ExtractError(
                    f"max_parallel_workers_per_gather is {parallel!r}, not 0."
                    f" temp_file_limit and work_mem are enforced per process and"
                    f" parallel workers are separate processes, so one query"
                    f" could spill several times the limit. See D23."
                )

            # THE WRITE CANARY. Read-onlyness -- or, more precisely, "this role
            # cannot write" -- is asserted by attempting a write and being
            # refused, not by reading a setting that claims it.
            #
            # It runs HERE, outside the transaction: a failed statement aborts a
            # PostgreSQL transaction, so attempting this after `BEGIN` would
            # poison the very transaction the extract is read from.
            reason = session.attempt_write()
            if reason is None:
                raise ExtractError(
                    "a write SUCCEEDED as the extraction role: neither the"
                    " SELECT-only grant nor default_transaction_read_only is in"
                    " force. Check the live cluster, not the migration file."
                )
            if reason not in WRITE_REFUSAL_REASONS:
                # An unexpected error is not evidence of anything. Treating it
                # as a refusal is how a canary comes to pass for a reason
                # unrelated to what it is asserting.
                raise ExtractError(
                    f"the write canary failed for an unexpected reason"
                    f" ({reason}); refusing rather than reading that as proof"
                    f" the role cannot write. Expected one of"
                    f" {sorted(WRITE_REFUSAL_REASONS)}."
                )

            snapshot_start_ts, snapshot = session.begin_snapshot()
            read_only = session.setting("transaction_read_only")
            if str(read_only) != "on":
                raise ExtractError(
                    f"transaction_read_only is {read_only!r} inside the"
                    f" extraction transaction; refusing to read a snapshot that"
                    f" could have written."
                )

            files = {}
            watermark = _Watermark()
            for name in sorted(inventory.DATASETS):
                dataset = inventory.DATASETS[name]
                params = inventory.bindings(name, request)
                columns, batches = session.query(name, dataset.sql, params)
                columns = list(columns)
                path = os.path.join(staging, dataset.file)

                rows_written = 0
                with self.writer.open(path, columns) as sink:
                    for batch in batches:
                        if not batch:
                            continue
                        sink.write(batch)
                        rows_written += len(batch)
                        watermark.update(columns, dataset.watermark_columns,
                                         batch)

                if rows_written == 0:
                    raise ExtractError(
                        f"{name} returned no rows. A zero-row extract is a"
                        f" plausible-looking artifact that trains a meaningless"
                        f" model, so it is refused rather than published."
                        f" Check the window and that the collector is running."
                    )

                files[name] = {
                    "file": dataset.file,
                    "sha256": self._digest(path),
                    "rows": rows_written,
                    # The bindings ACTUALLY used, so the manifest cannot claim a
                    # window that was never applied -- a whole-table read
                    # records an empty one.
                    "window": dict(params),
                    "columns": list(dataset.columns),
                }
        finally:
            # The session closes on every path. An open snapshot transaction
            # holds its xmin, which blocks vacuum on the very tables the
            # collector is writing.
            with contextlib.suppress(Exception):
                session.close()

        manifest = {
            "schema": 1,
            "request": dict(request),
            "request_hash": request_hash,
            "settlement_lag_s": self.settlement_lag_s,
            "snapshot_start_ts": snapshot_start_ts,
            "snapshot": snapshot,
            "watermark": watermark.as_manifest(),
            "files": files,
        }
        manifest["extract_hash"] = hashlib.sha256(
            _canonical(manifest)).hexdigest()
        with open(os.path.join(staging, MANIFEST), "w") as fh:
            json.dump(manifest, fh, sort_keys=True, indent=2)
        return manifest

    @staticmethod
    def _digest(path):
        """Of the BYTES ON DISK, streamed. A digest taken from rows in memory
        cannot detect a writer that serialised something else, and the manifest
        describes the file."""
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
