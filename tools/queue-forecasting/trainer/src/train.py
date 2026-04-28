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
from src.model import LightGBMQuantileModel, ResidualLightGBMQuantileModel
from src.evaluate import evaluate as do_eval, load_prior_manifest


MODELS_DIR = Path(__file__).resolve().parent.parent / "data" / "models"
TRAINER_ROOT = Path(__file__).resolve().parent.parent


def _strip_data_prefix(rel: str) -> str:
    """Strip a leading "data/" prefix; preserves relative-to-trainer-root semantics
    while letting configs spell out "data/baseline_filtered" naturally."""
    if rel.startswith("data/"):
        return rel[len("data/"):]
    return rel


def _baseline_dir(c: cfg.Config) -> Path:
    """Resolve the baseline-cache directory for a config.

    Defaults to <trainer_root>/data/baseline. A config-supplied baseline_dir
    is interpreted relative to the trainer root; a leading "data/" prefix is
    stripped so the on-disk layout mirrors the existing default.
    """
    if c.baseline_dir:
        return TRAINER_ROOT / "data" / _strip_data_prefix(c.baseline_dir)
    return TRAINER_ROOT / "data" / "baseline"


def _split_by_pending_at(df: pd.DataFrame, c: cfg.Config) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    w = cfg.compute_windows(c)
    pending = pd.to_datetime(df["pending_at"], utc=True)
    train = df[(pending >= w.train_start) & (pending < w.train_end)].reset_index(drop=True)
    val   = df[(pending >= w.val_start)   & (pending < w.val_end)].reset_index(drop=True)
    hold  = df[(pending >= w.hold_start)  & (pending < w.hold_end)].reset_index(drop=True)
    return train, val, hold


