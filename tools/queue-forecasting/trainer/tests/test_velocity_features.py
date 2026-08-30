import numpy as np
import pandas as pd

from src.velocity_features import add_velocity_features


def _wc(rows):
    return pd.DataFrame(rows)


def test_point_in_time_join_uses_latest_sample_within_tolerance():
    df = pd.DataFrame([
        {"task_id": "a", "run_id": 0,
         "pending_at": pd.Timestamp("2026-04-20 10:07", tz="UTC"),
         "task_queue_id": "q1", "queue_pending": 50},
    ])
    wc = _wc([
        {"task_queue_id": "q1", "sampled_at": pd.Timestamp("2026-04-20 10:00", tz="UTC"),
         "running_workers": 10, "claimed_tasks": 4, "existing_capacity": 12},
        {"task_queue_id": "q1", "sampled_at": pd.Timestamp("2026-04-20 10:05", tz="UTC"),
         "running_workers": 14, "claimed_tasks": 5, "existing_capacity": 16},
        # Any sample after 10:07 should NOT be picked (direction=backward).
        {"task_queue_id": "q1", "sampled_at": pd.Timestamp("2026-04-20 10:10", tz="UTC"),
         "running_workers": 20, "claimed_tasks": 8, "existing_capacity": 22},
    ])
    out = add_velocity_features(df, wc, pd.DataFrame(columns=["task_queue_id", "pool_kind", "provider_type"]),
                                tolerance_minutes=10)
    assert out["running_workers_now"].iloc[0] == 14
    assert out["claimed_tasks_now"].iloc[0] == 5
    assert out["idle_workers_now"].iloc[0] == 9
    assert np.isclose(out["utilization_now"].iloc[0], 5 / 14)
    assert out["provision_lag_now"].iloc[0] == 2  # 16 - 14
    assert np.isclose(out["tasks_per_worker"].iloc[0], 50 / 14)


def test_out_of_tolerance_yields_nan():
    df = pd.DataFrame([
        {"task_id": "a", "run_id": 0,
         "pending_at": pd.Timestamp("2026-04-20 10:30", tz="UTC"),
         "task_queue_id": "q1", "queue_pending": 50},
    ])
    wc = _wc([
        # Too old: last sample is 30 min before pending_at; tolerance is 10 min.
        {"task_queue_id": "q1", "sampled_at": pd.Timestamp("2026-04-20 10:00", tz="UTC"),
         "running_workers": 10, "claimed_tasks": 4, "existing_capacity": 12},
    ])
    out = add_velocity_features(df, wc, pd.DataFrame(columns=["task_queue_id", "pool_kind", "provider_type"]),
                                tolerance_minutes=10)
    assert pd.isna(out["running_workers_now"].iloc[0])


def test_trailing_window_60m_avg_and_delta():
    df = pd.DataFrame([
        {"task_id": "a", "run_id": 0,
         "pending_at": pd.Timestamp("2026-04-20 11:00", tz="UTC"),
         "task_queue_id": "q1", "queue_pending": 10},
    ])
    # Four samples in the trailing hour [10:00, 11:00): running 10, 14, 18, 22 -> mean 16
    # One earlier sample at 09:55 should NOT be included.
    wc = _wc([
        {"task_queue_id": "q1", "sampled_at": pd.Timestamp("2026-04-20 09:55", tz="UTC"),
         "running_workers": 1, "claimed_tasks": 0, "existing_capacity": None},
        {"task_queue_id": "q1", "sampled_at": pd.Timestamp("2026-04-20 10:00", tz="UTC"),
         "running_workers": 10, "claimed_tasks": 0, "existing_capacity": None},
        {"task_queue_id": "q1", "sampled_at": pd.Timestamp("2026-04-20 10:15", tz="UTC"),
         "running_workers": 14, "claimed_tasks": 0, "existing_capacity": None},
        {"task_queue_id": "q1", "sampled_at": pd.Timestamp("2026-04-20 10:30", tz="UTC"),
         "running_workers": 18, "claimed_tasks": 0, "existing_capacity": None},
        {"task_queue_id": "q1", "sampled_at": pd.Timestamp("2026-04-20 10:45", tz="UTC"),
         "running_workers": 22, "claimed_tasks": 0, "existing_capacity": None},
    ])
    out = add_velocity_features(df, wc, pd.DataFrame(columns=["task_queue_id", "pool_kind", "provider_type"]),
                                tolerance_minutes=20, trailing_windows_minutes=(60,))
    assert np.isclose(out["running_workers_60m_avg"].iloc[0], 16.0)
    # running_workers_now uses the 10:45 sample within 20-min tolerance -> 22
    assert out["running_workers_now"].iloc[0] == 22
    assert np.isclose(out["running_workers_60m_delta"].iloc[0], 6.0)  # 22 - 16


def test_pool_dim_join():
    df = pd.DataFrame([
        {"task_id": "a", "run_id": 0,
         "pending_at": pd.Timestamp("2026-04-20 10:00", tz="UTC"),
         "task_queue_id": "q1", "queue_pending": 0},
        {"task_id": "b", "run_id": 0,
         "pending_at": pd.Timestamp("2026-04-20 10:00", tz="UTC"),
         "task_queue_id": "q_unknown", "queue_pending": 0},
    ])
    wc = pd.DataFrame(columns=["task_queue_id", "sampled_at", "running_workers", "claimed_tasks", "existing_capacity"])
    pools = pd.DataFrame([
        {"task_queue_id": "q1", "pool_kind": "dynamic", "provider_type": "azure"},
    ])
    out = add_velocity_features(df, wc, pools, tolerance_minutes=10)
    assert out.iloc[0]["pool_kind"] == "dynamic"
    assert out.iloc[0]["provider_type"] == "azure"
    assert pd.isna(out.iloc[1]["pool_kind"])
    assert pd.isna(out.iloc[1]["provider_type"])


