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
