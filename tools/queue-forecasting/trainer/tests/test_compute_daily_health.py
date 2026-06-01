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
        # Worker-count fields default to "no signal" so existing tests
        # (which only care about task-side flags) keep passing.
        total_capacity_p50=None, total_capacity_min=None,
        total_running_p50=None, utilization_p50=None,
        n_worker_samples=0,
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


# ---------------------------------------------------------------------------
# Worker-side flags
# ---------------------------------------------------------------------------

def test_capacity_drop_flag():
    """today's capacity_p50 = 50; trailing 7d median = 200 -> capacity_drop."""
    from scripts.compute_daily_health import evaluate_flags
    today = _today(total_capacity_p50=50, n_worker_samples=288)
    history = _hist(7, total_capacity_p50=200, n_worker_samples=288)
    flags, reasons, _ = evaluate_flags(today, history)
    assert flags["flag_capacity_drop"]
    assert "capacity_drop" in reasons
    assert not flags["flag_capacity_spike"]


def test_capacity_spike_flag():
    """today's capacity_p50 = 600; trailing median = 200 -> capacity_spike."""
    from scripts.compute_daily_health import evaluate_flags
    today = _today(total_capacity_p50=600, n_worker_samples=288)
    history = _hist(7, total_capacity_p50=200, n_worker_samples=288)
    flags, reasons, _ = evaluate_flags(today, history)
    assert flags["flag_capacity_spike"]
    assert "capacity_spike" in reasons
    assert not flags["flag_capacity_drop"]


def test_low_utilization_flag():
    """utilization_p50 < 0.4 should fire low_utilization regardless of history."""
    from scripts.compute_daily_health import evaluate_flags
    today = _today(utilization_p50=0.25, n_worker_samples=288)
    history = _hist(7, utilization_p50=0.7, n_worker_samples=288)
    flags, reasons, _ = evaluate_flags(today, history)
    assert flags["flag_low_utilization"]
    assert "low_utilization" in reasons


def test_sampler_offline_flag():
    """n_worker_samples=100 < 144 (0.5 * 288) should fire sampler_offline."""
    from scripts.compute_daily_health import evaluate_flags
    today = _today(n_worker_samples=100, total_capacity_p50=200, utilization_p50=0.6)
    history = _hist(7, n_worker_samples=288, total_capacity_p50=200, utilization_p50=0.6)
    flags, reasons, _ = evaluate_flags(today, history)
    assert flags["flag_sampler_offline"]
    assert "sampler_offline" in reasons


def test_no_worker_data_skips_flags():
    """n_worker_samples=0 means no signal: none of the 4 worker flags should fire.

    Handles the "we backfilled task data further back than worker-counter
    has been collecting" case cleanly.
    """
    from scripts.compute_daily_health import evaluate_flags
    today = _today(
        n_worker_samples=0,
        total_capacity_p50=None,
        total_capacity_min=None,
        total_running_p50=None,
        utilization_p50=None,
    )
    # Even with strong history, today should look quiet on the worker side.
    history = _hist(7, n_worker_samples=288, total_capacity_p50=500, utilization_p50=0.7)
    flags, reasons, _ = evaluate_flags(today, history)
    assert not flags["flag_capacity_drop"]
    assert not flags["flag_capacity_spike"]
    assert not flags["flag_low_utilization"]
    assert not flags["flag_sampler_offline"]
    assert "capacity_drop" not in reasons
    assert "capacity_spike" not in reasons
    assert "low_utilization" not in reasons
    assert "sampler_offline" not in reasons


def test_scheduling_failure_classification():
    """wait_p99_spike + capacity_spike + low_utilization all fire together.

    Pins that flags are orthogonal: each independently asserts itself based on
    its own threshold; we don't pick one as "dominant".
    """
    from scripts.compute_daily_health import evaluate_flags
    today = _today(
        wait_p99_s=12000.0,           # 6x history -> wait_p99_spike
        total_capacity_p50=600,       # 3x history -> capacity_spike
        utilization_p50=0.15,         # < 0.4 -> low_utilization
        n_worker_samples=288,
    )
    history = _hist(
        7,
        wait_p99_s=2000.0,
        total_capacity_p50=200,
        utilization_p50=0.7,
        n_worker_samples=288,
    )
    flags, reasons, _ = evaluate_flags(today, history)
    assert flags["flag_wait_p99_spike"]
    assert flags["flag_capacity_spike"]
    assert flags["flag_low_utilization"]
    assert "wait_p99_spike" in reasons
    assert "capacity_spike" in reasons
    assert "low_utilization" in reasons


def test_capacity_spike_alone_not_in_default_is_anomalous():
    """capacity_spike is informational; it must not flip is_anomalous on its own."""
    from scripts.compute_daily_health import evaluate_flags, is_anomalous_default
    today = _today(total_capacity_p50=600, n_worker_samples=288)
    history = _hist(7, total_capacity_p50=200, n_worker_samples=288)
    flags, _, _ = evaluate_flags(today, history)
    assert flags["flag_capacity_spike"]
    assert not is_anomalous_default(flags), (
        "capacity_spike alone must not contribute to is_anomalous (informational only)"
    )


