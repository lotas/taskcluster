import numpy as np
import pandas as pd
import pytest

from scripts import group_eta_baselines as g
from scripts import group_eta_inflight as b2


def _cdf_uniform(lo, hi):
    """CDF on g.GRID of a uniform offset distribution on [lo, hi]."""
    return np.clip((g.GRID - lo) / (hi - lo), 0.0, 1.0)


def test_conditional_quantile_matches_truncated_uniform():
    F = np.stack([_cdf_uniform(1000, 3000)] * 3)
    e = np.array([0.0, 2000.0, 2600.0])
    p50 = b2.conditional_quantiles(F, e, 0.5)
    # unconditional median 2000; given > 2000 -> 2500; given > 2600 -> 2800 (grid ~2.2% steps)
    assert p50 == pytest.approx([2000, 2500, 2800], rel=0.03)
    p90 = b2.conditional_quantiles(F, e, 0.9)
    assert (p90 >= p50).all()
    assert (p50 >= e).all()


def test_conditional_quantile_past_history_uses_tail_multiplier():
    F = np.stack([_cdf_uniform(1000, 3000)])
    out = b2.conditional_quantiles(F, np.array([5000.0]), 0.9)
    assert out[0] == pytest.approx(5000 * b2.TAIL_MULT[0.9])


def _model():
    # two keys: "fast" ~ U(100, 300) s, "slow" ~ U(1000, 5000) s (family "try")
    rng = np.random.default_rng(0)
    rows = []
    for name, lo, hi in (("fast", 100, 300), ("slow", 1000, 5000)):
        for i, off in enumerate(rng.uniform(lo, hi, 400)):
            rows.append(dict(gid=i, repo_family="try", normalized_name=name, offset=off))
    tk = pd.DataFrame(rows)
    for c in ("repo_family", "normalized_name"):
        tk[c] = tk[c].astype("category")
    return g.fit_b1(tk)


def test_predict_b2_drops_resolved_tasks_and_conditions_on_elapsed():
    model = _model()
    groups = pd.DataFrame({"t_pred": [100.0, 100.0], "t0": [0.0, 0.0]}, index=[1, 2])
    known = pd.DataFrame({
        "gid": [1, 1, 2, 2],
        "repo_family": pd.Categorical(["try"] * 4),
        "normalized_name": pd.Categorical(["fast", "slow", "fast", "slow"]),
        "t0": [0.0] * 4,
        # group 1: slow task still running at tau=100+2900=3000; group 2: everything done by 2000
        "last_resolved": [250.0, 4000.0, 250.0, 2000.0],
    })
    out = b2.predict_b2(model, known, groups, c_s=2900.0)
    assert list(out.index) == [1]
    # slow | > 3000 on U(1000, 5000): median 4000, p90 4800
    assert out.loc[1, "p50"] == pytest.approx(4000, rel=0.05)
    assert out.loc[1, "p90"] == pytest.approx(4800, rel=0.05)


def test_remaining_factors_calibrate_remaining_time():
    rng = np.random.default_rng(3)
    n = 1000
    groups = pd.DataFrame({"repo_family": ["try"] * n, "span": 1000 + rng.uniform(100, 300, n)}, index=range(n))
    e = pd.Series(1000.0, index=groups.index)
    pred = pd.DataFrame({"p50": np.full(n, 2000.0), "p90": np.full(n, 5000.0)}, index=groups.index)  # 5x / 20x too wide
    f = b2.remaining_factors(pred, groups, e)
    out = b2.apply_remaining_factors(pred, groups, e, f)
    R = groups["span"] - e
    assert np.mean(R <= out["p90"] - e) == pytest.approx(0.9, abs=0.01)
    assert np.median(R / (out["p50"] - e)) == pytest.approx(1.0, abs=0.02)
    assert (out["p90"] >= out["p50"]).all()
