"""Postgres -> Parquet loader with content-hashed cache.

Cache key includes only the query-shaping parts of the config (target,
columns, filters, window). Model hyperparameters do not invalidate
the cache because they don't change the pulled rows.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import psycopg

from src import extract_source
from src.config import Config, compute_windows


CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"

# The trainer pulls 14–30 days of rows and does large joins/sorts. Give its
# connections a generous work_mem set PER SESSION (via libpq startup options)
# rather than globally in postgresql.conf — the trainer is one batch connection
# at a time, so the blast radius is just this query, not max_connections × the
# global value. The modest global default lives in docker-compose.yml.
TRAINER_WORK_MEM = os.environ.get("TRAINER_WORK_MEM", "512MB")

# High-cardinality string columns that repeat heavily across rows (queue ids,
# scheduler ids, task/normalized names, repo family, priority). These stay
# object dtype for the full lifetime of the loaded DataFrame -- through every
# join and feature-computation step -- unless downcast right after the fetch.
# At production data volumes that's the dominant fixable memory cost: observed
# live 2026-07-15, run_duration_residual OOM-killed twice against a 12g cgroup
# limit (dmesg: anon-rss ~11.6-11.8GB) on a config with no other known
# per-row-blowup bug, just a wide object-dtype frame held for the whole run.
CATEGORICAL_DOWNCAST_COLUMNS = [
    "task_queue_id", "scheduler_id", "metadata_name", "normalized_name",
    "repo_family", "priority_at_pending",
]


def _downcast_categorical_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Cast whichever of CATEGORICAL_DOWNCAST_COLUMNS are present to pandas
    'category' dtype, in place. Values and comparisons are unaffected --
    groupby/merge/`.to_numpy()` all still see the original string values --
    this only changes how repeated values are stored in memory."""
    for col in CATEGORICAL_DOWNCAST_COLUMNS:
        if col in df.columns:
            df[col] = df[col].astype("category")
    return df


def _connect(dsn: str):
    """psycopg connection with a generous session work_mem for batch queries."""
    return psycopg.connect(dsn, options=f"-c work_mem={TRAINER_WORK_MEM}")

# Keep in sync with REPO_FAMILY_DERIVATION_VERSION in src/repo-family.js.
# Bump this whenever the repo_family derivation LOGIC changes; the queue-context
# reference cache filename embeds it so a logic bump auto-invalidates the cache.
REPO_FAMILY_DERIVATION_VERSION = 1


def resolve_baseline_file(c: Config) -> str | None:
    """Which baseline-predictions file (if any) to join into the loaded
    dataframe.

    Historically this join only ever happened for residual configs (the
    join target doubled as the residual transform's reference point). A
    discrete-hazard config wants the same baseline percentile columns
    (bl_wait_p50/bl_wait_p90) as plain informative features and for the
    evaluation guardrail floor, without a residual target transform -- so
    `baseline_features` is a second, residual-free way to request the same
    join."""
    if c.residual and c.baseline_features:
        raise ValueError(
            "Config sets both `residual` and `baseline_features`; they request the same "
            "baseline join from different files. Use `residual` for a target transform, "
            "`baseline_features` for baseline columns without one -- not both."
        )
    if c.residual:
        return c.residual["baseline_file"]
    if c.baseline_features:
        return c.baseline_features["baseline_file"]
    return None


def cache_key(c: Config) -> str:
    """8-hex-char SHA256 over the query-shaping config."""
    shaping = {
        "target":               c.target,
        "target_column":        c.target_column,
        "filters":              sorted(c.filters),
        "categorical_features": sorted(c.categorical_features),
        "numeric_features":     sorted(c.numeric_features),
        "derived_features":     c.derived_features,
        "lookback_days":        c.lookback_days,
        "validation_days":      c.validation_days,
        "holdout_days":         c.holdout_days,
    }
    blob = json.dumps(shaping, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:8]


