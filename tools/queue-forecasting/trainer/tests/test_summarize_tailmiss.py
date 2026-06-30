"""Tests for the 30m+ wait p90 miss column in the walk-forward summarizer.

Covers both the source metric (per-bucket p90 coverage/miss in evaluate.py)
and the summarizer's extraction helper, without needing real walk-forward
manifest files on disk.
"""
import numpy as np


# --- summarizer extraction helper -----------------------------------------

def _eval_obj(target="wait_time", *, bucket_30m=None):
    """A manifest-shaped dict matching evaluate.py / train.py output."""
    buckets = {
        "<1m":   {"mae_s": 40.0,   "within_2x_rate": 0.45},
        "1-5m":  {"mae_s": 120.0,  "within_2x_rate": 0.65},
        "5-30m": {"mae_s": 500.0,  "within_2x_rate": 0.55},
    }
    if bucket_30m is not None:
        buckets["30m+"] = bucket_30m
    return {
        "target": target,
        "evaluation": {"primary": {"buckets_aggregate": buckets}},
    }


def test_extract_30mplus_wait_p90_miss_basic():
    import scripts.summarize_walk_forward as swf
    # 4 of 10 actuals exceeded the predicted p90 -> miss rate 0.4
    obj = _eval_obj(bucket_30m={
        "p90_coverage": {"eligible_n": 10, "covered_n": 6},
        "p90_coverage_rate": 0.6,
        "p90_miss_rate": 0.4,
    })
    assert swf.extract_30mplus_wait_p90_miss(obj) == 0.4


def test_extract_30mplus_wait_p90_miss_missing_bucket():
    import scripts.summarize_walk_forward as swf
    # No 30m+ bucket at all -> None
    assert swf.extract_30mplus_wait_p90_miss(_eval_obj()) is None
    # 30m+ bucket present but no p90_miss_rate (older manifest) -> None
    obj = _eval_obj(bucket_30m={"mae_s": 6000.0, "within_2x_rate": 0.3})
    assert swf.extract_30mplus_wait_p90_miss(obj) is None


def test_extract_30mplus_wait_p90_miss_non_wait_target():
    import scripts.summarize_walk_forward as swf
    obj = _eval_obj(target="run_duration", bucket_30m={"p90_miss_rate": 0.4})
    assert swf.extract_30mplus_wait_p90_miss(obj) is None


def test_extract_30mplus_wait_p90_miss_nan():
    import scripts.summarize_walk_forward as swf
    obj = _eval_obj(bucket_30m={"p90_miss_rate": float("nan")})
    assert swf.extract_30mplus_wait_p90_miss(obj) is None


# --- source metric in evaluate.py ------------------------------------------

def test_compute_bucket_metrics_emits_p90_coverage():
    from src.evaluate import compute_bucket_metrics
    # All in the 30m+ bucket (>= 1800s). 4 of 5 actuals exceed predicted p90.
    y_true = np.array([2000.0, 3000.0, 4000.0, 5000.0, 6000.0])
    # p90 covers only the first row (2000 <= 2500); the other 4 are misses.
    y_pred_p90 = np.array([2500.0, 100.0, 100.0, 100.0, 100.0])
    y_pred = y_true.copy()
    buckets = compute_bucket_metrics(y_true, y_pred, y_pred_p90=y_pred_p90)
    cov = buckets["30m+"]["p90_coverage"]
    assert cov["eligible_n"] == 5
    assert cov["covered_n"] == 1
    # Buckets with no rows still carry a zeroed p90_coverage entry.
    assert buckets["<1m"]["p90_coverage"] == {"eligible_n": 0, "covered_n": 0}


def test_compute_bucket_metrics_no_p90_omits_coverage():
    from src.evaluate import compute_bucket_metrics
    y_true = np.array([2000.0, 3000.0])
    buckets = compute_bucket_metrics(y_true, y_true.copy())
    assert "p90_coverage" not in buckets["30m+"]


def test_aggregate_buckets_derives_p90_miss_rate():
    from src.evaluate import aggregate_buckets
    # Two days, 30m+ only. Day1: 10 eligible, 6 covered. Day2: 10 eligible, 5 covered.
    # Combined: 20 eligible, 11 covered -> miss = 1 - 11/20 = 0.45.
    def _day(covered):
        return {
            "30m+": {
                "mae":          {"eligible_n": 10, "sum_abs_error": 0.0},
                "within_2x":    {"eligible_n": 10, "hit_n": 5},
                "p90_coverage": {"eligible_n": 10, "covered_n": covered},
            }
        }
    agg = aggregate_buckets([_day(6), _day(5)])
    b = agg["30m+"]
    assert b["p90_coverage"]["eligible_n"] == 20
    assert b["p90_coverage"]["covered_n"] == 11
    assert np.isclose(b["p90_coverage_rate"], 11 / 20)
    assert np.isclose(b["p90_miss_rate"], 1.0 - 11 / 20)


def test_aggregate_buckets_no_p90_when_absent():
    from src.evaluate import aggregate_buckets
    day = {
        "30m+": {
            "mae":       {"eligible_n": 10, "sum_abs_error": 0.0},
            "within_2x": {"eligible_n": 10, "hit_n": 5},
        }
    }
    agg = aggregate_buckets([day])
    assert "p90_miss_rate" not in agg["30m+"]
    assert "p90_coverage" not in agg["30m+"]
