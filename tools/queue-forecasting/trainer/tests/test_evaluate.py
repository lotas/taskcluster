import json
from pathlib import Path

import numpy as np
import pandas as pd

from src.evaluate import (
    compute_day_metrics,
    aggregate_days,
    load_baseline_days,
    per_row_metrics,
)


def test_within_2x_excludes_zero_rows():
    actual = np.array([10.0, 0.0, 5.0, 0.0])
    pred   = np.array([12.0, 0.0, 7.0, 3.0])
    m = per_row_metrics(y_true=actual, y_pred=pred)
    # MAE eligible = all 4 rows
    assert m["mae"]["eligible_n"] == 4
    assert np.isclose(m["mae"]["sum_abs_error"], abs(12-10) + 0 + abs(7-5) + abs(3-0))
    # within_2x eligible = only rows where both > 0 → rows 0, 2
    assert m["within_2x"]["eligible_n"] == 2
    # both of those are within 2x
    assert m["within_2x"]["hit_n"] == 2


def test_aggregate_days_weights_correctly():
    days = [
        {"mae": {"eligible_n": 100, "sum_abs_error": 2000.0},
         "within_2x": {"eligible_n": 90, "hit_n": 45}},
        {"mae": {"eligible_n": 200, "sum_abs_error": 1000.0},
         "within_2x": {"eligible_n": 180, "hit_n": 162}},
    ]
    agg = aggregate_days(days)
    assert agg["mae"]["eligible_n"] == 300
    assert agg["mae"]["sum_abs_error"] == 3000.0
    assert np.isclose(agg["mae_s"], 3000.0 / 300)
    assert agg["within_2x"]["eligible_n"] == 270
    assert agg["within_2x"]["hit_n"] == 207
    assert np.isclose(agg["within_2x_rate"], 207 / 270)


def test_load_baseline_days(tmp_path: Path):
    (tmp_path / "2026-04-19.json").write_text(json.dumps({
        "eval_date": "2026-04-19",
        "duration": {"n": 10, "mae": {"eligible_n": 10, "sum_abs_error": 100.0},
                     "within_2x": {"eligible_n": 10, "hit_n": 8}},
        "wait":     {"n": 10, "mae": {"eligible_n": 10, "sum_abs_error": 200.0},
                     "within_2x": {"eligible_n": 9,  "hit_n": 5}},
    }))
    days = load_baseline_days(tmp_path)
    assert "2026-04-19" in days
    assert days["2026-04-19"]["duration"]["mae"]["sum_abs_error"] == 100.0


def test_compute_day_metrics_uses_pending_at():
    meta = pd.DataFrame({
        "pending_at": pd.to_datetime([
            "2026-04-19T05:00Z", "2026-04-19T10:00Z", "2026-04-20T01:00Z",
        ]),
        "reason_resolved": ["completed", "completed", "completed"],
        "task_id": ["a", "b", "c"], "run_id": [0, 0, 0],
    })
    y_true = np.array([10.0, 20.0, 30.0])
    y_pred = np.array([12.0, 18.0, 28.0])
    per_day = compute_day_metrics(meta, y_true, y_pred)
    assert set(per_day.keys()) == {"2026-04-19", "2026-04-20"}
    assert per_day["2026-04-19"]["mae"]["eligible_n"] == 2


def test_per_row_metrics_with_p90_adds_pinball_and_coverage():
    # actual = 10, p50=12, p90=15 → within coverage; |p50-actual|=2
    actual  = np.array([10.0, 20.0, 30.0])
    pred_50 = np.array([12.0, 18.0, 28.0])
    pred_90 = np.array([15.0, 25.0, 35.0])
    m = per_row_metrics(y_true=actual, y_pred=pred_50, y_pred_p90=pred_90)
    # Existing fields still present
    assert m["mae"]["eligible_n"] == 3
    assert m["within_2x"]["eligible_n"] == 3
    # New fields: pinball loss at p50 and p90, p90 coverage
    assert "pinball_p50" in m and m["pinball_p50"]["eligible_n"] == 3
    assert "pinball_p90" in m and m["pinball_p90"]["eligible_n"] == 3
    assert "p90_coverage" in m
    # All three p90 predictions are >= actual → covered_n == 3
    assert m["p90_coverage"]["eligible_n"] == 3
    assert m["p90_coverage"]["covered_n"] == 3


