#!/usr/bin/env python3
"""Backfill historical claimed_tasks into queue_forecast_worker_counts.

Counts runs in-flight at each 5-min sample instant using only data
in queue_forecast_task_runs / queue_forecast_tasks.  Written when we
don't have Prometheus API access to pull running_workers /
existing_capacity historically from fxci_queue_* metrics.

Only claimed_tasks is filled; running_workers and existing_capacity
stay NULL for the rows written by this script.

Usage:
    python scripts/backfill_claimed_tasks.py \\
        --from 2026-03-23 \\
        --to   2026-04-24 \\
        [--source db_derived] \\
        [--step-seconds 300] \\
        [--dry-run]

The script is idempotent: re-running the same range is safe because
every INSERT uses ON CONFLICT (task_queue_id, sampled_at) DO NOTHING,
so existing rows (including those written by the live tc_api source or a
prior backfill) are never overwritten.

Requires:
    DATABASE_URL environment variable pointing at the Postgres instance.
    psycopg (v3) installed in the Python environment.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone, timedelta

# psycopg is imported lazily inside run() so that --dry-run and argument
# validation work even if psycopg is not installed in the local environment.


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BATCH_SIZE = 2000

# Count runs that were in-flight at instant T:
#   started_at <= T  AND  (resolved_at > T  OR  resolved_at IS NULL)
#
# Note on the generate_series interval: Postgres accepts integer literals
# for the step when using the timestamptz overload with an interval, but
# the cleanest portable approach is to cast a string literal via
# (%(step_seconds)s || ' seconds')::interval — this avoids having to
# embed a Python-interpolated integer directly into the SQL while keeping
# the query fully parameterised where possible.
QUERY_SQL = """\
WITH sample_ts AS (
    SELECT generate_series(
        %(day_start)s::timestamptz,
        %(day_end)s::timestamptz - (%(step_seconds)s || ' seconds')::interval,
        (%(step_seconds)s || ' seconds')::interval
    ) AS sampled_at
)
SELECT
    s.sampled_at,
    t.task_queue_id,
    COUNT(*)::int AS claimed_tasks
FROM sample_ts s
JOIN queue_forecast_task_runs r
    ON r.started_at <= s.sampled_at
   AND (r.resolved_at > s.sampled_at OR r.resolved_at IS NULL)
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE t.task_queue_id IS NOT NULL
GROUP BY s.sampled_at, t.task_queue_id
HAVING COUNT(*) > 0
ORDER BY s.sampled_at, t.task_queue_id
"""

INSERT_TEMPLATE = """\
INSERT INTO queue_forecast_worker_counts
    (sampled_at, task_queue_id, running_workers, claimed_tasks,
     existing_capacity, source)
