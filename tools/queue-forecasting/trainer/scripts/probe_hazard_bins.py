"""Diagnose why a discrete-hazard bin's booster stops improving immediately.

Loads the cohort ONCE (~40min, dominated by queue-context feature compute --
see data_loader.load, whose parquet cache covers only the raw SQL fetch) and
then refits selected bins across a matrix of variants in memory. One load
buys a dozen configurations instead of one.

The decisive variant is `random_split`: it early-stops on a random 10% of the
bin's OWN training risk set instead of the temporal validation split. If a
bin fits deeply against a random split but stops at 1-2 trees against the
temporal one, the problem is train/val distribution mismatch (the validation
window is a different regime), not capacity and not absent signal.

Usage:
  docker compose run --rm --entrypoint uv trainer run python -m scripts.probe_hazard_bins \
      --config configs/wait_hazard_qctx_d_priority_flow.yaml \
      --as-of-date 2026-07-18 --bins 3,4,5,6
"""
from __future__ import annotations

import argparse

import lightgbm as lgb
import numpy as np

from src import config as cfg
from src import data_loader
from src.features import FeatureBuilder
from src.hazard_labels import DEFAULT_BIN_EDGES_MINUTES, build_bin_risk_and_labels
from src.train import _split_by_pending_at

# Each variant is a (name, param-overrides, use_random_split) triple. Params
# layer on top of the config's own model_params.
VARIANTS: list[tuple[str, dict, bool]] = [
    ("baseline",         {},                                              False),
    ("random_split",     {},                                              True),
    ("low_capacity",     {"num_leaves": 15, "min_data_in_leaf": 200},     False),
    ("tame_categorical", {"cat_smooth": 200, "min_data_per_group": 500},  False),
    ("both",             {"num_leaves": 15, "min_data_in_leaf": 200,
                          "cat_smooth": 200, "min_data_per_group": 500},  False),
]


def _fit(X_tr, y_tr, X_va, y_va, params: dict, n_estimators: int, patience: int):
    lgb_params = {
        "objective": "binary",
        "metric": ["binary_logloss", "auc"],
        "verbosity": -1,
        **params,
    }
    train_set = lgb.Dataset(X_tr, label=y_tr, categorical_feature="auto", free_raw_data=False)
    val_set = lgb.Dataset(X_va, label=y_va, categorical_feature="auto",
                          reference=train_set, free_raw_data=False)
    booster = lgb.train(
        lgb_params, train_set, num_boost_round=n_estimators,
        valid_sets=[val_set], valid_names=["val"],
        callbacks=[lgb.early_stopping(patience, verbose=False)],
    )
    return booster


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--as-of-date", default=None)
    p.add_argument("--bins", default="3,4,5,6", help="comma-separated bin indices to probe")
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args(argv)

    c = cfg.load_config(args.config, as_of_date_override=args.as_of_date)
    w = cfg.compute_windows(c)
    target_bins = [int(x) for x in args.bins.split(",")]

    print(f"Loading {c.target} (as_of={c.as_of_date.date()}) -- this is the slow part", flush=True)
    worker_pools = data_loader.load_worker_pools() if (
        c.velocity_features and c.velocity_features.get("enabled")) else None
    df = data_loader.load(c, worker_pools=worker_pools)
    train_df, val_df, _hold_df = _split_by_pending_at(df, c)
    del df
    print(f"  train={len(train_df):,}  val={len(val_df):,}", flush=True)

    builder = FeatureBuilder(c)
    train = builder.fit_transform(train_df)
    val = builder.transform(val_df)
    del train_df, val_df

    edges = c.hazard_bins_minutes or DEFAULT_BIN_EDGES_MINUTES
    at_risk_tr, label_tr = build_bin_risk_and_labels(
        train.meta["pending_at"], train.meta["resolved_at"], train.y, w.train_end, edges)
    at_risk_va, label_va = build_bin_risk_and_labels(
        val.meta["pending_at"], val.meta["resolved_at"], val.y, w.val_end, edges)

    base_params = dict(c.model_params)
    n_estimators = int(base_params.pop("n_estimators", 500))
    patience = int(base_params.pop("early_stopping_rounds", 20))
    base_params.pop("min_val_rows_for_early_stop", None)
    rng = np.random.default_rng(args.seed)

    print(f"\n{'bin':>4} {'variant':<18} {'n_tr':>8} {'n_va':>7} {'best_it':>8} {'val_auc':>8} {'val_loss':>9}")
    print("-" * 68)
    for i in target_bins:
        mtr, mva = at_risk_tr[:, i], at_risk_va[:, i]
        X_tr_all, y_tr_all = train.X[mtr], label_tr[mtr, i]
        X_va_tmp, y_va_tmp = val.X[mva], label_va[mva, i]

        for name, overrides, random_split in VARIANTS:
            if random_split:
                # Hold out a random 10% of THIS bin's own training risk set.
                n = len(X_tr_all)
                idx = rng.permutation(n)
                cut = max(1, n // 10)
                va_idx, tr_idx = idx[:cut], idx[cut:]
                X_tr, y_tr = X_tr_all.iloc[tr_idx], y_tr_all[tr_idx]
                X_va, y_va = X_tr_all.iloc[va_idx], y_tr_all[va_idx]
            else:
                X_tr, y_tr, X_va, y_va = X_tr_all, y_tr_all, X_va_tmp, y_va_tmp

            if len(X_va) < 50 or len(np.unique(y_va)) < 2:
                print(f"{i:>4} {name:<18} {len(X_tr):>8,} {len(X_va):>7,}  (skipped: degenerate val)")
                continue

            b = _fit(X_tr, y_tr, X_va, y_va, {**base_params, **overrides}, n_estimators, patience)
            scores = b.best_score.get("val", {})
            print(f"{i:>4} {name:<18} {len(X_tr):>8,} {len(X_va):>7,} {b.best_iteration:>8} "
                  f"{scores.get('auc', float('nan')):>8.4f} {scores.get('binary_logloss', float('nan')):>9.4f}",
                  flush=True)
        print("-" * 68)

    print("\nReading the table:")
    print("  random_split deep but baseline shallow -> train/val DISTRIBUTION MISMATCH")
    print("  tame_categorical/low_capacity deep      -> OVERFITTING, fixable via model_params")
    print("  all variants shallow AND val_auc ~0.5   -> no signal in these features for this bin")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
