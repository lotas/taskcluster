import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from src.config import Config
from src.features import FeatureBuilder, Split


def _cfg(**overrides):
    base = dict(
        target="wait_time", target_column="y",
        lookback_days=14, holdout_days=5, validation_days=1,
        as_of_date=datetime(2026, 4, 24, tzinfo=timezone.utc),
        filters=[],
        categorical_features=["task_queue_id", "tags.kind"],
        numeric_features=["queue_pending", "hour_sin", "hour_cos", "day_sin", "day_cos"],
        derived_features={"cyclical_time": {"source": "pending_at"}},
        model_type="lightgbm", quantiles=[0.5], model_params={},
    )
    base.update(overrides)
    return Config(**base)


def _frame(rows):
    # META_COLUMNS includes resolved_at (needed by the discrete-hazard path);
    # default it to NaT for fixtures that don't care about it, so existing
    # test rows don't all need updating individually.
    rows = [dict(r) for r in rows]
    for r in rows:
        r.setdefault("resolved_at", pd.NaT)
    return pd.DataFrame(rows)


def test_fit_transform_and_transform_preserve_category_codes():
    c = _cfg()
    train = _frame([
        {"task_id": "a", "run_id": 0, "pending_at": pd.Timestamp("2026-04-10 01:00", tz="UTC"),
         "reason_resolved": "completed", "y": 5.0,
         "task_queue_id": "q1", "tags": {"kind": "build"}, "queue_pending": 10},
        {"task_id": "b", "run_id": 0, "pending_at": pd.Timestamp("2026-04-11 02:00", tz="UTC"),
         "reason_resolved": "completed", "y": 7.0,
         "task_queue_id": "q2", "tags": {"kind": "test"}, "queue_pending": 20},
    ])
    hold = _frame([
        {"task_id": "c", "run_id": 0, "pending_at": pd.Timestamp("2026-04-20 03:00", tz="UTC"),
         "reason_resolved": "completed", "y": 6.0,
         "task_queue_id": "q1", "tags": {"kind": "build"}, "queue_pending": 15},
        {"task_id": "d", "run_id": 0, "pending_at": pd.Timestamp("2026-04-20 04:00", tz="UTC"),
         "reason_resolved": "failed",   "y": 9.0,
         "task_queue_id": "q_unseen", "tags": {"kind": "wpt"}, "queue_pending": 30},
    ])

    b = FeatureBuilder(c)
    tr = b.fit_transform(train)
    ho = b.transform(hold)

    assert isinstance(tr, Split) and isinstance(ho, Split)
    # Train codes are stable into holdout
    tr_q_cats = list(tr.X["task_queue_id"].cat.categories)
    ho_q_cats = list(ho.X["task_queue_id"].cat.categories)
    assert tr_q_cats == ho_q_cats

    # Unseen holdout value becomes NaN (LightGBM unknown)
    unseen_row = ho.X.iloc[1]
    assert pd.isna(unseen_row["task_queue_id"])
    assert pd.isna(unseen_row["tags.kind"])

    # Meta carries slice columns
    assert list(ho.meta.columns) == ["pending_at", "resolved_at", "reason_resolved", "task_id", "run_id"]
    assert ho.meta["reason_resolved"].iloc[1] == "failed"


def test_cyclical_time_features():
    c = _cfg()
    df = _frame([
        {"task_id": "a", "run_id": 0, "pending_at": pd.Timestamp("2026-04-10 00:00", tz="UTC"),
         "reason_resolved": "completed", "y": 1.0,
         "task_queue_id": "q", "tags": {"kind": "k"}, "queue_pending": 1},
    ])
    b = FeatureBuilder(c)
    s = b.fit_transform(df)
    # hour=0, dow=4 (Friday)
    assert np.isclose(s.X["hour_sin"].iloc[0], 0.0)
    assert np.isclose(s.X["hour_cos"].iloc[0], 1.0)


def test_build_type_regex_extraction():
    c = _cfg(
        target="run_duration", target_column="y",
        categorical_features=["build_type"],
        numeric_features=[],
        derived_features={
            "build_type_regex": {"source": "metadata_name", "pattern": "/(debug|opt)[-/]"},
        },
    )
    df = _frame([
        {"task_id": "a", "run_id": 0, "pending_at": pd.Timestamp("2026-04-10 00:00", tz="UTC"),
         "reason_resolved": "completed", "y": 1.0,
         "metadata_name": "test-linux2404-64/debug-mochitest-1"},
        {"task_id": "b", "run_id": 0, "pending_at": pd.Timestamp("2026-04-10 00:00", tz="UTC"),
         "reason_resolved": "completed", "y": 1.0,
         "metadata_name": "test-linux2404-64/opt-mochitest-2"},
        {"task_id": "c", "run_id": 0, "pending_at": pd.Timestamp("2026-04-10 00:00", tz="UTC"),
         "reason_resolved": "completed", "y": 1.0,
         "metadata_name": "build-something"},
    ])
    b = FeatureBuilder(c)
    s = b.fit_transform(df)
    vals = list(s.X["build_type"].astype(object))
    assert vals[:2] == ["debug", "opt"]
    assert pd.isna(vals[2])