def serving_hash(c: Config) -> str:
    """8-hex-char SHA256 over every config field that affects the served model.

    Includes everything cache_key covers, plus model_params, quantiles,
    residual block, baseline_dir, anomaly_filter, throughput_features,
    velocity_features.

    Feature lists are preserved in YAML order (NOT sorted) because the
    position of each feature in the ONNX input tensor is part of the
    serving contract. cache_key sorts them (fine for cache identity, where
    row content is invariant under column order); serving_hash must not.
    """
    payload = {
        "shaping": {
            "target":               c.target,
            "target_column":        c.target_column,
            "filters":              sorted(c.filters),
            "categorical_features": list(c.categorical_features),  # ORDERED
            "numeric_features":     list(c.numeric_features),      # ORDERED
            "derived_features":     c.derived_features,
            "lookback_days":        c.lookback_days,
            "validation_days":      c.validation_days,
            "holdout_days":         c.holdout_days,
        },
        "model_type":          c.model_type,
        "model_params":        c.model_params,
        "quantiles":           list(c.quantiles),
        "residual":            c.residual,
        "baseline_dir":        c.baseline_dir,
        "baseline_features":   c.baseline_features,
        "hazard_bins_minutes": c.hazard_bins_minutes,
        "anomaly_filter":      c.anomaly_filter,
        "throughput_features": c.throughput_features,
        "queue_context_features": getattr(c, "queue_context_features", None),
        "velocity_features":   getattr(c, "velocity_features", None),
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:8]


def file_sha256(path: Path) -> str:
    """Hex SHA256 of file contents (full 64-char string)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def throughput_cache_path(c: Config) -> Path | None:
    """Return the parquet cache path for throughput features, or None if not enabled."""
    if not (c.throughput_features and c.throughput_features.get("enabled")):
        return None
    import pandas as pd
    w = compute_windows(c)
    windows = tuple(c.throughput_features.get("windows_minutes", [15, 60]))
    max_window = max(windows) if windows else 60
    window_start = w.train_start - pd.Timedelta(minutes=max_window + 30)
    window_end = w.as_of_date
    from_str = window_start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")
    to_str = window_end.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return CACHE_DIR / f"throughput_runs_{from_str}_{to_str}.parquet"


def worker_counts_cache_path(c: Config) -> Path | None:
    """Return the parquet cache path for worker-count velocity features, or None if not enabled."""
    if not (c.velocity_features and c.velocity_features.get("enabled")):
        return None
    from datetime import timedelta
    w = compute_windows(c)
    fetch_from = w.train_start - timedelta(hours=1, minutes=30)
    fetch_to = w.as_of_date
    from_str = fetch_from.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")
    to_str = fetch_to.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return CACHE_DIR / f"worker_counts_{from_str}_{to_str}.parquet"


def cache_filename(c: Config) -> str:
    as_of = c.as_of_date.astimezone(timezone.utc).strftime("%Y-%m-%d")
    return f"{c.target}_lb{c.lookback_days}_asof{as_of}_{cache_key(c)}.parquet"


def cache_path(c: Config) -> Path:
    return CACHE_DIR / cache_filename(c)


# Columns every target needs for splitting, slicing, and debugging.
BASE_META_COLUMNS = [
    "task_id", "run_id", "pending_at", "resolved_at", "reason_resolved",
]


# The loader pulls everything the feature builder or evaluator might
# need; builder decides which columns become features. Table-qualified for the
# SQL path; `_needed_source_columns` returns the bare names, which is what the
# extract path projects with (the extract is already joined).
CANDIDATE_SOURCE_COLUMNS = {
    "task_queue_id":       "t.task_queue_id",
    "scheduler_id":        "t.scheduler_id",
    "metadata_name":       "t.metadata_name",
    "normalized_name":     "t.normalized_name",
    "max_run_time_s":      "t.max_run_time_s",
    "priority_at_pending": "r.priority_at_pending",
    "queue_pending":       "r.queue_pending",
    "repo_family":         "t.repo_family",
    "tags":                "t.tags",
}


def _needed_source_columns(c: Config) -> set[str]:
    """The optional source columns this config's SELECT needs, bare names.

    Shared by both sources so the two cannot drift: a column the SQL path
    selected and the extract path did not would be a feature silently absent
    from one of them, and LightGBM does not object to a column that never
    arrives -- it just fits a different model.
    """
    needed: set[str] = set()
    for feat in c.categorical_features + c.numeric_features:
        if feat.startswith("tags."):
            needed.add("tags")
        elif feat in CANDIDATE_SOURCE_COLUMNS:
            needed.add(feat)
    # Derived sources
    derived_sources = {v.get("source") for v in c.derived_features.values() if isinstance(v, dict)}
    for src in derived_sources:
        if src in CANDIDATE_SOURCE_COLUMNS:
            needed.add(src)
    return needed


def _build_query(c: Config) -> str:
    # Always select meta columns + target. Append any feature-derived
    # source columns the builder will need (tags is JSONB — one column,
    # derived features parse from the same).
    source_cols = {
        "r.task_id",
        "r.run_id",
        "r.pending_at",
        "r.resolved_at",
        "r.reason_resolved",
        f"r.{c.target_column} AS y",
    }
    for col in _needed_source_columns(c):
        source_cols.add(CANDIDATE_SOURCE_COLUMNS[col])

    select_sql = ",\n       ".join(sorted(source_cols))
    where = ["r.pending_at >= %(train_start)s", "r.pending_at < %(as_of_date)s", *c.filters]
    where_sql = "\n  AND ".join(where)

    return f"""
