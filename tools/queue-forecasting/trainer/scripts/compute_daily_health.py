#!/usr/bin/env python3
"""Compute daily data-quality metrics for queue-forecasting.

For each day in [from, to), derives counts from queue_forecast_task_runs +
queue_forecast_tasks, computes per-day rates, applies rule-based +
trailing-window thresholds to set per-flag booleans, and upserts a row
into queue_forecast_daily_health.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import psycopg


DAY_METRICS_SQL = """
WITH day_runs AS (
    SELECT r.task_id, r.run_id, r.started_at, r.resolved_at, r.reason_resolved,
           r.wait_duration_s, r.run_duration_s
    FROM queue_forecast_task_runs r
    JOIN queue_forecast_tasks t ON r.task_id = t.task_id
    WHERE t.task_queue_id IS NOT NULL
      AND r.pending_at >= %(day_start)s
      AND r.pending_at <  %(day_end)s
)
SELECT
    COUNT(*) AS n_total,
    COUNT(*) FILTER (WHERE reason_resolved = 'completed')         AS n_completed,
    COUNT(*) FILTER (WHERE reason_resolved = 'failed')            AS n_failed,
    COUNT(*) FILTER (WHERE reason_resolved IN ('exception','intermittent-task','internal-error','resource-unavailable','malformed-payload')) AS n_exception,
    COUNT(*) FILTER (WHERE reason_resolved = 'worker-shutdown')   AS n_worker_shutdown,
    COUNT(*) FILTER (WHERE reason_resolved = 'claim-expired')     AS n_claim_expired,
    COUNT(*) FILTER (WHERE reason_resolved = 'deadline-exceeded') AS n_deadline_exceeded,
    COUNT(*) FILTER (WHERE reason_resolved = 'canceled')          AS n_canceled,
    COUNT(*) FILTER (WHERE started_at IS NOT NULL)                AS n_started,
    COUNT(*) FILTER (WHERE started_at IS NULL AND reason_resolved = 'deadline-exceeded') AS n_pending_no_start,
    percentile_cont(0.99) WITHIN GROUP (ORDER BY wait_duration_s) FILTER (WHERE started_at IS NOT NULL AND wait_duration_s IS NOT NULL) AS wait_p99_s,
    percentile_cont(0.99) WITHIN GROUP (ORDER BY run_duration_s)  FILTER (WHERE reason_resolved = 'completed' AND run_duration_s IS NOT NULL) AS run_p99_s
