from datetime import datetime, timedelta, timezone

import pytest

from src.config import Config
from src import data_loader as dl


def _cfg(**overrides):
    base = dict(
        target="wait_time",
        target_column="wait_duration_s",
        lookback_days=14,
        holdout_days=5,
        validation_days=1,
        as_of_date=datetime(2026, 4, 24, tzinfo=timezone.utc),
        filters=["r.started_at IS NOT NULL"],
        categorical_features=["task_queue_id"],
        numeric_features=["queue_pending"],
        derived_features={},
        model_type="lightgbm",
        quantiles=[0.5, 0.9],
        model_params={"num_leaves": 63},
    )
    base.update(overrides)
    return Config(**base)


def test_cache_key_stable_across_unrelated_changes():
    c1 = _cfg()
    c2 = _cfg(model_params={"num_leaves": 127})  # hyperparameter change
    assert dl.cache_key(c1) == dl.cache_key(c2)


def test_cache_key_changes_with_filter():
    c1 = _cfg()
    c2 = _cfg(filters=["r.started_at IS NOT NULL", "r.queue_pending IS NOT NULL"])
    assert dl.cache_key(c1) != dl.cache_key(c2)


def test_cache_key_changes_with_columns():
    c1 = _cfg()
    c2 = _cfg(categorical_features=["task_queue_id", "scheduler_id"])
    assert dl.cache_key(c1) != dl.cache_key(c2)


def test_cache_filename_shape():
    c = _cfg()
    name = dl.cache_filename(c)
    assert name.startswith("wait_time_lb14_asof2026-04-24_")
    assert name.endswith(".parquet")
    # cfg8 hash segment is 8 hex chars
    stem = name[: -len(".parquet")]
    cfg8 = stem.rsplit("_", 1)[-1]
    assert len(cfg8) == 8 and all(ch in "0123456789abcdef" for ch in cfg8)


def test_load_baseline_predictions(tmp_path):
    from src.data_loader import load_baseline_predictions
    ndjson = tmp_path / "bl.ndjson"
    ndjson.write_text(
        '{"task_id":"a","run_id":0,"pending_at":"2026-04-15T10:00:00Z","bl_duration_p50":100.0,"bl_duration_p90":200.0,"bl_wait_p50":30.0,"bl_wait_p90":90.0}\n'
        '{"task_id":"b","run_id":1,"pending_at":"2026-04-15T11:00:00Z","bl_duration_p50":null,"bl_duration_p90":null,"bl_wait_p50":45.5,"bl_wait_p90":null}\n'
    )
    df = load_baseline_predictions(ndjson)
    assert list(df.columns) == ["task_id", "run_id", "bl_duration_p50", "bl_duration_p90", "bl_wait_p50", "bl_wait_p90"]
    assert len(df) == 2
    import numpy as np
    assert np.isnan(df.iloc[1]["bl_duration_p50"])
    assert df.iloc[1]["bl_wait_p50"] == 45.5


def test_repo_family_derivation_version_matches_js():
    """The Python constant must stay in lockstep with src/repo-family.js."""
    import re
    from pathlib import Path

    js = Path(__file__).resolve().parents[2] / "src" / "repo-family.js"
    text = js.read_text()
    m = re.search(r"REPO_FAMILY_DERIVATION_VERSION\s*=\s*(\d+)", text)
    assert m, "could not find REPO_FAMILY_DERIVATION_VERSION in repo-family.js"
    assert int(m.group(1)) == dl.REPO_FAMILY_DERIVATION_VERSION


def test_queue_context_cache_filename_embeds_rfv(monkeypatch):
    """The queue-context reference cache path is version-keyed (rfv<N>)."""
    captured = {}

    def fake_read_parquet(path):
        captured["path"] = str(path)
        raise AssertionError("stop after path is built")

    # Make the cache "exist" so the function builds the path then reads it.
    monkeypatch.setattr(dl.Path, "exists", lambda self: True)
    monkeypatch.setattr(dl.pd, "read_parquet", fake_read_parquet)

    c = _cfg()
    try:
        dl.load_task_runs_for_queue_context(
            c,
            datetime(2026, 4, 1, tzinfo=timezone.utc),
            datetime(2026, 4, 24, tzinfo=timezone.utc),
        )
    except AssertionError:
        pass
    assert "queue_context_runs_" in captured["path"]
    assert f"_rfv{dl.REPO_FAMILY_DERIVATION_VERSION}.parquet" in captured["path"]


