#!/usr/bin/env python3
"""Evaluate a frozen (already-trained) model against a range of holdout windows.

Measures model staleness: compare a model frozen as of date X against the same
holdout windows used by the daily walk-forward, to see if daily retraining helps.

Usage:
    uv run python scripts/eval_frozen_model.py \
        --model-as-of 2026-04-30 \
        --from 2026-05-01 --to 2026-05-14 \
        --config configs/wait_time_residual_throughput_filtered_baseline.yaml \
        [--output frozen_eval.csv]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

_TRAINER_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_TRAINER_ROOT))

from src import config as cfg
from src import data_loader
from src.config import compute_windows, holdout_day_starts, load_config
from src.evaluate import evaluate as do_eval
from src.features import FeatureBuilder
from src.model import LightGBMQuantileModel, ResidualLightGBMQuantileModel


MODELS_DIR = _TRAINER_ROOT / "data" / "models"

FIELDNAMES = [
    "cohort_as_of", "model_as_of", "config", "target",
    "baseline_mae", "model_mae", "delta_mae_pct",
    "baseline_within_2x", "model_within_2x", "delta_within_2x_pp",
    "p90_coverage",
    "lt1m_mae", "1-5m_mae", "5-30m_mae", "30mplus_mae",
    "lt1m_within_2x", "1-5m_within_2x", "5-30m_within_2x", "30mplus_within_2x",
    "hold_rows",
]

BUCKET_KEYS = ["<1m", "1-5m", "5-30m", "30m+"]
BUCKET_CSV_KEYS = ["lt1m", "1-5m", "5-30m", "30mplus"]


def _parse_date(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _iter_dates(start: datetime, end: datetime):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _pct(numer, denom):
    if not denom:
        return None
    return 100.0 * (numer - denom) / denom


def _pp(a, b):
    if a is None or b is None:
        return None
    return 100.0 * (a - b)


def _baseline_dir(c: cfg.Config) -> Path:
    rel = c.baseline_dir or "baseline"
    if rel.startswith("data/"):
        rel = rel[len("data/"):]
    return _TRAINER_ROOT / "data" / rel


def _load_frozen_builder(frozen_config: cfg.Config, model_dir: Path, config_stem: str) -> FeatureBuilder:
    """Reconstruct a fitted FeatureBuilder using the frozen category vocabularies."""
    mappings_path = model_dir / f"{config_stem}_category_mappings.json"
    if not mappings_path.exists():
        raise FileNotFoundError(f"Category mappings not found: {mappings_path}")
    builder = FeatureBuilder(frozen_config)
    cats = json.loads(mappings_path.read_text())
    for col, values in cats.items():
        builder._categories[col] = pd.Index(values)
    builder._fitted = True
    return builder


def _load_frozen_models(model_dir: Path, config_stem: str, has_residual: bool):
    cls = ResidualLightGBMQuantileModel if has_residual else LightGBMQuantileModel
    return cls.load(model_dir / f"{config_stem}_p50.lgb"), \
           cls.load(model_dir / f"{config_stem}_p90.lgb")


def _eval_one_date(
    eval_date: datetime,
    model_as_of: datetime,
    config_path: Path,
    config_stem: str,
    frozen_builder: FeatureBuilder,
    model_p50,
    model_p90,
) -> dict | None:
    eval_config = load_config(config_path, as_of_date_override=eval_date)
    w = compute_windows(eval_config)
    holdout_day_keys = [d.strftime("%Y-%m-%d") for d in holdout_day_starts(eval_config)]
    baseline_dir = _baseline_dir(eval_config)

    missing = [d for d in holdout_day_keys if not (baseline_dir / f"{d}.json").exists()]
    if missing:
        print(f"  SKIP {eval_date.date()}: missing baseline JSONs: {missing}", file=sys.stderr)
        return None

    print(f"  eval {eval_date.date()}  holdout={w.hold_start.date()}..{w.hold_end.date()} ...", file=sys.stderr)

    df = data_loader.load(eval_config)
    pending = pd.to_datetime(df["pending_at"], utc=True)
    hold_df = df[(pending >= w.hold_start) & (pending < w.hold_end)].reset_index(drop=True)

    if len(hold_df) == 0:
        print(f"  SKIP {eval_date.date()}: empty holdout", file=sys.stderr)
        return None

    hold = frozen_builder.transform(hold_df)
    preds_p50 = model_p50.predict(hold.X)
    preds_p90 = model_p90.predict(hold.X)

    target_key = "duration" if eval_config.target == "run_duration" else "wait"
    report = do_eval(
        preds_p50=preds_p50,
        preds_p90=preds_p90,
        hold_meta=hold.meta,
        y_true=hold.y.to_numpy(),
        holdout_day_keys=holdout_day_keys,
        baseline_dir=baseline_dir,
        target=target_key,
    )

    agg = report.primary_agg
    bl_agg = report.baseline_agg
    model_mae   = agg.get("mae_s")
    baseline_mae = bl_agg.get("mae_s")
    delta = _pct(model_mae, baseline_mae)
    print(f"    mae={model_mae:.1f}s  baseline={baseline_mae:.1f}s  delta={delta:+.1f}%", file=sys.stderr)

    row: dict = {
        "cohort_as_of":       eval_date.strftime("%Y-%m-%d"),
        "model_as_of":        model_as_of.strftime("%Y-%m-%d"),
        "config":             config_stem,
        "target":             eval_config.target,
        "baseline_mae":       baseline_mae,
        "model_mae":          model_mae,
        "delta_mae_pct":      delta,
        "baseline_within_2x": bl_agg.get("within_2x_rate"),
        "model_within_2x":    agg.get("within_2x_rate"),
        "delta_within_2x_pp": _pp(agg.get("within_2x_rate"), bl_agg.get("within_2x_rate")),
        "p90_coverage":       agg.get("p90_coverage_rate"),
        "hold_rows":          len(hold_df),
    }
    for bk, csv_key in zip(BUCKET_KEYS, BUCKET_CSV_KEYS):
        b = report.primary_buckets_agg.get(bk) or {}
        row[f"{csv_key}_mae"]       = b.get("mae_s")
        row[f"{csv_key}_within_2x"] = b.get("within_2x_rate")
    return row


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Evaluate a frozen model over a range of holdout windows."
    )
    p.add_argument("--model-as-of", required=True,
                   help="Date of the frozen model to load (YYYY-MM-DD).")
    p.add_argument("--from", dest="from_date", required=True,
                   help="First as_of_date to evaluate (inclusive, YYYY-MM-DD).")
    p.add_argument("--to",   dest="to_date",   required=True,
                   help="Last as_of_date to evaluate (inclusive, YYYY-MM-DD).")
    p.add_argument("--config", required=True,
                   help="Path to the trainer config YAML.")
    p.add_argument("--output", default="frozen_eval.csv",
                   help="Output CSV path (default: frozen_eval.csv).")
    args = p.parse_args(argv)

    model_as_of = _parse_date(args.model_as_of)
    from_dt     = _parse_date(args.from_date)
    to_dt       = _parse_date(args.to_date)

    config_path = Path(args.config)
    config_stem = config_path.stem
    model_dir   = MODELS_DIR / model_as_of.strftime("%Y-%m-%d")

    if not model_dir.exists():
        print(f"ERROR: model directory not found: {model_dir}", file=sys.stderr)
        return 1

    frozen_config = load_config(config_path, as_of_date_override=model_as_of)
    frozen_builder = _load_frozen_builder(frozen_config, model_dir, config_stem)
    model_p50, model_p90 = _load_frozen_models(model_dir, config_stem, bool(frozen_config.residual))
    print(f"Loaded frozen model: {model_dir.name}  config={config_stem}", file=sys.stderr)

    rows = []
    for eval_date in _iter_dates(from_dt, to_dt):
        row = _eval_one_date(
            eval_date, model_as_of, config_path, config_stem,
            frozen_builder, model_p50, model_p90,
        )
        if row is not None:
            rows.append(row)

    if not rows:
        print("No rows produced.", file=sys.stderr)
        return 1

    out_path = Path(args.output)
    with out_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        w.writeheader()
        for row in rows:
            w.writerow({
                k: (f"{v:.4f}" if isinstance(v, float) else (v if v is not None else ""))
                for k, v in row.items()
            })

    print(f"\nWrote {len(rows)} rows to {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