SELECT {select_sql}
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE {where_sql};
"""


def load_baseline_predictions(path: Path) -> pd.DataFrame:
    """Load per-row baseline-prediction NDJSON (produced by predictor.js
    --export-baseline-predictions) into a DataFrame keyed by (task_id, run_id).

    Null p50/p90 values in the NDJSON stay as NaN in the DataFrame.
    """
    import json as _json
    records: list[dict] = []
    with Path(path).open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            records.append(_json.loads(line))
    df = pd.DataFrame.from_records(records)
    # Keep only the join keys + baseline columns; drop pending_at (already
    # on the main frame). Column types are float64; NaN for nulls.
    keep = ["task_id", "run_id", "bl_duration_p50", "bl_duration_p90",
            "bl_wait_p50", "bl_wait_p90"]
    return df[keep]


def load_worker_counts(
    c: Config,
    train_start: datetime,
    as_of_date: datetime,
    *,
    refresh_cache: bool = False,
) -> pd.DataFrame:
    """Load worker-count time-series from Postgres, cached to Parquet.

    The lookback covers train_start - 1.5 hours so that trailing-window
    averages at the very start of the training period have enough history.
    Worker counts don't depend on model config, so the cache key is purely
    the time range.
    """
    from datetime import timedelta

    fetch_from = train_start - timedelta(hours=1, minutes=30)
    fetch_to = as_of_date

    from_str = fetch_from.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")
    to_str = fetch_to.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")
    cache_file = CACHE_DIR / f"worker_counts_{from_str}_{to_str}.parquet"

    src = extract_source.active()
    if src is not None:
        return src.worker_counts(fetch_from, fetch_to)

    if cache_file.exists() and not refresh_cache:
        return pd.read_parquet(cache_file)

    dsn = os.environ["DATABASE_URL"]
    query = """
SELECT task_queue_id, sampled_at, running_workers, claimed_tasks, existing_capacity
FROM queue_forecast_worker_counts
WHERE sampled_at >= %(fetch_from)s
  AND sampled_at < %(fetch_to)s
ORDER BY task_queue_id, sampled_at;
"""
    params = {"fetch_from": fetch_from, "fetch_to": fetch_to}
    with _connect(dsn) as conn:
        try:
            df = pd.read_sql_query(query, conn, params=params)
        except Exception:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
                columns = [d.name for d in cur.description]
                df = pd.DataFrame(rows, columns=columns)

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_file, index=False)
    return df


def load_worker_pools() -> pd.DataFrame:
    """Load the worker-pool dimension table from Postgres.

    Small table (~650 rows), no caching needed.
    """
    src = extract_source.active()
    if src is not None:
        return src.worker_pools()

    dsn = os.environ["DATABASE_URL"]
    query = """
