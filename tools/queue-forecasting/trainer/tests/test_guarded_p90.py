"""Tests for the run-duration p90 guardrail evaluation view.

The guarded p90 = max(model_p90, bl_duration_p90) where bl_duration_p90 is
finite, then floored by p50. The eval helper reports coverage and pinball
loss for this guarded view alongside the raw-model metrics so we can compare
calibration globally.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.evaluate import (
    compute_guarded_p90,
    per_row_metrics,
    aggregate_days,
    evaluate,
)


def test_compute_guarded_p90_raises_above_finite_baseline():
    p50 = np.array([100.0, 50.0, 200.0])
    p90 = np.array([300.0, 100.0, 400.0])
    bl  = np.array([500.0, 80.0, 350.0])
    out = compute_guarded_p90(p50=p50, model_p90=p90, baseline_p90=bl)
    # Row 0: max(300, 500) = 500
    # Row 1: max(100, 80)  = 100, floor by p50=50 → 100
    # Row 2: max(400, 350) = 400
    assert np.allclose(out, [500.0, 100.0, 400.0])


def test_compute_guarded_p90_floors_below_p50():
    # model_p90 < p50, baseline missing → final must be p50.
    p50 = np.array([400.0])
    p90 = np.array([100.0])
    bl  = np.array([np.nan])
    out = compute_guarded_p90(p50=p50, model_p90=p90, baseline_p90=bl)
    assert np.allclose(out, [400.0])


def test_compute_guarded_p90_passes_through_when_baseline_nan():
    p50 = np.array([100.0, 50.0])
    p90 = np.array([300.0, 200.0])
    bl  = np.array([np.nan, np.nan])
    out = compute_guarded_p90(p50=p50, model_p90=p90, baseline_p90=bl)
    assert np.allclose(out, [300.0, 200.0])


def test_per_row_metrics_with_guarded_p90_adds_pinball_and_coverage():
    # actual = 10/20/30, model p90 = 12/22/28 (third misses),
    # guarded p90 = 12/22/40 (none miss).
    actual    = np.array([10.0, 20.0, 30.0])
    p50       = np.array([9.0, 18.0, 25.0])
    p90       = np.array([12.0, 22.0, 28.0])
    p90_guard = np.array([12.0, 22.0, 40.0])
    m = per_row_metrics(y_true=actual, y_pred=p50,
                        y_pred_p90=p90, y_pred_p90_guarded=p90_guard)
    # Raw model still reported.
    assert m["p90_coverage"]["covered_n"] == 2
    # Guarded view: all three covered.
    assert "pinball_p90_guarded" in m
    assert "p90_coverage_guarded" in m
    assert m["p90_coverage_guarded"]["eligible_n"] == 3
    assert m["p90_coverage_guarded"]["covered_n"] == 3


def test_aggregate_days_with_guarded_p90():
    days = [
        {"mae": {"eligible_n": 10, "sum_abs_error": 10.0},
         "within_2x": {"eligible_n": 10, "hit_n": 8},
         "pinball_p50": {"eligible_n": 10, "sum": 5.0},
         "pinball_p90": {"eligible_n": 10, "sum": 1.0},
         "p90_coverage": {"eligible_n": 10, "covered_n": 9},
         "pinball_p90_guarded":  {"eligible_n": 10, "sum": 0.6},
         "p90_coverage_guarded": {"eligible_n": 10, "covered_n": 10}},
        {"mae": {"eligible_n": 20, "sum_abs_error": 40.0},
         "within_2x": {"eligible_n": 20, "hit_n": 18},
         "pinball_p50": {"eligible_n": 20, "sum": 15.0},
         "pinball_p90": {"eligible_n": 20, "sum": 3.0},
         "p90_coverage": {"eligible_n": 20, "covered_n": 17},
         "pinball_p90_guarded":  {"eligible_n": 20, "sum": 2.0},
         "p90_coverage_guarded": {"eligible_n": 20, "covered_n": 19}},
    ]
    agg = aggregate_days(days)
    assert agg["pinball_p90_guarded"]["eligible_n"] == 30
    assert np.isclose(agg["pinball_p90_guarded_avg"], 2.6 / 30)
    assert agg["p90_coverage_guarded"]["eligible_n"] == 30
    assert agg["p90_coverage_guarded"]["covered_n"] == 29
    assert np.isclose(agg["p90_coverage_guarded_rate"], 29 / 30)


def test_evaluate_emits_guarded_metrics_when_baseline_p90_passed(tmp_path):
    meta = pd.DataFrame({
        "pending_at": pd.to_datetime([
            "2026-04-19T05:00Z", "2026-04-19T10:00Z", "2026-04-20T01:00Z",
        ]),
        "reason_resolved": ["completed", "completed", "completed"],
        "task_id": ["a", "b", "c"], "run_id": [0, 0, 0],
    })
    y_true    = np.array([10.0, 20.0, 30.0])
    preds_p50 = np.array([9.0, 18.0, 25.0])
    preds_p90 = np.array([12.0, 22.0, 28.0])  # misses on row 2
    bl_p90    = np.array([20.0, 30.0, 50.0])  # bigger tail
    report = evaluate(
        preds_p50=preds_p50, preds_p90=preds_p90,
        hold_meta=meta, y_true=y_true,
        holdout_day_keys=["2026-04-19", "2026-04-20"],
        baseline_dir=tmp_path,
        target="duration",
        baseline_p90=bl_p90,
    )
    # Raw model metrics still present.
    assert "p90_coverage" in report.primary_agg
    # Guarded metrics now present.
    assert "p90_coverage_guarded" in report.primary_agg
    assert "pinball_p90_guarded"  in report.primary_agg
    # Guarded must cover at least as many rows as raw model.
    assert (report.primary_agg["p90_coverage_guarded"]["covered_n"]
            >= report.primary_agg["p90_coverage"]["covered_n"])


def test_evaluate_omits_guarded_metrics_when_no_baseline_p90(tmp_path):
    # Wait-time runs don't pass baseline_p90 → no guarded keys.
    meta = pd.DataFrame({
        "pending_at": pd.to_datetime(["2026-04-19T05:00Z"]),
        "reason_resolved": ["completed"], "task_id": ["a"], "run_id": [0],
    })
    report = evaluate(
        preds_p50=np.array([9.0]), preds_p90=np.array([12.0]),
        hold_meta=meta, y_true=np.array([10.0]),
        holdout_day_keys=["2026-04-19"], baseline_dir=tmp_path,
        target="wait",
    )
    assert "p90_coverage" in report.primary_agg
    assert "p90_coverage_guarded" not in report.primary_agg
    assert "pinball_p90_guarded"  not in report.primary_agg
