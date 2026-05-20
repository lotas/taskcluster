from __future__ import annotations

import argparse
import hashlib
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
    # Pre-load worker_pools before data_loader.load() so the same snapshot is
    # used for both feature computation and lineage hashing (avoids a second
    # DB read that could reflect a changed table mid-run).
    _worker_pools_snapshot: "pd.DataFrame | None" = None
    if c.velocity_features and c.velocity_features.get("enabled"):
        _worker_pools_snapshot = data_loader.load_worker_pools()

    df = data_loader.load(c, refresh_cache=args.refresh_cache, worker_pools=_worker_pools_snapshot)
    print(f"  rows loaded: {len(df):,}")

    train_df, val_df, hold_df = _split_by_pending_at(df, c)
    print(f"  train={len(train_df):,}  val={len(val_df):,}  hold={len(hold_df):,}")

    # Dates actually excluded from train/val; captured here so lineage records
    # the exact set used, not a re-query that could differ if the health table
    # is updated mid-run.
    _anomalous_dates_used: set = set()
    if c.anomaly_filter and c.anomaly_filter.get("enabled"):
        mode = c.anomaly_filter.get("mode", "training")
        if mode in ("training", "both"):
            _anomalous_dates_used = data_loader.load_anomalous_dates(c)
            if _anomalous_dates_used:
                n_train_before = len(train_df)
                n_val_before   = len(val_df)
                train_df = train_df[~train_df["pending_at"].dt.tz_convert("UTC").dt.date.isin(_anomalous_dates_used)].reset_index(drop=True)
                val_df   = val_df  [~val_df  ["pending_at"].dt.tz_convert("UTC").dt.date.isin(_anomalous_dates_used)].reset_index(drop=True)
                print(f"  anomaly filter ({mode}): train {n_train_before:,}→{len(train_df):,}, "
                      f"val {n_val_before:,}→{len(val_df):,} ({len(_anomalous_dates_used)} anomalous days)")
            # Holdout is never filtered. Will be sliced for reporting in Stage 2.

    # Detect "filter emptied train or val" — write a skip-manifest and return
    # cleanly so walk-forward continues. Common with validation_days=1 when
    # the single val day happens to be flagged anomalous.
    if len(train_df) == 0 or len(val_df) == 0:
        empty_parts = [name for name, dfp in [("train", train_df), ("val", val_df)] if len(dfp) == 0]
        skip_reason = (
            f"anomaly filter emptied {', '.join(empty_parts)} for {c.as_of_date.date()}; "
            f"too few non-anomalous days in {','.join(empty_parts)} window — "
            f"increase validation_days or move the cohort"
        )
        run_dir = MODELS_DIR / c.as_of_date.strftime("%Y-%m-%d")
        run_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = run_dir / f"{c.source_path.stem}_manifest.json"
        with manifest_path.open("w") as fh:
            json.dump({
                "skipped": True,
                "skip_reason": skip_reason,
                "target": c.target,
                "config_path": str(c.source_path),
                "as_of_date": c.as_of_date.isoformat(),
                "trained_at": datetime.now(timezone.utc).isoformat(),
            }, fh, indent=2)
        print(f"  SKIP: {skip_reason}")
        print(f"  skip-manifest written: {manifest_path}")
        return 0

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

    # Save models + sidecars + manifest.
    run_dir = MODELS_DIR / c.as_of_date.strftime("%Y-%m-%d")
    run_dir.mkdir(parents=True, exist_ok=True)
    # Output filenames key off the config stem so each variant (e.g. log_ratio,
    # additive, log_diff) keeps its own manifest instead of overwriting.
    run_stem = c.source_path.stem
    for q, m in models.items():
        tag = f"p{int(q * 100)}"
        m.save(run_dir / f"{run_stem}_{tag}.lgb")
        m.save_onnx(run_dir / f"{run_stem}_{tag}.onnx")

    # Category-vocabulary sidecar (one per config, not per quantile).
    category_mappings_path = run_dir / f"{run_stem}_category_mappings.json"
    builder.dump_categories(category_mappings_path)

    # Feature-schema sidecar — the JS serving contract.
    s_hash = data_loader.serving_hash(c)
    model_version = f"v_{c.as_of_date.strftime('%Y-%m-%d')}_{s_hash}"
    feature_schema = {
        "model_version":   model_version,
        "target":          c.target,
        "feature_order":   list(next(iter(models.values())).feature_names_),
        "categorical_features": c.categorical_features,
        "numeric_features":     c.numeric_features,
        "derived_features":     c.derived_features,
        "residual":             c.residual,
        "anomaly_filter":       c.anomaly_filter,
        "throughput_features":  c.throughput_features,
        "velocity_features":    getattr(c, "velocity_features", None),
        "quantile_models": {
            str(q): f"{run_stem}_p{int(q * 100)}.onnx" for q in sorted(models)
        },
        "category_mappings_file": category_mappings_path.name,
        "cold_start_code": -1,
    }
    feature_schema_path = run_dir / f"{run_stem}_feature_schema.json"
    feature_schema_path.write_text(json.dumps(feature_schema, indent=2, default=str))

    # artifact_hash: SHA256 over the four serving files concatenated in order.
    serving_files = [
        run_dir / f"{run_stem}_p50.onnx",
        run_dir / f"{run_stem}_p90.onnx",
        category_mappings_path,
        feature_schema_path,
    ]
    h = hashlib.sha256()
    for sf in serving_files:
        with open(sf, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    artifact_hash = h.hexdigest()[:16]

    # training_lineage: content hashes of every input that produced this model.
    main_cache = data_loader.cache_path(c)
    training_lineage: dict = {
        "training_cache_file":            main_cache.name,
        "training_cache_content_sha256":  data_loader.file_sha256(main_cache) if main_cache.exists() else None,
    }
    if c.anomaly_filter and c.anomaly_filter.get("enabled"):
        mode = c.anomaly_filter.get("mode", "training")
        if mode in ("training", "both"):
            # Reuse the set captured at filter time — not a fresh DB query.
            excluded = sorted(d.isoformat() for d in _anomalous_dates_used)
            flag_subset = c.anomaly_filter.get("flag_subset")
            condition = (
                " OR ".join(f"{f} = TRUE" for f in flag_subset)
                if flag_subset else "is_anomalous = TRUE"
            )
            training_lineage["training_excluded_dates"] = excluded
            training_lineage["anomaly_filter_basis"] = {
                "mode": mode,
                "flag_subset": flag_subset,
                "query": f"SELECT sample_date FROM queue_forecast_daily_health WHERE {condition}",
            }
        else:
            training_lineage["training_excluded_dates"] = []
            training_lineage["anomaly_filter_basis"] = None
    else:
        training_lineage["training_excluded_dates"] = []
        training_lineage["anomaly_filter_basis"] = None

    engineered: dict = {}
    throughput_path = data_loader.throughput_cache_path(c)
    if throughput_path is not None:
        engineered["throughput_runs"] = {
            "file":           throughput_path.name,
            "content_sha256": data_loader.file_sha256(throughput_path) if throughput_path.exists() else None,
        }
    wc_path = data_loader.worker_counts_cache_path(c)
    if wc_path is not None:
        engineered["worker_counts"] = {
            "file":           wc_path.name,
            "content_sha256": data_loader.file_sha256(wc_path) if wc_path.exists() else None,
        }
        # worker_pools has no parquet cache; hash the snapshot captured before
        # data_loader.load() — same object used for training, no second DB read.
        if _worker_pools_snapshot is not None:
            try:
                import io as _io
                pools_sorted = _worker_pools_snapshot.sort_values("task_queue_id").reset_index(drop=True)
                buf = _io.BytesIO()
                pools_sorted.to_parquet(buf, index=False)
                pools_sha = hashlib.sha256(buf.getvalue()).hexdigest()
                engineered["worker_pools"] = {
                    "source":         "queue_forecast_worker_pools",
                    "row_count":      len(_worker_pools_snapshot),
                    "content_sha256": pools_sha,
                }
            except Exception as exc:
                engineered["worker_pools"] = {"error": str(exc)}
    if engineered:
        training_lineage["engineered_feature_inputs"] = engineered

    if c.residual:
        bl_path = _baseline_dir(c) / c.residual["baseline_file"]
        bl_meta_path = bl_path.with_suffix(".ndjson.meta.json") if bl_path.suffix == ".ndjson" else bl_path.with_name(bl_path.name + ".meta.json")
        bl_meta = None
        if bl_meta_path.exists():
            try:
                bl_meta = json.loads(bl_meta_path.read_text())
            except Exception:
                bl_meta = None
        training_lineage["baseline_ndjson_meta"] = {
            **(bl_meta or {}),
            "file":           str(bl_path.relative_to(TRAINER_ROOT)),
            "content_sha256": data_loader.file_sha256(bl_path) if bl_path.exists() else None,
        }
    else:
        training_lineage["baseline_ndjson_meta"] = None

    # Evaluate. Target key matches what the baseline JSON uses:
    # baseline stores both "duration" and "wait" blocks; we pick ours.
    target_key = "duration" if c.target == "run_duration" else "wait"
    preds_p50 = models[0.5].predict(hold.X) if 0.5 in models else np.full(len(hold.y), np.nan)
    preds_p90 = models[0.9].predict(hold.X) if 0.9 in models else np.full(len(hold.y), np.nan)
    # Run-duration runs floor the served p90 with the historical exact-name
    # baseline p90 (see live-predictor guardrail). Mirror that here so the
    # manifest can compare raw vs. guarded p90 coverage globally.
    baseline_p90 = (
        hold.X["bl_duration_p90"].to_numpy()
        if c.target == "run_duration" and "bl_duration_p90" in hold.X.columns
        else None
    )
    report = do_eval(
        preds_p50=preds_p50,
        preds_p90=preds_p90,
        hold_meta=hold.meta,
        y_true=hold.y.to_numpy(),
        holdout_day_keys=holdout_day_keys,
        baseline_dir=baseline_dir,
        target=target_key,
        baseline_p90=baseline_p90,
    )

    # Three-way compare only meaningful for residual runs.
    prior_manifest = None
    if c.residual:
        prior_manifest = load_prior_manifest(run_dir, c.target)

    manifest = {
        "target": c.target,
        "config_path": str(c.source_path),
        "config_hash": data_loader.cache_key(c),   # query-shaping only; kept for dashboard compat
        "serving_hash": s_hash,
        "model_version": model_version,
        "artifact_hash": artifact_hash,
        "serving_artifacts": [sf.name for sf in serving_files],
        "training_artifacts": [
            f"{run_stem}_p{int(q * 100)}.lgb"      for q in sorted(models)
        ] + [
            f"{run_stem}_p{int(q * 100)}.lgb.meta" for q in sorted(models)
        ],
        "training_lineage": training_lineage,
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
