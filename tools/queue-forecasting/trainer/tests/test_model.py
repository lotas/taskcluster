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


def _js_encode(X: pd.DataFrame, vocab: dict[str, list], feature_names: list[str]) -> np.ndarray:
    """Mimic the JS-side float32 tensor construction:
    categoricals → integer codes (vocab index, -1 for unseen/null), numerics → float32.
    """
    rows = np.zeros((len(X), len(feature_names)), dtype=np.float32)
    for col_i, col in enumerate(feature_names):
        if col in vocab:
            cats = vocab[col]
            idx_map = {v: i for i, v in enumerate(cats)}
            vals = X[col].astype(object)
            codes = np.array([idx_map.get(v, -1) if v is not None and not (isinstance(v, float) and np.isnan(v)) else -1
                              for v in vals], dtype=np.float32)
            rows[:, col_i] = codes
        else:
            rows[:, col_i] = X[col].astype(float).fillna(0.0).to_numpy(dtype=np.float32)
    return rows


def _make_small_model(tmp_path, alpha=0.5, with_baseline=False):
    """Fit a minimal LightGBMQuantileModel and return (model, X_test, vocab)."""
    rng = np.random.default_rng(42)
    n = 200
    vocab_vals = ["a", "b", "c", "d"]
    cols: dict = {
        "cat1": pd.Categorical(rng.choice(vocab_vals, n)),
        "num1": rng.normal(size=n).astype(np.float32),
        "num2": rng.uniform(1, 10, size=n).astype(np.float32),
    }
    if with_baseline:
        cols["baseline"] = rng.uniform(10, 100, size=n).astype(np.float32)
    X = pd.DataFrame(cols)
    y = pd.Series(X["num1"] * 2.0 + X["cat1"].cat.codes * 3.0 + rng.normal(size=n) * 0.1)
    split = int(n * 0.8)
    m = LightGBMQuantileModel(alpha=alpha, params={
        "num_leaves": 7, "n_estimators": 20, "min_data_in_leaf": 2,
        "early_stopping_rounds": 5, "learning_rate": 0.1,
    })
    m.fit(X.iloc[:split], y.iloc[:split], X.iloc[split:], y.iloc[split:])
    vocab = {"cat1": vocab_vals}
    return m, X.iloc[split:].reset_index(drop=True), vocab


def test_onnx_round_trip_plain(tmp_path: Path):
    """ONNX output must match booster.predict() within 1e-4 on the same rows."""
    import onnxruntime as rt

    m, X_test, vocab = _make_small_model(tmp_path)
    onnx_path = tmp_path / "m_p50.onnx"
    m.save_onnx(onnx_path)

    # Reference: pandas-DataFrame path (the path LightGBM uses internally).
    y_ref = m.predict(X_test)

    # JS-equivalent: float32 tensor with categorical codes.
    x_f32 = _js_encode(X_test, vocab, m.feature_names_)
    sess = rt.InferenceSession(str(onnx_path))
    out = sess.run(None, {sess.get_inputs()[0].name: x_f32})[0].flatten()

    assert out.shape == y_ref.shape
    assert np.max(np.abs(out - y_ref)) < 1e-4, (
        f"max diff {np.max(np.abs(out - y_ref)):.6f} exceeds 1e-4"
    )


def test_onnx_round_trip_unseen_category(tmp_path: Path):
    """Unseen category → code -1 in both reference and JS paths; outputs must agree."""
    import onnxruntime as rt

    m, X_test, vocab = _make_small_model(tmp_path)
    onnx_path = tmp_path / "m_p50_unseen.onnx"
    m.save_onnx(onnx_path)

    # Inject an unseen value: convert to object dtype first (Categorical rejects
    # new values directly), then assign.
    X_unseen = X_test.copy()
    X_unseen["cat1"] = X_unseen["cat1"].astype(object)
    X_unseen.loc[X_unseen.index[0], "cat1"] = "UNSEEN_VALUE"

    # Reference path: replace unseen with NaN using the training vocab Categorical.
    X_ref = X_unseen.copy()
    X_ref["cat1"] = pd.Categorical(
        X_ref["cat1"].where(X_ref["cat1"].isin(vocab["cat1"])),
        categories=vocab["cat1"],
    )
    y_ref = m.predict(X_ref)

    # JS-equivalent: unseen maps to -1.
    x_f32 = _js_encode(X_unseen, vocab, m.feature_names_)
    sess = rt.InferenceSession(str(onnx_path))
    out = sess.run(None, {sess.get_inputs()[0].name: x_f32})[0].flatten()

    assert np.max(np.abs(out - y_ref)) < 1e-4, (
        f"unseen-category: max diff {np.max(np.abs(out - y_ref)):.6f} exceeds 1e-4"
    )


def test_onnx_round_trip_residual(tmp_path: Path):
    """Residual model: raw ONNX output matches transformed-space booster.predict();
    JS-side inverse matches model.predict() (which applies the inverse internally)."""
    import onnxruntime as rt
    from src.model import ResidualLightGBMQuantileModel

    rng = np.random.default_rng(7)
    n = 200
    vocab_vals = ["a", "b", "c"]
    X = pd.DataFrame({
        "cat1":     pd.Categorical(rng.choice(vocab_vals, n)),
        "num1":     rng.normal(size=n).astype(np.float32),
        "baseline": rng.uniform(10, 100, size=n).astype(np.float32),
    })
    factor = np.exp(rng.normal(0, 0.3, size=n))
    y = pd.Series(X["baseline"].to_numpy() * factor)
    split = int(n * 0.8)

    m = ResidualLightGBMQuantileModel(
        alpha=0.5,
        params={"num_leaves": 7, "n_estimators": 20, "min_data_in_leaf": 2,
                "early_stopping_rounds": 5, "learning_rate": 0.1},
        baseline_feature="baseline",
        transform="log_ratio",
    )
    m.fit(X.iloc[:split], y.iloc[:split], X.iloc[split:], y.iloc[split:])

    onnx_path = tmp_path / "res_p50.onnx"
    m.save_onnx(onnx_path)

    X_test = X.iloc[split:].reset_index(drop=True)
    vocab = {"cat1": vocab_vals}

    # Reference raw (transformed-space) predictions from booster directly.
    y_raw_ref = m.booster.predict(X_test)

    # JS-equivalent float32 tensor.
    x_f32 = _js_encode(X_test, vocab, m.feature_names_)
    sess = rt.InferenceSession(str(onnx_path))
    raw_onnx = sess.run(None, {sess.get_inputs()[0].name: x_f32})[0].flatten()

    # Raw ONNX must match booster.predict().
    assert np.max(np.abs(raw_onnx - y_raw_ref)) < 1e-4, (
        f"residual raw: max diff {np.max(np.abs(raw_onnx - y_raw_ref)):.6f}"
    )

    # JS-side inverse (log_ratio) must match model.predict() (which applies it internally).
    bl = X_test["baseline"].to_numpy()
    y_inv = np.exp(raw_onnx) * (bl + 1.0) - 1.0
    y_model = m.predict(X_test)
    assert np.max(np.abs(y_inv - y_model)) < 1e-4, (
        f"residual inverse: max diff {np.max(np.abs(y_inv - y_model)):.6f}"
    )


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
