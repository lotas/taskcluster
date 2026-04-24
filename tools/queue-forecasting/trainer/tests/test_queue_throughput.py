import numpy as np
import pandas as pd

from src.queue_throughput import add_throughput_features


def _runs(rows):
    return pd.DataFrame(rows)


def test_basic_throughput_and_wait():
    """Events in the trailing 60m window should count and contribute to mean wait."""
    df = pd.DataFrame([
        {"task_id": "target", "run_id": 0,
         "pending_at": pd.Timestamp("2026-04-20 10:00", tz="UTC"),
         "task_queue_id": "q1"},
    ])
    # Three completed runs in the 60m window [09:00, 10:00):
    #  started: 09:15 waited 120s, ran 300s, resolved 09:20
    #  started: 09:33 waited  60s, ran 180s, resolved 09:36
    #  started: 09:51:40 waited 600s, ran 100s, resolved 09:53:20
    # One outside the window (resolved before 09:00)
    runs = _runs([
        {"task_queue_id": "q1",
         "started_at": pd.Timestamp("2026-04-20 09:15", tz="UTC"),
         "resolved_at": pd.Timestamp("2026-04-20 09:20", tz="UTC"),
         "wait_duration_s": 120.0, "run_duration_s": 300.0},
        {"task_queue_id": "q1",
         "started_at": pd.Timestamp("2026-04-20 09:33", tz="UTC"),
         "resolved_at": pd.Timestamp("2026-04-20 09:36", tz="UTC"),
         "wait_duration_s": 60.0, "run_duration_s": 180.0},
        {"task_queue_id": "q1",
         "started_at": pd.Timestamp("2026-04-20 09:51:40", tz="UTC"),
         "resolved_at": pd.Timestamp("2026-04-20 09:53:20", tz="UTC"),
         "wait_duration_s": 600.0, "run_duration_s": 100.0},
        # out of window
        {"task_queue_id": "q1",
         "started_at": pd.Timestamp("2026-04-20 08:00", tz="UTC"),
         "resolved_at": pd.Timestamp("2026-04-20 08:30", tz="UTC"),
         "wait_duration_s": 10.0, "run_duration_s": 1800.0},
    ])
    out = add_throughput_features(df, runs, windows_minutes=(60,))
    assert out["queue_tasks_completed_60m"].iloc[0] == 3
    assert out["queue_tasks_started_60m"].iloc[0]   == 3
    assert np.isclose(out["queue_avg_wait_60m"].iloc[0], (120 + 60 + 600) / 3)
    assert np.isclose(out["queue_avg_run_time_60m"].iloc[0], (300 + 180 + 100) / 3)


def test_leakage_gate_excludes_future_resolutions():
    """A run that resolved AFTER pending_at must be excluded even if started before."""
    df = pd.DataFrame([
        {"task_id": "target", "run_id": 0,
         "pending_at": pd.Timestamp("2026-04-20 10:00", tz="UTC"),
         "task_queue_id": "q1"},
    ])
    runs = _runs([
        # Started before pending, resolved AFTER — must not leak.
        {"task_queue_id": "q1",
         "started_at": pd.Timestamp("2026-04-20 09:55", tz="UTC"),
         "resolved_at": pd.Timestamp("2026-04-20 10:30", tz="UTC"),
         "wait_duration_s": 120.0, "run_duration_s": 1800.0},
    ])
    out = add_throughput_features(df, runs, windows_minutes=(60,))
    assert out["queue_tasks_completed_60m"].iloc[0] == 0
    assert out["queue_tasks_started_60m"].iloc[0]   == 0
    assert pd.isna(out["queue_avg_wait_60m"].iloc[0])
    assert pd.isna(out["queue_avg_run_time_60m"].iloc[0])


def test_empty_window_returns_nan():
    df = pd.DataFrame([
        {"task_id": "target", "run_id": 0,
         "pending_at": pd.Timestamp("2026-04-20 10:00", tz="UTC"),
         "task_queue_id": "q_quiet"},
    ])
    runs = _runs([])  # no runs at all
    out = add_throughput_features(df, runs, windows_minutes=(60,))
    # With no runs the code sees no group for q_quiet → leaves arrays as NaN.
    # queue_tasks_completed_60m is NaN (not 0) when queue has no history at all.
    assert pd.isna(out["queue_tasks_completed_60m"].iloc[0])
    assert pd.isna(out["queue_avg_wait_60m"].iloc[0])


