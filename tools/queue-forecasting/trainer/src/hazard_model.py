"""DiscreteHazardModel: one binary LightGBM classifier per bin, composing
hazard_labels.py's risk-set/label construction into a trainable model whose
survival curve S(t) supports full multi-quantile read-off.
See bet2-hazard-survival-design.md.
"""
from __future__ import annotations

from typing import Any, Sequence

import lightgbm as lgb
import numpy as np
import pandas as pd

from src.hazard_labels import (
    DEFAULT_BIN_EDGES_MINUTES,
    bin_edges_seconds,
    build_bin_risk_and_labels,
    fit_exponential_tail_rate,
)


class DiscreteHazardModel:
    def __init__(self, edges_minutes: Sequence[float] = DEFAULT_BIN_EDGES_MINUTES, params: dict[str, Any] | None = None):
        self.edges_minutes = list(edges_minutes)
        self.params = dict(params or {})
        self.boosters: list[lgb.Booster] = []
        self.feature_names_: list[str] = []
        self.tail_rate_: float | None = None

    def fit(
        self,
        X_train: pd.DataFrame, meta_train: pd.DataFrame, y_train: pd.Series, cutoff_train: pd.Timestamp,
        X_val: pd.DataFrame,   meta_val: pd.DataFrame,   y_val: pd.Series,   cutoff_val: pd.Timestamp,
    ) -> None:
        edges_s = bin_edges_seconds(self.edges_minutes)
        n_bins = len(edges_s) - 1

        at_risk_tr, label_tr = build_bin_risk_and_labels(
            meta_train["pending_at"], meta_train["resolved_at"], y_train, cutoff_train, self.edges_minutes)
        at_risk_va, label_va = build_bin_risk_and_labels(
            meta_val["pending_at"], meta_val["resolved_at"], y_val, cutoff_val, self.edges_minutes)

        boosters = []
        for i in range(n_bins):
            mtr, mva = at_risk_tr[:, i], at_risk_va[:, i]
            if mtr.sum() == 0:
                raise RuntimeError(f"bin {i} has an empty training risk set -- widen the cohort or coarsen bins")
            if mva.sum() == 0:
                raise RuntimeError(f"bin {i} has an empty validation risk set -- widen the cohort or coarsen bins")
            boosters.append(self._fit_one_bin(X_train[mtr], label_tr[mtr, i], X_val[mva], label_va[mva, i]))
        self.boosters = boosters
        self.feature_names_ = list(X_train.columns)
        self.tail_rate_ = fit_exponential_tail_rate(
            meta_train["pending_at"], meta_train["resolved_at"], y_train, cutoff_train, self.edges_minutes)

    def _fit_one_bin(self, X_tr: pd.DataFrame, y_tr: np.ndarray, X_va: pd.DataFrame, y_va: np.ndarray) -> lgb.Booster:
        n_estimators = int(self.params.get("n_estimators", 500))
        early_stop = int(self.params.get("early_stopping_rounds", 20))
        lgb_params = {
            "objective": "binary",
            "metric": "binary_logloss",
            "verbosity": -1,
            "num_leaves": int(self.params.get("num_leaves", 63)),
            "learning_rate": float(self.params.get("learning_rate", 0.05)),
            "min_data_in_leaf": int(self.params.get("min_data_in_leaf", 100)),
        }
        train_set = lgb.Dataset(X_tr, label=y_tr, categorical_feature="auto", free_raw_data=False)
        val_set = lgb.Dataset(X_va, label=y_va, categorical_feature="auto", reference=train_set, free_raw_data=False)
        return lgb.train(
            lgb_params, train_set, num_boost_round=n_estimators,
            valid_sets=[val_set], valid_names=["val"],
            callbacks=[lgb.early_stopping(early_stop, verbose=False)],
        )

    def predict_hazard(self, X: pd.DataFrame) -> np.ndarray:
        """Per-bin conditional hazard P(event in bin i | survived to bin i's start), shape (n_rows, n_bins)."""
        if not self.boosters:
            raise RuntimeError("model not fit")
        return np.column_stack([b.predict(X) for b in self.boosters])

    def predict_survival_grid(self, X: pd.DataFrame) -> np.ndarray:
        """Survival at each FINITE bin boundary (excludes the terminal bin's
        own hazard -- continuous quantile read-off beyond the last finite
        edge uses the separate exponential tail_rate_ instead).
        Shape (n_rows, n_bins - 1): column i = S(edges_s[i+1])."""
        hazard = self.predict_hazard(X)
        n_bins = hazard.shape[1]
        return np.cumprod(1.0 - hazard[:, : n_bins - 1], axis=1)

    def predict_quantile(self, X: pd.DataFrame, q: float) -> np.ndarray:
        """Read quantile q (0 < q < 1) off the survival curve.

        Within the finite bin grid: constant-hazard-within-bin interpolation
        (equivalently, a locally exponential survival shape whose rate is
        derived from that bin's own predicted hazard) -- the same
        assumption used for the terminal-bin tail below, applied
        consistently to every bin rather than switching interpolation
        bases at the grid boundary.
        Beyond the last finite edge: the exponential tail
        S(t) = S(t_last) * exp(-tail_rate_ * (t - t_last)), using the
        globally-fit tail_rate_ (not the terminal bin's own per-row
        classifier output, which only describes the discrete "resolves
        eventually" outcome, not a continuous-time shape).
        If tail_rate_ is non-positive (degenerate fit -- no observed
        "started" events in the terminal bin's risk set), tail quantiles
        are undefined and returned as np.inf.
        """
        if not (0.0 < q < 1.0):
            raise ValueError(f"q must be in (0, 1), got {q!r}")
        edges_s = bin_edges_seconds(self.edges_minutes)
        n_bins = len(edges_s) - 1
        t_last = edges_s[n_bins - 1]
        boundary_t = edges_s[1:n_bins]  # finite boundaries, length n_bins - 1
        bin_widths = np.diff(edges_s[:n_bins])  # width of each finite bin, length n_bins - 1

        hazard = self.predict_hazard(X)
        finite_hazard = hazard[:, : n_bins - 1]  # (n, n_bins - 1)
        finite_survival = np.cumprod(1.0 - finite_hazard, axis=1)  # (n, n_bins - 1): S(edges_s[1..n_bins-1])
        n = finite_survival.shape[0]
        s_star = 1.0 - q

        result = np.full(n, np.nan, dtype=np.float64)
        resolved = np.zeros(n, dtype=bool)
        s_prev = np.ones(n, dtype=np.float64)
        t_prev = 0.0

        for i in range(n_bins - 1):
            s_next = finite_survival[:, i]
            t_next = boundary_t[i]
            width = bin_widths[i]
            mask = (~resolved) & (s_star >= s_next)
            if mask.any():
                s_prev_m = s_prev[mask]
                s_next_m = s_next[mask]
                ratio = np.divide(s_next_m, s_prev_m, out=np.zeros_like(s_next_m), where=s_prev_m > 0)
                lam = np.where(ratio > 0, -np.log(np.clip(ratio, 1e-300, None)) / width, np.inf)
                t_result = np.where(
                    np.isfinite(lam) & (lam > 0),
                    t_prev + np.log(np.clip(s_prev_m, 1e-300, None) / s_star) / np.where(lam > 0, lam, 1.0),
                    t_prev,
                )
                result[mask] = t_result
                resolved[mask] = True
            s_prev = s_next
            t_prev = t_next

        if (~resolved).any():
            s_t_last = s_prev[~resolved]
            tail_result = np.full(s_t_last.shape, np.inf, dtype=np.float64)
            if self.tail_rate_ and self.tail_rate_ > 0:
                valid = s_t_last > 0
                tail_result[valid] = t_last - np.log(s_star / s_t_last[valid]) / self.tail_rate_
            result[~resolved] = tail_result

        return result
