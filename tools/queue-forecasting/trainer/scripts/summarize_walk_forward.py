#!/usr/bin/env python3
"""Summarize walk-forward evaluation manifests into a CSV + summary stats.

Usage:
    uv run python scripts/summarize_walk_forward.py \
        --from 2026-04-19 --to 2026-04-24 \
        [--output walk_forward_summary.csv]

Globs trainer/data/models/<YYYY-MM-DD>/*_manifest.json within the range,
extracts aggregate + per-bucket numbers, writes one row per
(cohort_as_of, config) to CSV. Appends summary stats at the end of stdout.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics as stats
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


MODELS_DIR = Path(__file__).resolve().parent.parent / "data" / "models"

FIELDNAMES = [
    "cohort_as_of", "config", "target",
    "baseline_mae", "model_mae", "delta_mae_pct",
    "baseline_within_2x", "model_within_2x", "delta_within_2x_pp",
    "p90_coverage",
    "lt1m_mae", "1-5m_mae", "5-30m_mae", "30mplus_mae",
    "lt1m_within_2x", "1-5m_within_2x", "5-30m_within_2x", "30mplus_within_2x",
    "hold_rows",
    "cohort_is_anomalous",
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
    if denom in (None, 0) or denom != denom:
        return None
    return 100.0 * (numer - denom) / denom


def _pp(a, b):
    if a is None or b is None:
        return None
    return 100.0 * (a - b)


def _holdout_dates(data: dict) -> list[str]:
    """Return YYYY-MM-DD strings for each whole UTC day inside the holdout window."""
    h = (data.get("windows", {}) or {}).get("holdout") or {}
    start_s = h.get("start")
    end_s   = h.get("end")
    if not start_s or not end_s:
        return []
    start = datetime.fromisoformat(start_s.replace("Z", "+00:00"))
    end   = datetime.fromisoformat(end_s.replace("Z", "+00:00"))
    out = []
    d = start
    while d < end:
        out.append(d.strftime("%Y-%m-%d"))
        d = d + timedelta(days=1)
    return out


def _load_anomalous_dates_from_db() -> set[str]:
    """Fetch flagged dates from queue_forecast_daily_health.

    Returns an empty set on any failure: missing DATABASE_URL, missing table,
    connection error, etc. Callers must treat absence as "not anomalous"
    so that the summarizer remains usable in offline / unit-test contexts.
    """
    import os
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        return set()
    try:
        import psycopg
    except ImportError:
        return set()
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT sample_date FROM queue_forecast_daily_health WHERE is_anomalous = TRUE")
                return {r[0].strftime("%Y-%m-%d") for r in cur.fetchall()}
    except Exception:
        return set()


def extract_row(manifest_path: Path, anomalous_dates: set[str] | None = None) -> dict | None:
    data = json.loads(manifest_path.read_text())
    cohort = manifest_path.parent.name   # YYYY-MM-DD
    config = manifest_path.stem.replace("_manifest", "")
    target = data.get("target", "unknown")   # 'wait_time' | 'run_duration' | ...

    primary = data.get("evaluation", {}).get("primary", {})
    agg = primary.get("aggregate") or {}
    baseline_agg = primary.get("baseline_aggregate") or {}
    buckets = primary.get("buckets_aggregate") or {}

    model_mae = agg.get("mae_s")
    baseline_mae = baseline_agg.get("mae_s")
    model_w2x = agg.get("within_2x_rate")
    baseline_w2x = baseline_agg.get("within_2x_rate")
    p90 = agg.get("p90_coverage_rate")

    # Cohort flagged anomalous if ANY day in its holdout window is flagged.
    # Missing days (e.g. daily_health not yet populated) count as non-anomalous.
    cohort_is_anomalous = False
    if anomalous_dates:
        for d in _holdout_dates(data):
            if d in anomalous_dates:
                cohort_is_anomalous = True
                break

    row = {
        "cohort_as_of": cohort,
        "config": config,
        "target": target,
        "baseline_mae": baseline_mae,
        "model_mae": model_mae,
        "delta_mae_pct": _pct(model_mae, baseline_mae),
        "baseline_within_2x": baseline_w2x,
        "model_within_2x": model_w2x,
        "delta_within_2x_pp": _pp(model_w2x, baseline_w2x),
        "p90_coverage": p90,
        "hold_rows": (data.get("windows", {}).get("holdout") or {}).get("rows"),
        "cohort_is_anomalous": cohort_is_anomalous,
    }
    for bk, csv_key in zip(BUCKET_KEYS, BUCKET_CSV_KEYS):
        b = buckets.get(bk) or {}
        row[f"{csv_key}_mae"] = b.get("mae_s")
        row[f"{csv_key}_within_2x"] = b.get("within_2x_rate")
    return row


def gather_rows(from_date: datetime, to_date: datetime,
                configs_filter: set[str] | None = None,
                anomalous_dates: set[str] | None = None) -> list[dict]:
    if anomalous_dates is None:
        anomalous_dates = _load_anomalous_dates_from_db()
    rows = []
    for d in _iter_dates(from_date, to_date):
        day_dir = MODELS_DIR / d.strftime("%Y-%m-%d")
        if not day_dir.exists():
            continue
        for manifest in sorted(day_dir.glob("*_manifest.json")):
            row = extract_row(manifest, anomalous_dates=anomalous_dates)
            if row is None:
                continue
            if configs_filter is not None and row["config"] not in configs_filter:
                continue
            rows.append(row)
    return rows


def _fmt(v, unit=""):
    return "n/a" if v is None else f"{v:.2f}{unit}"


def _print_target_block(target: str, target_rows: list[dict]) -> None:
    """Print the summary block for one target (wait_time / run_duration / ...).

    Win counts compare only within-target configs — absolute MAE between
    run_duration (~130s scale) and wait_time (~500s scale) is meaningless."""
    by_config: dict[str, list[dict]] = defaultdict(list)
    for r in target_rows:
        by_config[r["config"]].append(r)
    configs = sorted(by_config.keys())

    cohorts = sorted(set(r["cohort_as_of"] for r in target_rows))
    print(f"\n=== Target: {target}  ({len(cohorts)} cohorts, configs: {', '.join(configs)}) ===\n",
          file=sys.stderr)

    # Per-config stats
    for cfg_name in configs:
        cfg_rows = by_config[cfg_name]
        deltas_mae = [r["delta_mae_pct"]      for r in cfg_rows if r["delta_mae_pct"]      is not None]
        deltas_w2x = [r["delta_within_2x_pp"] for r in cfg_rows if r["delta_within_2x_pp"] is not None]
        p90s       = [r["p90_coverage"]       for r in cfg_rows if r["p90_coverage"]       is not None]
        bucket_30m = [r["30mplus_within_2x"]  for r in cfg_rows if r["30mplus_within_2x"]  is not None]

        print(f"--- {cfg_name} ({len(cfg_rows)} cohorts) ---", file=sys.stderr)
        if deltas_mae:
            print(f"  MAE Δ%   : mean={_fmt(stats.mean(deltas_mae))}  median={_fmt(stats.median(deltas_mae))}  worst={_fmt(max(deltas_mae))}", file=sys.stderr)
        if deltas_w2x:
            print(f"  w/in2x pp: mean={_fmt(stats.mean(deltas_w2x))}  median={_fmt(stats.median(deltas_w2x))}  worst={_fmt(min(deltas_w2x))}", file=sys.stderr)
        if p90s:
            in_band = sum(1 for p in p90s if 0.85 <= p <= 0.95)
            print(f"  p90 cov  : mean={_fmt(stats.mean(p90s) * 100, '%')}  in-band [85,95]%: {in_band}/{len(p90s)}", file=sys.stderr)
        if bucket_30m:
            over_50 = sum(1 for b in bucket_30m if b >= 0.50)
            print(f"  30m+ w/in2x: mean={_fmt(stats.mean(bucket_30m) * 100, '%')}  ≥50%: {over_50}/{len(bucket_30m)}", file=sys.stderr)
        print("", file=sys.stderr)

    # Win counts — only within this target's configs, and only counting cohorts
    # where all configs produced a manifest.
    win_by_metric: dict[str, dict[str, int]] = {
        "best_MAE":             defaultdict(int),
        "best_within_2x":       defaultdict(int),
        "best_30m+_within_2x":  defaultdict(int),
    }
    by_cohort: dict[str, list[dict]] = defaultdict(list)
    for r in target_rows:
        by_cohort[r["cohort_as_of"]].append(r)

    counted = 0
    for cohort, crows in by_cohort.items():
        # Only count cohorts that have all configs in this target.
        if len({r["config"] for r in crows}) < len(configs):
            continue
        counted += 1
        min_mae = min((r for r in crows if r["model_mae"] is not None), key=lambda r: r["model_mae"], default=None)
        if min_mae:
            win_by_metric["best_MAE"][min_mae["config"]] += 1
        max_w2x = max((r for r in crows if r["model_within_2x"] is not None), key=lambda r: r["model_within_2x"], default=None)
        if max_w2x:
            win_by_metric["best_within_2x"][max_w2x["config"]] += 1
        max_30m = max((r for r in crows if r["30mplus_within_2x"] is not None), key=lambda r: r["30mplus_within_2x"], default=None)
        if max_30m:
            win_by_metric["best_30m+_within_2x"][max_30m["config"]] += 1

    print(f"--- Win counts per cohort (of {counted} complete cohorts) ---", file=sys.stderr)
    for metric, counts in win_by_metric.items():
        if not counts:
            continue
        line = f"  {metric}: "
        line += ", ".join(f"{c}={n}" for c, n in sorted(counts.items(), key=lambda x: -x[1]))
        print(line, file=sys.stderr)
    print("", file=sys.stderr)


def print_summary(rows: list[dict]) -> None:
    print("\n=== Walk-forward summary ===\n", file=sys.stderr)
    all_configs = sorted({r["config"] for r in rows})
    all_cohorts = sorted({r["cohort_as_of"] for r in rows})
    print(f"Configs seen: {', '.join(all_configs)}", file=sys.stderr)
    print(f"Cohorts: {len(all_cohorts)}  ({all_cohorts[0]}..{all_cohorts[-1]})" if all_cohorts else "Cohorts: 0", file=sys.stderr)
    print(f"Total rows: {len(rows)}", file=sys.stderr)

    # Group by target and emit one summary block per target. Absolute MAE
    # between different targets is not comparable, so win counts are always
    # target-local.
    by_target: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_target[r["target"]].append(r)

    for target in sorted(by_target.keys()):
        _print_target_block(target, by_target[target])


DEFAULT_CONFIGS = "wait_time,wait_time_residual,wait_time_residual_throughput"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="from_date", required=True)
    p.add_argument("--to",   dest="to_date",   required=True)
    p.add_argument("--output", default="walk_forward_summary.csv")
    p.add_argument("--configs", default=DEFAULT_CONFIGS,
                   help="Comma-separated config stems to include (matches manifest filenames without _manifest.json). "
                        "Pass '*' or empty to disable filtering. Default: " + DEFAULT_CONFIGS)
    args = p.parse_args(argv)

    from_dt = _parse_date(args.from_date)
    to_dt   = _parse_date(args.to_date)

    configs_filter: set[str] | None
    if args.configs in ("", "*"):
        configs_filter = None
    else:
        configs_filter = {c.strip() for c in args.configs.split(",") if c.strip()}

    rows = gather_rows(from_dt, to_dt, configs_filter=configs_filter)
    if not rows:
        print("No manifests found in range.", file=sys.stderr)
        if configs_filter:
            print(f"  (config filter was: {sorted(configs_filter)})", file=sys.stderr)
        return 1

    out_path = Path(args.output)
    with out_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        w.writeheader()
        for row in sorted(rows, key=lambda r: (r["cohort_as_of"], r["config"])):
            w.writerow({k: (f"{v:.4f}" if isinstance(v, float) else (v if v is not None else "")) for k, v in row.items()})

    print(f"Wrote {len(rows)} rows to {out_path}", file=sys.stderr)
    print_summary(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
