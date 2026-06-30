import time

import numpy as np
import pandas as pd

from src.queue_context import (
    FEATURE_COLUMNS,
    PRIORITY_RANK,
    QUEUE_CONTEXT_FEATURE_VERSION,
    _add_queue_context_features_masked,
    add_queue_context_features,
)


def test_priority_rank_ordering():
    assert PRIORITY_RANK["highest"] > PRIORITY_RANK["very-high"] > PRIORITY_RANK["high"]
    assert (
        PRIORITY_RANK["medium"]
        > PRIORITY_RANK["low"]
        > PRIORITY_RANK["very-low"]
        > PRIORITY_RANK["lowest"]
    )
    assert PRIORITY_RANK["normal"] == PRIORITY_RANK["lowest"]


def test_version_is_int():
    assert isinstance(QUEUE_CONTEXT_FEATURE_VERSION, int)


def _runs(rows):
    return pd.DataFrame(
        rows,
        columns=[
            "task_id",
            "run_id",
            "pending_at",
            "started_at",
            "priority_at_pending",
            "task_queue_id",
            "repo_family",
        ],
    )


def _t(s):
    return pd.Timestamp(f"2026-06-01T{s}Z")


EMPTY_WC = pd.DataFrame(
    columns=[
        "task_queue_id",
        "sampled_at",
        "running_workers",
        "existing_capacity",
        "claimed_tasks",
    ]
)


def _target_df(runs, tid, qp=None):
    d = runs[runs.task_id == tid][
        [
            "task_id",
            "run_id",
            "pending_at",
            "priority_at_pending",
            "task_queue_id",
            "repo_family",
        ]
    ].copy()
    d["queue_pending"] = qp
    return d


def test_higher_priority_ahead_includes_same_instant():
    runs = _runs(
        [
            ["target", 0, _t("00:10:00"), None, "low", "q/a", "try"],
            ["hi", 0, _t("00:10:00"), None, "high", "q/a", "try"],
        ]
    )
    out = add_queue_context_features(_target_df(runs, "target"), runs, EMPTY_WC)
    assert out["pending_higher_priority_same_queue"].iloc[0] == 1


def test_same_priority_fifo_excludes_self_and_later():
    runs = _runs(
        [
            ["early", 0, _t("00:00:00"), None, "medium", "q/a", "try"],
            ["target", 0, _t("00:05:00"), None, "medium", "q/a", "try"],
            ["late", 0, _t("00:09:00"), None, "medium", "q/a", "try"],
        ]
    )
    out = add_queue_context_features(_target_df(runs, "target"), runs, EMPTY_WC)
    assert out["pending_same_priority_same_queue"].iloc[0] == 1


def test_started_peer_removed():
    runs = _runs(
        [
            ["gone", 0, _t("00:00:00"), _t("00:03:00"), "high", "q/a", "try"],
            ["target", 0, _t("00:05:00"), None, "low", "q/a", "try"],
        ]
    )
    out = add_queue_context_features(_target_df(runs, "target"), runs, EMPTY_WC)
    assert out["pending_higher_priority_same_queue"].iloc[0] == 0


def test_same_timestamp_tie_order():
    runs = _runs(
        [
            ["a", 0, _t("00:10:00"), None, "medium", "q/a", "try"],
            ["b", 0, _t("00:10:00"), None, "medium", "q/a", "try"],
            ["c", 0, _t("00:10:00"), None, "medium", "q/a", "try"],
        ]
    )
    out = add_queue_context_features(_target_df(runs, "b"), runs, EMPTY_WC)
    assert out["pending_same_priority_same_queue"].iloc[0] == 1  # only 'a' before 'b'


def test_oldest_higher_or_equal_age():
    runs = _runs(
        [
            ["old", 0, _t("00:00:00"), None, "high", "q/a", "try"],
            ["target", 0, _t("00:10:00"), None, "low", "q/a", "try"],
        ]
    )
    out = add_queue_context_features(_target_df(runs, "target"), runs, EMPTY_WC)
    assert out["oldest_higher_or_equal_pending_age_same_queue"].iloc[0] == 600.0


def test_repo_family_blocking_composition():
    runs = _runs(
        [
            ["beta", 0, _t("00:00:00"), None, "high", "q/a", "release_beta"],
            ["target", 0, _t("00:10:00"), None, "low", "q/a", "try"],
        ]
    )
    out = add_queue_context_features(_target_df(runs, "target"), runs, EMPTY_WC)
    assert out["pending_release_beta_higher_or_equal_same_queue"].iloc[0] == 1


