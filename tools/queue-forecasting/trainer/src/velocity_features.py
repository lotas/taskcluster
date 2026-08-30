"""Compute per-row queue-velocity features from worker-count time-series.

Given a DataFrame with (task_id, run_id, pending_at, task_queue_id) and a
time-series DataFrame of worker counts, compute for each row:
  - running_workers_now, claimed_tasks_now, existing_capacity_now (point-in-time,
    last sample <= pending_at within tolerance; NaN otherwise)
  - idle_workers_now = max(running - claimed, 0)
  - utilization_now = claimed / max(running, 1)
  - provision_lag_now = existing - running (NaN for static pools with no existing)
  - running_workers_{W}m_avg  (mean over [pending_at - W, pending_at))
  - running_workers_{W}m_delta (running_workers_now - running_workers_{W}m_avg)
  - tasks_per_worker = queue_pending / max(running, 1)   (needs queue_pending on input)
  - pool_kind (from pools dim table)
  - provider_type (from pools dim table)
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _align_join_key(reference: pd.DataFrame, main: pd.DataFrame,
                    key: str = "task_queue_id") -> pd.DataFrame:
    """Cast a reference table's join key to the main frame's dtype.

    `data_loader._downcast_categorical_columns` makes the main frame's
    `task_queue_id` a `category` -- deliberately, it was the dominant fixable
    memory cost -- while the reference tables keep whatever their source
    produced. Under pandas 3 that is `str` dtype from both `read_parquet` and a
    psycopg fetch, and `merge`/`merge_asof` REFUSE mismatched key dtypes:

        MergeError: incompatible merge keys [0] CategoricalDtype(..) and
        StringDtype(..), must be the same type

    The SMALL side is cast, never the large one: widening the main frame's key
    back to strings would undo the downcast for every row in the training
    window. A queue absent from the main frame's categories becomes NaN and so
    matches nothing, which is what a join miss already did.
    """
    if key not in reference.columns or key not in main.columns:
        return reference
    if reference[key].dtype == main[key].dtype:
        return reference
    return reference.assign(**{key: reference[key].astype(main[key].dtype)})


def add_velocity_features(
    df: pd.DataFrame,
    worker_counts: pd.DataFrame,
    worker_pools: pd.DataFrame,
    *,
    tolerance_minutes: int = 10,
    trailing_windows_minutes: tuple[int, ...] = (60,),
) -> pd.DataFrame:
    df = df.copy()
    if "task_queue_id" not in df.columns or "pending_at" not in df.columns:
        raise ValueError("input df must contain task_queue_id and pending_at")

    # --- Point-in-time features via merge_asof per queue ---
    df_sorted = df.sort_values("pending_at").reset_index()
    worker_counts = _align_join_key(worker_counts, df)
    wc_sorted = (
        worker_counts.sort_values("sampled_at")
        if not worker_counts.empty
        else worker_counts
    )

    if not worker_counts.empty:
        # merge_asof with "by=task_queue_id" requires both sides sorted on the time col
        merged = pd.merge_asof(
            df_sorted,
            wc_sorted[["task_queue_id", "sampled_at", "running_workers",
                        "claimed_tasks", "existing_capacity"]],
            left_on="pending_at",
            right_on="sampled_at",
            by="task_queue_id",
            direction="backward",
            tolerance=pd.Timedelta(minutes=tolerance_minutes),
        )
    else:
        merged = df_sorted.copy()
        for col in ("running_workers", "claimed_tasks", "existing_capacity"):
            merged[col] = np.nan

    # Restore original row order
    merged = merged.set_index("index").sort_index()

    # Force float64: psycopg can hand back INTEGER/DECIMAL columns as Python
    # objects, and unmatched merge_asof rows insert None. Either path leaves
    # the column with object dtype, which LightGBM refuses. Coerce here so
    # every downstream derivation (idle/utilization/provision_lag/tasks_per_worker)
    # inherits float64 with NaN for missing rather than object with None.
    df["running_workers_now"] = pd.to_numeric(
        merged["running_workers"].values, errors="coerce"
    ).astype("float64")
    df["claimed_tasks_now"] = pd.to_numeric(
        merged["claimed_tasks"].values, errors="coerce"
    ).astype("float64")
    df["existing_capacity_now"] = pd.to_numeric(
        merged["existing_capacity"].values, errors="coerce"
    ).astype("float64")

    rw = df["running_workers_now"]
    ct = df["claimed_tasks_now"]
    df["idle_workers_now"] = (rw - ct).clip(lower=0).astype("float64")
    df["utilization_now"] = (ct / rw.where(rw > 0).replace(0, np.nan)).astype("float64")
    df["provision_lag_now"] = (df["existing_capacity_now"] - rw).astype("float64")

    # tasks_per_worker needs queue_pending (already in the main frame for wait model)
    if "queue_pending" in df.columns:
        qp = pd.to_numeric(df["queue_pending"], errors="coerce").astype("float64")
        df["tasks_per_worker"] = (
            qp / rw.where(rw > 0).replace(0, np.nan)
        ).astype("float64")

    # --- Trailing-window averages (vectorised) ---
    for w_min in trailing_windows_minutes:
        col_avg = f"running_workers_{w_min}m_avg"
        col_delta = f"running_workers_{w_min}m_delta"
        df[col_avg], df[col_delta] = _trailing_mean_and_delta(
            df, worker_counts, window_minutes=w_min
        )

    # --- Pool dimension join ---
    if worker_pools is not None and not worker_pools.empty:
        worker_pools = _align_join_key(worker_pools, df)
        df = df.merge(
            worker_pools[["task_queue_id", "pool_kind", "provider_type"]],
            on="task_queue_id",
            how="left",
        )

    return df


def _trailing_mean_and_delta(
    df: pd.DataFrame, worker_counts: pd.DataFrame, window_minutes: int
) -> tuple[pd.Series, pd.Series]:
    """For each row, compute the mean of running_workers over
    [pending_at - window, pending_at) within the row's task_queue_id,
    and the delta between running_workers_now and that mean.

    Vectorised via per-queue cumulative prefix sums + np.searchsorted —
    O(R log M) per queue instead of the previous O(R * M) row loop.
    """
    n = len(df)
    out_avg = np.full(n, np.nan)
    out_delta = np.full(n, np.nan)

    if worker_counts.empty:
        return pd.Series(out_avg, index=df.index), pd.Series(out_delta, index=df.index)

    # Sort once globally; per-queue groups will be sorted as a consequence.
    wc = worker_counts.sort_values("sampled_at")
    window_td = np.timedelta64(window_minutes * 60, "s")

    pending = df["pending_at"].to_numpy(dtype="datetime64[ns]")
    queues = df["task_queue_id"].to_numpy()
    running_now = df.get("running_workers_now")

    # Build per-queue cumulative arrays keyed by sampled_at.
    tr_by_queue: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for q, g in wc.groupby("task_queue_id", sort=False):
        sampled = g["sampled_at"].to_numpy(dtype="datetime64[ns]")
        rw_arr = g["running_workers"].to_numpy(dtype="float64")
        mask = ~np.isnan(rw_arr)
        fill = np.where(mask, rw_arr, 0.0)
        cum_sum = np.concatenate([[0.0], np.cumsum(fill)])
        cum_n = np.concatenate([[0], np.cumsum(mask.astype(np.int64))])
        tr_by_queue[q] = (sampled, cum_sum, cum_n)

    for q in np.unique(queues):
        entry = tr_by_queue.get(q)
        if entry is None:
            # Queue has no worker-count history — leave as NaN.
            continue

        sampled, cum_sum, cum_n = entry
        rows = np.where(queues == q)[0]
        t_hi = pending[rows]
        t_lo = t_hi - window_td

        idx_hi = np.searchsorted(sampled, t_hi, side="left")
        idx_lo = np.searchsorted(sampled, t_lo, side="left")

        sum_ = cum_sum[idx_hi] - cum_sum[idx_lo]
        n_ = cum_n[idx_hi] - cum_n[idx_lo]
        avg = np.where(n_ > 0, sum_ / np.maximum(n_, 1), np.nan)
        out_avg[rows] = avg

    # Delta: running_workers_now - avg (NaN if either is NaN).
    if running_now is not None:
        rn = running_now.to_numpy(dtype="float64")
        # np.nan arithmetic propagates NaN correctly here.
        out_delta = rn - out_avg

    return pd.Series(out_avg, index=df.index), pd.Series(out_delta, index=df.index)
