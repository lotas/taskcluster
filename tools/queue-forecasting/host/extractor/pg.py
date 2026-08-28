"""The PostgreSQL side of the seam. Task 3.

**NOT COVERED BY THE TEST SUITE IN THIS REPOSITORY, AND SAID SO PLAINLY.**
`psycopg` is not importable in the development environment, so nothing here has
executed. Every ordering rule and every refusal lives in `extractor.py`, which is
stdlib-only and fully tested against a fake; this module is deliberately thin
enough that reading it is a reasonable substitute for running it, and it first
executes in the privileged tasks against the live cluster.

Keep it thin. Anything with a decision in it belongs on the other side of the
seam, where a test can reach it.
"""
from __future__ import annotations

import contextlib

import psycopg

# Rows per round trip. Small enough that peak memory is a batch rather than a
# dataset (D23), large enough that a months-long window is not a million round
# trips. The extractor's manifest is identical whatever this is -- there is a
# test for that -- so it is a performance knob and nothing else.
BATCH_ROWS = 10_000

# The canary's write statement. `WHERE false` is what makes it safe to run:
#
#   * refused by a read-only transaction   -> SQLSTATE 25006
#   * refused by the SELECT-only grant     -> SQLSTATE 42501
#   * if BOTH controls are somehow missing -> it succeeds, updates zero rows,
#     and the extractor refuses because a write succeeded
#
# `CREATE TEMP TABLE` was the first choice and was wrong: `phase0-setup.sh`
# revokes ALL on the database from PUBLIC and grants back only CONNECT, so TEMP
# is not held and the statement would fail with insufficient_privilege on a
# correctly configured host -- a canary passing for a reason unrelated to what it
# asserts. TEMP is a distinct database privilege in PostgreSQL's GRANT model.
_CANARY_SQL = ("UPDATE queue_forecast_worker_pools"
               " SET task_queue_id = task_queue_id WHERE false")

_READ_ONLY_SQLSTATE = "25006"        # read_only_sql_transaction
_INSUFFICIENT_PRIVILEGE = "42501"    # insufficient_privilege