def test_per_capacity_and_coverage():
    # 5 higher-or-equal peers pending at T (all 'high'), target 'low'; cap=10;
    # queue_pending=12 (incl target+peers)
    rows = [["target", 0, _t("00:10:00"), None, "low", "q/a", "try"]]
    for i in range(5):
        rows.append([f"p{i}", 0, _t("00:00:00"), None, "high", "q/a", "try"])
    runs = _runs(rows)
    wc = pd.DataFrame(
        [
            {
                "task_queue_id": "q/a",
                "sampled_at": _t("00:09:00"),
                "running_workers": 8,
                "existing_capacity": 10,
                "claimed_tasks": 7,
            }
        ]
    )
    out = add_queue_context_features(_target_df(runs, "target", qp=12), runs, wc)
    # higher-or-equal incl target = 5 peers + target = 6 -> /10 = 0.6
    assert abs(out["pending_higher_or_equal_per_capacity"].iloc[0] - 0.6) < 1e-9
    assert abs(out["pending_total_per_capacity"].iloc[0] - 1.2) < 1e-9
    assert abs(out["running_per_capacity"].iloc[0] - 0.8) < 1e-9
    assert out["capacity_null_reason"].iloc[0] == "ok"
    # coverage incl target: pending-at-T incl target = 6 (5 peers + target)
    # / queue_pending 12 = 0.5
    assert abs(out["backlog_coverage_ratio"].iloc[0] - 0.5) < 1e-9


def test_resolved_without_start_not_pending():
    # 'canceled' peer: higher priority, pending before T, NEVER started, resolved
    # before T -> NOT pending at T.
    runs = _runs(
        [
            ["canceled", 0, _t("00:00:00"), None, "high", "q/a", "try"],
            ["target", 0, _t("00:10:00"), None, "low", "q/a", "try"],
        ]
    )
    runs["resolved_at"] = [_t("00:03:00"), None]  # canceled resolved before T=00:10
    out = add_queue_context_features(_target_df(runs, "target"), runs, EMPTY_WC)
    assert out["pending_higher_priority_same_queue"].iloc[0] == 0


def test_resolved_without_start_still_pending_if_resolved_after_T():
    runs = _runs(
        [
            ["late_cancel", 0, _t("00:00:00"), None, "high", "q/a", "try"],
            ["target", 0, _t("00:10:00"), None, "low", "q/a", "try"],
        ]
    )
    runs["resolved_at"] = [_t("00:20:00"), None]  # resolved AFTER T -> still pending
    out = add_queue_context_features(_target_df(runs, "target"), runs, EMPTY_WC)
    assert out["pending_higher_priority_same_queue"].iloc[0] == 1


def test_stale_capacity_sample_treated_as_no_sample():
    # Worker-count sampled 20 min before T (> 900s staleness bound) -> no usable
    # reading: capacity_null_reason='no_sample' and per-capacity ratios NaN.
    runs = _runs([["target", 0, _t("00:25:00"), None, "low", "q/a", "try"]])
    wc = pd.DataFrame(
        [
            {
                "task_queue_id": "q/a",
                "sampled_at": _t("00:05:00"),  # 20 min before T=00:25
                "running_workers": 8,
                "existing_capacity": 10,
                "claimed_tasks": 7,
            }
        ]
    )
    out = add_queue_context_features(_target_df(runs, "target", qp=12), runs, wc)
    assert out["capacity_null_reason"].iloc[0] == "no_sample"
    assert np.isnan(out["pending_total_per_capacity"].iloc[0])
    assert np.isnan(out["pending_higher_or_equal_per_capacity"].iloc[0])
    assert np.isnan(out["running_per_capacity"].iloc[0])
    assert np.isnan(out["running_workers"].iloc[0])
    assert np.isnan(out["existing_capacity"].iloc[0])
    assert np.isnan(out["capacity_sample_age_s"].iloc[0])


def test_fresh_capacity_sample_within_staleness_bound_ok():
    # Sample 5 min before T (<= 900s) with cap=10 -> ok.
    runs = _runs([["target", 0, _t("00:25:00"), None, "low", "q/a", "try"]])
    wc = pd.DataFrame(
        [
            {
                "task_queue_id": "q/a",
                "sampled_at": _t("00:20:00"),  # 5 min before T=00:25
                "running_workers": 8,
                "existing_capacity": 10,
                "claimed_tasks": 7,
            }
        ]
    )
    out = add_queue_context_features(_target_df(runs, "target", qp=12), runs, wc)
    assert out["capacity_null_reason"].iloc[0] == "ok"
    assert abs(out["pending_total_per_capacity"].iloc[0] - 1.2) < 1e-9
    assert abs(out["capacity_sample_age_s"].iloc[0] - 300.0) < 1e-9