SELECT task_queue_id, pool_kind, provider_type
FROM queue_forecast_worker_pools;
"""
    with _connect(dsn) as conn:
        try:
            df = pd.read_sql_query(query, conn)
        except Exception:
            with conn.cursor() as cur:
                cur.execute(query)
                rows = cur.fetchall()
                columns = [d.name for d in cur.description]
                df = pd.DataFrame(rows, columns=columns)
    return df


def load_task_runs_for_throughput(
    c: Config,
    window_start: datetime,
    window_end: datetime,
    *,
    refresh_cache: bool = False,
) -> pd.DataFrame:
    """Load task-run history needed by add_throughput_features, cached to Parquet.

    Returns columns: task_queue_id, started_at, resolved_at,
    wait_duration_s, run_duration_s.

    The range [window_start, window_end] should be wide enough to cover the
    widest trailing window for every training row: callers typically pass
    train_start - max_window_minutes - 30m  as window_start and as_of_date
    as window_end.

    Rows are filtered to resolved_at IS NOT NULL (in-progress runs carry no
    useful throughput signal).  Caching is independent of the main query
    cache so that different model configs that share the same time range
    reuse the same Parquet file.
    """
    from_str = window_start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")
    to_str   = window_end.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")
    cache_file = CACHE_DIR / f"throughput_runs_{from_str}_{to_str}.parquet"

    src = extract_source.active()
    if src is not None:
        return src.throughput_runs(window_start, window_end)

    if cache_file.exists() and not refresh_cache:
        return pd.read_parquet(cache_file)

    dsn = os.environ["DATABASE_URL"]
    # task_queue_id lives on queue_forecast_tasks, not on queue_forecast_task_runs.
    query = """
SELECT t.task_queue_id,
       r.started_at,
       r.resolved_at,
       r.wait_duration_s,
       r.run_duration_s
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.resolved_at IS NOT NULL
  AND r.resolved_at >= %(window_start)s
  AND r.resolved_at <= %(window_end)s
  AND t.task_queue_id IS NOT NULL;
"""
    params = {"window_start": window_start, "window_end": window_end}
    with _connect(dsn) as conn:
        try:
            df = pd.read_sql_query(query, conn, params=params)
        except Exception:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
                columns = [d.name for d in cur.description]
                df = pd.DataFrame(rows, columns=columns)

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_file, index=False)
    return df


def load_task_runs_for_queue_context(
    c: Config,
    window_start: datetime,
    as_of_date: datetime,
    *,
    refresh_cache: bool = False,
) -> pd.DataFrame:
    """Reference runs whose pending interval can overlap any training row's
    pending_at, used by add_queue_context_features, cached to Parquet.

    Returns columns: task_id, run_id, pending_at, started_at, resolved_at,
    priority_at_pending, task_queue_id, repo_family.

    A reference run is relevant to a training row at time T iff it was pending
    at T (pending_at <= T and (exit is NULL or exit > T), where
    exit = COALESCE(started_at, resolved_at)). We load every run that pended
    before as_of_date and had not yet exited the pending state at window_start
    (still pending, or exited after window_start), which is a superset of what
    any training row in the window can see; the leakage-safe per-target
    filtering happens in add_queue_context_features.

    `pending_at`/`task_created` are additionally floored at
    `window_start - lookback_days` (2x lookback_days of total reach from
    as_of_date): without a floor, both sides of the join scan the FULL history
    of an ever-growing table on every call (confirmed via EXPLAIN: a multi-TB
    read profile), and the cost only worsens as more days of data accumulate.
    A reference run open (never started, never resolved) for longer than one
    full training window before that window even starts is, for real
    Taskcluster CI tasks, essentially always a collection artifact rather than
    real queue backlog — the floor is deliberately generous (a full
    lookback_days of grace, not e.g. a handful of days) to avoid excluding
    anything real. The tasks-side floor lets that join use the existing
    idx_qf_tasks_task_created index instead of a full-table scan; it's safe
    without a NULL guard because task_created is set by the same upsertTask
    insert that sets task_queue_id (already required NOT NULL by this query).
    """
    from_str = window_start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")
    to_str   = as_of_date.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")
    # Cache invalidation: the filename embeds REPO_FAMILY_DERIVATION_VERSION, so
    # a bump of the derivation LOGIC version auto-invalidates the cache. BUT
    # repo_family is a MUTABLE, out-of-band-backfilled column — a *same-version*
    # backfill that fills previously-NULL rows does NOT change the version and so
    # will NOT auto-invalidate this cache. Therefore the repo_family backfill MUST
    # run BEFORE the ablation, or trainer/data/cache/queue_context_runs_* must be
    # cleared after a backfill, otherwise Tier D trains on stale/NULL repo_family.
    cache_file = CACHE_DIR / f"queue_context_runs_{from_str}_{to_str}_rfv{REPO_FAMILY_DERIVATION_VERSION}.parquet"

    src = extract_source.active()
    if src is not None:
        return src.qctx_runs(
            window_start, as_of_date,
            window_start - timedelta(days=c.lookback_days),
        )

    if cache_file.exists() and not refresh_cache:
        return pd.read_parquet(cache_file)

    dsn = os.environ["DATABASE_URL"]
    # task_queue_id / repo_family live on queue_forecast_tasks.
    query = """
