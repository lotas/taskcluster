import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from src import config as cfg
from src.features import FeatureBuilder
from src.train import _run_discrete_hazard_training


def _hazard_config(tmp_path, **overrides):
    base = dict(
        target="wait_time", target_column="wait_duration_s",
        lookback_days=10, holdout_days=2, validation_days=1,
        as_of_date=datetime(2026, 5, 1, tzinfo=timezone.utc),
        filters=[], categorical_features=["q"], numeric_features=["x"],
        derived_features={}, model_type="discrete_hazard",
        quantiles=[0.5, 0.9],
        model_params={"n_estimators": 20, "min_data_in_leaf": 10, "early_stopping_rounds": 5},
        hazard_bins_minutes=[0, 2, 5, 15, 40, math.inf],
        source_path=tmp_path / "fake_hazard_config.yaml",
    )
    base.update(overrides)
    return cfg.Config(**base)


def _synthetic_rows(rng, start, end, n):
    pending_at = pd.Series(pd.to_datetime(
        rng.uniform(start.timestamp(), end.timestamp() - 3600, n), unit="s", utc=True))
    wait_s = np.where(
        rng.random(n) < 0.15,
        rng.exponential(scale=15000.0, size=n),
        rng.exponential(scale=300.0, size=n),
    )
    resolved_at = pending_at + pd.to_timedelta(wait_s + rng.exponential(200, n), unit="s")
    return pd.DataFrame({
        "pending_at": pending_at, "resolved_at": resolved_at, "y": wait_s,
        "reason_resolved": "completed",
        "task_id": [f"t{i}" for i in range(n)], "run_id": 0,
        "q": rng.choice(["q1", "q2"], n), "x": rng.normal(size=n),
    })


def test_run_discrete_hazard_training_end_to_end(tmp_path, monkeypatch):
    import src.train as train_module
    monkeypatch.setattr(train_module, "MODELS_DIR", tmp_path / "models")

    c = _hazard_config(tmp_path)
    w = cfg.compute_windows(c)
    rng = np.random.default_rng(3)

    train_df = _synthetic_rows(rng, w.train_start, w.train_end, 1500)
    val_df   = _synthetic_rows(rng, w.val_start,   w.val_end,   1500)
    hold_df  = _synthetic_rows(rng, w.hold_start,  w.hold_end,  1500)

    builder = FeatureBuilder(c)
    train = builder.fit_transform(train_df)
    val   = builder.transform(val_df)
    hold  = builder.transform(hold_df)

    holdout_day_keys = [d.strftime("%Y-%m-%d") for d in cfg.holdout_day_starts(c)]
    baseline_dir = tmp_path / "nonexistent_baseline_dir"

    manifest = _run_discrete_hazard_training(
        c, w, holdout_day_keys, baseline_dir,
        train, val, hold, len(train_df), len(val_df), len(hold_df),
    )

    assert manifest["model_type"] == "discrete_hazard"
    assert manifest["hazard_bins_minutes"] == [0, 2, 5, 15, 40, None]
    assert manifest["tail_rate"] is not None and manifest["tail_rate"] >= 0.0

    agg = manifest["evaluation"]["primary"]["aggregate"]
    assert agg["mae"]["eligible_n"] == 1500

    buckets = manifest["evaluation"]["primary"]["buckets_aggregate"]
    assert "30m+" in buckets
    assert "p90_miss_rate" in buckets["30m+"]

    import json
    json.dumps(manifest, default=str)

    run_dir = tmp_path / "models" / c.as_of_date.strftime("%Y-%m-%d")
    assert (run_dir / f"{c.source_path.stem}_manifest.json").exists()
    assert (run_dir / manifest["model_artifact_dir"] / "meta.json").exists()
    assert (run_dir / manifest["model_artifact_dir"] / "bin_0.lgb").exists()


def test_run_discrete_hazard_training_uses_default_bins_when_unset(tmp_path, monkeypatch):
    import src.train as train_module
    monkeypatch.setattr(train_module, "MODELS_DIR", tmp_path / "models")
    from src.hazard_labels import DEFAULT_BIN_EDGES_MINUTES

    c = _hazard_config(tmp_path, hazard_bins_minutes=None)
    w = cfg.compute_windows(c)
    rng = np.random.default_rng(5)

    train_df = _synthetic_rows(rng, w.train_start, w.train_end, 2000)
    val_df   = _synthetic_rows(rng, w.val_start,   w.val_end,   2000)
    hold_df  = _synthetic_rows(rng, w.hold_start,  w.hold_end,  2000)

    builder = FeatureBuilder(c)
    train = builder.fit_transform(train_df)
    val   = builder.transform(val_df)
    hold  = builder.transform(hold_df)

    holdout_day_keys = [d.strftime("%Y-%m-%d") for d in cfg.holdout_day_starts(c)]
    manifest = _run_discrete_hazard_training(
        c, w, holdout_day_keys, tmp_path / "nonexistent_baseline_dir",
        train, val, hold, len(train_df), len(val_df), len(hold_df),
    )
    expected = [e if e != float("inf") else None for e in DEFAULT_BIN_EDGES_MINUTES]
    assert manifest["hazard_bins_minutes"] == expected


