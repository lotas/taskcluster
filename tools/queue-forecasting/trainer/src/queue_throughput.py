"""Queue-throughput and drain-rate features derived from queue_forecast_task_runs.

For each training row, computes per-queue windowed metrics over events that
completed before the row's pending_at. Leakage-safe by construction: the
filter ``resolved_at < pending_at`` is applied uniformly.

Output columns (per window W in minutes):
  queue_tasks_started_{W}m     — runs started in Q over [T-W, T)
                                  (only runs whose resolved_at < T, to avoid leaking
                                  information about runs still in progress)
  queue_tasks_completed_{W}m   — runs with resolved_at in Q over [T-W, T)
  queue_avg_wait_{W}m          — mean wait_duration_s of resolved runs in [T-W, T)
  queue_avg_run_time_{W}m      — mean run_duration_s of resolved runs in [T-W, T)

NaN vs 0 policy:
  - If the queue has NO history at all in task_runs (no group), all columns are NaN.
  - If the queue has history but none falls inside the window, started/completed
    are 0 and avg_wait/avg_run_time are NaN (no data to average over).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def add_throughput_features(
    df: pd.DataFrame,
    task_runs: pd.DataFrame,
    *,
    windows_minutes: tuple[int, ...] = (15, 60),
) -> pd.DataFrame:
    """Return *df* with additional throughput columns per window.

    All features are NaN when there is no matching history for the row's
    (queue, time). When history exists but the window is empty, started/
    completed counts are 0 and averages are NaN.

    Parameters
    ----------
    df:
        Training rows. Required columns: task_id, run_id, pending_at,
        task_queue_id.
    task_runs:
        Reference table of task runs. Required columns: task_queue_id,
        started_at, resolved_at, wait_duration_s, run_duration_s.
        Only rows with a non-null resolved_at are considered.
    windows_minutes:
        Trailing window lengths in minutes. Each window W produces four
        columns: queue_tasks_started_{W}m, queue_tasks_completed_{W}m,
        queue_avg_wait_{W}m, queue_avg_run_time_{W}m.
    """
    if df.empty:
        return df.copy()

    required = {"task_id", "run_id", "pending_at", "task_queue_id"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"input df missing required columns: {missing}")

    tr_required = {
        "task_queue_id", "started_at", "resolved_at",
        "wait_duration_s", "run_duration_s",
    }
    # A fully-empty DataFrame (pd.DataFrame([])) has no columns — nothing to
    # validate and no data to process.  Only check required columns when the
    # DataFrame has at least one column (i.e. the caller provided structure).
    if len(task_runs.columns) > 0:
        tr_missing = tr_required - set(task_runs.columns)
        if tr_missing:
            raise ValueError(f"task_runs missing required columns: {tr_missing}")

    out = df.copy()
    n = len(out)

    # Preallocate output arrays per window (default NaN — overwritten for known queues).
    output: dict[str, np.ndarray] = {}
    for w_min in windows_minutes:
        output[f"queue_tasks_started_{w_min}m"] = np.full(n, np.nan)
        output[f"queue_tasks_completed_{w_min}m"] = np.full(n, np.nan)
        output[f"queue_avg_wait_{w_min}m"] = np.full(n, np.nan)
        output[f"queue_avg_run_time_{w_min}m"] = np.full(n, np.nan)

    # Narrow task_runs to rows with a valid (non-null) resolved_at.
    # Guard against a fully schema-less empty frame.
    if not task_runs.empty and "resolved_at" in task_runs.columns:
        tr = task_runs.loc[
            task_runs["resolved_at"].notna(),
            ["task_queue_id", "started_at", "resolved_at", "wait_duration_s", "run_duration_s"],
        ].copy()
    else:
        tr = pd.DataFrame(columns=list(tr_required))

    if tr.empty:
        # No history at all — leave all output arrays as NaN.
        for col, arr in output.items():
            out[col] = arr
        return out

    # --------------------------------------------------------------------------
    # Build per-queue vectorised index structures (sorted by resolved_at).
    # --------------------------------------------------------------------------
    # For each queue we maintain:
    #   resolved_ts   — sorted resolved_at timestamps (datetime64[ns])
    #   started_arr   — started_at in the same resolved_at sort order
    #   wait_cumsum/wait_cumn — prefix sums for wait_duration_s
    #   run_cumsum/run_cumn   — prefix sums for run_duration_s
    #
    # For started counts we additionally keep:
    #   started_ts    — started_at sorted ascending (datetime64[ns])
    #   started_resolved_ts — resolved_at in the same started_at sort order
    #
    # The started count for a row (t_lo, t_hi) is:
    #   count of runs where started_at ∈ [t_lo, t_hi) AND resolved_at < t_hi.
    # Because started_at ≤ resolved_at always, all runs with resolved_at < t_hi
    # and started_at ∈ [t_lo, t_hi) form a subset of [lo_idx, hi_idx) in the
    # started_ts sort.  We still need to verify resolved_at < t_hi within that
    # slice — handled by a second cumulative structure keyed on started_at order.
    #
    # Specifically, for the started-sort we build a prefix-count array
    # cum_resolved_before[k] = number of runs in started_ts[:k] where
    # resolved_at < (some threshold).  Since the threshold varies per row, we
    # cannot build a single prefix sum.  Instead we fall back to a compact
    # per-row loop, but ONLY over the O(runs_per_window) slice — not all history.
    # This is orders of magnitude faster than the original O(all_history) loop.

    def _cumsum_with_count(a: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Prefix sum + non-NaN count for array a (length N → output length N+1)."""
        mask = ~np.isnan(a)
        fill = np.where(mask, a, 0.0)
        cum_sum = np.concatenate([[0.0], np.cumsum(fill)])
        cum_n = np.concatenate([[0], np.cumsum(mask.astype(np.int64))])
        return cum_sum, cum_n

    tr_by_queue: dict[str, dict] = {}
    for q, g in tr.groupby("task_queue_id", sort=False):
        # Sort by resolved_at for completed-count and average lookups.
        g_res = g.sort_values("resolved_at").reset_index(drop=True)
        resolved_ts = g_res["resolved_at"].to_numpy(dtype="datetime64[ns]")
        wait_arr = g_res["wait_duration_s"].to_numpy(dtype="float64")
        run_arr = g_res["run_duration_s"].to_numpy(dtype="float64")

        wait_cumsum, wait_cumn = _cumsum_with_count(wait_arr)
        run_cumsum, run_cumn = _cumsum_with_count(run_arr)

        # Sort by started_at for started-count lookups.
        g_sta = g.sort_values("started_at").reset_index(drop=True)
        started_ts = g_sta["started_at"].to_numpy(dtype="datetime64[ns]")
        resolved_in_sta_order = g_sta["resolved_at"].to_numpy(dtype="datetime64[ns]")

        tr_by_queue[q] = {
            "resolved_ts": resolved_ts,
            "wait_cumsum": wait_cumsum,
            "wait_cumn": wait_cumn,
            "run_cumsum": run_cumsum,
            "run_cumn": run_cumn,
            # For started counts:
            "started_ts": started_ts,
            "resolved_in_sta_order": resolved_in_sta_order,
        }

    # --------------------------------------------------------------------------
    # Group training rows by queue to vectorise per-queue window lookups.
    # --------------------------------------------------------------------------
    pending = out["pending_at"].to_numpy(dtype="datetime64[ns]")
    queues = out["task_queue_id"].to_numpy()

    for w_min in windows_minutes:
        window_td = np.timedelta64(w_min * 60, "s")
        started_col = f"queue_tasks_started_{w_min}m"
        completed_col = f"queue_tasks_completed_{w_min}m"
        avg_wait_col = f"queue_avg_wait_{w_min}m"
        avg_run_col = f"queue_avg_run_time_{w_min}m"

        for q in np.unique(queues):
            entry = tr_by_queue.get(q)
            if entry is None:
                # Queue has no history in task_runs — leave all NaN.
                continue

            rows = np.where(queues == q)[0]
            t_hi = pending[rows]
            t_lo = t_hi - window_td

            resolved_ts = entry["resolved_ts"]

            # ------------------------------------------------------------------
            # COMPLETED counts: resolved_at ∈ [t_lo, t_hi)
            # ------------------------------------------------------------------
            idx_hi = np.searchsorted(resolved_ts, t_hi, side="left")
            idx_lo = np.searchsorted(resolved_ts, t_lo, side="left")
            completed_n = (idx_hi - idx_lo).astype(np.float64)

            # Average wait / run over the completed window.
            wait_sum = entry["wait_cumsum"][idx_hi] - entry["wait_cumsum"][idx_lo]
            wait_n = entry["wait_cumn"][idx_hi] - entry["wait_cumn"][idx_lo]
            run_sum = entry["run_cumsum"][idx_hi] - entry["run_cumsum"][idx_lo]
            run_n = entry["run_cumn"][idx_hi] - entry["run_cumn"][idx_lo]

            avg_wait = np.where(wait_n > 0, wait_sum / np.maximum(wait_n, 1), np.nan)
            avg_run = np.where(run_n > 0, run_sum / np.maximum(run_n, 1), np.nan)

            output[completed_col][rows] = completed_n
            output[avg_wait_col][rows] = avg_wait
            output[avg_run_col][rows] = avg_run

            # ------------------------------------------------------------------
            # STARTED counts: started_at ∈ [t_lo, t_hi) AND resolved_at < t_hi.
            #
            # Strategy: use the started_at-sorted arrays.  searchsorted gives
            # the [sta_lo, sta_hi) slice of runs with started_at ∈ [t_lo, t_hi).
            # Within that slice, count those with resolved_at < t_hi.
            #
            # We avoid the O(M) full-history scan by restricting to the small
            # window slice.  For most queue/row pairs this is tiny.
            # ------------------------------------------------------------------
            started_ts = entry["started_ts"]
            resolved_in_sta = entry["resolved_in_sta_order"]

            sta_hi = np.searchsorted(started_ts, t_hi, side="left")
            sta_lo = np.searchsorted(started_ts, t_lo, side="left")

            started_counts = np.empty(len(rows), dtype=np.float64)
            for local_i in range(len(rows)):
                lo = sta_lo[local_i]
                hi = sta_hi[local_i]
                if lo == hi:
                    started_counts[local_i] = 0.0
                    continue
                # Count runs in the started_at window whose resolved_at < t_hi.
                t_hi_i = t_hi[local_i]
                started_counts[local_i] = float(
                    (resolved_in_sta[lo:hi] < t_hi_i).sum()
                )

            output[started_col][rows] = started_counts

    for col, arr in output.items():
        out[col] = arr
    return out
