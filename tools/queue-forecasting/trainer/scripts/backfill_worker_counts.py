#!/usr/bin/env python3
"""Backfill queue_forecast_worker_counts from Prometheus.

See trainer-spec.md "Worker Count Sampling" for the live service; this
script fills in historical rows for periods before the live service was
running, using PromQL range queries against fxci_queue_running_workers,
fxci_queue_claimed_tasks, and fxci_worker_manager_existing_capacity.

Usage:
    python scripts/backfill_worker_counts.py \\
        --prometheus-url https://prometheus.example.com \\
        --from 2026-04-03 \\
        --to   2026-04-24 \\
        [--source prometheus_historical] \\
        [--bearer-token-env PROMETHEUS_TOKEN] \\
        [--step-seconds 300] \\
        [--dry-run]

The script is idempotent: re-running the same range is safe because every
INSERT uses ON CONFLICT (task_queue_id, sampled_at) DO NOTHING, so existing
rows (including those written by the live tc_api source) are never touched.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta


# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------
# Each tuple is:
#   (promql_metric_name, label_source, db_column)
#
# label_source == "_queue_pair" means we build task_queue_id from two labels
#   (provisionerId + workerType);  otherwise it's the label name used as-is.
METRICS: list[tuple[str, str, str]] = [
    ("fxci_queue_running_workers",            "_queue_pair",  "running_workers"),
    ("fxci_queue_claimed_tasks",              "_queue_pair",  "claimed_tasks"),
    ("fxci_worker_manager_existing_capacity", "workerPoolId", "existing_capacity"),
]

BATCH_SIZE = 1000


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Row:
    sampled_at: datetime
    task_queue_id: str
    running_workers: int | None = None
    claimed_tasks: int | None = None
    existing_capacity: int | None = None


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------

def _parse_instant(s: str) -> datetime:
    """Accept 'YYYY-MM-DD' (midnight UTC) or a full ISO instant."""
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        # Plain date — treat as midnight UTC
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    # Full ISO instant; normalise the Z suffix that fromisoformat doesn't handle
    # in Python < 3.11.
    s2 = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s2)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Prometheus helpers
# ---------------------------------------------------------------------------

def _query_range(
    prom_url: str,
    metric: str,
    start: datetime,
    end: datetime,
    step_seconds: int,
    auth_header: str | None,
) -> list[dict]:
    """Call Prometheus /api/v1/query_range and return the result list."""
    params = {
        "query": metric,
        "start": f"{start.timestamp():.3f}",
        "end":   f"{end.timestamp():.3f}",
        "step":  f"{step_seconds}s",
    }
    url = (
        f"{prom_url.rstrip('/')}/api/v1/query_range?"
        + urllib.parse.urlencode(params)
    )
    req = urllib.request.Request(url)
    if auth_header:
        req.add_header("Authorization", auth_header)

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(
            f"Prometheus HTTP {exc.code} for metric={metric} "
            f"{start.date()}..{end.date()}: {err_body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Prometheus connection error for metric={metric} "
            f"{start.date()}..{end.date()}: {exc.reason}"
        ) from exc

    if body.get("status") != "success":
        raise RuntimeError(
            f"Prometheus returned non-success for {metric}: {body}"
        )
    return body["data"]["result"]


def _task_queue_id(metric_labels: dict, label_source: str) -> str | None:
    """Derive the task_queue_id string from a Prometheus series' label set."""
    if label_source == "_queue_pair":
        pid = metric_labels.get("provisionerId")
        wt  = metric_labels.get("workerType")
        if not pid or not wt:
            return None
        return f"{pid}/{wt}"
    # Otherwise label_source is the label name to use directly.
    return metric_labels.get(label_source)


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

