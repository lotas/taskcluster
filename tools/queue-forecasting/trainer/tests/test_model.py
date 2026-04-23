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


def test_residual_model_roundtrip(tmp_path: Path):
    from src.model import ResidualLightGBMQuantileModel
    rng = np.random.default_rng(7)
    n = 500
    X = pd.DataFrame({
        "q": pd.Categorical(rng.choice(["a", "b", "c"], size=n)),
        "x": rng.normal(size=n),
        "baseline": rng.uniform(10, 100, size=n),
    })
    # Actual = baseline * factor, factor ~ exp(N(0, 0.3))
    factor = np.exp(rng.normal(0, 0.3, size=n))
    y = pd.Series(X["baseline"] * factor)

    split = int(n * 0.8)
    m = ResidualLightGBMQuantileModel(
        alpha=0.5,
        params={"num_leaves": 15, "n_estimators": 50, "min_data_in_leaf": 5, "early_stopping_rounds": 5, "learning_rate": 0.1},
        baseline_feature="baseline",
    )
    m.fit(X.iloc[:split], y.iloc[:split], X.iloc[split:], y.iloc[split:])
    preds = m.predict(X.iloc[split:])
    assert preds.shape == (n - split,)
    assert np.isfinite(preds).all()
    # Because the data-generating factor is symmetric around 1, residual p50 should
    # roughly track baseline: mean ratio pred/baseline ~ 1 within broad band.
    ratio = preds / X.iloc[split:]["baseline"].to_numpy()
    assert 0.5 < ratio.mean() < 2.0

    # Save/load roundtrip preserves predictions
    p = tmp_path / "r.lgb"
    m.save(p)
    m2 = ResidualLightGBMQuantileModel.load(p)
    preds2 = m2.predict(X.iloc[split:])
    assert np.allclose(preds, preds2)


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
