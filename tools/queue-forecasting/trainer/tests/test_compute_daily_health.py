"""Unit tests for the rule-based anomaly detector logic.

These test only the `evaluate_flags` pure function — no DB or psycopg.
The DB integration is exercised when you run the script for real.
"""
from datetime import datetime, timezone


def _today(**kwargs):
    from scripts.compute_daily_health import DailyMetrics
    base = dict(
        sample_date=datetime(2026, 4, 23, tzinfo=timezone.utc),
        n_total=10000, n_completed=9000, n_failed=500, n_exception=100,
        n_worker_shutdown=50, n_claim_expired=20, n_deadline_exceeded=200,
        n_canceled=130, n_started=9700, n_pending_no_start=50,
        exception_rate=0.017, stuck_pending_rate=0.005, completion_rate=0.90,
        wait_p99_s=1800.0, run_p99_s=3600.0,
    )
    base.update(kwargs)
    return DailyMetrics(**base)


def _hist(days: int, **kwargs):
    return [_today(**kwargs) for _ in range(days)]


def test_normal_day_not_flagged():
    from scripts.compute_daily_health import evaluate_flags
    today = _today()
    history = _hist(7)
    flags, reasons, _ = evaluate_flags(today, history)
    assert not any(flags.values()), f"unexpected flags: {flags}, reasons: {reasons}"


def test_exception_spike_absolute():
    from scripts.compute_daily_health import evaluate_flags
    today = _today(exception_rate=0.15)  # > 0.10 absolute
    history = _hist(7)
    flags, reasons, _ = evaluate_flags(today, history)
    assert flags["flag_exception_spike"]
    assert "exception_spike" in reasons


def test_exception_spike_relative():
    from scripts.compute_daily_health import evaluate_flags
    today = _today(exception_rate=0.05)            # below absolute threshold
    history = _hist(7, exception_rate=0.01)        # but 5x trailing median
    flags, reasons, _ = evaluate_flags(today, history)
    assert flags["flag_exception_spike"]


def test_volume_drought():
    from scripts.compute_daily_health import evaluate_flags
    today = _today(n_total=2000)                   # 0.2x normal
    history = _hist(7, n_total=10000)
    flags, reasons, _ = evaluate_flags(today, history)
    assert flags["flag_volume_anomaly"]


def test_volume_flood():
    from scripts.compute_daily_health import evaluate_flags
    today = _today(n_total=30000)                  # 3x normal
    history = _hist(7, n_total=10000)
    flags, reasons, _ = evaluate_flags(today, history)
    assert flags["flag_volume_anomaly"]


def test_volume_zero_flags_anomaly():
    """n_total=0 (e.g. Pulse queue overflow during consumer outage) must flag.
    Earlier the `today.n_total > 0` short-circuit silently passed empty days."""
    from scripts.compute_daily_health import evaluate_flags
    today = _today(n_total=0)
    history = _hist(7, n_total=200000)
    flags, reasons, _ = evaluate_flags(today, history)
    assert flags["flag_volume_anomaly"]
    assert "volume_anomaly" in reasons


def test_low_completion():
    from scripts.compute_daily_health import evaluate_flags
    today = _today(completion_rate=0.6)
    history = _hist(7)
    flags, reasons, _ = evaluate_flags(today, history)
    assert flags["flag_low_completion"]


def test_wait_p99_spike():
    from scripts.compute_daily_health import evaluate_flags
    today = _today(wait_p99_s=12000.0)
    history = _hist(7, wait_p99_s=2000.0)
    flags, reasons, _ = evaluate_flags(today, history)
    assert flags["flag_wait_p99_spike"]


def test_no_history_skips_relative_flags():
    """Relative thresholds should be silently skipped when there's no history."""
    from scripts.compute_daily_health import evaluate_flags
    today = _today(wait_p99_s=12000.0)
    flags, _, _ = evaluate_flags(today, [])
    # Wait p99 spike requires history; should NOT fire
    assert not flags["flag_wait_p99_spike"]
    # But absolute-threshold flags fire regardless
    today2 = _today(exception_rate=0.20)
    flags2, _, _ = evaluate_flags(today2, [])
    assert flags2["flag_exception_spike"]
