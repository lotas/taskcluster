from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
import onnx  # noqa: F401 — imported here so ImportError surfaces early
import onnxmltools
from onnxconverter_common.data_types import FloatTensorType

# onnxmltools._parse_node recurses once per tree node. LightGBM leaf-wise growth
# with 63 max leaves can produce very unbalanced trees whose depth exceeds
# Python's default 1000-frame limit on 500-estimator models.
_ONNX_RECURSION_LIMIT = 50_000


class QuantileModel(ABC):
    @abstractmethod
    def fit(self, X_train: pd.DataFrame, y_train: pd.Series,
            X_val: pd.DataFrame,   y_val: pd.Series) -> None: ...

    @abstractmethod
    def predict(self, X: pd.DataFrame) -> np.ndarray: ...

    @abstractmethod
    def save(self, path: Path) -> None: ...

    @classmethod
    @abstractmethod
    def load(cls, path: Path) -> "QuantileModel": ...


class LightGBMQuantileModel(QuantileModel):
    def __init__(self, alpha: float, params: dict[str, Any]):
        self.alpha = alpha
        self.params = dict(params)
        self.booster: lgb.Booster | None = None
        self.feature_names_: list[str] = []

    def fit(self, X_train, y_train, X_val, y_val) -> None:
        n_estimators = int(self.params.get("n_estimators", 500))
        early_stop = int(self.params.get("early_stopping_rounds", 20))

        lgb_params = {
            "objective":    "quantile",
            "alpha":        self.alpha,
            "metric":       "quantile",
            "verbosity":    -1,
            "num_leaves":   int(self.params.get("num_leaves", 63)),
            "learning_rate": float(self.params.get("learning_rate", 0.05)),
            "min_data_in_leaf": int(self.params.get("min_data_in_leaf", 100)),
        }

        train_set = lgb.Dataset(X_train, label=y_train, categorical_feature="auto", free_raw_data=False)
        val_set   = lgb.Dataset(X_val,   label=y_val,   categorical_feature="auto", reference=train_set, free_raw_data=False)

        self.booster = lgb.train(
            lgb_params,
            train_set,
            num_boost_round=n_estimators,
            valid_sets=[val_set],
            valid_names=["val"],
            callbacks=[lgb.early_stopping(early_stop, verbose=False)],
        )
        # Lock in the exact training-frame column order for ONNX export.
        # Using X_train.columns rather than booster.feature_name() preserves
        # original casing/spacing and matches the JS input tensor exactly.
        self.feature_names_ = list(X_train.columns)

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        if self.booster is None:
            raise RuntimeError("model not fit")
        return self.booster.predict(X)

    def save(self, path: Path) -> None:
        if self.booster is None:
            raise RuntimeError("model not fit")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.booster.save_model(str(path))
        meta = path.with_suffix(path.suffix + ".meta")
        meta.write_text(f"alpha={self.alpha}\n")

    def save_onnx(self, path: Path) -> None:
        """Export the booster as an ONNX model with a single float32 input tensor.

        Categorical features are expected as their integer codes (float32-cast),
        with -1 for unseen/null values — matching the pandas Categorical convention
        used during training and the cold_start_code in the feature schema.

        For ResidualLightGBMQuantileModel this exports the raw transformed-space
        booster; the inverse transform is applied by the JS serving layer using
        the residual block in the feature schema.
        """
        if self.booster is None:
            raise RuntimeError("model not fit")
        if not self.feature_names_:
            raise RuntimeError("feature_names_ not set — call fit() before save_onnx()")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        n_features = len(self.feature_names_)
        initial_types = [("input", FloatTensorType([None, n_features]))]
        old_limit = sys.getrecursionlimit()
        try:
            sys.setrecursionlimit(max(old_limit, _ONNX_RECURSION_LIMIT))
            onnx_model = onnxmltools.convert_lightgbm(
                self.booster,
                initial_types=initial_types,
                target_opset=12,
            )
        finally:
            sys.setrecursionlimit(old_limit)
        with open(path, "wb") as fh:
            fh.write(onnx_model.SerializeToString())

    @classmethod
    def load(cls, path: Path) -> "LightGBMQuantileModel":
        path = Path(path)
        booster = lgb.Booster(model_file=str(path))
        meta = path.with_suffix(path.suffix + ".meta")
        alpha = 0.5
        if meta.exists():
            for line in meta.read_text().splitlines():
                if line.startswith("alpha="):
                    alpha = float(line.split("=", 1)[1])
        m = cls(alpha=alpha, params={})
        m.booster = booster
        return m