FROM day_runs;
"""


@dataclass
class DailyMetrics:
    sample_date: datetime
    n_total: int
    n_completed: int
    n_failed: int
    n_exception: int
    n_worker_shutdown: int
    n_claim_expired: int
    n_deadline_exceeded: int
    n_canceled: int
    n_started: int
    n_pending_no_start: int
    exception_rate: float | None
    stuck_pending_rate: float | None
    completion_rate: float | None
    wait_p99_s: float | None
    run_p99_s: float | None


def fetch_day(conn, day_start: datetime) -> DailyMetrics:
    day_end = day_start + timedelta(days=1)
    with conn.cursor() as cur:
        cur.execute(DAY_METRICS_SQL, {"day_start": day_start, "day_end": day_end})
        row = cur.fetchone()
    cols = ("n_total", "n_completed", "n_failed", "n_exception",
            "n_worker_shutdown", "n_claim_expired", "n_deadline_exceeded",
            "n_canceled", "n_started", "n_pending_no_start",
            "wait_p99_s", "run_p99_s")
    d = dict(zip(cols, row))
    n = d["n_total"] or 0
    excp = (d["n_exception"] or 0) + (d["n_worker_shutdown"] or 0) + (d["n_claim_expired"] or 0)
    return DailyMetrics(
        sample_date         = day_start,
        n_total             = n,
        n_completed         = d["n_completed"] or 0,
        n_failed            = d["n_failed"] or 0,
        n_exception         = d["n_exception"] or 0,
        n_worker_shutdown   = d["n_worker_shutdown"] or 0,
        n_claim_expired     = d["n_claim_expired"] or 0,
        n_deadline_exceeded = d["n_deadline_exceeded"] or 0,
        n_canceled          = d["n_canceled"] or 0,
        n_started           = d["n_started"] or 0,
        n_pending_no_start  = d["n_pending_no_start"] or 0,
        exception_rate      = (excp / n)                       if n else None,
        stuck_pending_rate  = ((d["n_pending_no_start"] or 0) / n) if n else None,
        completion_rate     = ((d["n_completed"] or 0) / n)    if n else None,
        wait_p99_s          = d["wait_p99_s"],
        run_p99_s           = d["run_p99_s"],
    )


def trailing_median(values: list[float | None]) -> float | None:
    finite = [v for v in values if v is not None]
    if not finite:
        return None
    s = sorted(finite)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def evaluate_flags(today: DailyMetrics, history: list[DailyMetrics]) -> tuple[dict[str, bool], list[str], dict]:
    """Apply rule-based + trailing-window thresholds; return (flags, reasons, threshold_snapshot)."""
    flags = {
        "flag_exception_spike":     False,
        "flag_stuck_pending_spike": False,
        "flag_wait_p99_spike":      False,
        "flag_volume_anomaly":      False,
        "flag_low_completion":      False,
    }
    reasons: list[str] = []

    med_excp     = trailing_median([h.exception_rate     for h in history])
    med_stuck    = trailing_median([h.stuck_pending_rate for h in history])
    med_wait_p99 = trailing_median([h.wait_p99_s         for h in history])
    med_volume   = trailing_median([float(h.n_total)     for h in history])

    snapshot = {
        "abs_exception_rate":     0.10,
        "abs_stuck_pending_rate": 0.10,
        "abs_low_completion":     0.70,
        "rel_exception_x":        2.0,
        "rel_stuck_x":            3.0,
        "rel_wait_p99_x":         3.0,
        "rel_volume_low_x":       0.5,
        "rel_volume_high_x":      2.0,
        "trailing_window_days":   len(history),
        "trailing_median_exception_rate":     med_excp,
        "trailing_median_stuck_pending_rate": med_stuck,
        "trailing_median_wait_p99_s":         med_wait_p99,
        "trailing_median_n_total":            med_volume,
    }

    # Exception spike — abs OR (rel and have history)
    if today.exception_rate is not None and (
        today.exception_rate > 0.10
        or (med_excp is not None and today.exception_rate > 2.0 * med_excp)
    ):
        flags["flag_exception_spike"] = True
        reasons.append("exception_spike")

    # Stuck-pending spike
    if today.stuck_pending_rate is not None and (
        today.stuck_pending_rate > 0.10
        or (med_stuck is not None and today.stuck_pending_rate > 3.0 * med_stuck)
    ):
        flags["flag_stuck_pending_spike"] = True
        reasons.append("stuck_pending_spike")

    # Wait p99 spike — relative only (absolute thresholds are too queue-specific)
    if today.wait_p99_s is not None and med_wait_p99 is not None and today.wait_p99_s > 3.0 * med_wait_p99:
        flags["flag_wait_p99_spike"] = True
        reasons.append("wait_p99_spike")

    # Volume anomaly — both directions. n_total=0 (e.g. Pulse queue overflowed
    # while the consumer was offline) intentionally falls through: ratio=0 < 0.5
    # is exactly the case we want to flag, not skip.
    if med_volume is not None and med_volume > 0:
        ratio = today.n_total / med_volume
        if ratio < 0.5 or ratio > 2.0:
            flags["flag_volume_anomaly"] = True
            reasons.append("volume_anomaly")

    # Low completion — absolute only (sustained drops indicate trouble regardless of trend)
    if today.completion_rate is not None and today.completion_rate < 0.70:
        flags["flag_low_completion"] = True
        reasons.append("low_completion")

    return flags, reasons, snapshot


UPSERT_SQL = """
    INSERT INTO queue_forecast_daily_health (
        sample_date,
        n_total, n_completed, n_failed, n_exception, n_worker_shutdown, n_claim_expired,
        n_deadline_exceeded, n_canceled, n_started, n_pending_no_start,
        exception_rate, stuck_pending_rate, completion_rate, wait_p99_s, run_p99_s,
        flag_exception_spike, flag_stuck_pending_spike, flag_wait_p99_spike,
        flag_volume_anomaly, flag_low_completion,
        is_anomalous, anomaly_reasons, threshold_snapshot, computed_at
    ) VALUES (
        %(sample_date)s,
        %(n_total)s, %(n_completed)s, %(n_failed)s, %(n_exception)s, %(n_worker_shutdown)s, %(n_claim_expired)s,
        %(n_deadline_exceeded)s, %(n_canceled)s, %(n_started)s, %(n_pending_no_start)s,
        %(exception_rate)s, %(stuck_pending_rate)s, %(completion_rate)s, %(wait_p99_s)s, %(run_p99_s)s,
        %(flag_exception_spike)s, %(flag_stuck_pending_spike)s, %(flag_wait_p99_spike)s,
        %(flag_volume_anomaly)s, %(flag_low_completion)s,
        %(is_anomalous)s, %(anomaly_reasons)s, %(threshold_snapshot)s, NOW()
    )
    ON CONFLICT (sample_date) DO UPDATE SET
        n_total = EXCLUDED.n_total,
        n_completed = EXCLUDED.n_completed,
        n_failed = EXCLUDED.n_failed,
        n_exception = EXCLUDED.n_exception,
        n_worker_shutdown = EXCLUDED.n_worker_shutdown,
        n_claim_expired = EXCLUDED.n_claim_expired,
        n_deadline_exceeded = EXCLUDED.n_deadline_exceeded,
        n_canceled = EXCLUDED.n_canceled,
        n_started = EXCLUDED.n_started,
        n_pending_no_start = EXCLUDED.n_pending_no_start,
        exception_rate = EXCLUDED.exception_rate,
        stuck_pending_rate = EXCLUDED.stuck_pending_rate,
        completion_rate = EXCLUDED.completion_rate,
        wait_p99_s = EXCLUDED.wait_p99_s,
        run_p99_s = EXCLUDED.run_p99_s,
        flag_exception_spike     = EXCLUDED.flag_exception_spike,
        flag_stuck_pending_spike = EXCLUDED.flag_stuck_pending_spike,
        flag_wait_p99_spike      = EXCLUDED.flag_wait_p99_spike,
        flag_volume_anomaly      = EXCLUDED.flag_volume_anomaly,
        flag_low_completion      = EXCLUDED.flag_low_completion,
        is_anomalous             = EXCLUDED.is_anomalous,
        anomaly_reasons          = EXCLUDED.anomaly_reasons,
        threshold_snapshot       = EXCLUDED.threshold_snapshot,
        computed_at              = NOW()