VALUES {placeholders}
ON CONFLICT (task_queue_id, sampled_at) DO NOTHING
"""


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------

def _parse_instant(s: str) -> datetime:
    """Accept 'YYYY-MM-DD' (midnight UTC) or a full ISO instant."""
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        # Plain date — treat as midnight UTC
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    # Full ISO instant; normalise the Z suffix that fromisoformat doesn't
    # handle in Python < 3.11.
    s2 = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s2)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Day-window iterator
# ---------------------------------------------------------------------------

def _iterate_days(start: datetime, end: datetime):
    """Yield (day_start, day_end) 24-hour windows covering [start, end)."""
    d = start
    while d < end:
        nxt = min(d + timedelta(days=1), end)
        yield d, nxt
        d = nxt


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _process_day(
    conn: object,  # psycopg.Connection — imported lazily in run()
    day_start: datetime,
    day_end: datetime,
    step_seconds: int,
    source: str,
    dry_run: bool,
) -> tuple[int, int]:
    """Compute and (optionally) insert rows for one calendar day.

    Returns (seen, inserted).  On --dry-run, inserted is always 0.
    """
    print(
        f"[backfill] day {day_start.date()} → {day_end.date()} ...",
        file=sys.stderr,
    )

    with conn.cursor() as cur:
        cur.execute(
            QUERY_SQL,
            {
                "day_start": day_start,
                "day_end": day_end,
                "step_seconds": str(step_seconds),
            },
        )
        rows = cur.fetchall()

    print(
        f"  computed {len(rows):,} (task_queue_id, sample) pairs "
        "with claimed_tasks > 0",
        file=sys.stderr,
    )

    if dry_run or not rows:
        return len(rows), 0

    day_inserted = 0

    with conn.cursor() as cur:
        for i in range(0, len(rows), BATCH_SIZE):
            chunk = rows[i : i + BATCH_SIZE]
            placeholders = ", ".join(["(%s, %s, %s, %s, %s, %s)"] * len(chunk))
            params: list = []
            for sampled_at, task_queue_id, claimed_tasks in chunk:
                params += [sampled_at, task_queue_id, None, claimed_tasks, None, source]
            cur.execute(INSERT_TEMPLATE.format(placeholders=placeholders), params)
            batch_inserted = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
            day_inserted += batch_inserted

            print(
                f"  batch {i // BATCH_SIZE + 1}: "
                f"inserted={batch_inserted} "
                f"skipped={len(chunk) - batch_inserted} "
                f"(running total inserted={day_inserted:,})",
                file=sys.stderr,
            )

    conn.commit()

    day_skipped = len(rows) - day_inserted
    print(
        f"  day done — inserted {day_inserted:,}, "
        f"skipped {day_skipped:,} (already existed)",
        file=sys.stderr,
    )
    return len(rows), day_inserted


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    start = _parse_instant(args.from_date)
    end   = _parse_instant(args.to_date)

    if not (start < end):
        print(
            f"ERROR: --from ({start.isoformat()}) must be strictly before "
            f"--to ({end.isoformat()})",
            file=sys.stderr,
        )
        return 1

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print(
            "ERROR: DATABASE_URL environment variable is not set.",
            file=sys.stderr,
        )
        return 1

    print(
        f"[backfill] range [{start.isoformat()} , {end.isoformat()}) "
        f"step={args.step_seconds}s  source='{args.source}'",
        file=sys.stderr,
    )
    if args.dry_run:
        print("[backfill] --dry-run: no rows will be written.", file=sys.stderr)

    total_seen = 0
    total_inserted = 0

    import psycopg  # imported here so validation / --dry-run don't require psycopg installed

    with psycopg.connect(dsn) as conn:
        for day_start, day_end in _iterate_days(start, end):
            seen, inserted = _process_day(
                conn,
                day_start,
                day_end,
                args.step_seconds,
                args.source,
                args.dry_run,
            )
            total_seen += seen
            total_inserted += inserted

    if args.dry_run:
        print(
            f"\n[backfill] --dry-run complete — would process "
            f"{total_seen:,} (task_queue_id, sample) rows across "
            f"{sum(1 for _ in _iterate_days(start, end))} days.",
            file=sys.stderr,
        )
    else:
        total_skipped = total_seen - total_inserted
        print(
            f"\n[backfill] done — inserted {total_inserted:,} rows total "
            f"(from {total_seen:,} computed; "
            f"skipped {total_skipped:,} already existed).",
            file=sys.stderr,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Backfill historical claimed_tasks into queue_forecast_worker_counts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--from",
        dest="from_date",
        required=True,
        metavar="DATE",
        help="Start of range (inclusive).  Format: YYYY-MM-DD or ISO instant.",
    )
    p.add_argument(
        "--to",
        dest="to_date",
        required=True,
        metavar="DATE",
        help="End of range (exclusive).  Format: YYYY-MM-DD or ISO instant.",
    )
    p.add_argument(
        "--source",
        default="db_derived",
        help="Value written to the 'source' column (default: db_derived).",
    )
    p.add_argument(
        "--step-seconds",
        type=int,
        default=300,
        metavar="N",
        help="Sample interval in seconds (default: 300, i.e. 5 min).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Compute row counts per day but do not write to Postgres.  "
            "Useful for estimating data volume before committing."
        ),
    )
    return run(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
