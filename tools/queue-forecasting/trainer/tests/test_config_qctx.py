from pathlib import Path

from src.config import load_config


def test_qctx_config_parses_and_lists_features():
    c = load_config(Path("configs/wait_time_residual_throughput_filtered_baseline_qctx.yaml"))
    assert c.queue_context_features and c.queue_context_features.get("enabled") is True
    # 20 numeric queue-context features present
    for f in [
        "pending_higher_priority_same_queue",
        "backlog_coverage_ratio",
        "pending_release_beta_higher_or_equal_same_queue",
        "capacity_sample_age_s",
    ]:
        assert f in c.numeric_features, f
    # 2 categorical
    assert "repo_family" in c.categorical_features
    assert "capacity_null_reason" in c.categorical_features
    # capacity_null_reason must NOT be numeric (categorical-only)
    assert "capacity_null_reason" not in c.numeric_features


def test_production_config_has_no_queue_context():
    c = load_config(Path("configs/wait_time_residual_throughput_filtered_baseline.yaml"))
    assert not (getattr(c, "queue_context_features", None) or {})  # disabled/absent
