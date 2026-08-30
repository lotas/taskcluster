from __future__ import annotations

import argparse
import hashlib
import json
import resource
from datetime import datetime, timezone
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from src import config as cfg
from src import data_loader
from src import extract_source
from src import hazard_labels
from src.features import FeatureBuilder, Split
from src.hazard_model import DiscreteHazardModel
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


def _peak_rss_mb() -> float:
    """Process peak resident set size so far, in MB.

    `ru_maxrss` is a running high-watermark for this process's whole
    lifetime (Linux reports it in KB) -- reading it late in the run (right
    before the manifest is written) captures the true peak without needing
    to sample repeatedly. Added 2026-07-15 after run_duration_residual was
    OOM-killed twice with no visibility into how close prior runs had come
    to the ceiling -- this puts the number in every manifest going forward so
    a growth trend shows up before the next OOM does."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


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


def _run_discrete_hazard_training(
    c: cfg.Config,
    w: cfg.Windows,
    holdout_day_keys: list[str],
    baseline_dir: Path,
    train: Split,
    val: Split,
    hold: Split,
    n_train_rows: int,
    n_val_rows: int,
    n_hold_rows: int,
) -> dict:
    """Train + evaluate a DiscreteHazardModel (Bet 2's discrete-time hazard
    model for wait_time) and write its manifest + model artifacts.

    Deliberately reuses evaluate.py's existing do_eval() unchanged, so the
    resulting 30m+ wait p90 miss number is directly comparable to the
    quantile-regression path's numbers -- per bet2-hazard-survival-design.md's
    "Apples-to-apples with Bet 1's numbers", no new evaluation code is needed
    to get the primary-gate figure.

    Does not touch the ONNX/quantile-dict machinery in main()'s existing
    path below this function -- the hazard model has its own save format
    (one booster per bin, not a single quantile head) and doesn't support
    live serving yet (see bet2-hazard-survival-design.md's serving scope).
    """
    edges_minutes = c.hazard_bins_minutes or hazard_labels.DEFAULT_BIN_EDGES_MINUTES
    model = DiscreteHazardModel(edges_minutes=edges_minutes, params=c.model_params)
    model.fit(
        train.X, train.meta[["pending_at", "resolved_at"]], train.y, w.train_end,
        val.X,   val.meta[["pending_at", "resolved_at"]],   val.y,   w.val_end,
    )

    preds_p50 = model.predict_quantile(hold.X, 0.5)
    preds_p90 = model.predict_quantile(hold.X, 0.9)

    n_bad = int((~np.isfinite(preds_p90)).sum())
    if n_bad:
        print(f"  WARNING: {n_bad:,}/{len(preds_p90):,} holdout p90s non-finite "
              f"(tail_rate_={model.tail_rate_!r}); these rows are excluded from all metrics")

    # Hardcoded because discrete_hazard is wait-only; config.load_config
    # rejects any other target up front, but re-check here so a hand-built
    # Config can't route run_duration predictions into wait baselines.
    if c.target != "wait_time":
        raise ValueError(f"discrete_hazard supports only target: wait_time, got {c.target!r}")
    target_key = "wait"
    p90_col = f"bl_{target_key}_p90"
    baseline_p90 = hold.X[p90_col].to_numpy() if p90_col in hold.X.columns else None

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

    run_dir = MODELS_DIR / c.as_of_date.strftime("%Y-%m-%d")
    run_stem = c.source_path.stem
    run_dir.mkdir(parents=True, exist_ok=True)
    model_dir = run_dir / f"{run_stem}_hazard_model"
    model.save(model_dir)

    manifest = {
        "target": c.target,
        "config_path": str(c.source_path),
        "model_type": c.model_type,
        "hazard_bins_minutes": [e if e != float("inf") else None for e in edges_minutes],
        "tail_rate": model.tail_rate_,
        "degraded_bins": model.degraded_bins_,
        "bin_fit": model.bin_fit_,
        "model_artifact_dir": model_dir.name,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "lightgbm_version": lgb.__version__,
        "windows": {
            "as_of_date":      c.as_of_date.isoformat(),
            "lookback_days":   c.lookback_days,
            "validation_days": c.validation_days,
            "holdout_days":    c.holdout_days,
            "train":   {"start": w.train_start.isoformat(), "end": w.train_end.isoformat(), "rows": n_train_rows},
            "val":     {"start": w.val_start.isoformat(),   "end": w.val_end.isoformat(),   "rows": n_val_rows},
            "holdout": {"start": w.hold_start.isoformat(),  "end": w.hold_end.isoformat(),  "rows": n_hold_rows},
        },
        "features": {
            "categorical": c.categorical_features,
            "numeric":     c.numeric_features,
            "cardinalities": train.stats.get("cardinalities", {}),
            "null_rates":    train.stats.get("null_rates", {}),
            "unseen_rates_holdout": hold.stats.get("unseen_rate", {}),
        },
        "model_params": c.model_params,
        "resource_usage": {"peak_rss_mb": round(_peak_rss_mb(), 1)},
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
    # This path returns before `main`'s common lineage block, so it has to name
    # its own data. Read from module state rather than threaded through: the
    # source is process-wide by construction (`extract_source.active()`), and a
    # parameter that could disagree with what the loaders actually used would be
    # a second answer to the same question.
    _extract = extract_source.active()
    if _extract is not None:
        manifest["training_lineage"] = {
            "source": "extract",
            "extract": _extract.lineage(),
        }
    (run_dir / f"{run_stem}_manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--refresh-cache", action="store_true")
    parser.add_argument("--as-of-date", default=None,
                        help="Override as_of_date from config (UTC midnight of given day)")
    parser.add_argument("--from-extract", default=None, metavar="DIR",
                        help="Train from a frozen extract directory instead of "
                             "Postgres (default: $QF_EXTRACT_DIR if set). No "
                             "DATABASE_URL is read on this path.")
    args = parser.parse_args(argv)

    if args.from_extract:
        extract_source.configure(args.from_extract)
    _extract = extract_source.active()
    if _extract is not None:
        print(f"Source: frozen extract {_extract.root} "
              f"(extract_hash={str(_extract.extract_hash)[:12]})")

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
    # _split_by_pending_at copies each slice out (boolean-mask indexing always
    # copies in pandas); df itself is never referenced again but stays alive
    # for the rest of this function otherwise, doubling the peak footprint of
    # the whole train+val+holdout window for no reason.
    del df

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
    # Captured before deleting -- the only remaining uses of train_df/val_df/
    # hold_df were len() calls in the manifest, written after model training
    # and evaluation. Without this, all three full-size DataFrames (train_df
    # alone can be millions of rows) stay alive through the heaviest part of
    # the run -- LightGBM training's own internal binned copy, prediction,
    # evaluation -- for the sake of a row count read at the very end.
    n_train_rows, n_val_rows, n_hold_rows = len(train_df), len(val_df), len(hold_df)
    del train_df, val_df, hold_df

    if c.model_type == "discrete_hazard":
        manifest = _run_discrete_hazard_training(
            c, w, holdout_day_keys, baseline_dir,
            train, val, hold, n_train_rows, n_val_rows, n_hold_rows,
        )
        run_dir = MODELS_DIR / c.as_of_date.strftime("%Y-%m-%d")
        buckets30 = manifest["evaluation"]["primary"]["buckets_aggregate"].get("30m+", {})
        def _pct(v):
            return f"{v * 100:.1f}%" if v is not None and v == v else "n/a"
        guarded = buckets30.get("p90_miss_rate_guarded")
        raw = buckets30.get("p90_miss_rate")
        print(f"\n=== Discrete hazard model — 30m+ wait p90 miss: {_pct(guarded)} guarded (gate bar: 34.49%) / {_pct(raw)} raw ===")
        print(f"Models + manifest in {run_dir}")
        return 0

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
        "queue_context_features": getattr(c, "queue_context_features", None),
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
    #
    # THE TWO SOURCES ARE MUTUALLY EXCLUSIVE HERE, and that is not tidiness. A
    # cache filename encodes the config and the window but not the source, so on
    # any host that has ever trained from Postgres the extract run's cache paths
    # EXIST -- holding data it did not read. Hashing them because they are on
    # disk would put a digest in the manifest that names a file that contributed
    # nothing, which is worse than no digest: it is provenance that reads as
    # confirmed and is wrong.
    if _extract is not None:
        training_lineage: dict = {
            "source":                        "extract",
            "training_cache_file":           None,
            "training_cache_content_sha256": None,
            "extract":                       _extract.lineage(),
        }
    else:
        main_cache = data_loader.cache_path(c)
        training_lineage = {
            "source":                        "database",
            "training_cache_file":           main_cache.name,
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
    # Same exclusion as above, for the same reason: every entry below is a digest
    # of a `data/cache` file or of a DB snapshot, and on the extract path the
    # reference data came from `throughput_runs.parquet` / `worker_counts.parquet`
    # / `worker_pools.parquet` -- already digested, per file, under
    # `training_lineage["extract"]["files"]`.
    if _extract is not None:
        throughput_path = wc_path = None
    else:
        throughput_path = data_loader.throughput_cache_path(c)
        wc_path = data_loader.worker_counts_cache_path(c)
    if throughput_path is not None:
        engineered["throughput_runs"] = {
            "file":           throughput_path.name,
            "content_sha256": data_loader.file_sha256(throughput_path) if throughput_path.exists() else None,
        }
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
    p90_col = f"bl_{target_key}_p90"
    baseline_p90 = (
        hold.X[p90_col].to_numpy()
        if p90_col in hold.X.columns
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
            "train":   {"start": w.train_start.isoformat(), "end": w.train_end.isoformat(), "rows": n_train_rows},
            "val":     {"start": w.val_start.isoformat(),   "end": w.val_end.isoformat(),   "rows": n_val_rows},
            "holdout": {"start": w.hold_start.isoformat(),  "end": w.hold_end.isoformat(),  "rows": n_hold_rows},
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
        "resource_usage": {"peak_rss_mb": round(_peak_rss_mb(), 1)},
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
    print(f"  peak RSS: {manifest['resource_usage']['peak_rss_mb']:,.0f} MB")

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
