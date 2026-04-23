from datetime import datetime, timezone

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