class ResidualLightGBMQuantileModel(LightGBMQuantileModel):
    """Residual model — predicts a transformed target relative to a baseline feature.

    Supported transforms:
      - log_ratio : y_t = log((y + 1) / (bl + 1));   y_hat = exp(raw) * (bl + 1) - 1
      - log_diff  : y_t = log1p(y) - log1p(bl);      y_hat = expm1(log1p(bl) + raw)
      - additive  : y_t = y - bl;                    y_hat = raw + bl
    """

    SUPPORTED_TRANSFORMS = ("log_ratio", "log_diff", "additive")

    def __init__(self, alpha: float, params: dict, baseline_feature: str, transform: str = "log_ratio"):
        super().__init__(alpha, params)
        if transform not in self.SUPPORTED_TRANSFORMS:
            raise ValueError(
                f"Unknown transform: {transform}. "
                f"Supported: {self.SUPPORTED_TRANSFORMS}"
            )
        self.baseline_feature = baseline_feature
        self.transform = transform

    def _clean_baseline(self, baseline: pd.Series) -> pd.Series:
        return baseline.fillna(0.0).clip(lower=0.0)

    def _to_transformed(self, y: pd.Series, baseline: pd.Series) -> pd.Series:
        bl = self._clean_baseline(baseline)
        y_f = y.astype(float)
        if self.transform == "log_ratio":
            return np.log((y_f + 1.0) / (bl + 1.0))
        if self.transform == "log_diff":
            return np.log1p(y_f) - np.log1p(bl)
        if self.transform == "additive":
            return y_f - bl
        raise AssertionError(f"unreachable: {self.transform}")

    def _inverse(self, raw: np.ndarray, baseline: pd.Series) -> np.ndarray:
        bl = self._clean_baseline(baseline).to_numpy()
        if self.transform == "log_ratio":
            return np.exp(raw) * (bl + 1.0) - 1.0
        if self.transform == "log_diff":
            return np.expm1(np.log1p(bl) + raw)
        if self.transform == "additive":
            return raw + bl
        raise AssertionError(f"unreachable: {self.transform}")

    def fit(self, X_train, y_train, X_val, y_val) -> None:
        bl_train = X_train[self.baseline_feature]
        bl_val   = X_val[self.baseline_feature]
        yt_train = self._to_transformed(y_train, bl_train)
        yt_val   = self._to_transformed(y_val,   bl_val)
        super().fit(X_train, yt_train, X_val, yt_val)

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        raw = super().predict(X)
        return self._inverse(raw, X[self.baseline_feature])

    def save(self, path: Path) -> None:
        super().save(path)
        # Append residual-specific fields to the meta sidecar.
        meta = path.with_suffix(path.suffix + ".meta")
        with meta.open("a") as fh:
            fh.write(f"baseline_feature={self.baseline_feature}\n")
            fh.write(f"transform={self.transform}\n")

    @classmethod
    def load(cls, path: Path) -> "ResidualLightGBMQuantileModel":
        base = LightGBMQuantileModel.load(path)
        meta = path.with_suffix(path.suffix + ".meta")
        baseline_feature = None
        transform = "log_ratio"  # default for pre-transform meta files
        if meta.exists():
            for line in meta.read_text().splitlines():
                if line.startswith("baseline_feature="):
                    baseline_feature = line.split("=", 1)[1]
                elif line.startswith("transform="):
                    transform = line.split("=", 1)[1]
        if baseline_feature is None:
            raise RuntimeError(f"Cannot load residual model without baseline_feature in {meta}")
        m = cls(alpha=base.alpha, params={}, baseline_feature=baseline_feature, transform=transform)
        m.booster = base.booster
        return m