def test_run_discrete_hazard_training_uses_per_split_cutoffs_not_as_of_date(tmp_path, monkeypatch):
    """The exact invariant two rounds of design review were about: fit()
    must receive each split's OWN boundary (w.train_end / w.val_end), not
    the global c.as_of_date -- using as_of_date would leak future
    information into train/val labels relative to what a model retrained
    at that split's own boundary would have seen."""
    import src.train as train_module
    from src.hazard_model import DiscreteHazardModel
    monkeypatch.setattr(train_module, "MODELS_DIR", tmp_path / "models")

    c = _hazard_config(tmp_path)
    w = cfg.compute_windows(c)
    rng = np.random.default_rng(3)

    train_df = _synthetic_rows(rng, w.train_start, w.train_end, 1500)
    val_df   = _synthetic_rows(rng, w.val_start,   w.val_end,   1500)
    hold_df  = _synthetic_rows(rng, w.hold_start,  w.hold_end,  1500)

    builder = FeatureBuilder(c)
    train = builder.fit_transform(train_df)
    val   = builder.transform(val_df)
    hold  = builder.transform(hold_df)

    captured = {}
    real_fit = DiscreteHazardModel.fit
    def spy(self, Xt, mt, yt, ct, Xv, mv, yv, cv):
        captured["train_cutoff"] = ct
        captured["val_cutoff"] = cv
        return real_fit(self, Xt, mt, yt, ct, Xv, mv, yv, cv)
    monkeypatch.setattr(DiscreteHazardModel, "fit", spy)

    holdout_day_keys = [d.strftime("%Y-%m-%d") for d in cfg.holdout_day_starts(c)]
    _run_discrete_hazard_training(
        c, w, holdout_day_keys, tmp_path / "nonexistent_baseline_dir",
        train, val, hold, len(train_df), len(val_df), len(hold_df),
    )

    assert captured["train_cutoff"] == w.train_end
    assert captured["val_cutoff"] == w.val_end
    assert captured["train_cutoff"] != c.as_of_date
    assert captured["val_cutoff"] != c.as_of_date


def test_run_discrete_hazard_training_handles_censored_holdout_rows(tmp_path, monkeypatch):
    """Holdout rows that are still pending (NaN wait_duration_s, NULL
    reason_resolved) must be excluded from primary-slice metrics rather
    than corrupting aggregates -- this is exactly the row type the old
    survivorship-biased quantile configs silently dropped at the SQL
    filter level, which this hazard config deliberately no longer does."""
    import src.train as train_module
    monkeypatch.setattr(train_module, "MODELS_DIR", tmp_path / "models")

    c = _hazard_config(tmp_path)
    w = cfg.compute_windows(c)
    rng = np.random.default_rng(9)

    train_df = _synthetic_rows(rng, w.train_start, w.train_end, 1500)
    val_df   = _synthetic_rows(rng, w.val_start,   w.val_end,   1500)
    hold_df  = _synthetic_rows(rng, w.hold_start,  w.hold_end,  1500)

    n_censored = 200
    censored_idx = hold_df.index[:n_censored]
    hold_df.loc[censored_idx, "y"] = np.nan
    hold_df.loc[censored_idx, "resolved_at"] = pd.NaT
    hold_df.loc[censored_idx, "reason_resolved"] = None

    builder = FeatureBuilder(c)
    train = builder.fit_transform(train_df)
    val   = builder.transform(val_df)
    hold  = builder.transform(hold_df)

    holdout_day_keys = [d.strftime("%Y-%m-%d") for d in cfg.holdout_day_starts(c)]
    manifest = _run_discrete_hazard_training(
        c, w, holdout_day_keys, tmp_path / "nonexistent_baseline_dir",
        train, val, hold, len(train_df), len(val_df), len(hold_df),
    )

    agg = manifest["evaluation"]["primary"]["aggregate"]
    assert agg["mae"]["eligible_n"] == len(hold_df) - n_censored
    assert agg["mae_s"] == agg["mae_s"]  # not NaN
    buckets = manifest["evaluation"]["primary"]["buckets_aggregate"]
    assert buckets["30m+"]["p90_miss_rate"] == buckets["30m+"]["p90_miss_rate"]  # not NaN