def fetch_rows(args: argparse.Namespace, start: datetime, end: datetime) -> list[Row]:
    """Query Prometheus for all three metrics and merge into Row objects.

    Returns a list of Row instances, one per distinct (task_queue_id, sampled_at)
    pair.  Columns from different metrics at the same key are merged; if a
    metric has no data point at a given timestamp the corresponding column
    stays None.
    """
    auth: str | None = None
    if args.bearer_token_env:
        tok = os.environ.get(args.bearer_token_env)
        if not tok:
            print(
                f"ERROR: env var '{args.bearer_token_env}' is not set or empty",
                file=sys.stderr,
            )
            sys.exit(1)
        auth = f"Bearer {tok}"

    # (task_queue_id, sampled_at) -> Row
    bucket: dict[tuple[str, datetime], Row] = {}
    stats = {"queries": 0, "series_total": 0, "points_total": 0}

    for day_start, day_end in _iterate_days(start, end):
        for metric, label_source, db_column in METRICS:
            result = _query_range(
                args.prometheus_url,
                metric,
                day_start,
                day_end,
                args.step_seconds,
                auth,
            )
            stats["queries"] += 1
            stats["series_total"] += len(result)

            for series in result:
                tqid = _task_queue_id(series["metric"], label_source)
                if not tqid:
                    continue
                for ts_str, val_str in series.get("values", []):
                    try:
                        ts  = datetime.fromtimestamp(float(ts_str), tz=timezone.utc)
                        val = int(float(val_str))
                    except (ValueError, TypeError):
                        continue
                    key = (tqid, ts)
                    row = bucket.get(key)
                    if row is None:
                        row = Row(sampled_at=ts, task_queue_id=tqid)
                        bucket[key] = row
                    setattr(row, db_column, val)
                    stats["points_total"] += 1

            print(
                f"  [{metric}] {day_start.date()}..{day_end.date()}: "
                f"{len(result)} series, running distinct rows: {len(bucket):,}",
                file=sys.stderr,
            )

    rows = list(bucket.values())
    print(
        f"\n[backfill] prepared {len(rows):,} distinct (task_queue_id, sampled_at) rows "
        f"from {stats['queries']} Prometheus queries "
        f"({stats['series_total']:,} series, {stats['points_total']:,} data points)",
        file=sys.stderr,
    )
    return rows


def write_rows(rows: list[Row], source: str, dsn: str) -> tuple[int, int]:
    """Insert rows into queue_forecast_worker_counts.

    Uses ON CONFLICT DO NOTHING so existing live-service rows are never
    overwritten.  Returns (inserted, skipped).
    """
    import psycopg  # imported here so --dry-run works without psycopg installed

    inserted = 0
    skipped  = 0

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            for i in range(0, len(rows), BATCH_SIZE):
                chunk = rows[i : i + BATCH_SIZE]

                # Build a flat parameter list: 6 values per row
                params: list = []
                for r in chunk:
                    params += [
                        r.sampled_at,
                        r.task_queue_id,
                        r.running_workers,
                        r.claimed_tasks,
                        r.existing_capacity,
                        source,
                    ]

                # psycopg3 uses %s placeholders (not $N postgres-native syntax)
                placeholders = ", ".join(["(%s, %s, %s, %s, %s, %s)"] * len(chunk))
                cur.execute(
                    f"""
                    INSERT INTO queue_forecast_worker_counts
                        (sampled_at, task_queue_id, running_workers, claimed_tasks,
                         existing_capacity, source)
                    VALUES {placeholders}
                    ON CONFLICT (task_queue_id, sampled_at) DO NOTHING
                    """,
                    params,
                )

                # rowcount is the number of rows actually inserted (skips excluded)
                batch_inserted = cur.rowcount if cur.rowcount >= 0 else 0
                batch_skipped  = len(chunk) - batch_inserted
                inserted += batch_inserted
                skipped  += batch_skipped

                print(
                    f"  batch {i // BATCH_SIZE + 1}: inserted={batch_inserted} "
                    f"skipped={batch_skipped} (running total inserted={inserted:,})",
                    file=sys.stderr,
                )

        conn.commit()

    return inserted, skipped


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

    print(
        f"[backfill] range [{start.isoformat()} , {end.isoformat()}) "
        f"step={args.step_seconds}s  source='{args.source}'",
        file=sys.stderr,
    )

    rows = fetch_rows(args, start, end)

    if args.dry_run:
        print("[backfill] --dry-run: skipping database writes.", file=sys.stderr)
        if rows:
            sample = rows[:3]
            print("[backfill] sample rows (up to 3):", file=sys.stderr)
            for r in sample:
                print(f"  {r}", file=sys.stderr)
        return 0

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print(
            "ERROR: DATABASE_URL environment variable is not set.",
            file=sys.stderr,
        )
        return 1

    inserted, skipped = write_rows(rows, args.source, dsn)
    print(
        f"\n[backfill] done — inserted {inserted:,} rows, skipped {skipped:,} (already existed).",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Backfill queue_forecast_worker_counts from Prometheus.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--prometheus-url",
        required=True,
        help="Base URL of the Prometheus HTTP API (e.g. https://prometheus.example.com).",
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
        default="prometheus_historical",
        help="Value written to the 'source' column (default: prometheus_historical).",
    )
    p.add_argument(
        "--bearer-token-env",
        default=None,
        metavar="ENV_VAR",
        help=(
            "Name of the environment variable holding a bearer token for "
            "Prometheus authentication.  If unset, no Authorization header is sent."
        ),
    )
    p.add_argument(
        "--step-seconds",
        type=int,
        default=300,
        metavar="N",
        help="Prometheus range-query step in seconds (default: 300, i.e. 5 min).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Fetch and parse Prometheus data but do not write to Postgres.  "
            "Useful for validating reachability and label shapes."
        ),
    )
    return run(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
