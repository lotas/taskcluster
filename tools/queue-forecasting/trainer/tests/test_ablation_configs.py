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


def _yaml(path):
    import yaml
    with open(path) as fh:
        return yaml.safe_load(fh)


PROMOTED = "configs/wait_time_residual_throughput_filtered_baseline.yaml"
POOL = "configs/wait_time_residual_throughput_filtered_baseline_pool.yaml"


def test_the_pool_config_is_a_one_variable_delta_from_the_promoted_one():
    """The property the existing experiment history does NOT have.

    `wait_time_residual_velocity` also carried `pool_kind`/`provider_type`, also
    scored worse, and was dropped -- while differing from the candidate in six
    keys and about nineteen features, including losing Policy B, which is the
    change that fixed the regime fragility. Its numbers cannot attribute anything
    to the pool columns.

    So this asserts the delta, not the result: two categoricals added, one block
    added to join them, and NOTHING removed or changed. A future edit that
    bundles a second idea in here fails this test, which is the point -- the cost
    of a confounded config is a cohort nobody can interpret, paid twenty minutes
    at a time.
    """
    ref, pool = _yaml(PROMOTED), _yaml(POOL)

    assert set(pool["categorical_features"]) - set(ref["categorical_features"]) \
        == {"pool_kind", "provider_type"}
    assert not set(ref["categorical_features"]) - set(pool["categorical_features"])
    assert pool["numeric_features"] == ref["numeric_features"]

    # `velocity_features` is what joins the pool dimension on. It must not bring
    # its trailing-average machinery with it.
    assert pool["velocity_features"]["enabled"] is True
    assert pool["velocity_features"]["trailing_windows_minutes"] == []

    differing = {k for k in set(ref) | set(pool)
                 if ref.get(k, "<absent>") != pool.get(k, "<absent>")}
    assert differing == {"categorical_features", "velocity_features"}, differing


def test_the_pool_config_lists_none_of_velocitys_capacity_numerics():
    """Enabling velocity COMPUTES those columns; listing them would make the
    model see them, which is the other nine variables this config exists to
    leave out."""
    pool = _yaml(POOL)
    velocity = _yaml("configs/wait_time_residual_velocity.yaml")
    capacity = set(velocity["numeric_features"]) - set(
        _yaml(PROMOTED)["numeric_features"])
    assert capacity, "the velocity config no longer adds any numerics"
    assert not capacity & set(pool["numeric_features"])
