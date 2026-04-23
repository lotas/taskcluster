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
