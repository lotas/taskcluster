from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd


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
