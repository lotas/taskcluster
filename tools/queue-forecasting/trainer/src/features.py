from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.config import Config


META_COLUMNS = ["pending_at", "reason_resolved", "task_id", "run_id"]


@dataclass
class Split:
    X: pd.DataFrame
    y: pd.Series
    meta: pd.DataFrame
    stats: dict = field(default_factory=dict)


class FeatureBuilder:
    """Fit categorical vocabulary on train, apply verbatim to val/holdout."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._categories: dict[str, pd.Index] = {}
        self._fitted = False

    def fit_transform(self, df: pd.DataFrame) -> Split:
        X, y, meta = self._derive(df)
        for col in self.config.categorical_features:
            series = X[col].astype("object")
            cats = pd.Index(sorted({v for v in series.dropna()}))
            self._categories[col] = cats
            X[col] = pd.Categorical(series, categories=cats)
        self._fitted = True
        stats = self._stats(X, unseen=None)
        return Split(X=X, y=y, meta=meta, stats=stats)

    def transform(self, df: pd.DataFrame) -> Split:
        if not self._fitted:
            raise RuntimeError("FeatureBuilder.transform called before fit_transform")
        X, y, meta = self._derive(df)
        unseen: dict[str, float] = {}
        for col in self.config.categorical_features:
            cats = self._categories[col]
            series = X[col].astype("object")
            unseen_mask = series.notna() & ~series.isin(cats)
            n_observed = int(series.notna().sum())
            unseen[col] = (int(unseen_mask.sum()) / n_observed) if n_observed else 0.0
            # Null out unseen values before constructing Categorical to avoid
            # Pandas 4 DeprecationWarning about values outside dtype's categories.
            X[col] = pd.Categorical(series.where(series.isin(cats)), categories=cats)
        stats = self._stats(X, unseen=unseen)
        return Split(X=X, y=y, meta=meta, stats=stats)

    def dump_categories(self, path: Path) -> None:
        """Write category vocabularies to a JSON file.

        Format: {"col_name": ["val0", "val1", ...], ...}
        Index position in each list is the integer code seen by the model.
        Unseen or null values at inference time should be encoded as -1
        (the cold_start_code).  Must be called after fit_transform().
        """
        if not self._fitted:
            raise RuntimeError("dump_categories called before fit_transform")
        out: dict[str, list] = {}
        for col, cats in self._categories.items():
            out[col] = [v if v is not None else None for v in cats.tolist()]
        Path(path).write_text(json.dumps(out, indent=2, default=str))

    # -- internals -----------------------------------------------------

    def _derive(self, df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
        df = df.copy()
        df = self._extract_tags(df)
        df = self._apply_derived(df)

        feature_cols = self.config.categorical_features + self.config.numeric_features
        X = df[feature_cols].copy()
        y = df["y"].astype(float) if "y" in df.columns else df[self.config.target_column].astype(float)
        meta = df[META_COLUMNS].copy()
        return X, y, meta

    def _extract_tags(self, df: pd.DataFrame) -> pd.DataFrame:
        tag_features = [c for c in self.config.categorical_features + self.config.numeric_features
                        if c.startswith("tags.")]
        if not tag_features or "tags" not in df.columns:
            return df
        def _get(tags, key):
            if tags is None:
                return None
            if isinstance(tags, dict):
                return tags.get(key)
            return None
        for feat in tag_features:
            key = feat.split(".", 1)[1]
            df[feat] = df["tags"].apply(lambda t, k=key: _get(t, k))
        return df

    def _apply_derived(self, df: pd.DataFrame) -> pd.DataFrame:
        derived = self.config.derived_features or {}
        if "cyclical_time" in derived:
            src = derived["cyclical_time"]["source"]
            hour = df[src].dt.hour
            dow = df[src].dt.dayofweek
            df["hour_sin"] = np.sin(2 * np.pi * hour / 24)
            df["hour_cos"] = np.cos(2 * np.pi * hour / 24)
            df["day_sin"]  = np.sin(2 * np.pi * dow / 7)
            df["day_cos"]  = np.cos(2 * np.pi * dow / 7)
        if "build_type_regex" in derived:
            spec = derived["build_type_regex"]
            src = spec["source"]
            pattern = spec["pattern"]
            df["build_type"] = df[src].astype("string").str.extract(pattern, expand=False)
        return df

    def _stats(self, X: pd.DataFrame, unseen: dict[str, float] | None) -> dict:
        s: dict[str, Any] = {
            "cardinalities": {
                c: int(X[c].cat.categories.size) for c in self.config.categorical_features
            },
            "null_rates": {
                c: float(X[c].isna().mean()) for c in X.columns
            },
        }
        if unseen is not None:
            s["unseen_rate"] = unseen
        return s