def _require_baselines(holdout_day_keys: list[str], baseline_dir: Path) -> None:
    missing = [d for d in holdout_day_keys if not (baseline_dir / f"{d}.json").exists()]
    if missing:
        lines = "\n".join(f"    {d}" for d in missing)
        raise SystemExit(
            f"ERROR: baseline JSONs missing in {baseline_dir} for {len(missing)} holdout days:\n{lines}\n\n"
            f"Generate them first:\n"
            f"    ./scripts/run_training.sh <config>\n"
            f"or manually per day:\n"
            f"    docker compose run --rm predictor node src/predictor.js \\\n"
            f"      --pending-eval-date YYYY-MM-DD \\\n"
            f"      --output-json /app/tools/queue-forecasting/{baseline_dir.relative_to(TRAINER_ROOT.parent)}/YYYY-MM-DD.json"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--refresh-cache", action="store_true")
    parser.add_argument("--as-of-date", default=None,
                        help="Override as_of_date from config (UTC midnight of given day)")
    args = parser.parse_args(argv)

    c = cfg.load_config(args.config, as_of_date_override=args.as_of_date)
    w = cfg.compute_windows(c)
    baseline_dir = _baseline_dir(c)
    holdout_day_keys = [d.strftime("%Y-%m-%d") for d in cfg.holdout_day_starts(c)]
    _require_baselines(holdout_day_keys, baseline_dir)

    if c.residual:
        print(f"  residual mode: baseline_feature={c.residual['baseline_feature']} "
              f"transform={c.residual.get('transform', 'log_ratio')}")
    print(f"Loading data for {c.target} (as_of={c.as_of_date.isoformat()}, "
          f"train={w.train_start.date()}..{w.train_end.date()}, "
          f"val={w.val_start.date()}, "
          f"holdout={w.hold_start.date()}..{w.hold_end.date()})")
    df = data_loader.load(c, refresh_cache=args.refresh_cache)
    print(f"  rows loaded: {len(df):,}")

    train_df, val_df, hold_df = _split_by_pending_at(df, c)
    print(f"  train={len(train_df):,}  val={len(val_df):,}  hold={len(hold_df):,}")

    if c.anomaly_filter and c.anomaly_filter.get("enabled"):
        mode = c.anomaly_filter.get("mode", "training")
        if mode in ("training", "both"):
            anomalous_dates = data_loader.load_anomalous_dates(c)
            if anomalous_dates:
                n_train_before = len(train_df)
                n_val_before   = len(val_df)
                train_df = train_df[~train_df["pending_at"].dt.tz_convert("UTC").dt.date.isin(anomalous_dates)].reset_index(drop=True)
                val_df   = val_df  [~val_df  ["pending_at"].dt.tz_convert("UTC").dt.date.isin(anomalous_dates)].reset_index(drop=True)
                print(f"  anomaly filter ({mode}): train {n_train_before:,}→{len(train_df):,}, "
                      f"val {n_val_before:,}→{len(val_df):,} ({len(anomalous_dates)} anomalous days)")
            # Holdout is never filtered. Will be sliced for reporting in Stage 2.

    builder = FeatureBuilder(c)
    train = builder.fit_transform(train_df)
    val   = builder.transform(val_df)
    hold  = builder.transform(hold_df)

    # Train one model per quantile.
    def _make_model(alpha: float) -> LightGBMQuantileModel:
        if c.residual:
            return ResidualLightGBMQuantileModel(
                alpha=alpha,
                params=c.model_params,
                baseline_feature=c.residual["baseline_feature"],
                transform=c.residual.get("transform", "log_ratio"),
            )
        return LightGBMQuantileModel(alpha=alpha, params=c.model_params)

    models: dict[float, LightGBMQuantileModel] = {}
    for q in c.quantiles:
        print(f"  training quantile={q} …")
        m = _make_model(q)
        m.fit(train.X, train.y, val.X, val.y)
        models[q] = m

    # Save models + manifest.
    run_dir = MODELS_DIR / c.as_of_date.strftime("%Y-%m-%d")
    run_dir.mkdir(parents=True, exist_ok=True)
    # Output filenames key off the config stem so each variant (e.g. log_ratio,
    # additive, log_diff) keeps its own manifest instead of overwriting.
    run_stem = c.source_path.stem
    for q, m in models.items():
        tag = f"p{int(q * 100)}"
        m.save(run_dir / f"{run_stem}_{tag}.lgb")

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
        baseline_dir=baseline_dir,
        target=target_key,
    )

    # Three-way compare only meaningful for residual runs.
    prior_manifest = None
    if c.residual:
        prior_manifest = load_prior_manifest(run_dir, c.target)

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
                "buckets_per_day": report.primary_buckets_per_day,
                "buckets_aggregate": report.primary_buckets_agg,
                "baseline_buckets_per_day": report.baseline_buckets_per_day,
                "baseline_buckets_aggregate": report.baseline_buckets_agg,
            },
            "supplemental": {
                "slice": "reason_resolved IN ('completed','failed')",
                "per_day": report.supplemental_per_day,
                "aggregate": report.supplemental_agg,
            },
        },
    }

    if c.residual and prior_manifest:
        prior_primary = prior_manifest.get("evaluation", {}).get("primary", {})
        manifest["evaluation"]["primary"]["lightgbm_only_aggregate"]         = prior_primary.get("aggregate")
        manifest["evaluation"]["primary"]["lightgbm_only_buckets_aggregate"] = prior_primary.get("buckets_aggregate")
        manifest["evaluation"]["primary"]["prior_manifest_trained_at"]       = prior_manifest.get("trained_at")

    (run_dir / f"{run_stem}_manifest.json").write_text(json.dumps(manifest, indent=2, default=str))

    # Summary printing ------------------------------------------------------

    def _fmt_mae(agg):
        v = agg.get("mae_s") if agg else None
        return f"{v:.1f}s" if v is not None and v == v else "n/a"

    def _fmt_w2x(agg):
        v = agg.get("within_2x_rate") if agg else None
        return f"{v * 100:.1f}%" if v is not None and v == v else "n/a"

    three_way = bool(c.residual and prior_manifest)

    print("\n=== Evaluation (primary: completed-only) ===")
    if three_way:
        lgb_only = prior_manifest["evaluation"]["primary"].get("aggregate", {})
        print(f"  {'':20s} {'Baseline':>12s} {'LGB-only':>12s} {'Residual':>12s}")
        print(f"  {'MAE':<20s} {_fmt_mae(report.baseline_agg):>12s} {_fmt_mae(lgb_only):>12s} {_fmt_mae(report.primary_agg):>12s}")
        print(f"  {'within-2x':<20s} {_fmt_w2x(report.baseline_agg):>12s} {_fmt_w2x(lgb_only):>12s} {_fmt_w2x(report.primary_agg):>12s}")
    else:
        print(f"  LightGBM : MAE={_fmt_mae(report.primary_agg)}  within_2x={_fmt_w2x(report.primary_agg)}")
        print(f"  Baseline : MAE={_fmt_mae(report.baseline_agg)}  within_2x={_fmt_w2x(report.baseline_agg)}")
        base_mae = report.baseline_agg.get("mae_s", 0.0) or 0.0
        lgb_mae  = report.primary_agg.get("mae_s", 0.0) or 0.0
        if base_mae > 0:
            print(f"  Delta MAE: {(lgb_mae - base_mae) / base_mae * 100:+.1f}%")

    p90_cov_rate = report.primary_agg.get("p90_coverage_rate")
    if p90_cov_rate is not None and p90_cov_rate == p90_cov_rate:  # NaN check
        print(f"  p90 coverage: {p90_cov_rate * 100:.1f}% (target [85%, 95%])")

    # Per-bucket breakdown for wait target
    if c.target == "wait_time" and report.primary_buckets_agg:
        print("\n=== Wait model — per-bucket breakdown ===")
        if three_way:
            lgb_buckets = prior_manifest["evaluation"]["primary"].get("buckets_aggregate", {}) or {}
            header = f"{'bucket':<7} {'n':>10} | {'Base MAE':>10} {'LGB MAE':>10} {'Res MAE':>10} | {'Base w/in2x':>12} {'LGB w/in2x':>12} {'Res w/in2x':>12}"
            print(header)
            for name in ["<1m", "1-5m", "5-30m", "30m+"]:
                res   = report.primary_buckets_agg.get(name, {})
                lgb_b = lgb_buckets.get(name, {})
                base  = report.baseline_buckets_agg.get(name, {})
                n     = res.get("mae", {}).get("eligible_n", 0)
                print(f"{name:<7} {n:>10,} | "
                      f"{_fmt_mae(base):>10} {_fmt_mae(lgb_b):>10} {_fmt_mae(res):>10} | "
                      f"{_fmt_w2x(base):>12} {_fmt_w2x(lgb_b):>12} {_fmt_w2x(res):>12}")
        else:
            header = f"{'bucket':<7} {'n (LGB)':>10} {'MAE (LGB)':>12} {'MAE (base)':>12} {'w/in2x (LGB)':>14} {'w/in2x (base)':>15}"
            print(header)
            for name in ["<1m", "1-5m", "5-30m", "30m+"]:
                lgb_b  = report.primary_buckets_agg.get(name, {})
                base_b = report.baseline_buckets_agg.get(name, {})
                n      = lgb_b.get("mae", {}).get("eligible_n", 0)
                print(f"{name:<7} {n:>10,} {_fmt_mae(lgb_b):>12} {_fmt_mae(base_b):>12} {_fmt_w2x(lgb_b):>14} {_fmt_w2x(base_b):>15}")

    print(f"\nModels + manifest in {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
