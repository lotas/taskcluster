import pytest
from pathlib import Path

import numpy as np
import pandas as pd

from src.model import LightGBMQuantileModel


def _toy_data(n=500, seed=0):
    rng = np.random.default_rng(seed)
    X = pd.DataFrame({
        "q": pd.Categorical(rng.choice(["a", "b", "c"], size=n)),
        "x": rng.normal(size=n),
    })
    noise = rng.normal(size=n) * 0.1
    y = pd.Series(X["x"] * 2.0 + noise + (X["q"].cat.codes * 3.0))
    return X, y


@pytest.mark.parametrize("transform", ["log_ratio", "log_diff", "additive"])
def test_residual_model_roundtrip_all_transforms(tmp_path: Path, transform: str):
    from src.model import ResidualLightGBMQuantileModel
    rng = np.random.default_rng(7)
    n = 500
    X = pd.DataFrame({
        "q": pd.Categorical(rng.choice(["a", "b", "c"], size=n)),
        "x": rng.normal(size=n),
        "baseline": rng.uniform(10, 100, size=n),
    })
    factor = np.exp(rng.normal(0, 0.3, size=n))
    y = pd.Series(X["baseline"] * factor)
    split = int(n * 0.8)
    m = ResidualLightGBMQuantileModel(
        alpha=0.5,
        params={"num_leaves": 15, "n_estimators": 50, "min_data_in_leaf": 5, "early_stopping_rounds": 5, "learning_rate": 0.1},
        baseline_feature="baseline",
        transform=transform,
    )
    m.fit(X.iloc[:split], y.iloc[:split], X.iloc[split:], y.iloc[split:])
    preds = m.predict(X.iloc[split:])
    assert preds.shape == (n - split,)
    assert np.isfinite(preds).all()
    # Save/load roundtrip preserves predictions AND transform
    p = tmp_path / f"r_{transform}.lgb"
    m.save(p)
    m2 = ResidualLightGBMQuantileModel.load(p)
    assert m2.transform == transform
    preds2 = m2.predict(X.iloc[split:])
    assert np.allclose(preds, preds2)


def test_residual_model_unknown_transform_raises():
    from src.model import ResidualLightGBMQuantileModel
    with pytest.raises(ValueError, match="Unknown transform"):
        ResidualLightGBMQuantileModel(
            alpha=0.5, params={}, baseline_feature="baseline", transform="nonsense"
        )


def test_residual_model_default_transform_is_log_ratio():
    from src.model import ResidualLightGBMQuantileModel
    m = ResidualLightGBMQuantileModel(alpha=0.5, params={}, baseline_feature="b")
    assert m.transform == "log_ratio"


def test_residual_model_additive_transform_symmetry():
    """additive: y_t = y - bl, y_hat = raw + bl — should be a no-op if raw == y_t."""
    from src.model import ResidualLightGBMQuantileModel
    m = ResidualLightGBMQuantileModel(alpha=0.5, params={}, baseline_feature="b", transform="additive")
    y = pd.Series([10.0, 20.0, 30.0])
    bl = pd.Series([5.0, 15.0, 40.0])
    yt = m._to_transformed(y, bl)
    assert np.allclose(yt.to_numpy(), np.array([5.0, 5.0, -10.0]))
    reconstructed = m._inverse(yt.to_numpy(), bl)
    assert np.allclose(reconstructed, y.to_numpy())


def test_residual_model_log_diff_transform_symmetry():
    from src.model import ResidualLightGBMQuantileModel
    m = ResidualLightGBMQuantileModel(alpha=0.5, params={}, baseline_feature="b", transform="log_diff")
    y = pd.Series([10.0, 20.0, 0.0])
    bl = pd.Series([5.0, 15.0, 10.0])
    yt = m._to_transformed(y, bl)
    reconstructed = m._inverse(yt.to_numpy(), bl)
    assert np.allclose(reconstructed, y.to_numpy(), atol=1e-9)


def test_lightgbm_quantile_fit_predict_save_load(tmp_path: Path):
    X, y = _toy_data()
    split = int(len(X) * 0.8)
    Xt, yt = X.iloc[:split], y.iloc[:split]
    Xv, yv = X.iloc[split:], y.iloc[split:]

    m = LightGBMQuantileModel(alpha=0.5, params={
        "num_leaves": 15,
        "learning_rate": 0.1,
        "n_estimators": 50,
        "early_stopping_rounds": 5,
        "min_data_in_leaf": 5,
    })
    m.fit(Xt, yt, Xv, yv)
    preds = m.predict(Xv)
    assert len(preds) == len(Xv)
    assert np.isfinite(preds).all()

    p = tmp_path / "m.lgb"
    m.save(p)
    m2 = LightGBMQuantileModel.load(p)
    preds2 = m2.predict(Xv)
    assert np.allclose(preds, preds2)