def test_per_row_metrics_p90_optional():
    # When y_pred_p90 is not provided, only p50 metrics are emitted.
    actual = np.array([10.0, 20.0])
    p50    = np.array([9.0, 22.0])
    m = per_row_metrics(y_true=actual, y_pred=p50)
    assert "mae" in m and "within_2x" in m
    assert "pinball_p90" not in m
    assert "p90_coverage" not in m


def test_aggregate_days_with_p90():
    days = [
        {"mae": {"eligible_n": 10, "sum_abs_error": 10.0},
         "within_2x": {"eligible_n": 10, "hit_n": 8},
         "pinball_p50": {"eligible_n": 10, "sum": 5.0},
         "pinball_p90": {"eligible_n": 10, "sum": 1.0},
         "p90_coverage": {"eligible_n": 10, "covered_n": 9}},
        {"mae": {"eligible_n": 20, "sum_abs_error": 40.0},
         "within_2x": {"eligible_n": 20, "hit_n": 18},
         "pinball_p50": {"eligible_n": 20, "sum": 15.0},
         "pinball_p90": {"eligible_n": 20, "sum": 3.0},
         "p90_coverage": {"eligible_n": 20, "covered_n": 17}},
    ]
    agg = aggregate_days(days)
    assert agg["pinball_p50"]["eligible_n"] == 30
    assert np.isclose(agg["pinball_p50_avg"], 20.0 / 30)
    assert np.isclose(agg["pinball_p90_avg"], 4.0 / 30)
    assert agg["p90_coverage"]["eligible_n"] == 30
    assert agg["p90_coverage"]["covered_n"] == 26
    assert np.isclose(agg["p90_coverage_rate"], 26 / 30)


def test_evaluate_manifest_has_p90_fields(tmp_path):
    # End-to-end mini: feed synthetic data + models' predictions to evaluate()
    # and verify primary_agg carries pinball_p50, pinball_p90, p90_coverage.
    from src.evaluate import evaluate
    meta = pd.DataFrame({
        "pending_at": pd.to_datetime([
            "2026-04-19T05:00Z", "2026-04-19T10:00Z", "2026-04-20T01:00Z",
        ]),
        "reason_resolved": ["completed", "completed", "completed"],
        "task_id": ["a", "b", "c"], "run_id": [0, 0, 0],
    })
    y_true = np.array([10.0, 20.0, 30.0])
    preds_p50 = np.array([12.0, 18.0, 28.0])
    preds_p90 = np.array([15.0, 25.0, 40.0])
    report = evaluate(
        preds_p50=preds_p50, preds_p90=preds_p90,
        hold_meta=meta, y_true=y_true,
        holdout_day_keys=["2026-04-19", "2026-04-20"],
        baseline_dir=tmp_path,  # empty → baseline_agg is safe-nan
        target="wait",
    )
    assert "pinball_p50" in report.primary_agg
    assert "pinball_p90" in report.primary_agg
    assert "p90_coverage" in report.primary_agg
    # per-day too
    day = report.primary_per_day["2026-04-19"]
    assert "pinball_p50" in day
    assert "p90_coverage" in day


def test_compute_bucket_metrics_assigns_correctly():
    # 5 rows: waits = [30, 120, 600, 3600, 90]
    # Buckets: <1m, 1-5m, 5-30m, 30m+, 1-5m
    y_true = np.array([30.0, 120.0, 600.0, 3600.0, 90.0])
    y_pred = np.array([25.0, 130.0, 500.0, 3000.0, 95.0])
    from src.evaluate import compute_bucket_metrics
    buckets = compute_bucket_metrics(y_true, y_pred)
    assert buckets["<1m"]["mae"]["eligible_n"] == 1
    assert buckets["1-5m"]["mae"]["eligible_n"] == 2
    assert buckets["5-30m"]["mae"]["eligible_n"] == 1
    assert buckets["30m+"]["mae"]["eligible_n"] == 1


def test_compute_bucket_metrics_can_report_guarded_p90_coverage():
    # True 30m+ wait = 2000s. Raw model p90 undershoots badly (1500s -> miss).
    # The production guardrail floor (baseline p90 = 2500s) would have
    # covered it -- what's actually served differs from the raw model head.
    # compute_bucket_metrics had no way to report this: it only ever accepted
    # the raw model p90, so the bucket-level miss rate (the walk-forward
    # ablation's primary gate metric) never reflected what's served live.
    y_true = np.array([2000.0])
    y_pred = np.array([1800.0])
    y_pred_p90_raw = np.array([1500.0])
    y_pred_p90_guarded = np.array([2500.0])

    from src.evaluate import compute_bucket_metrics

    raw_only = compute_bucket_metrics(y_true, y_pred, y_pred_p90=y_pred_p90_raw)
    assert raw_only["30m+"]["p90_coverage"]["covered_n"] == 0

    with_guard = compute_bucket_metrics(
        y_true, y_pred,
        y_pred_p90=y_pred_p90_raw,
        y_pred_p90_guarded=y_pred_p90_guarded,
    )
    # Raw coverage is unchanged...
    assert with_guard["30m+"]["p90_coverage"]["covered_n"] == 0
    # ...but the guarded view (what's actually served) shows it covered.
    assert with_guard["30m+"]["p90_coverage_guarded"]["covered_n"] == 1
    assert with_guard["30m+"]["p90_coverage_guarded"]["eligible_n"] == 1


