from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from src import config as cfg
from src import data_loader
from src.features import FeatureBuilder
from src.model import LightGBMQuantileModel
from src.evaluate import evaluate as do_eval


MODELS_DIR = Path(__file__).resolve().parent.parent / "data" / "models"
BASELINE_DIR = Path(__file__).resolve().parent.parent / "data" / "baseline"


def _split_by_pending_at(df: pd.DataFrame, c: cfg.Config) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    w = cfg.compute_windows(c)
    pending = pd.to_datetime(df["pending_at"], utc=True)
    train = df[(pending >= w.train_start) & (pending < w.train_end)].reset_index(drop=True)
    val   = df[(pending >= w.val_start)   & (pending < w.val_end)].reset_index(drop=True)
    hold  = df[(pending >= w.hold_start)  & (pending < w.hold_end)].reset_index(drop=True)
    return train, val, hold


def _require_baselines(holdout_day_keys: list[str]) -> None:
    missing = [d for d in holdout_day_keys if not (BASELINE_DIR / f"{d}.json").exists()]
    if missing:
        lines = "\n".join(f"    {d}" for d in missing)
        raise SystemExit(
            f"ERROR: baseline JSONs missing for {len(missing)} holdout days:\n{lines}\n\n"
            f"Generate them first:\n"
            f"    ./scripts/run_training.sh <config>\n"
            f"or manually per day:\n"
            f"    docker compose run --rm predictor node src/predictor.js \\\n"
            f"      --pending-eval-date YYYY-MM-DD \\\n"
            f"      --output-json /app/tools/queue-forecasting/trainer/data/baseline/YYYY-MM-DD.json"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--refresh-cache", action="store_true")
    args = parser.parse_args(argv)

    c = cfg.load_config(args.config)
    w = cfg.compute_windows(c)
    holdout_day_keys = [d.strftime("%Y-%m-%d") for d in cfg.holdout_day_starts(c)]
    _require_baselines(holdout_day_keys)

    print(f"Loading data for {c.target} (as_of={c.as_of_date.isoformat()}, "
          f"train={w.train_start.date()}..{w.train_end.date()}, "
          f"val={w.val_start.date()}, "
          f"holdout={w.hold_start.date()}..{w.hold_end.date()})")
    df = data_loader.load(c, refresh_cache=args.refresh_cache)
    print(f"  rows loaded: {len(df):,}")

    train_df, val_df, hold_df = _split_by_pending_at(df, c)
    print(f"  train={len(train_df):,}  val={len(val_df):,}  hold={len(hold_df):,}")

    builder = FeatureBuilder(c)
    train = builder.fit_transform(train_df)
    val   = builder.transform(val_df)
    hold  = builder.transform(hold_df)

    # Train one model per quantile.
    models: dict[float, LightGBMQuantileModel] = {}
    for q in c.quantiles:
        print(f"  training quantile={q} …")
        m = LightGBMQuantileModel(alpha=q, params=c.model_params)
        m.fit(train.X, train.y, val.X, val.y)
        models[q] = m

    # Save models + manifest.
    run_dir = MODELS_DIR / c.as_of_date.strftime("%Y-%m-%d")
    run_dir.mkdir(parents=True, exist_ok=True)
    for q, m in models.items():
        tag = f"p{int(q * 100)}"
        m.save(run_dir / f"{c.target}_{tag}.lgb")

    # Evaluate. Target key matches what the baseline JSON uses:
    # baseline stores both "duration" and "wait" blocks; we pick ours.
    target_key = "duration" if c.target == "run_duration" else "wait"
    preds_p50 = models[0.5].predict(hold.X) if 0.5 in models else np.full(len(hold.y), np.nan)
    preds_p90 = models[0.9].predict(hold.X) if 0.9 in models else np.full(len(hold.y), np.nan)
    report = do_eval(
        preds_p50=preds_p50,
        preds_p90=preds_p90,
        hold_meta=hold.meta,
        y_true=hold.y.to_numpy(),
        holdout_day_keys=holdout_day_keys,
        baseline_dir=BASELINE_DIR,
        target=target_key,
    )

    manifest = {
        "target": c.target,
        "config_path": str(c.source_path),
        "config_hash": data_loader.cache_key(c),
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "model_type": c.model_type,
        "lightgbm_version": lgb.__version__,
        "windows": {
            "as_of_date":      c.as_of_date.isoformat(),
            "lookback_days":   c.lookback_days,
            "validation_days": c.validation_days,
            "holdout_days":    c.holdout_days,
            "train":   {"start": w.train_start.isoformat(), "end": w.train_end.isoformat(), "rows": int(len(train_df))},
            "val":     {"start": w.val_start.isoformat(),   "end": w.val_end.isoformat(),   "rows": int(len(val_df))},
            "holdout": {"start": w.hold_start.isoformat(),  "end": w.hold_end.isoformat(),  "rows": int(len(hold_df))},
        },
        "features": {
            "categorical":           c.categorical_features,
            "numeric":               c.numeric_features,
            "cardinalities":         train.stats.get("cardinalities", {}),
            "null_rates":            train.stats.get("null_rates", {}),
            "unseen_rates_holdout":  hold.stats.get("unseen_rate", {}),
        },
        "model_params": c.model_params,
        "quantiles": c.quantiles,
        "evaluation": {
            "primary": {
                "slice": "reason_resolved = 'completed'",
                "per_day": report.primary_per_day,
                "aggregate": report.primary_agg,
                "baseline_per_day": report.baseline_per_day,
                "baseline_aggregate": report.baseline_agg,
            },
            "supplemental": {
                "slice": "reason_resolved IN ('completed','failed')",
                "per_day": report.supplemental_per_day,
                "aggregate": report.supplemental_agg,
            },
        },
    }
    (run_dir / f"{c.target}_manifest.json").write_text(json.dumps(manifest, indent=2, default=str))

    # Console summary.
    print("\n=== Evaluation (primary: completed-only) ===")
    print(f"  LightGBM : MAE={report.primary_agg['mae_s']:.1f}s  within_2x={report.primary_agg['within_2x_rate']*100:.1f}%")
    print(f"  Baseline : MAE={report.baseline_agg['mae_s']:.1f}s  within_2x={report.baseline_agg['within_2x_rate']*100:.1f}%")
    if report.baseline_agg['mae_s'] > 0:
        print(f"  Delta MAE: {(report.primary_agg['mae_s'] - report.baseline_agg['mae_s']) / report.baseline_agg['mae_s'] * 100:+.1f}%")
    p90_cov_rate = report.primary_agg.get("p90_coverage_rate")
    if p90_cov_rate is not None:
        print(f"  p90 coverage: {p90_cov_rate * 100:.1f}% (target [85%, 95%])")
    print(f"\nModels + manifest in {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