def test_queue_context_refresh_cache_bypasses_parquet(monkeypatch, tmp_path):
    """--refresh-cache must reach the queue-context loader: refresh_cache=False
    serves the cached parquet without touching the DB; refresh_cache=True bypasses
    the cache and enters the query path (re-running the DB read + parquet write).
    """
    import pandas as pd

    # Isolate the cache to a tmp dir.
    monkeypatch.setattr(dl, "CACHE_DIR", tmp_path)

    window_start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    as_of_date = datetime(2026, 4, 24, tzinfo=timezone.utc)

    # Reproduce the exact cache filename the function builds.
    from_str = window_start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")
    to_str = as_of_date.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")
    cache_file = tmp_path / (
        f"queue_context_runs_{from_str}_{to_str}"
        f"_rfv{dl.REPO_FAMILY_DERIVATION_VERSION}.parquet"
    )

    # Write a 1-row dummy parquet with the columns the query produces.
    dummy = pd.DataFrame([{
        "task_id": "dummy-task",
        "run_id": 0,
        "pending_at": window_start,
        "started_at": None,
        "resolved_at": None,
        "priority_at_pending": "normal",
        "task_queue_id": "q/dummy",
        "repo_family": "from-cache",
    }])
    dummy.to_parquet(cache_file, index=False)

    class _ConnectCalled(Exception):
        pass

    def _sentinel_connect(*args, **kwargs):
        raise _ConnectCalled()

    monkeypatch.setattr(dl.psycopg, "connect", _sentinel_connect)
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")

    c = _cfg()

    # refresh_cache=False -> cache hit, DB never touched, dummy returned.
    out = dl.load_task_runs_for_queue_context(
        c, window_start, as_of_date, refresh_cache=False
    )
    assert list(out["repo_family"]) == ["from-cache"]

    # refresh_cache=True -> cache bypassed, query path entered (connect called).
    import pytest
    with pytest.raises(_ConnectCalled):
        dl.load_task_runs_for_queue_context(
            c, window_start, as_of_date, refresh_cache=True
        )


def test_downcast_categorical_columns_converts_present_columns():
    import pandas as pd

    df = pd.DataFrame({
        "task_queue_id": ["q/a", "q/b", "q/a"],
        "scheduler_id": ["s1", "s1", "s2"],
        "metadata_name": ["m1", "m2", "m1"],
        "normalized_name": ["n1", "n2", "n1"],
        "repo_family": ["try", "autoland", "try"],
        "priority_at_pending": ["high", "low", "high"],
        "queue_pending": [1, 2, 3],
    })
    out = dl._downcast_categorical_columns(df)
    for col in ["task_queue_id", "scheduler_id", "metadata_name",
                "normalized_name", "repo_family", "priority_at_pending"]:
        assert isinstance(out[col].dtype, pd.CategoricalDtype), col
    # Values are unchanged, just the dtype/storage.
    assert list(out["task_queue_id"]) == ["q/a", "q/b", "q/a"]
    # Untouched columns keep their original dtype.
    assert out["queue_pending"].dtype.kind in "iu"


def test_downcast_categorical_columns_skips_absent_columns():
    import pandas as pd

    df = pd.DataFrame({"queue_pending": [1, 2, 3]})
    out = dl._downcast_categorical_columns(df)  # no candidate columns present
    assert list(out.columns) == ["queue_pending"]


def test_queue_context_query_bounds_pending_at_and_task_created(monkeypatch, tmp_path):
    """load_task_runs_for_queue_context must floor pending_at/task_created at
    (window_start - lookback_days). Without this the reference-run query scans
    the FULL history of an ever-growing table on every cohort (confirmed live
    via EXPLAIN: Seq Scan on both queue_forecast_task_runs and
    queue_forecast_tasks, multi-TB cumulative read profile).
    """
    import pandas as pd

    monkeypatch.setattr(dl, "CACHE_DIR", tmp_path)  # force a cache miss

    captured = {}

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake_connect(dsn):
        return _FakeConn()

    def _fake_read_sql_query(query, conn, params=None):
        captured["query"] = query
        captured["params"] = params
        return pd.DataFrame(columns=[
            "task_id", "run_id", "pending_at", "started_at", "resolved_at",
            "priority_at_pending", "task_queue_id", "repo_family",
        ])

    monkeypatch.setattr(dl, "_connect", _fake_connect)
    monkeypatch.setattr(dl.pd, "read_sql_query", _fake_read_sql_query)
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")

    c = _cfg(lookback_days=14)
    window_start = datetime(2026, 5, 30, tzinfo=timezone.utc)
    as_of_date = datetime(2026, 6, 13, tzinfo=timezone.utc)

    dl.load_task_runs_for_queue_context(c, window_start, as_of_date)

    query, params = captured["query"], captured["params"]
    assert "r.pending_at >= %(ref_lower)s" in query
    assert "t.task_created >= %(ref_lower)s" in query
    assert params["ref_lower"] == window_start - timedelta(days=c.lookback_days)
    # Sanity: the floor must be strictly before window_start, not after it —
    # a wrong-direction bound would silently exclude the whole training window.
    assert params["ref_lower"] < window_start


def test_resolve_baseline_file_none_when_neither_set():
    c = _cfg()
    assert dl.resolve_baseline_file(c) is None


def test_resolve_baseline_file_from_residual():
    c = _cfg(residual={"baseline_file": "baseline_predictions.ndjson", "baseline_feature": "bl_wait_p50"})
    assert dl.resolve_baseline_file(c) == "baseline_predictions.ndjson"


def test_resolve_baseline_file_from_baseline_features():
    c = _cfg(baseline_features={"baseline_file": "hazard_baseline.ndjson"})
    assert dl.resolve_baseline_file(c) == "hazard_baseline.ndjson"


def test_resolve_baseline_file_raises_when_both_set():
    c = _cfg(
        residual={"baseline_file": "residual.ndjson", "baseline_feature": "bl_wait_p50"},
        baseline_features={"baseline_file": "hazard.ndjson"},
    )
    with pytest.raises(ValueError, match="both"):
        dl.resolve_baseline_file(c)
