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

Two implementations live here and MUST agree byte-for-byte:

* ``_add_queue_context_features_masked`` -- the original, per-target numpy-mask
  reference. O(targets x queue-size). Retained as the correctness oracle and is
  exercised by the equivalence test; do not change its logic.
* ``add_queue_context_features`` -- an event-sweep implementation that produces
  identical output in ~O((n+m) log n) per queue. This is the production path.
"""

from __future__ import annotations

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

    out = _attach_capacity(
        out, worker_counts, capacity_staleness_s=capacity_staleness_s
    )

    drop_cols = ["_rank", "_t_ns", "_tie", _HIGHER_OR_EQUAL_INCL_SELF]
    return out.drop(columns=[c for c in drop_cols if c in out.columns])


def _range_count(sorted_arr: np.ndarray, lo_excl: float, hi_incl: float) -> int:
    """Count of values v with lo_excl < v <= hi_incl, on an ascending array."""
    left = int(np.searchsorted(sorted_arr, lo_excl, side="right"))
    right = int(np.searchsorted(sorted_arr, hi_incl, side="right"))
    return right - left


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
    out["running_workers"] = np.nan
    out["existing_capacity"] = np.nan
    out["claimed_tasks"] = np.nan
    out["capacity_sample_age_s"] = np.nan
    out["capacity_null_reason"] = "no_sample"
    out["pending_total_per_capacity"] = np.nan
    out["pending_higher_or_equal_per_capacity"] = np.nan
    out["running_per_capacity"] = np.nan

    if n == 0:
        return out

    queue_pending = pd.to_numeric(out["queue_pending"], errors="coerce").to_numpy(
        dtype=float
    )
    he_incl_self = out[_HIGHER_OR_EQUAL_INCL_SELF].to_numpy(dtype=float)
    t_ns = out["_t_ns"].to_numpy()

    if worker_counts is None or len(worker_counts) == 0:
        return out

    wc = worker_counts.copy()
    wc["sampled_at"] = pd.to_datetime(wc["sampled_at"], utc=True).astype(
        "datetime64[ns, UTC]"
    )
    wc["_s_ns"] = wc["sampled_at"].astype("int64")
    wc_by_queue = {q: g.sort_values("_s_ns") for q, g in wc.groupby("task_queue_id")}

    for pos, idx in enumerate(out.index):
        qid = out.at[idx, "task_queue_id"]
        T = t_ns[pos]
        g = wc_by_queue.get(qid)
        if g is None or len(g) == 0:
            continue

        s_ns = g["_s_ns"].to_numpy()
        eligible = s_ns <= T
        if not eligible.any():
            continue

        # Latest sample at or before T.
        sel = np.flatnonzero(eligible)[-1]
        # Enforce the staleness bound: a sample older than capacity_staleness_s
        # is no usable reading -> leave as no_sample / NaN (matches JS).
        age_s = (T - s_ns[sel]) / 1_000_000_000.0
        if age_s > capacity_staleness_s:
            continue
        row = g.iloc[sel]

        running = row["running_workers"]
        cap = row["existing_capacity"]
        claimed = row["claimed_tasks"]

        out.at[idx, "running_workers"] = running
        out.at[idx, "existing_capacity"] = cap
        out.at[idx, "claimed_tasks"] = claimed
        out.at[idx, "capacity_sample_age_s"] = age_s

        cap_is_null = (
            cap is None
            or (isinstance(cap, float) and np.isnan(cap))
            or pd.isna(cap)
        )
        if cap_is_null:
            out.at[idx, "capacity_null_reason"] = "static_pool_null"
            continue
        cap_val = float(cap)
        if cap_val == 0:
            out.at[idx, "capacity_null_reason"] = "zero_capacity"
            continue

        out.at[idx, "capacity_null_reason"] = "ok"
        qp = queue_pending[pos]
        if np.isfinite(qp):
            out.at[idx, "pending_total_per_capacity"] = qp / cap_val
        out.at[idx, "pending_higher_or_equal_per_capacity"] = (
            he_incl_self[pos] / cap_val
        )
        running_val = (
            float(running)
            if running is not None and not pd.isna(running)
            else np.nan
        )
        if np.isfinite(running_val):
            out.at[idx, "running_per_capacity"] = running_val / cap_val

    return out