class PgSession:
    """One connection, one snapshot.

    The interface `extractor.Extractor` expects: `setting`, `attempt_write`,
    `begin_snapshot`, `query`, `close`.
    """

    def __init__(self, dsn, *, batch_rows=BATCH_ROWS):
        # No `options=` and no session tuning in the constructor: the role
        # carries `work_mem`, `statement_timeout`, `temp_file_limit` and
        # `default_transaction_read_only` (phase0-setup.sh), and a session that
        # overrode them here would make NC17b's live-role assertions describe
        # this code rather than the cluster.
        # `autocommit=True` with an EXPLICIT `BEGIN` in `begin_snapshot`, rather
        # than psycopg's implicit transaction. The extraction needs one
        # transaction with a specific isolation level and read-only flag, opened
        # at a moment this code chooses -- and the write canary needs to run
        # OUTSIDE it. An implicitly managed transaction would wrap the canary
        # too, and a failed statement aborts the transaction it is in.
        self.conn = psycopg.connect(dsn, autocommit=True)
        self.batch_rows = batch_rows

    def setting(self, name):
        """The INHERITED value, read before anything overrides it.

        What matters is *when* this is called: `extractor.py` reads
        `max_parallel_workers_per_gather` before `begin_snapshot`, so it sees
        what the role configured rather than what `begin_snapshot` sets.
        """
        with self.conn.cursor() as cur:
            cur.execute(f"SHOW {_ident(name)}")
            row = cur.fetchone()
        return None if row is None else row[0]

    def attempt_write(self):
        """Attempt a write. Returns WHY it was refused, or None if it succeeded.

        None is the failure -- `extractor.py` refuses on it. An unrecognised
        reason is also a refusal to proceed, because an error nobody expected is
        not evidence that the role cannot write; it is evidence that something
        else is wrong.

        Runs OUTSIDE the extraction transaction (see `extractor.py`, which orders
        it before `begin_snapshot`): a failed statement aborts a PostgreSQL
        transaction, and this one is expected to fail.
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute(_CANARY_SQL)
        except psycopg.Error as e:
            code = getattr(e, "sqlstate", None)
            if code == _READ_ONLY_SQLSTATE:
                return "read_only"
            if code == _INSUFFICIENT_PRIVILEGE:
                return "insufficient_privilege"
            # Deliberately NOT folded into a refusal. A connection error, a
            # missing table or a statement timeout says nothing about whether
            # this role can write, and reporting it as "refused" is how a
            # control comes to pass for the wrong reason.
            return f"unexpected sqlstate {code}: {e}"
        return None

    def begin_snapshot(self):
        """Open the one transaction every file is read from, and record it.

        Returns `(snapshot_start_ts, snapshot)`. Both are captured INSIDE the
        transaction and before any data query, so they describe the read that
        follows rather than a moment near it.

        `pg_current_snapshot()` and not `txid_current()`: a transaction id does
        not encode what that transaction could SEE, and the reason the snapshot
        is recorded at all is so a later reader can tell whether two files could
        have straddled a collector write (D19).
        """
        cur = self.conn.cursor()
        # One statement, so there is no window in which the transaction exists
        # with the wrong isolation level or the wrong read-only flag.
        cur.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                    " READ ONLY")
        # Belt and braces, and it cannot make the extractor's assertion vacuous
        # because that assertion has already run against the inherited value.
        cur.execute("SET LOCAL max_parallel_workers_per_gather = 0")
        cur.execute("SELECT to_char(now() AT TIME ZONE 'UTC',"
                    " 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'),"
                    " pg_catalog.pg_current_snapshot()::text")
        started, snapshot = cur.fetchone()
        cur.close()
        return started, snapshot

    def query(self, name, sql, params):
        """Run one of `inventory`'s literal queries and return `(columns, batches)`.

        A SERVER-SIDE cursor, and the batching is the point rather than a
        refinement. `fetchall()` materialises the whole result in the extractor,
        and with a 4 GiB output allowance on a host whose disk floor is already
        contested that is a second resource bound nobody declared. A named cursor
        keeps the result set on the server and streams it.

        The cursor's name is derived from the dataset name, which comes from
        `inventory.DATASETS` -- a module constant, never a caller.
        """
        cursor_name = f"qf_extract_{name}"
        cur = self.conn.cursor(name=cursor_name)
        cur.itersize = self.batch_rows
        cur.execute(sql, params)
        columns = [d.name for d in cur.description]

        def batches():
            try:
                while True:
                    rows = cur.fetchmany(self.batch_rows)
                    if not rows:
                        return
                    yield rows
            finally:
                # The cursor closes even if the consumer stops early, e.g. when
                # the extractor raises on a zero-row dataset.
                with contextlib.suppress(Exception):
                    cur.close()

        return columns, batches()

    def close(self):
        """Always closed, on every path.

        An open snapshot transaction holds its xmin, which blocks vacuum on the
        very tables the collector is writing -- so leaking one degrades the
        system that produces the data.
        """
        with contextlib.suppress(Exception):
            self.conn.close()


def _ident(name):
    """A setting name is an identifier, so it is validated rather than quoted.

    `SHOW` will not take a parameter, so this is the one place a name reaches
    SQL as text. The allowlist is what `extractor.py` asks for; a third setting
    is a code change here, which is the correct friction.
    """
    allowed = {"max_parallel_workers_per_gather", "transaction_read_only",
               "temp_file_limit", "work_mem", "statement_timeout"}
    if name not in allowed:
        raise ValueError(f"{name!r} is not an allowlisted setting name")
    return name


def session_factory(dsn):
    """What Task 4's service passes to `Extractor`."""
    return lambda: PgSession(dsn)