"""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="from_date", required=True)
    p.add_argument("--to",   dest="to_date",   required=True)
    p.add_argument("--rolling-window-days", type=int, default=7)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    start = datetime.fromisoformat(args.from_date).replace(tzinfo=timezone.utc)
    end   = datetime.fromisoformat(args.to_date).replace(tzinfo=timezone.utc)

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 1

    days_metrics: list[DailyMetrics] = []
    flagged_days: list[tuple[datetime, list[str]]] = []
    with psycopg.connect(dsn) as conn:
        d = start
        while d < end:
            m = fetch_day(conn, d)
            history = days_metrics[-args.rolling_window_days:]   # use prior days only
            flags, reasons, snapshot = evaluate_flags(m, history)
            is_anom = any(flags.values())

            print(f"[{m.sample_date.date()}] n={m.n_total:>7,} excp={m.exception_rate or 0:.3f} stuck={m.stuck_pending_rate or 0:.3f} wait_p99={m.wait_p99_s or 0:>7.0f}s anom={is_anom}{' (' + ','.join(reasons) + ')' if reasons else ''}", file=sys.stderr)

            if not args.dry_run:
                row = {
                    "sample_date": m.sample_date.date(),
                    "n_total": m.n_total, "n_completed": m.n_completed, "n_failed": m.n_failed,
                    "n_exception": m.n_exception, "n_worker_shutdown": m.n_worker_shutdown,
                    "n_claim_expired": m.n_claim_expired, "n_deadline_exceeded": m.n_deadline_exceeded,
                    "n_canceled": m.n_canceled, "n_started": m.n_started, "n_pending_no_start": m.n_pending_no_start,
                    "exception_rate": m.exception_rate, "stuck_pending_rate": m.stuck_pending_rate,
                    "completion_rate": m.completion_rate, "wait_p99_s": m.wait_p99_s, "run_p99_s": m.run_p99_s,
                    **flags,
                    "is_anomalous": is_anom,
                    "anomaly_reasons": reasons,
                    "threshold_snapshot": json.dumps(snapshot),
                }
                with conn.cursor() as cur:
                    cur.execute(UPSERT_SQL, row)
                conn.commit()

            days_metrics.append(m)
            if is_anom:
                flagged_days.append((m.sample_date, reasons))
            d += timedelta(days=1)

    print(f"\n[health] processed {len(days_metrics)} days; flagged {len(flagged_days)}", file=sys.stderr)
    for day, reasons in flagged_days:
        print(f"  {day.date()}: {','.join(reasons)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