def test_multiple_windows():
    df = pd.DataFrame([
        {"task_id": "target", "run_id": 0,
         "pending_at": pd.Timestamp("2026-04-20 10:00", tz="UTC"),
         "task_queue_id": "q1"},
    ])
    # 1 run in last 15m, 2 more in last 60m (but outside 15m window).
    runs = _runs([
        {"task_queue_id": "q1",
         "started_at": pd.Timestamp("2026-04-20 09:50", tz="UTC"),  # in 15m window
         "resolved_at": pd.Timestamp("2026-04-20 09:55", tz="UTC"),
         "wait_duration_s": 30.0, "run_duration_s": 300.0},
        {"task_queue_id": "q1",
         "started_at": pd.Timestamp("2026-04-20 09:20", tz="UTC"),  # outside 15m, in 60m
         "resolved_at": pd.Timestamp("2026-04-20 09:25", tz="UTC"),
         "wait_duration_s": 60.0, "run_duration_s": 300.0},
        {"task_queue_id": "q1",
         "started_at": pd.Timestamp("2026-04-20 09:10", tz="UTC"),
         "resolved_at": pd.Timestamp("2026-04-20 09:15", tz="UTC"),
         "wait_duration_s": 90.0, "run_duration_s": 300.0},
    ])
    out = add_throughput_features(df, runs, windows_minutes=(15, 60))
    assert out["queue_tasks_completed_15m"].iloc[0] == 1
    assert out["queue_tasks_completed_60m"].iloc[0] == 3
    assert np.isclose(out["queue_avg_wait_15m"].iloc[0], 30.0)
    assert np.isclose(out["queue_avg_wait_60m"].iloc[0], (30 + 60 + 90) / 3)


def test_empty_df_returns_empty():
    """add_throughput_features on an empty input returns an empty DataFrame."""
    df = pd.DataFrame(columns=["task_id", "run_id", "pending_at", "task_queue_id"])
    runs = _runs([])
    out = add_throughput_features(df, runs, windows_minutes=(60,))
    assert out.empty


def test_missing_required_df_columns_raises():
    df = pd.DataFrame([{"task_id": "t1", "run_id": 0}])  # missing pending_at, task_queue_id
    runs = _runs([])
    import pytest
    with pytest.raises(ValueError, match="missing required columns"):
        add_throughput_features(df, runs)


def test_missing_required_runs_columns_raises():
    df = pd.DataFrame([{
        "task_id": "t1", "run_id": 0,
        "pending_at": pd.Timestamp("2026-04-20 10:00", tz="UTC"),
        "task_queue_id": "q1",
    }])
    runs = pd.DataFrame([{"task_queue_id": "q1"}])  # missing started_at etc.
    import pytest
    with pytest.raises(ValueError, match="task_runs missing required columns"):
        add_throughput_features(df, runs)


def test_queue_with_runs_but_none_in_window():
    """Queue has history but none falls in the trailing window — counts 0, mean NaN."""
    df = pd.DataFrame([
        {"task_id": "target", "run_id": 0,
         "pending_at": pd.Timestamp("2026-04-20 10:00", tz="UTC"),
         "task_queue_id": "q1"},
    ])
    # Only runs from 2 hours ago — outside the 60m window.
    runs = _runs([
        {"task_queue_id": "q1",
         "started_at": pd.Timestamp("2026-04-20 07:00", tz="UTC"),
         "resolved_at": pd.Timestamp("2026-04-20 07:30", tz="UTC"),
         "wait_duration_s": 100.0, "run_duration_s": 1800.0},
    ])
    out = add_throughput_features(df, runs, windows_minutes=(60,))
    assert out["queue_tasks_completed_60m"].iloc[0] == 0
    assert pd.isna(out["queue_avg_wait_60m"].iloc[0])