def test_extract_tags_downcasts_categorical_tag_columns_immediately():
    """tags.* columns are built object dtype by .apply() regardless (dict
    values aren't natively categorical); for a tag-heavy config (e.g.
    run_duration_residual's 9 tags.* fields) letting all of them sit object
    dtype through the rest of _derive()'s wide intermediate frame -- rather
    than downcasting the instant each is created -- is real, avoidable peak
    memory (2026-07-15, alongside the same fix for the raw columns loaded in
    data_loader.py)."""
    c = _cfg(categorical_features=["tags.kind"], numeric_features=[])
    b = FeatureBuilder(c)
    df = _frame([{"tags": {"kind": "build"}}, {"tags": {"kind": "test"}}])
    out = b._extract_tags(df)
    assert isinstance(out["tags.kind"].dtype, pd.CategoricalDtype)
    assert list(out["tags.kind"]) == ["build", "test"]


def test_extract_tags_leaves_numeric_tag_columns_uncast():
    c = _cfg(categorical_features=[], numeric_features=["tags.retries"])
    b = FeatureBuilder(c)
    df = _frame([{"tags": {"retries": "2"}}])
    out = b._extract_tags(df)
    assert not isinstance(out["tags.retries"].dtype, pd.CategoricalDtype)


def test_apply_derived_downcasts_build_type_when_categorical():
    c = _cfg(
        target="run_duration", categorical_features=["build_type"], numeric_features=[],
        derived_features={"build_type_regex": {"source": "metadata_name", "pattern": "/(debug|opt)[-/]"}},
    )
    b = FeatureBuilder(c)
    df = _frame([
        {"metadata_name": "test-linux2404-64/debug-mochitest-1"},
        {"metadata_name": "test-linux2404-64/opt-mochitest-2"},
    ])
    out = b._apply_derived(df)
    assert isinstance(out["build_type"].dtype, pd.CategoricalDtype)


def test_dump_categories_round_trip(tmp_path):
    c = _cfg()
    train = _frame([
        {"task_id": "a", "run_id": 0, "pending_at": pd.Timestamp("2026-04-10 01:00", tz="UTC"),
         "reason_resolved": "completed", "y": 5.0,
         "task_queue_id": "q1", "tags": {"kind": "build"}, "queue_pending": 10},
        {"task_id": "b", "run_id": 0, "pending_at": pd.Timestamp("2026-04-11 02:00", tz="UTC"),
         "reason_resolved": "completed", "y": 7.0,
         "task_queue_id": "q2", "tags": {"kind": "test"}, "queue_pending": 20},
        {"task_id": "c", "run_id": 0, "pending_at": pd.Timestamp("2026-04-12 03:00", tz="UTC"),
         "reason_resolved": "completed", "y": 3.0,
         "task_queue_id": "q1", "tags": {"kind": "build"}, "queue_pending": 5},
    ])
    b = FeatureBuilder(c)
    b.fit_transform(train)

    out_path = tmp_path / "cats.json"
    b.dump_categories(out_path)
    assert out_path.exists()

    loaded = json.loads(out_path.read_text())
    assert set(loaded.keys()) == {"task_queue_id", "tags.kind"}

    # Values must match the _categories index (sorted during fit_transform).
    assert loaded["task_queue_id"] == sorted(["q1", "q2"])
    assert loaded["tags.kind"] == sorted(["build", "test"])

    # Index positions must match pandas Categorical codes: category at index i
    # should have code i in the fitted FeatureBuilder.
    for col in ["task_queue_id", "tags.kind"]:
        cats_in_json = loaded[col]
        cats_in_builder = list(b._categories[col])
        assert cats_in_json == cats_in_builder, (
            f"{col}: JSON order {cats_in_json} != builder order {cats_in_builder}"
        )


def test_dump_categories_raises_before_fit(tmp_path):
    c = _cfg()
    b = FeatureBuilder(c)
    import pytest
    with pytest.raises(RuntimeError, match="fit_transform"):
        b.dump_categories(tmp_path / "cats.json")


def test_stats_record_unseen_rate():
    c = _cfg()
    train = _frame([
        {"task_id": "a", "run_id": 0, "pending_at": pd.Timestamp("2026-04-10 00:00", tz="UTC"),
         "reason_resolved": "completed", "y": 1.0,
         "task_queue_id": "q1", "tags": {"kind": "k"}, "queue_pending": 1},
    ])
    hold = _frame([
        {"task_id": "b", "run_id": 0, "pending_at": pd.Timestamp("2026-04-20 00:00", tz="UTC"),
         "reason_resolved": "completed", "y": 2.0,
         "task_queue_id": "q_unseen", "tags": {"kind": "k"}, "queue_pending": 2},
    ])
    b = FeatureBuilder(c)
    b.fit_transform(train)
    h = b.transform(hold)
    assert h.stats["unseen_rate"]["task_queue_id"] == 1.0
    assert h.stats["unseen_rate"]["tags.kind"] == 0.0
