"""Diagnose why a discrete-hazard bin's booster stops improving immediately.

Loads the cohort ONCE (~40min, dominated by queue-context feature compute --
see data_loader.load, whose parquet cache covers only the raw SQL fetch) and
then refits selected bins across a matrix of variants in memory. One load
buys a dozen configurations instead of one.

Compare variants by val AUC, not by tree count. Every variant except
`blocked_val` shares one validation set, so their AUCs are directly
comparable; `blocked_val` answers the separate question of whether the real
validation WINDOW is an unrepresentative regime.

Usage:
  docker compose run --rm --entrypoint uv trainer run python -m scripts.probe_hazard_bins \
      --config configs/wait_hazard_qctx_d_priority_flow.yaml \
      --as-of-date 2026-07-18 --bins 3,4,5,6
"""
from __future__ import annotations

import argparse

import lightgbm as lgb
import numpy as np
import pandas as pd

from src import config as cfg
from src import data_loader
from src.features import FeatureBuilder
from src.hazard_labels import DEFAULT_BIN_EDGES_MINUTES, build_bin_risk_and_labels
from src.train import _split_by_pending_at

CAT_TAME = {"cat_smooth": 200, "min_data_per_group": 500}

# (name, param-overrides, split, drop_cols). Params layer on top of the
# config's own model_params.
#
# split="temporal" uses the real validation window. split="blocked" instead
# holds out the LAST 2 DAYS of the training window, by pending_at.
#
# There is deliberately no random-row split here. An earlier version of this
# probe had one and it scored AUC 0.9997-1.0000 on every bin -- not signal,
# but leakage: CI submits tasks in large per-push batches, so rows sharing a
# queue and an instant have near-identical features AND near-identical fates.
# A random split scatters each batch across both sides and the model
# memorizes neighbours. Any split of this data must be temporal.
VARIANTS: list[tuple[str, dict, str, list[str]]] = [
    ("baseline",       {},                                          "temporal", []),
    ("blocked_val",    {},                                          "blocked",  []),
    ("leaves31",       {"num_leaves": 31, "min_data_in_leaf": 100},  "temporal", []),
    ("leaves15",       {"num_leaves": 15, "min_data_in_leaf": 200},  "temporal", []),
    ("leaves7",        {"num_leaves": 7,  "min_data_in_leaf": 500},  "temporal", []),
    ("cat_tame",       {**CAT_TAME},                                 "temporal", []),
    ("leaves15_cat",   {"num_leaves": 15, "min_data_in_leaf": 200, **CAT_TAME}, "temporal", []),
    ("leaves7_cat",    {"num_leaves": 7,  "min_data_in_leaf": 500, **CAT_TAME}, "temporal", []),
    ("no_queue_id",    {},                                          "temporal", ["task_queue_id"]),
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

    print(f"\n{'bin':>4} {'variant':<18} {'n_tr':>8} {'n_va':>7} {'best_it':>8} {'val_auc':>8} {'val_loss':>9}")
    print("-" * 68)
    for i in target_bins:
        mtr, mva = at_risk_tr[:, i], at_risk_va[:, i]
        X_tr_all, y_tr_all = train.X[mtr], label_tr[mtr, i]
        pending_tr = train.meta["pending_at"][mtr]
        X_va_tmp, y_va_tmp = val.X[mva], label_va[mva, i]

        # Blocked split: last 2 days of the training window, by pending_at.
        block_cut = w.train_end - pd.Timedelta(days=2)
        blk = (pending_tr >= block_cut).to_numpy()

        for name, overrides, split, drop_cols in VARIANTS:
            if split == "blocked":
                X_tr, y_tr = X_tr_all[~blk], y_tr_all[~blk]
                X_va, y_va = X_tr_all[blk], y_tr_all[blk]
            else:
                X_tr, y_tr, X_va, y_va = X_tr_all, y_tr_all, X_va_tmp, y_va_tmp
            if drop_cols:
                keep = [col for col in X_tr.columns if col not in drop_cols]
                X_tr, X_va = X_tr[keep], X_va[keep]

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
    print("  blocked_val >> baseline           -> the real val WINDOW is a different regime")
    print("  a capacity variant >> baseline    -> OVERFITTING; adopt it per-bin")
    print("  no_queue_id >> baseline           -> high-cardinality categorical is the culprit")
    print("  everything flat at auc ~0.5       -> no signal in these features for this bin")
    print("\nCompare AUC, not tree count. All rows except blocked_val share one val set.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
