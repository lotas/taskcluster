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


# 5-minute sample cadence x 24h. Used to detect sampler outages: if we see
# fewer than half the expected samples on a day with any data at all, the
# sampler was at least partially down.
EXPECTED_WORKER_SAMPLES_PER_DAY = 288


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


WORKER_METRICS_SQL = """
WITH per_timestamp AS (
    SELECT sampled_at,
           SUM(COALESCE(existing_capacity, 0)) AS total_capacity,
           SUM(COALESCE(running_workers, 0))   AS total_running
    FROM queue_forecast_worker_counts
    WHERE sampled_at >= %(day_start)s
      AND sampled_at <  %(day_end)s
    GROUP BY sampled_at
)
SELECT COUNT(*)                                                                    AS n_samples,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY total_capacity)                 AS total_capacity_p50,
       MIN(total_capacity)                                                         AS total_capacity_min,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY total_running)                  AS total_running_p50,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY total_running::float / NULLIF(total_capacity, 0)) AS utilization_p50
FROM per_timestamp;
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
    # Worker-count daily aggregates. Default to None / 0 so existing test
    # helpers (and any caller that doesn't yet construct worker fields)
    # continue to work without modification.
    total_capacity_p50: int | None = None
    total_capacity_min: int | None = None
    total_running_p50: int | None = None
    utilization_p50: float | None = None
    n_worker_samples: int = 0


@dataclass
class WorkerMetrics:
    total_capacity_p50: int | None
    total_capacity_min: int | None
    total_running_p50: int | None
    utilization_p50: float | None
    n_samples: int


# Flags that contribute to the default `is_anomalous` aggregate. Other
# (informational) flags are still persisted but only kick in when callers
# opt them in via the trainer's anomaly_filter.flag_subset config.
_DEFAULT_IS_ANOMALOUS_FLAGS = (
    "flag_exception_spike",
    "flag_stuck_pending_spike",
    "flag_wait_p99_spike",
    "flag_volume_anomaly",
    "flag_low_completion",
    "flag_capacity_drop",
    "flag_sampler_offline",
)


def is_anomalous_default(flags: dict[str, bool]) -> bool:
    """Aggregate flags into the default is_anomalous bit.

    Informational flags (capacity_spike, low_utilization) are excluded:
    they classify operational regimes but don't make a day's training
    data unusable.
    """
    return any(flags[k] for k in _DEFAULT_IS_ANOMALOUS_FLAGS if k in flags)


def merge_latched(
    existing: dict | None,
    new_flags: dict[str, bool],
    new_reasons: list[str],
) -> tuple[dict[str, bool], list[str], bool]:
    """Latch flags so a day's verdict can only ever gain anomalies, never lose them.

    Once a flag (or ``is_anomalous``) is true in the stored row it stays true on
    every subsequent recompute. This guarantees a day we marked anomalous is
    never silently cleared by a later tick — the property the whole baseline /
    training exclusion relies on for reproducibility.

    Metric columns (counts, rates, capacities) are NOT latched; only the
    anomaly verdict is. ``existing`` is the previously stored row (or None for a
    first-time insert). Returns ``(latched_flags, latched_reasons, is_anomalous)``.
    """
    if existing is None:
        flags = dict(new_flags)
        return flags, list(new_reasons), is_anomalous_default(flags)

    flags = {k: bool(existing.get(k)) or bool(new_flags.get(k)) for k in new_flags}

    # Union reasons, keeping a stable order: previously-stored reasons first,
    # then any newly-observed ones.
    reasons = list(existing.get("anomaly_reasons") or [])
    for r in new_reasons:
        if r not in reasons:
            reasons.append(r)

    is_anom = is_anomalous_default(flags) or bool(existing.get("is_anomalous"))
    return flags, reasons, is_anom


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
    wm = fetch_worker_metrics(conn, day_start)
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
        total_capacity_p50  = wm.total_capacity_p50,
        total_capacity_min  = wm.total_capacity_min,
        total_running_p50   = wm.total_running_p50,
        utilization_p50     = wm.utilization_p50,
        n_worker_samples    = wm.n_samples,
    )


def fetch_worker_metrics(conn, day_start: datetime) -> WorkerMetrics:
    """Aggregate worker-count samples for a day.

    Returns all-None when there are no samples for the day so callers can
    treat absent worker-count history as "no signal" (not anomaly).
    """
    day_end = day_start + timedelta(days=1)
    with conn.cursor() as cur:
        cur.execute(WORKER_METRICS_SQL, {"day_start": day_start, "day_end": day_end})
        row = cur.fetchone()
    n_samples = int(row[0] or 0)
    if n_samples == 0:
        return WorkerMetrics(None, None, None, None, 0)
    cap_p50, cap_min, run_p50, util_p50 = row[1], row[2], row[3], row[4]
    # Postgres percentile_cont returns numeric for an integer input -> cast.
    return WorkerMetrics(
        total_capacity_p50 = int(cap_p50) if cap_p50 is not None else None,
        total_capacity_min = int(cap_min) if cap_min is not None else None,
        total_running_p50  = int(run_p50) if run_p50 is not None else None,
        utilization_p50    = float(util_p50) if util_p50 is not None else None,
        n_samples          = n_samples,
    )


_FLAG_COLUMNS = (
    "flag_exception_spike", "flag_stuck_pending_spike", "flag_wait_p99_spike",
    "flag_volume_anomaly", "flag_low_completion",
    "flag_capacity_drop", "flag_capacity_spike", "flag_low_utilization",
    "flag_sampler_offline",
)


def fetch_existing_health(conn, day_date) -> dict | None:
    """Return the previously stored anomaly verdict for a day, or None.

    Used to latch flags (see ``merge_latched``): an existing anomalous marking
    must survive recomputation. Only the verdict columns are read — metrics are
    always recomputed fresh.
    """
    cols = (*_FLAG_COLUMNS, "is_anomalous", "anomaly_reasons")
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(cols)} FROM queue_forecast_daily_health WHERE sample_date = %(d)s",
            {"d": day_date},
        )
        row = cur.fetchone()
    if row is None:
        return None
    return dict(zip(cols, row))


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
        "flag_capacity_drop":       False,
        "flag_capacity_spike":      False,
        "flag_low_utilization":     False,
        "flag_sampler_offline":     False,
    }
    reasons: list[str] = []

    med_excp     = trailing_median([h.exception_rate     for h in history])
    med_stuck    = trailing_median([h.stuck_pending_rate for h in history])
    med_wait_p99 = trailing_median([h.wait_p99_s         for h in history])
    med_volume   = trailing_median([float(h.n_total)     for h in history])
    med_total_capacity = trailing_median(
        [float(h.total_capacity_p50) for h in history if h.total_capacity_p50 is not None]
    )

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
        "trailing_median_total_capacity_p50": med_total_capacity,
        "abs_low_utilization_threshold":      0.4,
        "expected_worker_samples_per_day":    EXPECTED_WORKER_SAMPLES_PER_DAY,
        "rel_capacity_drop_x":                0.5,
        "rel_capacity_spike_x":               2.0,
        "rel_sampler_offline_x":              0.5,
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

    # ---- Worker-side flags ----
    # Skip everything when n_worker_samples == 0: that's "no data, no signal"
    # (e.g. backfilled task data from before the worker-counter started).
    if today.n_worker_samples > 0:
        # Sampler offline: partial-day outages. Full silence (n=0) is handled
        # by the guard above and treated as no-signal, not anomaly.
        if today.n_worker_samples < 0.5 * EXPECTED_WORKER_SAMPLES_PER_DAY:
            flags["flag_sampler_offline"] = True
            reasons.append("sampler_offline")

        # Capacity drop / spike — both relative to trailing 7d median.
        if (
            today.total_capacity_p50 is not None
            and med_total_capacity is not None
            and med_total_capacity > 0
        ):
            if today.total_capacity_p50 < 0.5 * med_total_capacity:
                flags["flag_capacity_drop"] = True
                reasons.append("capacity_drop")
            elif today.total_capacity_p50 > 2.0 * med_total_capacity:
                flags["flag_capacity_spike"] = True
                reasons.append("capacity_spike")

        # Low utilization — absolute (utilization is a ratio, comparable across days).
        if today.utilization_p50 is not None and today.utilization_p50 < 0.4:
            flags["flag_low_utilization"] = True
            reasons.append("low_utilization")

    return flags, reasons, snapshot


def process_window(fetch, start, end, rolling_window_days: int):
    """Evaluate every day in [start, end) with a *deterministic* trailing window.

    The trailing median is always computed over the real ``rolling_window_days``
    calendar days immediately before each day — seeded from history before the
    loop begins — so a day's flags never depend on which ``--from`` the run
    happened to use. Without this seed the first days of a window saw a
    truncated (or empty) history, which made the same calendar day flip
    anomalous/clean depending on its position in the sliding loop window.

    ``fetch`` maps a day_start datetime -> DailyMetrics. Yields, for each day in
    [start, end): ``(metrics, flags, reasons, snapshot, is_anomalous)``.
    """
    days_metrics: list[DailyMetrics] = []

    # Seed the trailing window with the real prior N calendar days (not upserted).
    d = start - timedelta(days=rolling_window_days)
    while d < start:
        days_metrics.append(fetch(d))
        d += timedelta(days=1)

    d = start
    while d < end:
        m = fetch(d)
        history = days_metrics[-rolling_window_days:]
        flags, reasons, snapshot = evaluate_flags(m, history)
        is_anom = is_anomalous_default(flags)
        yield m, flags, reasons, snapshot, is_anom
        days_metrics.append(m)
        d += timedelta(days=1)


UPSERT_SQL = """
    INSERT INTO queue_forecast_daily_health (
        sample_date,
        n_total, n_completed, n_failed, n_exception, n_worker_shutdown, n_claim_expired,
        n_deadline_exceeded, n_canceled, n_started, n_pending_no_start,
        exception_rate, stuck_pending_rate, completion_rate, wait_p99_s, run_p99_s,
        total_capacity_p50, total_capacity_min, total_running_p50, utilization_p50, n_worker_samples,
        flag_exception_spike, flag_stuck_pending_spike, flag_wait_p99_spike,
        flag_volume_anomaly, flag_low_completion,
        flag_capacity_drop, flag_capacity_spike, flag_low_utilization, flag_sampler_offline,
        is_anomalous, anomaly_reasons, threshold_snapshot, computed_at
    ) VALUES (
        %(sample_date)s,
        %(n_total)s, %(n_completed)s, %(n_failed)s, %(n_exception)s, %(n_worker_shutdown)s, %(n_claim_expired)s,
        %(n_deadline_exceeded)s, %(n_canceled)s, %(n_started)s, %(n_pending_no_start)s,
        %(exception_rate)s, %(stuck_pending_rate)s, %(completion_rate)s, %(wait_p99_s)s, %(run_p99_s)s,
        %(total_capacity_p50)s, %(total_capacity_min)s, %(total_running_p50)s, %(utilization_p50)s, %(n_worker_samples)s,
        %(flag_exception_spike)s, %(flag_stuck_pending_spike)s, %(flag_wait_p99_spike)s,
        %(flag_volume_anomaly)s, %(flag_low_completion)s,
        %(flag_capacity_drop)s, %(flag_capacity_spike)s, %(flag_low_utilization)s, %(flag_sampler_offline)s,
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
        total_capacity_p50 = EXCLUDED.total_capacity_p50,
        total_capacity_min = EXCLUDED.total_capacity_min,
        total_running_p50  = EXCLUDED.total_running_p50,
        utilization_p50    = EXCLUDED.utilization_p50,
        n_worker_samples   = EXCLUDED.n_worker_samples,
        -- Latch the anomaly verdict atomically: once a flag is true it stays
        -- true, regardless of what this writer computed. The OR is evaluated
        -- against the row's *current* value at update time, so a backfill
        -- overlapping the hourly monitor can never clear a flag another
        -- process just set. (merge_latched mirrors this for first inserts and
        -- for the log/dry-run preview; the OR here is the load-bearing one.)
        flag_exception_spike     = queue_forecast_daily_health.flag_exception_spike     OR EXCLUDED.flag_exception_spike,
        flag_stuck_pending_spike = queue_forecast_daily_health.flag_stuck_pending_spike OR EXCLUDED.flag_stuck_pending_spike,
        flag_wait_p99_spike      = queue_forecast_daily_health.flag_wait_p99_spike      OR EXCLUDED.flag_wait_p99_spike,
        flag_volume_anomaly      = queue_forecast_daily_health.flag_volume_anomaly      OR EXCLUDED.flag_volume_anomaly,
        flag_low_completion      = queue_forecast_daily_health.flag_low_completion      OR EXCLUDED.flag_low_completion,
        flag_capacity_drop       = queue_forecast_daily_health.flag_capacity_drop       OR EXCLUDED.flag_capacity_drop,
        flag_capacity_spike      = queue_forecast_daily_health.flag_capacity_spike      OR EXCLUDED.flag_capacity_spike,
        flag_low_utilization     = queue_forecast_daily_health.flag_low_utilization     OR EXCLUDED.flag_low_utilization,
        flag_sampler_offline     = queue_forecast_daily_health.flag_sampler_offline     OR EXCLUDED.flag_sampler_offline,
        is_anomalous             = queue_forecast_daily_health.is_anomalous             OR EXCLUDED.is_anomalous,
        -- Union reasons, preserving first-appearance order (existing first).
        anomaly_reasons          = ARRAY(
            SELECT u.r FROM (
                SELECT r, MIN(ord) AS ord
                FROM unnest(queue_forecast_daily_health.anomaly_reasons || EXCLUDED.anomaly_reasons)
                     WITH ORDINALITY AS t(r, ord)
                GROUP BY r
            ) u ORDER BY u.ord
        ),
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

    n_days = 0
    flagged_days: list[tuple[datetime, list[str]]] = []
    with psycopg.connect(dsn) as conn:
        for m, flags, reasons, snapshot, is_anom in process_window(
            lambda d: fetch_day(conn, d), start, end, args.rolling_window_days
        ):
            n_days += 1

            # Latch: a day already marked anomalous must never be cleared by a
            # later recompute. Merge this run's verdict with what's stored so the
            # log line / dry-run preview reflect the latched outcome. The
            # authoritative latch happens atomically in UPSERT_SQL's ON CONFLICT.
            existing = fetch_existing_health(conn, m.sample_date.date())
            flags, reasons, is_anom = merge_latched(existing, flags, reasons)

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
                    "total_capacity_p50": m.total_capacity_p50,
                    "total_capacity_min": m.total_capacity_min,
                    "total_running_p50":  m.total_running_p50,
                    "utilization_p50":    m.utilization_p50,
                    "n_worker_samples":   m.n_worker_samples,
                    **flags,
                    "is_anomalous": is_anom,
                    "anomaly_reasons": reasons,
                    "threshold_snapshot": json.dumps(snapshot),
                }
                with conn.cursor() as cur:
                    cur.execute(UPSERT_SQL, row)
                conn.commit()

            if is_anom:
                flagged_days.append((m.sample_date, reasons))

    print(f"\n[health] processed {n_days} days; flagged {len(flagged_days)}", file=sys.stderr)
    for day, reasons in flagged_days:
        print(f"  {day.date()}: {','.join(reasons)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
