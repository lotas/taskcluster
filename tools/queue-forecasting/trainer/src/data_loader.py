"""Postgres -> Parquet loader with content-hashed cache.

Cache key includes only the query-shaping parts of the config (target,
columns, filters, window). Model hyperparameters do not invalidate
the cache because they don't change the pulled rows.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from datetime import timezone

import pandas as pd
import psycopg

from src.config import Config, compute_windows


CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"


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


def cache_filename(c: Config) -> str:
    as_of = c.as_of_date.astimezone(timezone.utc).strftime("%Y-%m-%d")
    return f"{c.target}_lb{c.lookback_days}_asof{as_of}_{cache_key(c)}.parquet"


def cache_path(c: Config) -> Path:
    return CACHE_DIR / cache_filename(c)


# Columns every target needs for splitting, slicing, and debugging.
BASE_META_COLUMNS = [
    "task_id", "run_id", "pending_at", "resolved_at", "reason_resolved",
]


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
    # The loader pulls everything the feature builder or evaluator might
    # need; builder decides which columns become features.
    candidate_cols = {
        "task_queue_id":       "t.task_queue_id",
        "scheduler_id":        "t.scheduler_id",
        "metadata_name":       "t.metadata_name",
        "normalized_name":     "t.normalized_name",
        "max_run_time_s":      "t.max_run_time_s",
        "priority_at_pending": "r.priority_at_pending",
        "queue_pending":       "r.queue_pending",
        "tags":                "t.tags",
    }
    needed: set[str] = set()
    for feat in c.categorical_features + c.numeric_features:
        if feat.startswith("tags."):
            needed.add("tags")
        elif feat in candidate_cols:
            needed.add(feat)
    # Derived sources
    derived_sources = {v.get("source") for v in c.derived_features.values() if isinstance(v, dict)}
    for src in derived_sources:
        if src in candidate_cols:
            needed.add(src)

    for col in needed:
        source_cols.add(candidate_cols[col])

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


def load(c: Config, *, refresh_cache: bool = False) -> pd.DataFrame:
    path = cache_path(c)
    if path.exists() and not refresh_cache:
        df = pd.read_parquet(path)
    else:
        dsn = os.environ["DATABASE_URL"]
        w = compute_windows(c)
        query = _build_query(c)
        params = {
            "train_start": w.train_start,
            "as_of_date":  w.as_of_date,
        }
        with psycopg.connect(dsn) as conn:
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

        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path, index=False)

    if c.residual:
        bl_path = CACHE_DIR.parent / "baseline" / c.residual["baseline_file"]
        bl = load_baseline_predictions(bl_path)
        before = len(df)
        df = df.merge(bl, on=["task_id", "run_id"], how="left")
        if len(df) != before:
            raise RuntimeError(
                f"Baseline join changed row count: {before} -> {len(df)} (duplicate keys?)"
            )
    return df