def test_static_pool_null_capacity():
    runs = _runs([["target", 0, _t("00:10:00"), None, "low", "q/a", "try"]])
    wc = pd.DataFrame(
        [
            {
                "task_queue_id": "q/a",
                "sampled_at": _t("00:09:00"),
                "running_workers": None,
                "existing_capacity": None,
                "claimed_tasks": 4,
            }
        ]
    )
    out = add_queue_context_features(_target_df(runs, "target", qp=3), runs, wc)
    assert np.isnan(out["pending_total_per_capacity"].iloc[0])
    assert out["capacity_null_reason"].iloc[0] == "static_pool_null"


# ---------------------------------------------------------------------------
# Equivalence: the event sweep must match the masked oracle byte-for-byte.
# ---------------------------------------------------------------------------

RANKS = ["highest", "very-high", "high", "medium", "low", "very-low", "lowest",
         "normal", "unknown-priority", None]
FAMILIES = ["try", "autoland", "central", "release_beta", "other", "unknown", None]


def _rand_ts(rng, base_ns, span_ns):
    return pd.Timestamp(base_ns + int(rng.integers(0, span_ns)), tz="UTC")


def _make_scenario(rng, n_ref, n_targets, n_queues):
    base = pd.Timestamp("2026-06-01T00:00:00Z").value
    span = 6 * 3600 * 1_000_000_000  # 6h window in ns

    queues = [f"q/{i}" for i in range(n_queues)]
    ref_rows = []
    # Cluster many rows at identical pending_at to stress same-instant logic.
    cluster_instants = [base + int(rng.integers(0, span)) for _ in range(5)]

    for i in range(n_ref):
        qid = queues[int(rng.integers(0, n_queues))]
        if rng.random() < 0.35:
            p = cluster_instants[int(rng.integers(0, len(cluster_instants)))]
        else:
            p = base + int(rng.integers(0, span))
        pending_at = pd.Timestamp(p, tz="UTC")

        r = rng.random()
        started_at = None
        resolved_at = None
        if r < 0.45:
            # started (and possibly resolved later, irrelevant to exit)
            started_at = pd.Timestamp(p + int(rng.integers(1, span)), tz="UTC")
            if rng.random() < 0.5:
                resolved_at = pd.Timestamp(
                    started_at.value + int(rng.integers(1, span)), tz="UTC"
                )
        elif r < 0.7:
            # resolved without ever starting (canceled etc.)
            resolved_at = pd.Timestamp(p + int(rng.integers(1, span)), tz="UTC")
        # else: both NULL -> still pending forever

        ref_rows.append({
            "task_id": f"t{i}",
            "run_id": int(rng.integers(0, 3)),
            "pending_at": pending_at,
            "started_at": started_at,
            "resolved_at": resolved_at,
            "priority_at_pending": RANKS[int(rng.integers(0, len(RANKS)))],
            "task_queue_id": qid,
            "repo_family": FAMILIES[int(rng.integers(0, len(FAMILIES)))],
        })
    runs = pd.DataFrame(ref_rows)

    # Targets: a mix of (a) rows drawn from the reference set (so self-exclusion
    # is exercised) and (b) fresh synthetic rows.
    tgt_rows = []
    for j in range(n_targets):
        if len(ref_rows) and rng.random() < 0.5:
            src = ref_rows[int(rng.integers(0, len(ref_rows)))]
            qid = src["task_queue_id"]
            pending_at = src["pending_at"]
            tid = src["task_id"]
            rid = src["run_id"]
            prio = src["priority_at_pending"]
            fam = src["repo_family"]
        else:
            qid = queues[int(rng.integers(0, n_queues))]
            if rng.random() < 0.35:
                p = cluster_instants[int(rng.integers(0, len(cluster_instants)))]
            else:
                p = base + int(rng.integers(0, span))
            pending_at = pd.Timestamp(p, tz="UTC")
            tid = f"tgt{j}"
            rid = int(rng.integers(0, 3))
            prio = RANKS[int(rng.integers(0, len(RANKS)))]
            fam = FAMILIES[int(rng.integers(0, len(FAMILIES)))]

        qpr = rng.random()
        if qpr < 0.15:
            qp = 0
        elif qpr < 0.3:
            qp = np.nan
        else:
            qp = int(rng.integers(1, 50))

        tgt_rows.append({
            "task_id": tid,
            "run_id": rid,
            "pending_at": pending_at,
            "priority_at_pending": prio,
            "task_queue_id": qid,
            "repo_family": fam,
            "queue_pending": qp,
        })
    df = pd.DataFrame(tgt_rows)

    # worker_counts: some fresh, some stale, some null/zero capacity.
    wc_rows = []
    for qid in queues:
        for _ in range(int(rng.integers(0, 4))):
            sp = base + int(rng.integers(0, span))
            capr = rng.random()
            if capr < 0.2:
                cap = None
            elif capr < 0.35:
                cap = 0
            else:
                cap = int(rng.integers(1, 30))
            wc_rows.append({
                "task_queue_id": qid,
                "sampled_at": pd.Timestamp(sp, tz="UTC"),
                "running_workers": (
                    None if rng.random() < 0.15 else int(rng.integers(0, 30))
                ),
                "existing_capacity": cap,
                "claimed_tasks": int(rng.integers(0, 30)),
            })
    wc = pd.DataFrame(
        wc_rows,
        columns=[
            "task_queue_id",
            "sampled_at",
            "running_workers",
            "existing_capacity",
            "claimed_tasks",
        ],
    )
    return df, runs, wc