def test_aggregate_buckets_aggregates_guarded_p90_and_derives_miss_rate():
    from src.evaluate import aggregate_buckets

    d1 = {
        "30m+": {
            "mae": {"eligible_n": 2, "sum_abs_error": 100.0},
            "within_2x": {"eligible_n": 2, "hit_n": 1},
            "p90_coverage_guarded": {"eligible_n": 2, "covered_n": 2},
        },
    }
    d2 = {
        "30m+": {
            "mae": {"eligible_n": 2, "sum_abs_error": 100.0},
            "within_2x": {"eligible_n": 2, "hit_n": 1},
            "p90_coverage_guarded": {"eligible_n": 2, "covered_n": 0},
        },
    }
    agg = aggregate_buckets([d1, d2])
    assert agg["30m+"]["p90_coverage_guarded"]["eligible_n"] == 4
    assert agg["30m+"]["p90_coverage_guarded"]["covered_n"] == 2
    assert np.isclose(agg["30m+"]["p90_coverage_guarded_rate"], 0.5)
    assert np.isclose(agg["30m+"]["p90_miss_rate_guarded"], 0.5)


def test_aggregate_buckets_sums_raw_counts():
    from src.evaluate import aggregate_buckets
    d1 = {
        "<1m":   {"mae": {"eligible_n": 10, "sum_abs_error": 50.0},
                  "within_2x": {"eligible_n": 10, "hit_n": 8}},
        "1-5m":  {"mae": {"eligible_n": 5, "sum_abs_error": 100.0},
                  "within_2x": {"eligible_n": 5, "hit_n": 3}},
        "5-30m": {"mae": {"eligible_n": 0, "sum_abs_error": 0.0},
                  "within_2x": {"eligible_n": 0, "hit_n": 0}},
        "30m+":  {"mae": {"eligible_n": 0, "sum_abs_error": 0.0},
                  "within_2x": {"eligible_n": 0, "hit_n": 0}},
    }
    d2 = {
        "<1m":   {"mae": {"eligible_n": 20, "sum_abs_error": 120.0},
                  "within_2x": {"eligible_n": 20, "hit_n": 14}},
        "1-5m":  {"mae": {"eligible_n": 0, "sum_abs_error": 0.0},
                  "within_2x": {"eligible_n": 0, "hit_n": 0}},
        "5-30m": {"mae": {"eligible_n": 0, "sum_abs_error": 0.0},
                  "within_2x": {"eligible_n": 0, "hit_n": 0}},
        "30m+":  {"mae": {"eligible_n": 0, "sum_abs_error": 0.0},
                  "within_2x": {"eligible_n": 0, "hit_n": 0}},
    }
    agg = aggregate_buckets([d1, d2])
    assert agg["<1m"]["mae"]["eligible_n"] == 30
    assert agg["<1m"]["mae"]["sum_abs_error"] == 170.0
    assert np.isclose(agg["<1m"]["mae_s"], 170.0 / 30)
    assert agg["<1m"]["within_2x"]["hit_n"] == 22


def test_load_prior_manifest(tmp_path):
    from src.evaluate import load_prior_manifest
    run_dir = tmp_path / "2026-04-23"
    run_dir.mkdir()
    (run_dir / "wait_time_manifest.json").write_text('{"target": "wait_time", "evaluation": {"primary": {"aggregate": {"mae_s": 539.1}}}}')
    m = load_prior_manifest(run_dir, "wait_time")
    assert m is not None
    assert m["target"] == "wait_time"
    assert m["evaluation"]["primary"]["aggregate"]["mae_s"] == 539.1


def test_load_prior_manifest_missing_returns_none(tmp_path):
    from src.evaluate import load_prior_manifest
    m = load_prior_manifest(tmp_path, "wait_time")
    assert m is None
