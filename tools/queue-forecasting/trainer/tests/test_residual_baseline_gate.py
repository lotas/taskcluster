"""`train._require_residual_baseline_values`: a residual run stops, with counts,
when any split has rows without a baseline value.

The gate replaces a silent `fillna(0.0)` (see `model.MissingBaselineError`).
It has to say WHICH DAYS, because "widen the baseline export to those days" is
the remedy, and it has to distinguish a row the NDJSON never had from a row it
carried with a null quantile, because those are two different export problems.
"""
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src import train
from src.features import Split


def _config(residual=True):
    return SimpleNamespace(
        residual={"baseline_feature": "bl_wait_p50", "transform": "log_ratio",
                  "baseline_file": "baseline_predictions.ndjson"} if residual else None,
        baseline_dir=None,
    )


def _split(days, bl_p50, bl_p90=None, extra=None):
    """One row per entry of `days`; `bl_p50`/`bl_p90` are per-row values."""
    n = len(days)
    X = pd.DataFrame({
        "x": np.arange(n, dtype=float),
        "bl_wait_p50": np.asarray(bl_p50, dtype=float),
        "bl_wait_p90": np.asarray(bl_p90 if bl_p90 is not None else bl_p50, dtype=float),
    })
    if extra:
        for k, v in extra.items():
            X[k] = v
    y = pd.Series(np.ones(n))
    meta = pd.DataFrame({"pending_at": pd.to_datetime([f"{d}T12:00:00Z" for d in days], utc=True)})
    return Split(X=X, y=y, meta=meta)


def test_clean_splits_pass_and_are_not_mutated():
    train_s = _split(["2026-08-12", "2026-08-13"], [1.0, 2.0])
    before = train_s.X.copy()
    train._require_residual_baseline_values(_config(), {"train": train_s})
    pd.testing.assert_frame_equal(train_s.X, before)


def test_a_non_residual_config_is_not_gated():
    s = _split(["2026-08-12"], [np.nan])
    train._require_residual_baseline_values(_config(residual=False), {"train": s})


def test_missing_values_stop_the_run_with_split_and_day_counts():
    # train: two rows on 08-12 (one unjoined: every bl_* NaN), one on 08-13 (a
    # null p50 with a real p90 -- the NDJSON HAD the row).
    train_s = _split(["2026-08-12", "2026-08-12", "2026-08-13"],
                     bl_p50=[np.nan, 4.0, np.nan], bl_p90=[np.nan, 6.0, 9.0])
    val_s = _split(["2026-08-26"], [3.0])
    hold_s = _split(["2026-08-27", "2026-08-27", "2026-08-28"],
                    bl_p50=[np.nan, np.nan, 2.0], bl_p90=[np.nan, np.nan, 3.0])
    with pytest.raises(SystemExit) as caught:
        train._require_residual_baseline_values(
            _config(), {"train": train_s, "val": val_s, "hold": hold_s})
    text = str(caught.value)
    assert "4 of 7 rows" in text                       # across the splits
    assert "'bl_wait_p50'" in text
    assert "train: 2 of 3 rows missing (1 with no baseline row at all, 1 with a null bl_wait_p50)" in text
    assert "2026-08-12: 1 of 2 rows" in text
    assert "2026-08-13: 1 of 1 rows" in text
    assert "hold: 2 of 3 rows missing (2 with no baseline row at all, 0 with a null bl_wait_p50)" in text
    assert "2026-08-27: 2 of 2 rows" in text
    assert "val:" not in text                          # a clean split is not listed
    # The two forbidden remedies are named as forbidden, and the real one given.
    assert "0.0 would redefine the target" in text
    assert "cannot be dropped" in text
    assert "widen the export" in text


def test_a_missing_feature_column_is_its_own_error():
    s = _split(["2026-08-12"], [1.0])
    s.X = s.X.drop(columns=["bl_wait_p50"])
    with pytest.raises(SystemExit, match="is not a column of the train features"):
        train._require_residual_baseline_values(_config(), {"train": s})


def test_the_gate_runs_before_any_model_is_built_in_train_main():
    """`main` calls the gate right before `_make_model`; pin the order by
    reading the source rather than running a full training job."""
    import inspect
    src = inspect.getsource(train.main)
    gate = src.index("_require_residual_baseline_values(c, {")
    make = src.index("def _make_model(")
    fit = src.index("model.fit(") if "model.fit(" in src else len(src)
    assert gate < make < fit