def test_object_dtype_input_with_unmatched_rows_yields_float64_nan():
    """Regression: psycopg returns INTEGER/DECIMAL columns as Python objects,
    and merge_asof rows that fall outside the tolerance window get None.
    The combination used to produce object-dtype output columns, which
    LightGBM rejects ("pandas dtypes must be int, float or bool"). All five
    derived velocity fields must come out as float64 with NaN (not None) for
    missing rows.
    """
    df = pd.DataFrame([
        # Row 0: outside tolerance -> no match, should be NaN.
        {"task_id": "a", "run_id": 0,
         "pending_at": pd.Timestamp("2026-04-20 10:30", tz="UTC"),
         "task_queue_id": "q1", "queue_pending": 50},
        # Row 1: matches the 10:05 sample.
        {"task_id": "b", "run_id": 0,
         "pending_at": pd.Timestamp("2026-04-20 10:07", tz="UTC"),
         "task_queue_id": "q1", "queue_pending": 50},
    ])
    wc = _wc([
        {"task_queue_id": "q1", "sampled_at": pd.Timestamp("2026-04-20 10:00", tz="UTC"),
         "running_workers": 10, "claimed_tasks": 4, "existing_capacity": 12},
        {"task_queue_id": "q1", "sampled_at": pd.Timestamp("2026-04-20 10:05", tz="UTC"),
         "running_workers": 14, "claimed_tasks": 5, "existing_capacity": 16},
    ])
    # Force object dtype to mimic psycopg's INTEGER/DECIMAL handling.
    for col in ("running_workers", "claimed_tasks", "existing_capacity"):
        wc[col] = wc[col].astype(object)

    out = add_velocity_features(
        df, wc,
        pd.DataFrame(columns=["task_queue_id", "pool_kind", "provider_type"]),
        tolerance_minutes=10,
    )

    affected = [
        "running_workers_now",
        "idle_workers_now",
        "utilization_now",
        "provision_lag_now",
        "tasks_per_worker",
    ]
    for col in affected:
        assert out[col].dtype == np.float64, (
            f"{col} expected float64, got {out[col].dtype}"
        )
        # Row 0 must be NaN (numpy float NaN), not Python None.
        val = out[col].iloc[0]
        assert pd.isna(val)
        assert isinstance(val, float), (
            f"{col} row 0 expected np.float64 NaN, got {type(val).__name__}"
        )

    # Row 1 (matched) must produce the right numeric values, still float64.
    assert out["running_workers_now"].iloc[1] == 14.0
    assert out["idle_workers_now"].iloc[1] == 9.0
    assert np.isclose(out["utilization_now"].iloc[1], 5 / 14)
    assert out["provision_lag_now"].iloc[1] == 2.0
    assert np.isclose(out["tasks_per_worker"].iloc[1], 50 / 14)


def test_categorical_join_key_merges_against_string_reference_tables():
    """`data_loader` hands this function a main frame whose `task_queue_id` is
    `category` and reference tables whose key is `str`.

    Under pandas 3 both `read_parquet` and a psycopg fetch produce `str` dtype,
    and `merge`/`merge_asof` refuse mismatched key dtypes outright -- so
    without alignment the first velocity-config cohort dies in the merge with
    `MergeError: incompatible merge keys`. The main frame's key stays
    categorical: widening it back to strings is the memory cost the downcast
    exists to avoid.
    """
    df = pd.DataFrame({
        "task_id": ["a", "b"],
        "run_id": [0, 0],
        "pending_at": pd.to_datetime(
            ["2026-04-10T12:00:00Z", "2026-04-10T12:00:00Z"]),
        "task_queue_id": pd.Series(["q/one", "q/two"], dtype="str")
        .astype("category"),
        "queue_pending": [10, 20],
    })
    worker_counts = pd.DataFrame({
        "task_queue_id": pd.Series(["q/one", "q/two"], dtype="str"),
        "sampled_at": pd.to_datetime(
            ["2026-04-10T11:59:00Z", "2026-04-10T11:59:00Z"]),
        "running_workers": [4, 8],
        "claimed_tasks": [2, 4],
        "existing_capacity": [5, 9],
    })
    worker_pools = pd.DataFrame({
        "task_queue_id": pd.Series(["q/one", "q/two"], dtype="str"),
        "pool_kind": ["worker-pool", "worker-pool"],
        "provider_type": ["fxci-level1-gcp", "azure2"],
    })

    out = add_velocity_features(df, worker_counts, worker_pools,
                                tolerance_minutes=10,
                                trailing_windows_minutes=(60,))

    assert list(out["running_workers_now"]) == [4.0, 8.0]
    assert list(out["provider_type"]) == ["fxci-level1-gcp", "azure2"]
    # The main frame's key was not widened.
    assert isinstance(out["task_queue_id"].dtype, pd.CategoricalDtype)
