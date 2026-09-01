"""Queue-context feature builder for the queue-forecasting trainer.

Computes, for each training row at time ``T = pending_at`` on a given
``task_queue_id``, a leakage-safe snapshot of the queue state visible *at or
before* T: how many runs sit ahead of it (by priority and FIFO order), the age
of the oldest blocking run, recent arrival/start flow, repo-family blocking
composition, and worker-capacity ratios.

Leakage discipline: only reference-run timestamps ``<= T`` are ever consulted.
A reference run ``s`` leaves the pending state at ``exit = COALESCE(started_at,
resolved_at)``: if it started, ``exit = started_at`` (resolved_at irrelevant);
else if it was resolved without ever starting (canceled / claim-expired /
deadline-exceeded before claim), ``exit = resolved_at``; else it is still
pending. A reference run ``s`` is "pending at T" iff
``s.pending_at <= T and (exit is NULL or exit > T)``.

Three implementations live here and MUST agree byte-for-byte:

* ``_add_queue_context_features_masked`` -- the original, per-target numpy-mask
  reference. O(targets x queue-size). Retained as the correctness oracle and is
  exercised by the equivalence test; do not change its logic.
* ``_sweep_queue_rowwise`` -- the event sweep with one binary search per target
  ROW. ~O((n+m) log n) but with a ~150-searchsorted-per-row constant that made
  a live 6M-row cohort take 50 minutes. Retained as the second reference,
  because it is testable at scales the O(n x m) oracle cannot reach.
* ``_sweep_queue`` -- the same event sweep with every binary search issued once
  per (queue, rank) over the whole target VECTOR. This is the production path.
  See its docstring for the measurements.
"""

from __future__ import annotations

import bisect
import time

import numpy as np
import pandas as pd

QUEUE_CONTEXT_FEATURE_VERSION = 1

PRIORITY_RANK = {
    "highest": 7,
    "very-high": 6,
    "high": 5,
    "medium": 4,
    "low": 3,
    "very-low": 2,
    "lowest": 1,
    "normal": 1,
}
UNKNOWN_RANK = 0

REPO_FAMILIES = ["try", "autoland", "central", "release_beta", "other", "unknown"]

FEATURE_COLUMNS = [
    "pending_higher_priority_same_queue",
    "pending_same_priority_same_queue",
    "pending_lower_priority_same_queue",
    "oldest_higher_or_equal_pending_age_same_queue",
    "arrivals_15m_same_queue",
    "arrivals_60m_same_queue",
    "arrivals_higher_or_equal_15m_same_queue",
    "arrivals_higher_or_equal_60m_same_queue",
    "starts_higher_or_equal_15m_same_queue",
    "pending_total_per_capacity",
    "pending_higher_or_equal_per_capacity",
    "running_per_capacity",
    "running_workers",
    "existing_capacity",
    "claimed_tasks",
    "capacity_sample_age_s",
    "capacity_null_reason",
    "backlog_coverage_ratio",
    "pending_try_higher_or_equal_same_queue",
    "pending_autoland_higher_or_equal_same_queue",
    "pending_release_beta_higher_or_equal_same_queue",
]

# Internal scratch column used to pass the raw "count rank>=target incl target"
# from the event sweep into _attach_capacity for the per-capacity division.
_HIGHER_OR_EQUAL_INCL_SELF = "_higher_or_equal_incl_self"


def _rank(priority: object) -> int:
    if priority is None or (isinstance(priority, float) and np.isnan(priority)):
        return UNKNOWN_RANK
    return PRIORITY_RANK.get(str(priority), UNKNOWN_RANK)


def _tie_key(task_id: object, run_id: object) -> tuple:
    return (str(task_id), run_id)


def _normalise_refs(runs_df: pd.DataFrame) -> pd.DataFrame:
    """Shared reference-frame normalisation used by both implementations.

    Produces the ns-resolution integer views and exit/started semantics. Kept in
    one place so the oracle and the sweep see byte-identical inputs.
    """
    ref = runs_df.copy()
    ref["pending_at"] = pd.to_datetime(ref["pending_at"], utc=True).astype(
        "datetime64[ns, UTC]"
    )
    ref["started_at"] = pd.to_datetime(ref["started_at"], utc=True).astype(
        "datetime64[ns, UTC]"
    )
    # resolved_at is optional (back-compat): a missing column means "never
    # resolved" -> all-NaT, so exit reduces to started_at as before.
    if "resolved_at" in ref.columns:
        ref["resolved_at"] = pd.to_datetime(ref["resolved_at"], utc=True).astype(
            "datetime64[ns, UTC]"
        )
    else:
        ref["resolved_at"] = pd.Series(
            pd.NaT, index=ref.index, dtype="datetime64[ns, UTC]"
        )
    ref["_rank"] = ref["priority_at_pending"].map(_rank)
    ref["_pending_ns"] = ref["pending_at"].astype("int64")
    # started_at NaT -> represent as +inf so "started_at > T" is always true.
    started_ns = ref["started_at"].astype("int64")
    ref["_started_ns"] = np.where(
        ref["started_at"].isna(), np.iinfo("int64").max, started_ns
    )
    # exit = COALESCE(started_at, resolved_at). A run leaves pending at exit;
    # NaT exit (never started, never resolved) -> +inf so "exit > T" is always
    # true (still pending). _started_ns is kept separate for the starts window.
    exit_at = ref["started_at"].where(ref["started_at"].notna(), ref["resolved_at"])
    exit_ns = exit_at.astype("int64")
    ref["_exit_ns"] = np.where(exit_at.isna(), np.iinfo("int64").max, exit_ns)
    ref["_tie"] = [
        _tie_key(tid, rid) for tid, rid in zip(ref["task_id"], ref["run_id"])
    ]
    return ref


def _prep_targets(out: pd.DataFrame) -> None:
    """Annotate targets with rank/ns/tie scratch columns (in place)."""
    out["pending_at"] = pd.to_datetime(out["pending_at"], utc=True).astype(
        "datetime64[ns, UTC]"
    )
    out["_rank"] = out["priority_at_pending"].map(_rank)
    out["_t_ns"] = out["pending_at"].astype("int64")
    out["_tie"] = [
        _tie_key(tid, rid) for tid, rid in zip(out["task_id"], out["run_id"])
    ]


def _init_feature_columns(out: pd.DataFrame) -> None:
    int_features = [
        "pending_higher_priority_same_queue",
        "pending_same_priority_same_queue",
        "pending_lower_priority_same_queue",
        "arrivals_15m_same_queue",
        "arrivals_60m_same_queue",
        "arrivals_higher_or_equal_15m_same_queue",
        "arrivals_higher_or_equal_60m_same_queue",
        "starts_higher_or_equal_15m_same_queue",
        "pending_try_higher_or_equal_same_queue",
        "pending_autoland_higher_or_equal_same_queue",
        "pending_release_beta_higher_or_equal_same_queue",
    ]
    for col in int_features:
        out[col] = 0
    out["oldest_higher_or_equal_pending_age_same_queue"] = np.nan
    out[_HIGHER_OR_EQUAL_INCL_SELF] = 0


W15 = 900 * 1_000_000_000
W60 = 3600 * 1_000_000_000

# How many target rows one vectorised pass handles. Module-level so
# `test_chunk_boundaries_do_not_change_results` can shrink it and actually
# cross a boundary; see `_sweep_queue` for why chunking exists at all.
SWEEP_CHUNK = 250_000


# ---------------------------------------------------------------------------
# Production path: event sweep.
# ---------------------------------------------------------------------------