SELECT r.task_id,
       r.run_id,
       r.pending_at,
       r.started_at,
       r.resolved_at,
       r.priority_at_pending,
       t.task_queue_id,
       t.repo_family
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.pending_at < %(as_of)s
  AND r.pending_at >= %(ref_lower)s
  AND (COALESCE(r.started_at, r.resolved_at) IS NULL
       OR COALESCE(r.started_at, r.resolved_at) > %(wstart)s)
  AND t.task_queue_id IS NOT NULL
  AND t.task_created >= %(ref_lower)s;
"""
    ref_lower = window_start - timedelta(days=c.lookback_days)
    params = {"as_of": as_of_date, "wstart": window_start, "ref_lower": ref_lower}
    with _connect(dsn) as conn:
        try:
            df = pd.read_sql_query(query, conn, params=params)
        except Exception:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
                columns = [d.name for d in cur.description]
                df = pd.DataFrame(rows, columns=columns)

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_file, index=False)
    return df


_ALLOWED_FLAGS = frozenset({
    "flag_exception_spike",
    "flag_stuck_pending_spike",
    "flag_wait_p99_spike",
    "flag_volume_anomaly",
    "flag_low_completion",
    # New worker-side flags. Trainer can opt into these via flag_subset.
    "flag_capacity_drop",
    "flag_capacity_spike",
    "flag_low_utilization",
    "flag_sampler_offline",
})


def load_anomalous_dates(c: Config) -> set[datetime.date]:
    """Return the set of dates flagged anomalous in queue_forecast_daily_health.

    Honors ``c.anomaly_filter["flag_subset"]``: when set, only the listed flags
    contribute to the anomaly determination. Default: any of the per-flag
    booleans true -> anomalous.
    """
    if c.anomaly_filter is None or not c.anomaly_filter.get("enabled"):
        return set()
    flag_subset = c.anomaly_filter.get("flag_subset")  # list[str] | None
    if flag_subset:
        invalid = [f for f in flag_subset if f not in _ALLOWED_FLAGS]
        if invalid:
            raise ValueError(
                f"Unknown flag(s) in anomaly_filter.flag_subset: {invalid}. "
                f"Allowed: {sorted(_ALLOWED_FLAGS)}"
            )
    # The allowlist check runs on BOTH paths, before either source is touched:
    # it is the config validation that catches a typo'd flag name, and only
    # incidentally the thing that keeps the SQL below non-injectable.
    src = extract_source.active()
    if src is not None:
        return src.anomalous_dates(flag_subset)

    dsn = os.environ["DATABASE_URL"]
    if flag_subset:
        condition = " OR ".join(f"{f} = TRUE" for f in flag_subset)
    else:
        condition = "is_anomalous = TRUE"
    query = f"SELECT sample_date FROM queue_forecast_daily_health WHERE {condition}"
    with _connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            return {r[0] for r in cur.fetchall()}


def _log_step(label: str, t0: float) -> None:
    """Bracket-style timing log, matching this codebase's plain-print
    convention elsewhere (e.g. [backfill], [ensure-baseline]). Temporary
    instrumentation to localize which stage of load() is slow now that the
    queue-context SQL query itself is no longer the bottleneck (2026-07)."""
    print(f"[data_loader] {label}: {time.monotonic() - t0:.1f}s", flush=True)


def load(c: Config, *, refresh_cache: bool = False, worker_pools: pd.DataFrame | None = None) -> pd.DataFrame:
    src = extract_source.active()
    path = cache_path(c)
    t0 = time.monotonic()
    if src is not None:
        # The extract path neither reads nor writes `data/cache`: the extract is
        # already an immutable, content-hashed cache, and a cache filename that
        # cannot say which source produced it would let the two mix. See the
        # module docstring in `extract_source.py`.
        w = compute_windows(c)
        df = src.runs(
            train_start=w.train_start,
            as_of_date=w.as_of_date,
            target_column=c.target_column,
            keep_columns=BASE_META_COLUMNS + sorted(_needed_source_columns(c)),
            filters=c.filters,
        )
        _log_step(f"main dataset (extract, {len(df)} rows)", t0)
    elif path.exists() and not refresh_cache:
        df = pd.read_parquet(path)
        _log_step("main dataset (cache hit)", t0)
    else:
        dsn = os.environ["DATABASE_URL"]
        w = compute_windows(c)
        query = _build_query(c)
        params = {
            "train_start": w.train_start,
            "as_of_date":  w.as_of_date,
        }
        with _connect(dsn) as conn:
            try:
                df = pd.read_sql_query(query, conn, params=params)
            except Exception:
                # pandas may not support psycopg3 connections directly;
                # fall back to manual cursor fetch.
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    rows = cur.fetchall()
                    columns = [d.name for d in cur.description]
                    df = pd.DataFrame(rows, columns=columns)

        df = _downcast_categorical_columns(df)
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path, index=False)
        _log_step(f"main dataset (SQL fetch, {len(df)} rows)", t0)

    # Idempotent (astype("category") on an already-categorical column is a
    # cheap no-op) -- also covers cache-hit reads of parquet files written
    # before this downcast existed, without needing a cache-format bump.
    df = _downcast_categorical_columns(df)

    baseline_file = resolve_baseline_file(c)
    if baseline_file:
        t0 = time.monotonic()
        bl_dir = c.baseline_dir or "baseline"
        # Strip leading "data/" so configs can spell either "baseline_filtered" or
        # "data/baseline_filtered" interchangeably; both resolve under data/.
        if bl_dir.startswith("data/"):
            bl_dir = bl_dir[len("data/"):]
        bl_path = CACHE_DIR.parent / bl_dir / baseline_file
        bl = load_baseline_predictions(bl_path)
        before = len(df)
        df = df.merge(bl, on=["task_id", "run_id"], how="left")
        if len(df) != before:
            raise RuntimeError(
                f"Baseline join changed row count: {before} -> {len(df)} (duplicate keys?)"
            )
        _log_step("baseline join", t0)

    if c.velocity_features and c.velocity_features.get("enabled"):
        from src.velocity_features import add_velocity_features

        t0 = time.monotonic()
        w = compute_windows(c)
        worker_counts = load_worker_counts(c, w.train_start, w.as_of_date, refresh_cache=refresh_cache)
        if worker_pools is None:
            worker_pools = load_worker_pools()
        trailing = tuple(c.velocity_features.get("trailing_windows_minutes", [60]))
        tol = int(c.velocity_features.get("tolerance_minutes", 10))
        df = add_velocity_features(
            df, worker_counts, worker_pools,
            tolerance_minutes=tol,
            trailing_windows_minutes=trailing,
        )
        _log_step("velocity features (load + compute)", t0)

    if c.throughput_features and c.throughput_features.get("enabled"):
        from src.queue_throughput import add_throughput_features

        w = compute_windows(c)
        windows = tuple(c.throughput_features.get("windows_minutes", [15, 60]))
        max_window = max(windows) if windows else 60
        t0 = time.monotonic()
        runs_df = load_task_runs_for_throughput(
            c,
            w.train_start - pd.Timedelta(minutes=max_window + 30),
            w.as_of_date,
            refresh_cache=refresh_cache,
        )
        _log_step(f"throughput reference load (SQL, {len(runs_df)} rows)", t0)
        t0 = time.monotonic()
        df = add_throughput_features(df, runs_df, windows_minutes=windows)
        _log_step("throughput features (compute)", t0)

    if getattr(c, "queue_context_features", None) and c.queue_context_features.get("enabled"):
        from src.queue_context import add_queue_context_features

        w = compute_windows(c)
        t0 = time.monotonic()
        runs_qc = load_task_runs_for_queue_context(
            c, w.train_start - pd.Timedelta(minutes=90), w.as_of_date,
            refresh_cache=refresh_cache,
        )
        _log_step(f"queue-context reference load (SQL, {len(runs_qc)} rows)", t0)
        t0 = time.monotonic()
        wc = load_worker_counts(c, w.train_start - pd.Timedelta(minutes=30), w.as_of_date,
                                refresh_cache=refresh_cache)
        _log_step(f"worker-counts load (SQL, {len(wc)} rows)", t0)
        t0 = time.monotonic()
        df = add_queue_context_features(df, runs_qc, wc)
        _log_step(f"queue-context features (compute, {len(df)} rows)", t0)

    return df
