"""Holdout metrics with raw numerator/denominator — aggregate-safe.

Always report per-metric raw counts so the trainer and baseline can be
aggregated identically across days. See trainer-spec.md §"Evaluation"
for the rule; within-2x denominator excludes zero-valued rows per
src/predictor.js.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


def per_row_metrics(y_true: np.ndarray, y_pred: np.ndarray,
                    y_pred_p90: np.ndarray | None = None,
                    y_pred_p90_guarded: np.ndarray | None = None) -> dict:
    yt = np.asarray(y_true, dtype=float)
    yp = np.asarray(y_pred, dtype=float)
    mae_mask = np.isfinite(yt) & np.isfinite(yp)
    mae_eligible_n = int(mae_mask.sum())
    sum_abs_error = float(np.abs(yp[mae_mask] - yt[mae_mask]).sum())

    w2x_mask = mae_mask & (yt > 0) & (yp > 0)
    w2x_eligible_n = int(w2x_mask.sum())
    if w2x_eligible_n:
        ratio = np.maximum(yp[w2x_mask] / yt[w2x_mask], yt[w2x_mask] / yp[w2x_mask])
        w2x_hits = int((ratio <= 2).sum())
    else:
        w2x_hits = 0

    out = {
        "mae":       {"eligible_n": mae_eligible_n, "sum_abs_error": sum_abs_error},
        "within_2x": {"eligible_n": w2x_eligible_n, "hit_n": w2x_hits},
    }

    if y_pred_p90 is not None:
        out["pinball_p50"] = pinball_loss(yt, yp, alpha=0.5)
        out["pinball_p90"] = pinball_loss(yt, np.asarray(y_pred_p90, dtype=float), alpha=0.9)
        out["p90_coverage"] = p90_coverage(yt, np.asarray(y_pred_p90, dtype=float))

    if y_pred_p90_guarded is not None:
        ypg = np.asarray(y_pred_p90_guarded, dtype=float)
        out["pinball_p90_guarded"]  = pinball_loss(yt, ypg, alpha=0.9)
        out["p90_coverage_guarded"] = p90_coverage(yt, ypg)

    return out


def compute_guarded_p90(*, p50: np.ndarray, model_p90: np.ndarray,
                        baseline_p90: np.ndarray) -> np.ndarray:
    """Floor the model's p90 with the historical baseline p90 (where finite),
    then floor by p50 so p90 >= p50 always holds.

    NaN baseline rows fall back to model_p90 (no guard applied to that row).
    """
    p50 = np.asarray(p50, dtype=float)
    model_p90 = np.asarray(model_p90, dtype=float)
    baseline_p90 = np.asarray(baseline_p90, dtype=float)
    bl_floor = np.where(np.isfinite(baseline_p90), baseline_p90, -np.inf)
    return np.maximum(p50, np.maximum(model_p90, bl_floor))


def pinball_loss(y_true: np.ndarray, y_pred: np.ndarray, alpha: float) -> dict:
    yt = np.asarray(y_true, dtype=float)
    yp = np.asarray(y_pred, dtype=float)
    mask = np.isfinite(yt) & np.isfinite(yp)
    diff = yt[mask] - yp[mask]
    vals = np.maximum(alpha * diff, (alpha - 1) * diff)
    return {"eligible_n": int(mask.sum()), "sum": float(vals.sum())}


def p90_coverage(y_true: np.ndarray, y_pred_p90: np.ndarray) -> dict:
    yt = np.asarray(y_true, dtype=float)
    yp = np.asarray(y_pred_p90, dtype=float)
    mask = np.isfinite(yt) & np.isfinite(yp)
    return {"eligible_n": int(mask.sum()), "covered_n": int((yt[mask] <= yp[mask]).sum())}


def compute_day_metrics(meta: pd.DataFrame, y_true: np.ndarray, y_pred: np.ndarray,
                        y_pred_p90: np.ndarray | None = None,
                        y_pred_p90_guarded: np.ndarray | None = None) -> dict[str, dict]:
    """Per-day metrics keyed by YYYY-MM-DD (UTC date of pending_at)."""
    out: dict[str, dict] = {}
    day_keys = meta["pending_at"].dt.tz_convert("UTC").dt.strftime("%Y-%m-%d")
    for day, idx in day_keys.groupby(day_keys).groups.items():
        sel = idx.to_numpy()
        p90_slice = y_pred_p90[sel] if y_pred_p90 is not None else None
        guarded_slice = y_pred_p90_guarded[sel] if y_pred_p90_guarded is not None else None
        out[str(day)] = per_row_metrics(
            y_true[sel], y_pred[sel],
            y_pred_p90=p90_slice,
            y_pred_p90_guarded=guarded_slice,
        )
    return out


def aggregate_days(days: Iterable[dict]) -> dict:
    days = list(days)
    mae_e  = sum(d["mae"]["eligible_n"]    for d in days)
    mae_s  = sum(d["mae"]["sum_abs_error"] for d in days)
    w2x_e  = sum(d["within_2x"]["eligible_n"] for d in days)
    w2x_h  = sum(d["within_2x"]["hit_n"]      for d in days)
    out = {
        "mae":       {"eligible_n": mae_e, "sum_abs_error": mae_s},
        "within_2x": {"eligible_n": w2x_e, "hit_n": w2x_h},
        "mae_s":          (mae_s / mae_e) if mae_e else float("nan"),
        "within_2x_rate": (w2x_h / w2x_e) if w2x_e else float("nan"),
    }

    # p50/p90 metrics are only present in trainer per-day dicts, not in baseline
    # JSONs (which only carry mae + within_2x). Aggregate only when all days carry them.
    if all("pinball_p50" in d for d in days) and days:
        pb50_e = sum(d["pinball_p50"]["eligible_n"] for d in days)
        pb50_s = sum(d["pinball_p50"]["sum"]        for d in days)
        out["pinball_p50"] = {"eligible_n": pb50_e, "sum": pb50_s}
        out["pinball_p50_avg"] = (pb50_s / pb50_e) if pb50_e else float("nan")

    if all("pinball_p90" in d for d in days) and days:
        pb90_e = sum(d["pinball_p90"]["eligible_n"] for d in days)
        pb90_s = sum(d["pinball_p90"]["sum"]        for d in days)
        out["pinball_p90"] = {"eligible_n": pb90_e, "sum": pb90_s}
        out["pinball_p90_avg"] = (pb90_s / pb90_e) if pb90_e else float("nan")

    if all("p90_coverage" in d for d in days) and days:
        cov_e = sum(d["p90_coverage"]["eligible_n"] for d in days)
        cov_n = sum(d["p90_coverage"]["covered_n"]  for d in days)
        out["p90_coverage"] = {"eligible_n": cov_e, "covered_n": cov_n}
        out["p90_coverage_rate"] = (cov_n / cov_e) if cov_e else float("nan")

    if all("pinball_p90_guarded" in d for d in days) and days:
        pbg_e = sum(d["pinball_p90_guarded"]["eligible_n"] for d in days)
        pbg_s = sum(d["pinball_p90_guarded"]["sum"]        for d in days)
        out["pinball_p90_guarded"] = {"eligible_n": pbg_e, "sum": pbg_s}
        out["pinball_p90_guarded_avg"] = (pbg_s / pbg_e) if pbg_e else float("nan")

    if all("p90_coverage_guarded" in d for d in days) and days:
        covg_e = sum(d["p90_coverage_guarded"]["eligible_n"] for d in days)
        covg_n = sum(d["p90_coverage_guarded"]["covered_n"]  for d in days)
        out["p90_coverage_guarded"] = {"eligible_n": covg_e, "covered_n": covg_n}
        out["p90_coverage_guarded_rate"] = (covg_n / covg_e) if covg_e else float("nan")

    return out


def load_baseline_days(directory: Path) -> dict[str, dict]:
    """Load per-day baseline eval JSONs (named `YYYY-MM-DD.json`), keyed by eval_date.

    Restricted to the date-shaped filename so unrelated `.json` files in the
    same directory (notably `baseline_predictions.ndjson.meta.json`) are
    ignored.
    """
    out: dict[str, dict] = {}
    if not directory.exists():
        return out
    for p in sorted(directory.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].json")):
        data = json.loads(p.read_text())
        out[data["eval_date"]] = data
    return out


WAIT_BUCKETS = [
    ("<1m",   0.0,    60.0),
    ("1-5m",  60.0,   300.0),
    ("5-30m", 300.0,  1800.0),
    ("30m+",  1800.0, float("inf")),
]


def compute_bucket_metrics(y_true: np.ndarray, y_pred: np.ndarray,
                           y_pred_p90: np.ndarray | None = None,
                           y_pred_p90_guarded: np.ndarray | None = None) -> dict[str, dict]:
    """Per-bucket (MAE + within_2x) keyed by bucket name. Uses actual (y_true)
    to assign buckets, half-open intervals matching predictor.js.

    When ``y_pred_p90`` is supplied, each bucket also carries a raw-count
    ``p90_coverage`` entry ({eligible_n, covered_n}) — same shape as the
    aggregate p90_coverage — so the tail (p90 miss) can be tracked per bucket.

    ``y_pred_p90_guarded`` (the production guardrail floor applied to
    ``y_pred_p90``) adds a parallel ``p90_coverage_guarded`` entry. The two
    diverge whenever the guardrail floor binds -- which per the wait-model
    guardrail is the common case on strong cells -- so a bucket's raw
    ``p90_coverage`` does NOT represent what is actually served live; callers
    that want the served miss rate must use the guarded entry."""
    yt = np.asarray(y_true, dtype=float)
    yp = np.asarray(y_pred, dtype=float)
    yp90 = np.asarray(y_pred_p90, dtype=float) if y_pred_p90 is not None else None
    yp90g = np.asarray(y_pred_p90_guarded, dtype=float) if y_pred_p90_guarded is not None else None
    out: dict[str, dict] = {}
    for name, lo, hi in WAIT_BUCKETS:
        mask = np.isfinite(yt) & (yt >= lo) & (yt < hi)
        if not mask.any():
            empty = {
                "mae":       {"eligible_n": 0, "sum_abs_error": 0.0},
                "within_2x": {"eligible_n": 0, "hit_n": 0},
            }
            if yp90 is not None:
                empty["p90_coverage"] = {"eligible_n": 0, "covered_n": 0}
            if yp90g is not None:
                empty["p90_coverage_guarded"] = {"eligible_n": 0, "covered_n": 0}
            out[name] = empty
            continue
        row = per_row_metrics(y_true=yt[mask], y_pred=yp[mask],
                              y_pred_p90=(yp90[mask] if yp90 is not None else None),
                              y_pred_p90_guarded=(yp90g[mask] if yp90g is not None else None))
        # Strip any extra keys to keep shape identical to predictor.js output,
        # plus the optional per-bucket p90 coverage entries when supplied.
        bucket = {
            "mae":       row["mae"],
            "within_2x": row["within_2x"],
        }
        if "p90_coverage" in row:
            bucket["p90_coverage"] = row["p90_coverage"]
        if "p90_coverage_guarded" in row:
            bucket["p90_coverage_guarded"] = row["p90_coverage_guarded"]
        out[name] = bucket
    return out


def aggregate_buckets(day_buckets: list[dict[str, dict]]) -> dict[str, dict]:
    """Given a list of per-day bucket-dicts, sum raw counts across days."""
    agg: dict[str, dict] = {
        name: {
            "mae":       {"eligible_n": 0, "sum_abs_error": 0.0},
            "within_2x": {"eligible_n": 0, "hit_n": 0},
        }
        for name, _, _ in WAIT_BUCKETS
    }
    # p90 coverage is only present when bucket metrics were computed with a p90
    # prediction; aggregate it only when every contributing day carries it.
    has_p90 = bool(day_buckets) and all(
        "p90_coverage" in day[name]
        for day in day_buckets for name, _, _ in WAIT_BUCKETS if name in day
    )
    has_p90_guarded = bool(day_buckets) and all(
        "p90_coverage_guarded" in day[name]
        for day in day_buckets for name, _, _ in WAIT_BUCKETS if name in day
    )
    for day in day_buckets:
        for name, _, _ in WAIT_BUCKETS:
            if name not in day:
                continue
            agg[name]["mae"]["eligible_n"]    += day[name]["mae"]["eligible_n"]
            agg[name]["mae"]["sum_abs_error"] += day[name]["mae"]["sum_abs_error"]
            agg[name]["within_2x"]["eligible_n"] += day[name]["within_2x"]["eligible_n"]
            agg[name]["within_2x"]["hit_n"]      += day[name]["within_2x"]["hit_n"]
            if has_p90:
                agg[name].setdefault("p90_coverage", {"eligible_n": 0, "covered_n": 0})
                agg[name]["p90_coverage"]["eligible_n"] += day[name]["p90_coverage"]["eligible_n"]
                agg[name]["p90_coverage"]["covered_n"]  += day[name]["p90_coverage"]["covered_n"]
            if has_p90_guarded:
                agg[name].setdefault("p90_coverage_guarded", {"eligible_n": 0, "covered_n": 0})
                agg[name]["p90_coverage_guarded"]["eligible_n"] += day[name]["p90_coverage_guarded"]["eligible_n"]
                agg[name]["p90_coverage_guarded"]["covered_n"]  += day[name]["p90_coverage_guarded"]["covered_n"]
    # Derived rates (so the manifest is self-contained)
    for name in list(agg.keys()):
        mae = agg[name]["mae"]
        w2x = agg[name]["within_2x"]
        agg[name]["mae_s"]          = (mae["sum_abs_error"] / mae["eligible_n"]) if mae["eligible_n"] else float("nan")
        agg[name]["within_2x_rate"] = (w2x["hit_n"] / w2x["eligible_n"]) if w2x["eligible_n"] else float("nan")
        cov = agg[name].get("p90_coverage")
        if cov is not None:
            e = cov["eligible_n"]
            # p90 miss = fraction with actual > predicted p90 = 1 - coverage.
            agg[name]["p90_coverage_rate"] = (cov["covered_n"] / e) if e else float("nan")
            agg[name]["p90_miss_rate"]     = (1.0 - cov["covered_n"] / e) if e else float("nan")
        covg = agg[name].get("p90_coverage_guarded")
        if covg is not None:
            eg = covg["eligible_n"]
            # This is the miss rate that matters for the gate: it reflects the
            # guardrail-floored p90 actually served live, not the raw model head.
            agg[name]["p90_coverage_guarded_rate"] = (covg["covered_n"] / eg) if eg else float("nan")
            agg[name]["p90_miss_rate_guarded"]      = (1.0 - covg["covered_n"] / eg) if eg else float("nan")
    return agg


@dataclass
class MetricsReport:
    primary_per_day: dict[str, dict]
    primary_agg: dict
    supplemental_per_day: dict[str, dict]
    supplemental_agg: dict
    baseline_per_day: dict[str, dict]
    baseline_agg: dict
    # Only populated for wait-target runs
    primary_buckets_per_day: dict[str, dict[str, dict]] = field(default_factory=dict)
    primary_buckets_agg: dict[str, dict] = field(default_factory=dict)
    baseline_buckets_per_day: dict[str, dict[str, dict]] = field(default_factory=dict)
    baseline_buckets_agg: dict[str, dict] = field(default_factory=dict)


def load_prior_manifest(run_dir: Path, target: str) -> dict | None:
    """Load the non-residual LightGBM-only manifest from the same run directory.

    Returns None if no such manifest exists (e.g. this is the first training run).
    The filename convention is `<target>_manifest.json` for non-residual,
    `<target>_residual_manifest.json` for residual.
    """
    import json as _json
    p = run_dir / f"{target}_manifest.json"
    if not p.exists():
        return None
    return _json.loads(p.read_text())


def evaluate(*, preds_p50: np.ndarray, preds_p90: np.ndarray,
             hold_meta: pd.DataFrame, y_true: np.ndarray,
             holdout_day_keys: list[str], baseline_dir: Path,
             target: str,
             baseline_p90: np.ndarray | None = None) -> MetricsReport:
    """Compute per-day + aggregate metrics on primary/supplemental slices
    and load the matching baseline JSONs.

    `target` is "duration" or "wait" — selects which field of the baseline
    JSONs to read.

    When ``baseline_p90`` is provided (run-duration runs), the report also
    carries a guarded p90 view: max(preds_p90, baseline_p90) floored by p50.
    Coverage and pinball loss for the guarded view are emitted alongside the
    raw-model p90 metrics so we can compare calibration globally.
    """
    primary_mask      = hold_meta["reason_resolved"].isin(["completed"]).to_numpy()
    supplemental_mask = hold_meta["reason_resolved"].isin(["completed", "failed"]).to_numpy()

    preds_p90_guarded = (
        compute_guarded_p90(p50=preds_p50, model_p90=preds_p90, baseline_p90=baseline_p90)
        if baseline_p90 is not None else None
    )

    def _slice(mask):
        guarded_slice = preds_p90_guarded[mask] if preds_p90_guarded is not None else None
        return (hold_meta[mask].reset_index(drop=True),
                y_true[mask], preds_p50[mask], preds_p90[mask], guarded_slice)

    p_meta, p_yt, p_yp, p_yp90, p_yp90g = _slice(primary_mask)
    s_meta, s_yt, s_yp, s_yp90, s_yp90g = _slice(supplemental_mask)

    primary_per_day      = compute_day_metrics(p_meta, p_yt, p_yp,
                                               y_pred_p90=p_yp90,
                                               y_pred_p90_guarded=p_yp90g)
    supplemental_per_day = compute_day_metrics(s_meta, s_yt, s_yp,
                                               y_pred_p90=s_yp90,
                                               y_pred_p90_guarded=s_yp90g)

    primary_agg      = aggregate_days(primary_per_day.values())
    supplemental_agg = aggregate_days(supplemental_per_day.values())

    baseline_raw = load_baseline_days(baseline_dir)
    baseline_per_day = {
        day: blob[target] for day, blob in baseline_raw.items()
        if day in holdout_day_keys
    }
    baseline_agg = aggregate_days(baseline_per_day.values())

    primary_buckets_per_day: dict[str, dict[str, dict]] = {}
    primary_buckets_agg: dict[str, dict] = {}
    baseline_buckets_per_day: dict[str, dict[str, dict]] = {}
    baseline_buckets_agg: dict[str, dict] = {}

    if target == "wait":
        # Trainer-side: recompute per-bucket per day
        day_keys_series = p_meta["pending_at"].dt.tz_convert("UTC").dt.strftime("%Y-%m-%d")
        for day, idx in day_keys_series.groupby(day_keys_series).groups.items():
            sel = idx.to_numpy()
            primary_buckets_per_day[str(day)] = compute_bucket_metrics(
                p_yt[sel], p_yp[sel], y_pred_p90=p_yp90[sel],
                y_pred_p90_guarded=(p_yp90g[sel] if p_yp90g is not None else None))
        primary_buckets_agg = aggregate_buckets(list(primary_buckets_per_day.values()))

        # Baseline-side: pull from the JSONs
        for day, blob in baseline_raw.items():
            if day in holdout_day_keys and "buckets" in blob.get("wait", {}):
                baseline_buckets_per_day[day] = blob["wait"]["buckets"]
        baseline_buckets_agg = aggregate_buckets(list(baseline_buckets_per_day.values()))

    return MetricsReport(
        primary_per_day=primary_per_day,
        primary_agg=primary_agg,
        supplemental_per_day=supplemental_per_day,
        supplemental_agg=supplemental_agg,
        baseline_per_day=baseline_per_day,
        baseline_agg=baseline_agg,
        primary_buckets_per_day=primary_buckets_per_day,
        primary_buckets_agg=primary_buckets_agg,
        baseline_buckets_per_day=baseline_buckets_per_day,
        baseline_buckets_agg=baseline_buckets_agg,
    )