def add_queue_context_features(
    df: pd.DataFrame,
    runs_df: pd.DataFrame,
    worker_counts: pd.DataFrame,
    *,
    capacity_staleness_s: float = 900,
) -> pd.DataFrame:
    """Add queue-context FEATURE_COLUMNS to ``df`` via an event sweep.

    Output is byte-identical to ``_add_queue_context_features_masked`` for every
    one of the FEATURE_COLUMNS, but runs in ~O((n+m) log n) per queue instead of
    the masked oracle's O(targets x queue-size).

    ``df`` rows carry: task_id, run_id, pending_at, priority_at_pending,
    task_queue_id, repo_family, queue_pending.
    ``runs_df`` carries: task_id, run_id, pending_at, started_at, resolved_at,
    priority_at_pending, task_queue_id, repo_family. ``resolved_at`` is
    optional for back-compat: a missing column is treated as all-NaT (never
    resolved), so a run's exit reduces to started_at exactly as before.
    ``worker_counts`` carries: task_queue_id, sampled_at, running_workers,
    existing_capacity, claimed_tasks (may be empty).
    """
    out = df.copy()
    n = len(out)

    _init_feature_columns(out)

    if n == 0:
        out["backlog_coverage_ratio"] = np.full(0, np.nan)
        out = _attach_capacity(
            out, worker_counts, capacity_staleness_s=capacity_staleness_s
        )
        return out.drop(columns=[_HIGHER_OR_EQUAL_INCL_SELF])

    _prep_targets(out)
    ref = _normalise_refs(runs_df)

    # Output buffers indexed by positional row order.
    higher_arr = np.zeros(n, dtype=np.int64)
    same_arr = np.zeros(n, dtype=np.int64)
    lower_arr = np.zeros(n, dtype=np.int64)
    oldest_arr = np.full(n, np.nan)
    arr15_arr = np.zeros(n, dtype=np.int64)
    arr60_arr = np.zeros(n, dtype=np.int64)
    arr15_he_arr = np.zeros(n, dtype=np.int64)
    arr60_he_arr = np.zeros(n, dtype=np.int64)
    starts_he_arr = np.zeros(n, dtype=np.int64)
    fam_try_arr = np.zeros(n, dtype=np.int64)
    fam_autoland_arr = np.zeros(n, dtype=np.int64)
    fam_beta_arr = np.zeros(n, dtype=np.int64)
    he_incl_self_arr = np.zeros(n, dtype=np.int64)
    coverage_arr = np.full(n, np.nan)

    qp = pd.to_numeric(out["queue_pending"], errors="coerce").to_numpy(dtype=float)

    # Group targets and references by queue.
    target_pos_by_queue: dict[object, list[int]] = {}
    for pos, qid in enumerate(out["task_queue_id"].to_numpy()):
        target_pos_by_queue.setdefault(qid, []).append(pos)

    out_t_ns = out["_t_ns"].to_numpy()
    out_rank = out["_rank"].to_numpy()
    out_tie = out["_tie"].to_numpy()
    out_tid = out["task_id"].to_numpy()
    out_rid = out["run_id"].to_numpy()

    ref_by_queue = {q: g for q, g in ref.groupby("task_queue_id", sort=False)}

    # Temporary instrumentation (2026-07): localize the actual per-queue
    # throughput now that this runs against production-scale reference sets
    # (millions of rows) rather than the 20k-row scale this was benchmarked
    # at. Prints a heartbeat every ~5% of queues so a slow run is visible
    # within minutes instead of only at completion.
    n_queues = len(target_pos_by_queue)
    print(f"[queue_context] sweeping {n} target rows across {n_queues} queues "
          f"({len(ref)} reference rows)", flush=True)
    _sweep_t0 = time.monotonic()
    _heartbeat_every = max(1, n_queues // 20)
    _targets_done = 0

    for _qi, (qid, positions) in enumerate(target_pos_by_queue.items()):
        if _qi % _heartbeat_every == 0:
            elapsed = time.monotonic() - _sweep_t0
            rate = _targets_done / elapsed if elapsed > 0 else 0.0
            print(f"[queue_context] queue {_qi}/{n_queues}, "
                  f"{_targets_done}/{n} target rows done, "
                  f"{elapsed:.1f}s elapsed, {rate:.0f} rows/s", flush=True)
        _targets_done += len(positions)

        g = ref_by_queue.get(qid)
        if g is None or len(g) == 0:
            # No reference runs: counts stay 0; oldest NaN. Coverage handled below.
            for pos in positions:
                queue_pending = qp[pos]
                if np.isfinite(queue_pending) and queue_pending > 0:
                    # numerator = pending-at-T incl target = 0 + 1
                    coverage_arr[pos] = 1.0 / queue_pending
                he_incl_self_arr[pos] = 1
            continue

        _sweep_queue(
            g,
            positions,
            out_t_ns,
            out_rank,
            out_tie,
            out_tid,
            out_rid,
            qp,
            higher_arr,
            same_arr,
            lower_arr,
            oldest_arr,
            arr15_arr,
            arr60_arr,
            arr15_he_arr,
            arr60_he_arr,
            starts_he_arr,
            fam_try_arr,
            fam_autoland_arr,
            fam_beta_arr,
            he_incl_self_arr,
            coverage_arr,
        )

    print(f"[queue_context] sweep done: {n} rows / {n_queues} queues in "
          f"{time.monotonic() - _sweep_t0:.1f}s", flush=True)

    out["pending_higher_priority_same_queue"] = higher_arr
    out["pending_same_priority_same_queue"] = same_arr
    out["pending_lower_priority_same_queue"] = lower_arr
    out["oldest_higher_or_equal_pending_age_same_queue"] = oldest_arr
    out["arrivals_15m_same_queue"] = arr15_arr
    out["arrivals_60m_same_queue"] = arr60_arr
    out["arrivals_higher_or_equal_15m_same_queue"] = arr15_he_arr
    out["arrivals_higher_or_equal_60m_same_queue"] = arr60_he_arr
    out["starts_higher_or_equal_15m_same_queue"] = starts_he_arr
    out["pending_try_higher_or_equal_same_queue"] = fam_try_arr
    out["pending_autoland_higher_or_equal_same_queue"] = fam_autoland_arr
    out["pending_release_beta_higher_or_equal_same_queue"] = fam_beta_arr
    out[_HIGHER_OR_EQUAL_INCL_SELF] = he_incl_self_arr
    out["backlog_coverage_ratio"] = coverage_arr

    _cap_t0 = time.monotonic()
    out = _attach_capacity(
        out, worker_counts, capacity_staleness_s=capacity_staleness_s
    )
    print(f"[queue_context] capacity attach done: {n} rows in "
          f"{time.monotonic() - _cap_t0:.1f}s", flush=True)

    drop_cols = ["_rank", "_t_ns", "_tie", _HIGHER_OR_EQUAL_INCL_SELF]
    return out.drop(columns=[c for c in drop_cols if c in out.columns])


def _range_count(sorted_arr: np.ndarray, lo_excl: float, hi_incl: float) -> int:
    """Count of values v with lo_excl < v <= hi_incl, on an ascending array."""
    left = int(np.searchsorted(sorted_arr, lo_excl, side="right"))
    right = int(np.searchsorted(sorted_arr, hi_incl, side="right"))
    return right - left


def _sweep_queue_rowwise(
    g: pd.DataFrame,
    positions: list[int],
    out_t_ns: np.ndarray,
    out_rank: np.ndarray,
    out_tie: np.ndarray,
    out_tid: np.ndarray,
    out_rid: np.ndarray,
    qp: np.ndarray,
    higher_arr: np.ndarray,
    same_arr: np.ndarray,
    lower_arr: np.ndarray,
    oldest_arr: np.ndarray,
    arr15_arr: np.ndarray,
    arr60_arr: np.ndarray,
    arr15_he_arr: np.ndarray,
    arr60_he_arr: np.ndarray,
    starts_he_arr: np.ndarray,
    fam_try_arr: np.ndarray,
    fam_autoland_arr: np.ndarray,
    fam_beta_arr: np.ndarray,
    he_incl_self_arr: np.ndarray,
    coverage_arr: np.ndarray,
) -> None:
    """Compute all sweep-derived features for one queue group.

    Core identity: a reference run is "pending at T" iff ``p <= T AND exit > T``.
    Because ``exit >= p`` always (exit = COALESCE(started, resolved), and both
    are ``>= pending``), ``count(p<=T AND exit>T) == count(p<=T) -
    count(exit<=T)``, and both terms are prefix counts on ascending arrays via
    ``searchsorted``. Arrivals/starts windows are range counts on ascending
    arrays. Everything is evaluated per *rank class* (at most 8 distinct ranks)
    so each target costs O(ranks * log n), i.e. the whole queue is
    O((n + m) * log n).

    Self-exclusion mirrors the oracle's ``not_self`` mask: the target's own
    contribution to each raw count is subtracted afterwards (only matters when
    the target itself appears as a reference run and is pending/arrived at T).
    The same-instant cohort (refs with ``p == T``) and the FIFO tie-break for
    same-priority are handled with explicit boundary searches over per-rank
    arrays, so behaviour at exactly ``p == T`` matches the oracle byte-for-byte.
    """
    p_ns = g["_pending_ns"].to_numpy()
    s_ns = g["_started_ns"].to_numpy()
    exit_ns = g["_exit_ns"].to_numpy()
    ranks = g["_rank"].to_numpy()
    ties = g["_tie"].to_numpy()
    fams = g["repo_family"].to_numpy()
    tids = g["task_id"].to_numpy()
    rids = g["run_id"].to_numpy()

    # Global ascending pending array (all ranks): arrivals windows.
    p_sorted_all = np.sort(p_ns, kind="stable")

    distinct_ranks = np.sort(np.unique(ranks))

    # Per-rank precomputed structures, all keyed by integer rank value:
    #   p_sorted   -- ascending pending_at  (for count(p<=T))
    #   exit_sorted-- ascending exit        (for count(exit<=T))
    #   started_sorted -- ascending started (for starts window)
    #   p_by_pending / exit_by_pending / tie_by_pending -- arrays co-sorted by
    #       pending_at ascending; used for oldest-age and the same-instant FIFO
    #       cohort (a contiguous block where pending == T).
    #   fam_p / fam_exit -- per family, ascending pending and exit (family
    #       composition among rank>=r pending-at-T).
    rank_p_sorted: dict[int, np.ndarray] = {}
    rank_exit_sorted: dict[int, np.ndarray] = {}
    rank_started_sorted: dict[int, np.ndarray] = {}
    rank_p_byp: dict[int, np.ndarray] = {}
    rank_exit_byp: dict[int, np.ndarray] = {}
    rank_tie_byp: dict[int, np.ndarray] = {}
    # Running max of exit_byp (monotonic non-decreasing since it's a prefix
    # max). Lets the oldest-age query below binary-search for the smallest
    # index whose exit exceeds T in O(log m), instead of scanning a prefix of
    # up to the whole rank's ref count per target row (see its usage below).
    rank_prefix_max_exit: dict[int, np.ndarray] = {}
    fam_keys = ("try", "autoland", "release_beta")
    rank_fam_p: dict[tuple[int, str], np.ndarray] = {}
    rank_fam_exit: dict[tuple[int, str], np.ndarray] = {}

    for rv in distinct_ranks:
        rvi = int(rv)
        m = ranks == rv
        p_m = p_ns[m]
        exit_m = exit_ns[m]
        started_m = s_ns[m]
        ties_m = ties[m]
        fams_m = fams[m]

        rank_p_sorted[rvi] = np.sort(p_m, kind="stable")
        rank_exit_sorted[rvi] = np.sort(exit_m, kind="stable")
        rank_started_sorted[rvi] = np.sort(started_m, kind="stable")

        order = np.argsort(p_m, kind="stable")
        rank_p_byp[rvi] = p_m[order]
        rank_exit_byp[rvi] = exit_m[order]
        # ties is an object array of tuples; index via the same order.
        rank_tie_byp[rvi] = ties_m[order]
        rank_prefix_max_exit[rvi] = np.maximum.accumulate(rank_exit_byp[rvi])

        for fk in fam_keys:
            fm = fams_m == fk
            rank_fam_p[(rvi, fk)] = np.sort(p_m[fm], kind="stable")
            rank_fam_exit[(rvi, fk)] = np.sort(exit_m[fm], kind="stable")

    def pending_count(p_arr_sorted, exit_arr_sorted, T):
        return int(np.searchsorted(p_arr_sorted, T, side="right")) - int(
            np.searchsorted(exit_arr_sorted, T, side="right")
        )

    # (task_id, run_id) is unique per PRIMARY KEY on queue_forecast_task_runs,
    # so at most one reference row can match a given target's own key. Was:
    # `(tids == tid) & (rids == rid)` -- two full-array comparisons over the
    # WHOLE queue's reference set (size m), executed unconditionally at the
    # top of EVERY target row's iteration. That's an O(n*m) term paid by every
    # row regardless of whether self-exclusion even applies, and profiling
    # showed it as the single largest contributor to production-scale runtime
    # (see the same_ahead/oldest-age fixes above for the other two). One O(m)
    # dict build per queue replaces it with an O(1) lookup per target row.
    self_idx_by_key: dict[tuple, int] = {
        (t, r): i for i, (t, r) in enumerate(zip(tids, rids))
    }

    for pos in positions:
        T = out_t_ns[pos]
        target_rank = out_rank[pos]
        target_tie = out_tie[pos]
        tid = out_tid[pos]
        rid = out_rid[pos]

        # Self row present in this reference group (same task_id & run_id). The
        # oracle excludes exactly this from every count.
        self_idx = self_idx_by_key.get((tid, rid))
        self_present = self_idx is not None
        if self_present:
            self_p = p_ns[self_idx : self_idx + 1]
            self_exit = exit_ns[self_idx : self_idx + 1]
            self_started = s_ns[self_idx : self_idx + 1]
            self_rank = ranks[self_idx : self_idx + 1]
            self_fam = fams[self_idx : self_idx + 1]
            self_tie = ties[self_idx : self_idx + 1]

        # --- pending-at-T counts by rank class (raw, incl any self). ---
        n_higher = 0
        n_lower = 0
        n_equal = 0
        for rv in distinct_ranks:
            rvi = int(rv)
            cnt = pending_count(rank_p_sorted[rvi], rank_exit_sorted[rvi], T)
            if rvi > target_rank:
                n_higher += cnt
            elif rvi < target_rank:
                n_lower += cnt
            else:
                n_equal += cnt
        n_he_incl = n_higher + n_equal

        # --- same-priority FIFO (raw): rank==r, pending-at-T, ordered before
        # target. earlier (p<T) all count; same-instant (p==T) iff tie<target. ---
        same_ahead = 0
        if target_rank in (int(r) for r in distinct_ranks):
            p_byp = rank_p_byp[target_rank]
            exit_byp = rank_exit_byp[target_rank]
            tie_byp = rank_tie_byp[target_rank]
            # earlier: p < T AND exit > T. Was: exit_byp[:lt] > T).sum() -- an
            # O(lt) slice+sum per target row (lt up to the whole rank's ref
            # count), which is the O(n*m) term that dominated wall-clock at
            # production scale (measured: 100k/100k=56s, 200k/200k=196s --
            # ~3.5x for a 2x input, far worse than the O((n+m)log n) the rest
            # of this function achieves). Same subtract-two-sorted-counts
            # identity as pending_count() above, restricted to strict p<T:
            #   count(p<T & exit>T) = count(p<T) - count(exit<=T)
            #                         + count(p==T & exit==T)
            # The correction undoes exit<=T rows with p==T (zero-duration,
            # exit>=p forces exit==T exactly) that count(exit<=T) included but
            # count(p<T) didn't -- bounded by the same-instant cohort size
            # (already sliced below for the tie-break loop), not by m.
            lt = int(np.searchsorted(p_byp, T, side="left"))  # entries with p<T
            rt = int(np.searchsorted(p_byp, T, side="right"))  # end of p==T block
            if lt > 0:
                count_exit_le = int(np.searchsorted(rank_exit_sorted[target_rank], T, side="right"))
                same_ahead += lt - count_exit_le
                if rt > lt:
                    same_ahead += int((exit_byp[lt:rt] == T).sum())
            # same-instant block: p == T.
            if rt > lt:
                si_exit = exit_byp[lt:rt]
                si_tie = tie_byp[lt:rt]
                for se, st in zip(si_exit, si_tie):
                    if se > T and st < target_tie:
                        same_ahead += 1

        # --- arrivals windows (raw): pending in (T-w, T]. ---
        a15 = _range_count(p_sorted_all, T - W15, T)
        a60 = _range_count(p_sorted_all, T - W60, T)
        a15_he = 0
        a60_he = 0
        starts_he = 0
        # family + he-arrivals + starts over rank>=r.
        f_try = f_autoland = f_beta = 0
        for rv in distinct_ranks:
            rvi = int(rv)
            if rvi < target_rank:
                continue
            ps = rank_p_sorted[rvi]
            a15_he += _range_count(ps, T - W15, T)
            a60_he += _range_count(ps, T - W60, T)
            starts_he += _range_count(rank_started_sorted[rvi], T - W15, T)
            f_try += pending_count(
                rank_fam_p[(rvi, "try")], rank_fam_exit[(rvi, "try")], T
            )
            f_autoland += pending_count(
                rank_fam_p[(rvi, "autoland")],
                rank_fam_exit[(rvi, "autoland")],
                T,
            )
            f_beta += pending_count(
                rank_fam_p[(rvi, "release_beta")],
                rank_fam_exit[(rvi, "release_beta")],
                T,
            )

        # --- oldest higher-or-equal pending age (raw min pending, rank>=r,
        # pending-at-T). ---
        oldest_p = None
        for rv in distinct_ranks:
            rvi = int(rv)
            if rvi < target_rank:
                continue
            p_byp = rank_p_byp[rvi]
            hi = int(np.searchsorted(p_byp, T, side="right"))
            if hi == 0:
                continue
            # Was: exit_byp[:hi] > T).any() + argmax -- an O(hi) scan per rank
            # per target row (hi up to the whole rank's ref count), the second
            # O(n*m)-shaped term found at production scale (paired with the
            # same_ahead fix above; see its comment for the measured impact).
            # rank_prefix_max_exit is monotonic non-decreasing (a running max),
            # so binary-searching it for where it first exceeds T finds the
            # smallest index whose exit_byp value itself exceeds T: if an
            # earlier index qualified, the prefix max would already have
            # exceeded T there. O(log m) instead of O(hi).
            first = int(np.searchsorted(rank_prefix_max_exit[rvi], T, side="right"))
            if first >= hi:
                continue
            cand = int(p_byp[first])
            if oldest_p is None or cand < oldest_p:
                oldest_p = cand

        # --- subtract self contributions (mirror oracle not_self). ---
        if self_present:
            for sp, se, sst, sr, sf, sti in zip(
                self_p, self_exit, self_started, self_rank, self_fam, self_tie
            ):
                pending_at_t_self = sp <= T and se > T
                if pending_at_t_self:
                    if sr > target_rank:
                        n_higher -= 1
                    elif sr < target_rank:
                        n_lower -= 1
                    else:
                        n_equal -= 1
                    if sr >= target_rank:
                        n_he_incl -= 1
                        if sf == "try":
                            f_try -= 1
                        elif sf == "autoland":
                            f_autoland -= 1
                        elif sf == "release_beta":
                            f_beta -= 1
                    # same-priority ahead self removal: only if it would have
                    # been counted (rank==r and ordered before target).
                    if sr == target_rank:
                        if sp < T or (sp == T and sti < target_tie):
                            same_ahead -= 1
                # arrivals: pending in window (regardless of pending-at-T state).
                if T - W15 < sp <= T:
                    a15 -= 1
                    if sr >= target_rank:
                        a15_he -= 1
                if T - W60 < sp <= T:
                    a60 -= 1
                    if sr >= target_rank:
                        a60_he -= 1
                # starts: started in window, rank>=r.
                if T - W15 < sst <= T and sr >= target_rank:
                    starts_he -= 1

        # Recompute oldest with self excluded only if self could be the min.
        if self_present and oldest_p is not None:
            self_min_he = None
            for sp, se, sr in zip(self_p, self_exit, self_rank):
                if sp <= T and se > T and sr >= target_rank:
                    if self_min_he is None or sp < self_min_he:
                        self_min_he = int(sp)
            if self_min_he is not None and self_min_he == oldest_p:
                oldest_p = _oldest_he_excl_self(
                    p_ns, exit_ns, ranks, tids, rids, tid, rid, target_rank, T
                )

        # --- store. ---
        higher_arr[pos] = n_higher
        lower_arr[pos] = n_lower
        same_arr[pos] = same_ahead
        he_incl_self_arr[pos] = n_he_incl + 1
        arr15_arr[pos] = a15
        arr60_arr[pos] = a60
        arr15_he_arr[pos] = a15_he
        arr60_he_arr[pos] = a60_he
        starts_he_arr[pos] = starts_he

        if n_he_incl > 0:
            if oldest_p is not None:
                oldest_arr[pos] = (T - oldest_p) / 1_000_000_000.0
            fam_try_arr[pos] = f_try
            fam_autoland_arr[pos] = f_autoland
            fam_beta_arr[pos] = f_beta

        queue_pending = qp[pos]
        if np.isfinite(queue_pending) and queue_pending > 0:
            n_pending_all = n_higher + n_lower + n_equal
            coverage_arr[pos] = (n_pending_all + 1) / queue_pending


def _range_count_vec(
    sorted_arr: np.ndarray, lo_excl: np.ndarray, hi_incl: np.ndarray
) -> np.ndarray:
    """Vector form of `_range_count`: count of lo_excl < v <= hi_incl per query."""
    return (
        np.searchsorted(sorted_arr, hi_incl, side="right").astype(np.int64)
        - np.searchsorted(sorted_arr, lo_excl, side="right").astype(np.int64)
    )


def _sweep_queue(
    g: pd.DataFrame,
    positions: list[int],
    out_t_ns: np.ndarray,
    out_rank: np.ndarray,
    out_tie: np.ndarray,
    out_tid: np.ndarray,
    out_rid: np.ndarray,
    qp: np.ndarray,
    higher_arr: np.ndarray,
    same_arr: np.ndarray,
    lower_arr: np.ndarray,
    oldest_arr: np.ndarray,
    arr15_arr: np.ndarray,
    arr60_arr: np.ndarray,
    arr15_he_arr: np.ndarray,
    arr60_he_arr: np.ndarray,
    starts_he_arr: np.ndarray,
    fam_try_arr: np.ndarray,
    fam_autoland_arr: np.ndarray,
    fam_beta_arr: np.ndarray,
    he_incl_self_arr: np.ndarray,
    coverage_arr: np.ndarray,
) -> None:
    """Compute all sweep-derived features for one queue group, VECTORISED.

    Byte-identical to `_sweep_queue_rowwise` (and so to the masked oracle); the
    only difference is that every binary search is issued once per (queue, rank)
    over the whole target vector instead of once per target ROW.

    WHY THIS EXISTS, measured 2026-08-31. The row-wise version is
    O((n+m) log n) -- the algorithm was never the problem -- but it pays ~150
    SCALAR `np.searchsorted` calls per target row (8 rank classes x {pending,
    exit} + 3 families x 2 + arrivals + starts + oldest). At production scale
    each of those is a cache-missing binary search over a multi-million-element
    array, and the per-call numpy overhead is paid 150 times per row:

        reference rows      per target row
             20,000              65 us
            200,000              73 us
          1,000,000             123 us
          3,000,000             260 us

    Live cohort, 6,019,770 target rows against a reference frame of the same
    order: 502 us/row = 3019.7s. That is the whole reason the qctx probe could
    not fit inside the dispatcher's `TIMEOUT_MAX` of 3600s -- the training
    sweep alone took 50 minutes and the prediction pass then re-ran it.

    HOW THE OLD PERFORMANCE TESTS MISSED IT. `test_sweep_stays_linear_at_
    production_scale` pins `priority_at_pending` to a single value and
    `repo_family` to a single value, so `distinct_ranks` has ONE element and
    every per-rank loop above executes once instead of eight times; and its
    100k-row reference set fits in cache, so the dominant term -- cache-missing
    searches over millions of rows -- is absent by construction. It asserted
    linearity, correctly, while the constant factor was ~8x understated.
    `test_sweep_cost_is_flat_in_rank_cardinality` and
    `test_sweep_at_production_reference_scale` now cover both axes.

    WHAT IS STILL PER-ROW, deliberately, because each is cheap and bounded:
      * the same-instant FIFO tie-break -- one `bisect_left` per row against a
        block sorted ONCE per instant (see the loop below). Tuples cannot be
        compared by numpy, and building order-preserving integer ordinals for
        millions of tie keys costs more than it saves.
      * `_oldest_he_excl_self`, reached only when the target's own row is the
        single oldest pending peer at its instant.
    """
    pos_all = np.asarray(positions, dtype=np.int64)


    p_ns = g["_pending_ns"].to_numpy()
    s_ns = g["_started_ns"].to_numpy()
    exit_ns = g["_exit_ns"].to_numpy()
    ranks = g["_rank"].to_numpy().astype(np.int64)
    ties = g["_tie"].to_numpy()
    fams = g["repo_family"].to_numpy()
    tids = g["task_id"].to_numpy()
    rids = g["run_id"].to_numpy()


    p_sorted_all = np.sort(p_ns, kind="stable")
    distinct_ranks = [int(r) for r in np.sort(np.unique(ranks))]

    # Per-rank structures. Identical to the row-wise version's, plus
    # `rank_zero_dur_p` (see the same_ahead correction below).
    rank_p_sorted: dict[int, np.ndarray] = {}
    rank_exit_sorted: dict[int, np.ndarray] = {}
    rank_started_sorted: dict[int, np.ndarray] = {}
    rank_p_byp: dict[int, np.ndarray] = {}
    rank_exit_byp: dict[int, np.ndarray] = {}
    rank_tie_byp: dict[int, np.ndarray] = {}
    rank_prefix_max_exit: dict[int, np.ndarray] = {}
    # Ascending pending_at of refs whose exit == pending (zero-duration).
    rank_zero_dur_p: dict[int, np.ndarray] = {}
    fam_keys = ("try", "autoland", "release_beta")
    rank_fam_p: dict[tuple[int, str], np.ndarray] = {}
    rank_fam_exit: dict[tuple[int, str], np.ndarray] = {}

    # THREE SORTS PER RANK, not eleven. Once the per-row search cost is gone
    # (above), this setup becomes the floor -- and at a 200k-target / 4M-
    # reference shape it was ALL of the remaining time, hiding the speedup
    # entirely. Every array below is derived from two orderings by GATHERING or
    # by boolean-indexing an already-sorted array, both of which preserve
    # order in O(m), instead of re-sorting the same values:
    #
    #   was: p, exit, started, argsort(p), zero-duration p,
    #        3 families x {p, exit}                        = 11 sorts
    #   now: argsort(p), argsort(exit), sort(started)      =  3
    for rvi in distinct_ranks:
        m = ranks == rvi
        p_m = p_ns[m]
        exit_m = exit_ns[m]
        started_m = s_ns[m]
        ties_m = ties[m]
        fams_m = fams[m]

        order_p = np.argsort(p_m, kind="stable")
        order_e = np.argsort(exit_m, kind="stable")
        p_by_p = p_m[order_p]
        exit_by_p = exit_m[order_p]
        fams_by_p = fams_m[order_p]
        exit_by_e = exit_m[order_e]
        fams_by_e = fams_m[order_e]

        # `p_sorted` and `p_byp` are the same array: both are p ascending. The
        # row-wise version built them with a separate sort and argsort.
        rank_p_sorted[rvi] = p_by_p
        rank_p_byp[rvi] = p_by_p
        rank_exit_sorted[rvi] = exit_by_e
        rank_exit_byp[rvi] = exit_by_p
        rank_tie_byp[rvi] = ties_m[order_p]
        rank_started_sorted[rvi] = np.sort(started_m, kind="stable")
        rank_prefix_max_exit[rvi] = np.maximum.accumulate(exit_by_p)
        # Boolean-indexing an ascending array yields an ascending array, so
        # this needs no sort of its own.
        rank_zero_dur_p[rvi] = p_by_p[exit_by_p == p_by_p]

        for fk in fam_keys:
            rank_fam_p[(rvi, fk)] = p_by_p[fams_by_p == fk]
            rank_fam_exit[(rvi, fk)] = exit_by_e[fams_by_e == fk]

    def pending_count_vec(p_arr, exit_arr, q):
        """count(p <= q AND exit > q), using exit >= p so the two prefix counts
        subtract. Same identity as the row-wise version, one call per rank."""
        return (
            np.searchsorted(p_arr, q, side="right").astype(np.int64)
            - np.searchsorted(exit_arr, q, side="right").astype(np.int64)
        )


    # (task_id, run_id) is unique per PRIMARY KEY on queue_forecast_task_runs,
    # so at most one reference row can match a given target's own key. Built
    # ONCE per queue -- it is O(reference rows) and independent of the chunking
    # below.
    self_by_key: dict[tuple, int] = {
        (t, r): i for i, (t, r) in enumerate(zip(tids, rids))
    }

    # CHUNKED OVER TARGETS, and the reason is memory rather than speed. Every
    # vector below is as long as the slice being processed, and there are ~20
    # of them plus searchsorted temporaries -- so an unchunked hot queue sized
    # the peak. Measured on a 1M-target / 1M-reference single queue: 1,318 MB
    # peak row-wise vs 1,579 MB unchunked, i.e. +262 MB of transient arrays,
    # against a live probe whose `rss_high_water_kb` was 20,971,476 against a
    # `mem_limit` of 20g -- exactly at the ceiling, killed with exit 137.
    #
    # `SWEEP_CHUNK` keeps every vector call long enough that the per-call
    # overhead is still amortised away (the whole point of the rewrite) while
    # making peak memory a function of the CHUNK rather than of the largest
    # queue -- so queue skew can no longer move it. The per-rank structures
    # above are built once and shared across chunks; only the target-side
    # vectors are re-cut.
    def sweep_chunk(pos):
        npos = pos.size
        T = out_t_ns[pos].astype(np.int64)
        tr = out_rank[pos].astype(np.int64)
        tie_local = out_tie[pos]

        # --- pending-at-T counts by rank class (raw, incl any self). ---
        n_higher = np.zeros(npos, dtype=np.int64)
        n_lower = np.zeros(npos, dtype=np.int64)
        n_equal = np.zeros(npos, dtype=np.int64)
        for rvi in distinct_ranks:
            cnt = pending_count_vec(rank_p_sorted[rvi], rank_exit_sorted[rvi], T)
            n_higher += np.where(rvi > tr, cnt, 0)
            n_lower += np.where(rvi < tr, cnt, 0)
            n_equal += np.where(rvi == tr, cnt, 0)
        n_he_incl = n_higher + n_equal

        # --- arrivals windows (raw): pending in (T-w, T]. ---
        a15 = _range_count_vec(p_sorted_all, T - W15, T)
        a60 = _range_count_vec(p_sorted_all, T - W60, T)

        # --- rank >= target: he-arrivals, starts, family composition. ---
        # Restricted to the rows the rank applies to (`idx`) rather than computed
        # for all rows and masked, so the work stays O(rows) rather than
        # O(rows x ranks).
        a15_he = np.zeros(npos, dtype=np.int64)
        a60_he = np.zeros(npos, dtype=np.int64)
        starts_he = np.zeros(npos, dtype=np.int64)
        f_try = np.zeros(npos, dtype=np.int64)
        f_autoland = np.zeros(npos, dtype=np.int64)
        f_beta = np.zeros(npos, dtype=np.int64)
        fam_out = {"try": f_try, "autoland": f_autoland, "release_beta": f_beta}
        for rvi in distinct_ranks:
            idx = np.flatnonzero(rvi >= tr)
            if idx.size == 0:
                continue
            Tm = T[idx]
            ps = rank_p_sorted[rvi]
            a15_he[idx] += _range_count_vec(ps, Tm - W15, Tm)
            a60_he[idx] += _range_count_vec(ps, Tm - W60, Tm)
            starts_he[idx] += _range_count_vec(
                rank_started_sorted[rvi], Tm - W15, Tm
            )
            for fk in fam_keys:
                fam_out[fk][idx] += pending_count_vec(
                    rank_fam_p[(rvi, fk)], rank_fam_exit[(rvi, fk)], Tm
                )

        # --- same-priority FIFO (raw). ---
        same_ahead = np.zeros(npos, dtype=np.int64)
        for rvi in distinct_ranks:
            idx = np.flatnonzero(tr == rvi)
            if idx.size == 0:
                continue
            Tm = T[idx]
            p_byp = rank_p_byp[rvi]
            exit_byp = rank_exit_byp[rvi]
            tie_byp = rank_tie_byp[rvi]

            lt = np.searchsorted(p_byp, Tm, side="left").astype(np.int64)
            rt = np.searchsorted(p_byp, Tm, side="right").astype(np.int64)
            # earlier: count(p<T & exit>T) = count(p<T) - count(exit<=T) + corr,
            # where corr undoes the p==T & exit==T rows that count(exit<=T)
            # included but count(p<T) did not. The row-wise version spells corr as
            # `(exit_byp[lt:rt] == T).sum()`, a variable-length slice per row.
            # Since exit >= p always, `p == T AND exit == T` is exactly
            # `p == T AND exit == p` -- a T-INDEPENDENT property of the reference
            # row -- so the same count comes from two searches on the pre-built
            # ascending array of zero-duration pending instants.
            count_exit_le = np.searchsorted(
                rank_exit_sorted[rvi], Tm, side="right"
            ).astype(np.int64)
            zd = rank_zero_dur_p[rvi]
            corr = np.searchsorted(zd, Tm, side="right").astype(np.int64) - \
                np.searchsorted(zd, Tm, side="left").astype(np.int64)
            # `corr` is 0 whenever there is no p==T block, so the row-wise
            # version's `if rt > lt` guard around it is implied.
            base = np.where(lt > 0, lt - count_exit_le + corr, 0)
            same_ahead[idx] += base

            # Same-instant cohort: refs with p == T, still pending, ordered before
            # the target. Within the p==T block `exit > T` is `exit > p`, so the
            # eligible block is T-independent and is sorted ONCE per instant --
            # (lt, rt) identifies the instant, because rt > lt implies p_byp[lt]
            # == T. This is what removes the row-wise version's O(cohort) scan per
            # row, i.e. its O(cohort^2) cost per push-sized batch of identical
            # pending_at.
            have = np.flatnonzero(rt > lt)
            if have.size == 0:
                continue
            block_cache: dict[tuple[int, int], list] = {}
            for j in have:
                key = (int(lt[j]), int(rt[j]))
                blk = block_cache.get(key)
                if blk is None:
                    a, b = key
                    e = exit_byp[a:b]
                    blk = sorted(tie_byp[a:b][e > Tm[j]])
                    block_cache[key] = blk
                if blk:
                    same_ahead[idx[j]] += bisect.bisect_left(blk, tie_local[idx[j]])

        # --- oldest higher-or-equal pending age (raw min pending, rank>=r,
        # pending-at-T). ---
        INF = np.iinfo(np.int64).max
        oldest = np.full(npos, INF, dtype=np.int64)
        found = np.zeros(npos, dtype=bool)
        for rvi in distinct_ranks:
            idx = np.flatnonzero(rvi >= tr)
            if idx.size == 0:
                continue
            Tm = T[idx]
            p_byp = rank_p_byp[rvi]
            hi = np.searchsorted(p_byp, Tm, side="right").astype(np.int64)
            # Same monotonic prefix-max trick as the row-wise version: the smallest
            # index whose own exit exceeds T.
            first = np.searchsorted(
                rank_prefix_max_exit[rvi], Tm, side="right"
            ).astype(np.int64)
            ok = np.flatnonzero((hi > 0) & (first < hi))
            if ok.size == 0:
                continue
            sel = idx[ok]
            cand = p_byp[first[ok]]
            cur = oldest[sel]
            oldest[sel] = np.where(cand < cur, cand, cur)
            found[sel] = True

        # --- subtract self contributions (mirror oracle not_self). ---
        # (task_id, run_id) is unique per PRIMARY KEY on queue_forecast_task_runs,
        # so at most one reference row can match a given target's own key.
        self_idx = np.fromiter(
            (self_by_key.get((t, r), -1) for t, r in zip(out_tid[pos], out_rid[pos])),
            dtype=np.int64,
            count=npos,
        )
        has_self = self_idx >= 0
        if has_self.any():
            gi = np.where(has_self, self_idx, 0)
            sp = p_ns[gi]
            se = exit_ns[gi]
            sst = s_ns[gi]
            sr = ranks[gi]
            sf = fams[gi]

            pend_self = has_self & (sp <= T) & (se > T)
            n_higher -= (pend_self & (sr > tr)).astype(np.int64)
            n_lower -= (pend_self & (sr < tr)).astype(np.int64)
            n_equal -= (pend_self & (sr == tr)).astype(np.int64)
            he_self = pend_self & (sr >= tr)
            n_he_incl -= he_self.astype(np.int64)
            for fk in fam_keys:
                fam_out[fk] -= (he_self & (sf == fk)).astype(np.int64)

            # Same-priority-ahead self removal. The row-wise version tests
            # `sp < T or (sp == T and self_tie < target_tie)`; the second disjunct
            # is UNREACHABLE and is therefore not evaluated here. The self row is
            # matched BY (task_id, run_id), so its `_tie` -- `(str(task_id),
            # run_id)` -- is equal to the target's, never less. Asserted by
            # `test_self_tie_is_never_ahead_of_itself`.
            same_ahead -= (pend_self & (sr == tr) & (sp < T)).astype(np.int64)

            # arrivals: pending in window, regardless of pending-at-T state.
            in15 = has_self & (sp > T - W15) & (sp <= T)
            in60 = has_self & (sp > T - W60) & (sp <= T)
            a15 -= in15.astype(np.int64)
            a60 -= in60.astype(np.int64)
            a15_he -= (in15 & (sr >= tr)).astype(np.int64)
            a60_he -= (in60 & (sr >= tr)).astype(np.int64)
            # starts: started in window, rank>=r.
            starts_he -= (
                has_self & (sst > T - W15) & (sst <= T) & (sr >= tr)
            ).astype(np.int64)

            # Recompute oldest with self excluded only where self IS the current
            # minimum -- reached when the target's own row is the single oldest
            # pending peer at its instant.
            redo = np.flatnonzero(found & pend_self & (sr >= tr) & (sp == oldest))
            for j in redo:
                got = _oldest_he_excl_self(
                    p_ns, exit_ns, ranks, tids, rids,
                    out_tid[pos[j]], out_rid[pos[j]], int(tr[j]), int(T[j]),
                )
                if got is None:
                    oldest[j] = INF
                    found[j] = False
                else:
                    oldest[j] = got

        # --- store. ---
        higher_arr[pos] = n_higher
        lower_arr[pos] = n_lower
        same_arr[pos] = same_ahead
        he_incl_self_arr[pos] = n_he_incl + 1
        arr15_arr[pos] = a15
        arr60_arr[pos] = a60
        arr15_he_arr[pos] = a15_he
        arr60_he_arr[pos] = a60_he
        starts_he_arr[pos] = starts_he

        # Family counts and the oldest age are published ONLY where there is at
        # least one higher-or-equal pending peer, exactly as the row-wise version
        # gates them behind `if n_he_incl > 0`.
        he_pos = n_he_incl > 0
        fam_sel = pos[he_pos]
        fam_try_arr[fam_sel] = f_try[he_pos]
        fam_autoland_arr[fam_sel] = f_autoland[he_pos]
        fam_beta_arr[fam_sel] = f_beta[he_pos]
        age_ok = he_pos & found
        oldest_arr[pos[age_ok]] = (T[age_ok] - oldest[age_ok]) / 1_000_000_000.0

        qpv = qp[pos]
        cov_ok = np.isfinite(qpv) & (qpv > 0)
        n_pending_all = n_higher + n_lower + n_equal
        coverage_arr[pos[cov_ok]] = (
            (n_pending_all[cov_ok] + 1) / qpv[cov_ok]
        )

    for _start in range(0, pos_all.size, SWEEP_CHUNK):
        sweep_chunk(pos_all[_start:_start + SWEEP_CHUNK])


def _oldest_he_excl_self(
    p_ns, exit_ns, ranks, tids, rids, tid, rid, target_rank, T
) -> int | None:
    """Exact min pending among rank>=r pending-at-T refs, explicitly excl self.

    Only invoked in the rare case where the raw minimum coincides with a self
    pending value; operates on the (small) qualifying set directly.
    """
    not_self = ~((tids == tid) & (rids == rid))
    mask = not_self & (p_ns <= T) & (exit_ns > T) & (ranks >= target_rank)
    if not mask.any():
        return None
    return int(p_ns[mask].min())


# ---------------------------------------------------------------------------
# Correctness oracle: original per-target numpy-mask implementation.
# DO NOT change its logic -- it is the reference the sweep is validated against.
# ---------------------------------------------------------------------------


def _add_queue_context_features_masked(
    df: pd.DataFrame,
    runs_df: pd.DataFrame,
    worker_counts: pd.DataFrame,
    *,
    capacity_staleness_s: float = 900,
) -> pd.DataFrame:
    """Original masked implementation. Retained as the correctness oracle.

    ``df`` rows carry: task_id, run_id, pending_at, priority_at_pending,
    task_queue_id, repo_family, queue_pending.
    ``runs_df`` carries: task_id, run_id, pending_at, started_at, resolved_at,
    priority_at_pending, task_queue_id, repo_family. ``resolved_at`` is
    optional for back-compat: a missing column is treated as all-NaT (never
    resolved), so a run's exit reduces to started_at exactly as before.
    ``worker_counts`` carries: task_queue_id, sampled_at, running_workers,
    existing_capacity, claimed_tasks (may be empty).
    """
    out = df.copy()
    n = len(out)

    # Initialise integer-count features to 0 and float features to NaN.
    int_features = [
        "pending_higher_priority_same_queue",
        "pending_same_priority_same_queue",
        "pending_lower_priority_same_queue",
        "arrivals_15m_same_queue",
        "arrivals_60m_same_queue",
        "arrivals_higher_or_equal_15m_same_queue",
        "arrivals_higher_or_equal_60m_same_queue",
        "starts_higher_or_equal_15m_same_queue",
        "pending_try_higher_or_equal_same_queue",
        "pending_autoland_higher_or_equal_same_queue",
        "pending_release_beta_higher_or_equal_same_queue",
    ]
    for col in int_features:
        out[col] = 0
    out["oldest_higher_or_equal_pending_age_same_queue"] = np.nan
    out[_HIGHER_OR_EQUAL_INCL_SELF] = 0

    if n == 0:
        out = _attach_capacity(
            out, worker_counts, capacity_staleness_s=capacity_staleness_s
        )
        return out.drop(columns=[_HIGHER_OR_EQUAL_INCL_SELF])

    # Normalise timestamps to datetime for both frames. Coerce to nanosecond
    # resolution so integer views are always in ns (pandas defaults to us for
    # some inputs, which would silently break the /1e9 conversions below).
    out["pending_at"] = pd.to_datetime(out["pending_at"], utc=True).astype(
        "datetime64[ns, UTC]"
    )
    ref = runs_df.copy()
    ref["pending_at"] = pd.to_datetime(ref["pending_at"], utc=True).astype(
        "datetime64[ns, UTC]"
    )
    ref["started_at"] = pd.to_datetime(ref["started_at"], utc=True).astype(
        "datetime64[ns, UTC]"
    )
    # resolved_at is optional (back-compat): a missing column means "never
    # resolved" -> all-NaT, so exit reduces to started_at as before.
    if "resolved_at" in ref.columns:
        ref["resolved_at"] = pd.to_datetime(ref["resolved_at"], utc=True).astype(
            "datetime64[ns, UTC]"
        )
    else:
        ref["resolved_at"] = pd.Series(
            pd.NaT, index=ref.index, dtype="datetime64[ns, UTC]"
        )
    ref["_rank"] = ref["priority_at_pending"].map(_rank)
    ref["_pending_ns"] = ref["pending_at"].astype("int64")
    # started_at NaT -> represent as +inf so "started_at > T" is always true.
    started_ns = ref["started_at"].astype("int64")
    ref["_started_ns"] = np.where(
        ref["started_at"].isna(), np.iinfo("int64").max, started_ns
    )
    # exit = COALESCE(started_at, resolved_at). A run leaves pending at exit;
    # NaT exit (never started, never resolved) -> +inf so "exit > T" is always
    # true (still pending). _started_ns is kept separate for the starts window.
    exit_at = ref["started_at"].where(ref["started_at"].notna(), ref["resolved_at"])
    exit_ns = exit_at.astype("int64")
    ref["_exit_ns"] = np.where(exit_at.isna(), np.iinfo("int64").max, exit_ns)
    ref["_tie"] = [
        _tie_key(tid, rid) for tid, rid in zip(ref["task_id"], ref["run_id"])
    ]

    out["_rank"] = out["priority_at_pending"].map(_rank)
    out["_t_ns"] = out["pending_at"].astype("int64")
    out["_tie"] = [
        _tie_key(tid, rid) for tid, rid in zip(out["task_id"], out["run_id"])
    ]

    w15 = 900 * 1_000_000_000
    w60 = 3600 * 1_000_000_000

    # Group reference runs by queue for efficient per-target masking.
    ref_by_queue = {q: g for q, g in ref.groupby("task_queue_id", sort=False)}

    for idx in out.index:
        tid = out.at[idx, "task_id"]
        rid = out.at[idx, "run_id"]
        qid = out.at[idx, "task_queue_id"]
        T = out.at[idx, "_t_ns"]
        target_rank = out.at[idx, "_rank"]
        target_tie = out.at[idx, "_tie"]

        g = ref_by_queue.get(qid)
        if g is None or len(g) == 0:
            continue

        p_ns = g["_pending_ns"].to_numpy()
        s_ns = g["_started_ns"].to_numpy()
        exit_ns = g["_exit_ns"].to_numpy()
        ranks = g["_rank"].to_numpy()
        ties = g["_tie"].to_numpy()
        fams = g["repo_family"].to_numpy()

        # Exclude the target itself (same task_id & run_id).
        not_self = ~(
            (g["task_id"].to_numpy() == tid) & (g["run_id"].to_numpy() == rid)
        )

        # Pending-at-T: pending_at <= T AND (exit is NULL OR exit > T), where
        # exit = COALESCE(started_at, resolved_at). A run that was resolved
        # without ever starting (canceled etc.) leaves pending at resolved_at.
        pending_at_t = (p_ns <= T) & (exit_ns > T) & not_self

        higher = pending_at_t & (ranks > target_rank)
        lower = pending_at_t & (ranks < target_rank)
        equal = pending_at_t & (ranks == target_rank)

        # Same-priority FIFO: ordered before target by (pending_at, task_id, run_id).
        # earlier pending always counts; same-instant counts only if tie-key < target.
        earlier = equal & (p_ns < T)
        same_instant = equal & (p_ns == T)
        same_instant_before = same_instant & np.array(
            [t < target_tie for t in ties], dtype=bool
        )
        same_priority_ahead = earlier | same_instant_before

        out.at[idx, "pending_higher_priority_same_queue"] = int(higher.sum())
        out.at[idx, "pending_lower_priority_same_queue"] = int(lower.sum())
        out.at[idx, "pending_same_priority_same_queue"] = int(
            same_priority_ahead.sum()
        )

        # rank >= target among pending-at-T (used for oldest-age, families, capacity).
        higher_or_equal = pending_at_t & (ranks >= target_rank)
        n_he = int(higher_or_equal.sum())
        # higher-or-equal INCLUDING target (for per-capacity divisions).
        out.at[idx, _HIGHER_OR_EQUAL_INCL_SELF] = n_he + 1

        if n_he > 0:
            oldest_p = p_ns[higher_or_equal].min()
            out.at[idx, "oldest_higher_or_equal_pending_age_same_queue"] = (
                T - oldest_p
            ) / 1_000_000_000.0

            he_fams = fams[higher_or_equal]
            out.at[idx, "pending_try_higher_or_equal_same_queue"] = int(
                (he_fams == "try").sum()
            )
            out.at[idx, "pending_autoland_higher_or_equal_same_queue"] = int(
                (he_fams == "autoland").sum()
            )
            out.at[idx, "pending_release_beta_higher_or_equal_same_queue"] = int(
                (he_fams == "release_beta").sum()
            )

        # Flow: arrivals = pending_at in (T-w, T], excluding self.
        arrivals_15 = not_self & (p_ns > T - w15) & (p_ns <= T)
        arrivals_60 = not_self & (p_ns > T - w60) & (p_ns <= T)
        out.at[idx, "arrivals_15m_same_queue"] = int(arrivals_15.sum())
        out.at[idx, "arrivals_60m_same_queue"] = int(arrivals_60.sum())
        out.at[idx, "arrivals_higher_or_equal_15m_same_queue"] = int(
            (arrivals_15 & (ranks >= target_rank)).sum()
        )
        out.at[idx, "arrivals_higher_or_equal_60m_same_queue"] = int(
            (arrivals_60 & (ranks >= target_rank)).sum()
        )

        # starts_higher_or_equal_15m: started_at in (T-900s, T], rank>=target.
        # Only timestamps <= T are allowed (started_at <= T here).
        started_15 = (
            not_self & (s_ns > T - w15) & (s_ns <= T) & (ranks >= target_rank)
        )
        out.at[idx, "starts_higher_or_equal_15m_same_queue"] = int(
            started_15.sum()
        )

    # Coverage counts ALL pending-at-T runs (any rank) including the target,
    # divided by queue_pending (which itself includes the target).
    out = _attach_coverage(out, ref_by_queue)

    out = _attach_capacity(
        out, worker_counts, capacity_staleness_s=capacity_staleness_s
    )

    drop_cols = ["_rank", "_t_ns", "_tie", _HIGHER_OR_EQUAL_INCL_SELF]
    return out.drop(columns=[c for c in drop_cols if c in out.columns])


def _attach_coverage(out: pd.DataFrame, ref_by_queue: dict) -> pd.DataFrame:
    """Compute backlog_coverage_ratio = (pending-at-T incl target) / queue_pending."""
    ratios = np.full(len(out), np.nan)
    qp = pd.to_numeric(out["queue_pending"], errors="coerce").to_numpy(dtype=float)

    for pos, idx in enumerate(out.index):
        queue_pending = qp[pos]
        if not np.isfinite(queue_pending) or queue_pending <= 0:
            continue
        qid = out.at[idx, "task_queue_id"]
        tid = out.at[idx, "task_id"]
        rid = out.at[idx, "run_id"]
        T = out.at[idx, "_t_ns"]

        g = ref_by_queue.get(qid)
        if g is None or len(g) == 0:
            n_pending = 0
        else:
            p_ns = g["_pending_ns"].to_numpy()
            exit_ns = g["_exit_ns"].to_numpy()
            not_self = ~(
                (g["task_id"].to_numpy() == tid) & (g["run_id"].to_numpy() == rid)
            )
            pending_at_t = (p_ns <= T) & (exit_ns > T) & not_self
            n_pending = int(pending_at_t.sum())

        # Include the target itself in the numerator.
        ratios[pos] = (n_pending + 1) / queue_pending

    out["backlog_coverage_ratio"] = ratios
    return out


def _attach_capacity(
    out: pd.DataFrame,
    worker_counts: pd.DataFrame,
    *,
    capacity_staleness_s: float = 900,
) -> pd.DataFrame:
    """Attach worker-capacity features and per-capacity ratios.

    For each row, pick the latest worker_counts sample with sampled_at <= T on
    the same task_queue_id. Defaults to capacity_null_reason="no_sample"; refined
    to static_pool_null (existing_capacity NULL), zero_capacity (==0), or ok.
    Per-capacity ratios are NaN (never imputed 0) when capacity is NULL or 0.
    """
    n = len(out)

    running_arr = np.full(n, np.nan)
    capacity_arr = np.full(n, np.nan)
    claimed_arr = np.full(n, np.nan)
    age_arr = np.full(n, np.nan)
    reason_arr = np.full(n, "no_sample", dtype=object)
    total_per_cap = np.full(n, np.nan)
    he_per_cap = np.full(n, np.nan)
    running_per_cap = np.full(n, np.nan)

    def _write_columns() -> None:
        out["running_workers"] = running_arr
        out["existing_capacity"] = capacity_arr
        out["claimed_tasks"] = claimed_arr
        out["capacity_sample_age_s"] = age_arr
        out["capacity_null_reason"] = reason_arr
        out["pending_total_per_capacity"] = total_per_cap
        out["pending_higher_or_equal_per_capacity"] = he_per_cap
        out["running_per_capacity"] = running_per_cap

    if n == 0 or worker_counts is None or len(worker_counts) == 0:
        _write_columns()
        return out

    queue_pending = pd.to_numeric(out["queue_pending"], errors="coerce").to_numpy(
        dtype=float
    )
    he_incl_self = out[_HIGHER_OR_EQUAL_INCL_SELF].to_numpy(dtype=float)
    t_ns = out["_t_ns"].to_numpy()
    qids = out["task_queue_id"].to_numpy()

    wc = worker_counts.copy()
    wc["sampled_at"] = pd.to_datetime(wc["sampled_at"], utc=True).astype(
        "datetime64[ns, UTC]"
    )
    wc["_s_ns"] = wc["sampled_at"].astype("int64")

    # Group target rows by queue once, then for each queue binary-search each
    # target's timestamp into that queue's (sorted) sample timestamps instead
    # of rescanning the whole per-queue sample array for every row -- the
    # previous version recomputed `s_ns <= T` (O(samples)) inside a Python
    # loop over every one of the n target rows (O(n x samples) overall, plus
    # ~8 `.at[]` scalar writes per row), which is what made this the
    # remaining bottleneck once the sweep itself was fixed.
    target_pos_by_queue: dict[object, list[int]] = {}
    for pos, qid in enumerate(qids):
        target_pos_by_queue.setdefault(qid, []).append(pos)

    for qid, g in wc.groupby("task_queue_id", sort=False):
        positions = target_pos_by_queue.get(qid)
        if not positions:
            continue

        g_sorted = g.sort_values("_s_ns")
        s_ns = g_sorted["_s_ns"].to_numpy()
        running_vals = pd.to_numeric(
            g_sorted["running_workers"], errors="coerce"
        ).to_numpy(dtype=float)
        cap_vals = pd.to_numeric(
            g_sorted["existing_capacity"], errors="coerce"
        ).to_numpy(dtype=float)
        claimed_vals = pd.to_numeric(
            g_sorted["claimed_tasks"], errors="coerce"
        ).to_numpy(dtype=float)

        positions = np.asarray(positions)
        T = t_ns[positions]
        # Index of the latest sample with sampled_at <= T (searchsorted on a
        # sorted array is the vectorized equivalent of the old
        # flatnonzero(s_ns <= T)[-1]); -1 means no eligible sample.
        sample_idx = np.searchsorted(s_ns, T, side="right") - 1
        has_sample = sample_idx >= 0
        if not has_sample.any():
            continue

        sp = positions[has_sample]
        si = sample_idx[has_sample]
        age_s = (T[has_sample] - s_ns[si]) / 1_000_000_000.0

        # Enforce the staleness bound: a sample older than
        # capacity_staleness_s is no usable reading -> leave as
        # no_sample / NaN (matches JS).
        fresh = age_s <= capacity_staleness_s
        if not fresh.any():
            continue

        fp = sp[fresh]
        fi = si[fresh]
        fage = age_s[fresh]

        running_arr[fp] = running_vals[fi]
        capacity_arr[fp] = cap_vals[fi]
        claimed_arr[fp] = claimed_vals[fi]
        age_arr[fp] = fage

        cap_f = capacity_arr[fp]
        cap_null = np.isnan(cap_f)
        reason_arr[fp[cap_null]] = "static_pool_null"

        nonnull = ~cap_null
        zero_cap = nonnull & (cap_f == 0)
        reason_arr[fp[zero_cap]] = "zero_capacity"

        ok = nonnull & (cap_f != 0)
        if not ok.any():
            continue
        ok_pos = fp[ok]
        ok_cap = cap_f[ok]
        reason_arr[ok_pos] = "ok"

        qp_ok = queue_pending[ok_pos]
        finite_qp = np.isfinite(qp_ok)
        total_per_cap[ok_pos[finite_qp]] = qp_ok[finite_qp] / ok_cap[finite_qp]

        he_per_cap[ok_pos] = he_incl_self[ok_pos] / ok_cap

        running_ok = running_arr[ok_pos]
        running_finite = np.isfinite(running_ok)
        running_per_cap[ok_pos[running_finite]] = (
            running_ok[running_finite] / ok_cap[running_finite]
        )

    _write_columns()

    return out
