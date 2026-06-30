from pathlib import Path
from src.config import load_config

CUMULATIVE = {
  "configs/wait_qctx_a_capacity.yaml": ["running_per_capacity","backlog_coverage_ratio"],
  "configs/wait_qctx_b_priority.yaml": ["running_per_capacity","pending_higher_priority_same_queue","pending_higher_or_equal_per_capacity"],
  "configs/wait_qctx_c_flow.yaml": ["pending_higher_priority_same_queue","arrivals_15m_same_queue","starts_higher_or_equal_15m_same_queue"],
}

def test_ablation_configs_enable_qctx_and_are_cumulative():
    for path, must_have in CUMULATIVE.items():
        c = load_config(Path(path))
        assert c.queue_context_features and c.queue_context_features.get("enabled") is True, path
        assert "capacity_null_reason" in c.categorical_features, path
        assert "capacity_null_reason" not in c.numeric_features, path
        for f in must_have:
            assert f in c.numeric_features, f"{path} missing {f}"

def test_capacity_step_has_no_priority_features():
    c = load_config(Path("configs/wait_qctx_a_capacity.yaml"))
    assert "pending_higher_priority_same_queue" not in c.numeric_features
    assert "arrivals_15m_same_queue" not in c.numeric_features

def test_priority_step_has_no_flow_features():
    c = load_config(Path("configs/wait_qctx_b_priority.yaml"))
    assert "pending_higher_priority_same_queue" in c.numeric_features
    assert "arrivals_15m_same_queue" not in c.numeric_features