def _assert_equal_features(a, b):
    assert list(a.index) == list(b.index)
    for col in FEATURE_COLUMNS:
        sa = a[col]
        sb = b[col]
        if col == "capacity_null_reason":
            assert (sa.astype(object).values == sb.astype(object).values).all(), col
            continue
        va = pd.to_numeric(sa, errors="coerce").to_numpy(dtype=float)
        vb = pd.to_numeric(sb, errors="coerce").to_numpy(dtype=float)
        both_nan = np.isnan(va) & np.isnan(vb)
        close = np.isclose(va, vb, rtol=0, atol=1e-9, equal_nan=False)
        ok = both_nan | close
        if not ok.all():
            bad = np.flatnonzero(~ok)[:5]
            raise AssertionError(
                f"column {col} differs at positions {bad.tolist()}: "
                f"sweep={va[bad].tolist()} oracle={vb[bad].tolist()}"
            )


def test_sweep_matches_masked_oracle():
    rng = np.random.default_rng(0)
    n_scenarios = 0
    for _ in range(36):
        n_queues = int(rng.integers(1, 5))
        n_ref = int(rng.integers(0, 250))
        n_targets = int(rng.integers(1, 120))
        # occasionally a bigger scenario
        if rng.random() < 0.15:
            n_ref = int(rng.integers(800, 2000))
            n_targets = int(rng.integers(200, 600))
        df, runs, wc = _make_scenario(rng, n_ref, n_targets, n_queues)
        sweep = add_queue_context_features(df.copy(), runs.copy(), wc.copy())
        oracle = _add_queue_context_features_masked(
            df.copy(), runs.copy(), wc.copy()
        )
        _assert_equal_features(sweep, oracle)
        n_scenarios += 1
    assert n_scenarios >= 30


def test_sweep_is_subquadratic():
    rng = np.random.default_rng(1)
    base = pd.Timestamp("2026-06-01T00:00:00Z").value
    span = 12 * 3600 * 1_000_000_000
    n = 20000

    p = base + rng.integers(0, span, size=n)
    started = p + rng.integers(1, span, size=n)
    runs = pd.DataFrame({
        "task_id": [f"t{i}" for i in range(n)],
        "run_id": 0,
        "pending_at": pd.to_datetime(p, utc=True),
        "started_at": pd.to_datetime(started, utc=True),
        "resolved_at": pd.NaT,
        "priority_at_pending": "medium",
        "task_queue_id": "q/hot",
        "repo_family": "try",
    })
    df = pd.DataFrame({
        "task_id": [f"t{i}" for i in range(n)],
        "run_id": 0,
        "pending_at": pd.to_datetime(p, utc=True),
        "priority_at_pending": "medium",
        "task_queue_id": "q/hot",
        "repo_family": "try",
        "queue_pending": 10,
    })
    wc = pd.DataFrame(
        columns=[
            "task_queue_id", "sampled_at", "running_workers",
            "existing_capacity", "claimed_tasks",
        ]
    )

    t0 = time.perf_counter()
    add_queue_context_features(df.copy(), runs.copy(), wc.copy())
    sweep_s = time.perf_counter() - t0
    # 20k x 20k single hot queue must stay fast.
    assert sweep_s < 5.0, f"sweep too slow: {sweep_s:.2f}s"

    # Prove the win against the oracle on a smaller (5k) slice.
    k = 5000
    df_s = df.iloc[:k].copy()
    runs_s = runs.iloc[:k].copy()
    t0 = time.perf_counter()
    add_queue_context_features(df_s.copy(), runs_s.copy(), wc.copy())
    sweep_small_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    _add_queue_context_features_masked(df_s.copy(), runs_s.copy(), wc.copy())
    oracle_small_s = time.perf_counter() - t0
    print(
        f"\n[timing] sweep 20k={sweep_s:.3f}s | "
        f"sweep 5k={sweep_small_s:.3f}s oracle 5k={oracle_small_s:.3f}s "
        f"speedup={oracle_small_s / max(sweep_small_s, 1e-9):.1f}x"
    )
    assert oracle_small_s / max(sweep_small_s, 1e-9) >= 5.0