def test_capacity_drop_in_default_is_anomalous():
    """capacity_drop is a real data-quality issue and DOES contribute to is_anomalous."""
    from scripts.compute_daily_health import evaluate_flags, is_anomalous_default
    today = _today(total_capacity_p50=50, n_worker_samples=288)
    history = _hist(7, total_capacity_p50=200, n_worker_samples=288)
    flags, _, _ = evaluate_flags(today, history)
    assert flags["flag_capacity_drop"]
    assert is_anomalous_default(flags)


def test_low_utilization_alone_not_in_default_is_anomalous():
    """low_utilization is informational; not a default anomaly."""
    from scripts.compute_daily_health import evaluate_flags, is_anomalous_default
    today = _today(utilization_p50=0.25, n_worker_samples=288)
    history = _hist(7, utilization_p50=0.7, n_worker_samples=288)
    flags, _, _ = evaluate_flags(today, history)
    assert flags["flag_low_utilization"]
    assert not is_anomalous_default(flags)


def test_sampler_offline_in_default_is_anomalous():
    """sampler_offline indicates real data loss -> contributes to is_anomalous."""
    from scripts.compute_daily_health import evaluate_flags, is_anomalous_default
    today = _today(n_worker_samples=100, total_capacity_p50=200, utilization_p50=0.6)
    history = _hist(7, n_worker_samples=288, total_capacity_p50=200, utilization_p50=0.6)
    flags, _, _ = evaluate_flags(today, history)
    assert flags["flag_sampler_offline"]
    assert is_anomalous_default(flags)


# ---------------------------------------------------------------------------
# Deterministic trailing window (process_window) — a day's verdict must not
# depend on where the processed window happens to start.
# ---------------------------------------------------------------------------

def _all_flags_false():
    return {
        "flag_exception_spike": False, "flag_stuck_pending_spike": False,
        "flag_wait_p99_spike": False, "flag_volume_anomaly": False,
        "flag_low_completion": False, "flag_capacity_drop": False,
        "flag_capacity_spike": False, "flag_low_utilization": False,
        "flag_sampler_offline": False,
    }


def test_process_window_is_position_independent():
    """The same calendar day must get the same flags whether it's mid-window or
    the very first day processed. This is the bug: without seeding the trailing
    window from real prior days, a volume drought on the first day saw an empty
    history and silently dropped flag_volume_anomaly."""
    from datetime import datetime, timezone, timedelta
    from scripts.compute_daily_health import process_window

    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    target = base + timedelta(days=20)

    def fetch(d):
        # All days are normal except the target, a 0.2x volume drought that only
        # trips relative to a trailing median (i.e. needs real history present).
        n = 2000 if d == target else 10000
        return _today(sample_date=d, n_total=n)

    def run(start, end):
        return {
            m.sample_date: (flags, is_anom)
            for m, flags, reasons, snap, is_anom in process_window(fetch, start, end, 7)
        }

    mid = run(target - timedelta(days=2), target + timedelta(days=3))   # target mid-window
    first = run(target, target + timedelta(days=3))                     # target = first day

    assert mid[target][0]["flag_volume_anomaly"], "drought must flag when mid-window"
    assert first[target][0]["flag_volume_anomaly"], "drought must STILL flag when first day"
    assert mid[target] == first[target], "verdict must be position-independent"


# ---------------------------------------------------------------------------
# Latch — once anomalous, always anomalous.
# ---------------------------------------------------------------------------

def test_merge_latched_keeps_anomaly_sticky():
    """A stored anomalous day must survive a recompute that now sees a clean day."""
    from scripts.compute_daily_health import merge_latched
    existing = {
        **_all_flags_false(),
        "flag_volume_anomaly": True,
        "is_anomalous": True,
        "anomaly_reasons": ["volume_anomaly"],
    }
    flags, reasons, is_anom = merge_latched(existing, _all_flags_false(), [])
    assert flags["flag_volume_anomaly"] is True
    assert is_anom is True
    assert reasons == ["volume_anomaly"]


def test_merge_latched_unions_new_anomalies():
    """New anomalies are added on top of the latched ones; reasons are unioned."""
    from scripts.compute_daily_health import merge_latched
    existing = {
        **_all_flags_false(),
        "flag_volume_anomaly": True,
        "is_anomalous": True,
        "anomaly_reasons": ["volume_anomaly"],
    }
    new_flags = {**_all_flags_false(), "flag_exception_spike": True}
    flags, reasons, is_anom = merge_latched(existing, new_flags, ["exception_spike"])
    assert flags["flag_volume_anomaly"] is True   # latched
    assert flags["flag_exception_spike"] is True  # newly observed
    assert is_anom is True
    assert reasons == ["volume_anomaly", "exception_spike"]


def test_merge_latched_first_insert_uses_new_verdict():
    """With no stored row, the new computation is used verbatim."""
    from scripts.compute_daily_health import merge_latched
    new_flags = {**_all_flags_false(), "flag_low_completion": True}
    flags, reasons, is_anom = merge_latched(None, new_flags, ["low_completion"])
    assert flags == new_flags
    assert reasons == ["low_completion"]
    assert is_anom is True


def test_merge_latched_clean_day_stays_clean():
    """A clean stored day with a clean recompute stays clean (no false latch)."""
    from scripts.compute_daily_health import merge_latched
    existing = {**_all_flags_false(), "is_anomalous": False, "anomaly_reasons": []}
    flags, reasons, is_anom = merge_latched(existing, _all_flags_false(), [])
    assert not any(flags.values())
    assert is_anom is False
    assert reasons == []
