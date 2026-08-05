import math

import numpy as np
import pandas as pd
import pytest

import src.hazard_model as hazard_model_module
from src.hazard_labels import DEFAULT_BIN_EDGES_MINUTES, build_bin_risk_and_labels
from src.hazard_model import DiscreteHazardModel

SCENARIO_A_EDGES = [0, 2, 5, 15, 40, math.inf]  # minutes; matched to a ~10min-mean exponential


def _synthetic_wait_df(n, rate, seed, cutoff_margin, censor=False):
    rng = np.random.default_rng(seed)
    pending_at = pd.Series(pd.Timestamp("2026-01-01T00:00:00Z") + pd.to_timedelta(rng.uniform(0, 14 * 86400, n), unit="s"))
    wait_s = rng.exponential(scale=1.0 / rate, size=n)
    cutoff = pending_at.max() + cutoff_margin
    resolved_at = pd.Series(pd.NaT, index=range(n))
    y = wait_s.copy()
    if censor:
        started_at = pending_at + pd.to_timedelta(wait_s, unit="s")
        still_pending = started_at > cutoff
        y[still_pending] = np.nan
    df = pd.DataFrame({
        "pending_at": pending_at, "resolved_at": resolved_at, "y": y,
        "x": rng.normal(size=n),
    })
    return df, cutoff


def _split(df, train_frac=0.7, val_frac=0.15):
    n = len(df)
    n_tr, n_va = int(n * train_frac), int(n * val_frac)
    return (
        df.iloc[:n_tr].reset_index(drop=True),
        df.iloc[n_tr:n_tr + n_va].reset_index(drop=True),
        df.iloc[n_tr + n_va:].reset_index(drop=True),
    )


@pytest.fixture(scope="module")
def scenario_a():
    """Short exponential wait (mean 10min), no censoring -- p50/p90 should
    land entirely within the finite bin grid. Fit once, reused by every
    test in this module that needs it (LightGBM training dominates test
    time, so this amortizes it).

    While fitting, transiently wraps lgb.Dataset to record the row count of
    every *training* Dataset constructed (identified by reference=None --
    the val Dataset always passes reference=train_set), so
    test_fit_trains_each_booster_only_on_its_bin_risk_set can check those
    counts against the risk-set sizes without needing a second, separate
    fit. This installed lightgbm (4.6.0) has no Booster.num_data(), so a
    Dataset-level spy is the most direct substitute available."""
    rate = 1.0 / 600.0
    df, cutoff = _synthetic_wait_df(2000, rate, seed=7, cutoff_margin=pd.Timedelta(hours=6))
    train_df, val_df, hold_df = _split(df)
    model = DiscreteHazardModel(
        edges_minutes=SCENARIO_A_EDGES,
        params={"n_estimators": 25, "min_data_in_leaf": 15, "early_stopping_rounds": 5},
    )

    lgb = hazard_model_module.lgb
    real_dataset_cls = lgb.Dataset
    train_set_sizes: list[int] = []

    class _SizeRecordingDataset(real_dataset_cls):
        def __init__(self, *args, **kwargs):
            if kwargs.get("reference") is None:
                train_set_sizes.append(len(kwargs.get("label")))
            super().__init__(*args, **kwargs)

    lgb.Dataset = _SizeRecordingDataset
    try:
        model.fit(
            train_df[["x"]], train_df[["pending_at", "resolved_at"]], train_df["y"], cutoff,
            val_df[["x"]], val_df[["pending_at", "resolved_at"]], val_df["y"], cutoff,
        )
    finally:
        lgb.Dataset = real_dataset_cls

    return model, hold_df, rate, train_df, cutoff, train_set_sizes


def test_fit_creates_one_booster_per_bin(scenario_a):
    model, _, _, _, _, _ = scenario_a
    assert len(model.boosters) == len(SCENARIO_A_EDGES) - 1


def test_fit_sets_feature_names_and_nonnegative_tail_rate(scenario_a):
    model, _, _, _, _, _ = scenario_a
    assert model.feature_names_ == ["x"]
    assert model.tail_rate_ is not None and model.tail_rate_ >= 0.0


def test_fit_trains_each_booster_only_on_its_bin_risk_set(scenario_a):
    """The whole point of the per-bin loop is that booster i only ever sees
    bin i's at-risk subset, not the full training frame -- verify that by
    recomputing the at-risk matrix for the same train split/cutoff the
    fixture fit on, and checking it against the actual per-bin training
    Dataset row counts the fixture recorded (see scenario_a's Dataset spy;
    this installed lightgbm has no Booster.num_data() to read back after
    the fact)."""
    model, _, _, train_df, cutoff, train_set_sizes = scenario_a
    at_risk_tr, _ = build_bin_risk_and_labels(
        train_df["pending_at"], train_df["resolved_at"], train_df["y"], cutoff, SCENARIO_A_EDGES,
    )
    expected = [int(at_risk_tr[:, i].sum()) for i in range(at_risk_tr.shape[1])]
    assert len(train_set_sizes) == len(model.boosters) == len(expected)
    assert train_set_sizes == expected


def test_fit_raises_on_empty_training_risk_set():
    """3-bin edges [0,5)[5,10)[10,inf) minutes, but every row resolves
    within the first 61 seconds -- bins 1 and 2 never get any at-risk rows."""
    n = 200
    pending_at = pd.Series(pd.Timestamp("2026-01-01T00:00:00Z") + pd.to_timedelta(np.arange(n), unit="s"))
    y = pd.Series(np.full(n, 60.0))
    resolved_at = pd.Series(pd.NaT, index=range(n))
    X = pd.DataFrame({"x": np.zeros(n)})
    cutoff = pending_at.max() + pd.Timedelta(hours=1)
    model = DiscreteHazardModel(edges_minutes=[0, 5, 10, math.inf], params={"n_estimators": 5})
    with pytest.raises(RuntimeError, match="bin 1 has an empty training risk set"):
        model.fit(X, pd.DataFrame({"pending_at": pending_at, "resolved_at": resolved_at}), y, cutoff,
                   X, pd.DataFrame({"pending_at": pending_at, "resolved_at": resolved_at}), y, cutoff)


def test_fit_raises_on_empty_validation_risk_set():
    """Train has some rows reaching bin 1 ([5,10)min), but val has none --
    the empty-risk-set guard must fire on the validation side too, not
    just training."""
    n = 200
    pending_at = pd.Series(pd.Timestamp("2026-01-01T00:00:00Z") + pd.to_timedelta(np.arange(n), unit="s"))
    resolved_at = pd.Series(pd.NaT, index=range(n))
    X = pd.DataFrame({"x": np.zeros(n)})
    cutoff = pending_at.max() + pd.Timedelta(hours=1)
    y_train = pd.Series(np.where(np.arange(n) % 2 == 0, 400.0, 60.0))
    y_val = pd.Series(np.full(n, 60.0))
    model = DiscreteHazardModel(edges_minutes=[0, 5, 10, math.inf], params={"n_estimators": 5})
    with pytest.raises(RuntimeError, match="bin 1 has an empty validation risk set"):
        model.fit(X, pd.DataFrame({"pending_at": pending_at, "resolved_at": resolved_at}), y_train, cutoff,
                   X, pd.DataFrame({"pending_at": pending_at, "resolved_at": resolved_at}), y_val, cutoff)


@pytest.fixture(scope="module")
def scenario_b():
    """Heavy-tailed wait (mean 600min), with genuine censoring -- exercises
    the real DEFAULT_BIN_EDGES_MINUTES terminal bin and tail_rate_
    extrapolation."""
    rate = 1.0 / 36000.0
    df, cutoff = _synthetic_wait_df(1500, rate, seed=11, cutoff_margin=pd.Timedelta(days=10), censor=True)
    train_df, val_df, hold_df = _split(df)
    model = DiscreteHazardModel(
        edges_minutes=DEFAULT_BIN_EDGES_MINUTES,
        params={"n_estimators": 20, "min_data_in_leaf": 10, "early_stopping_rounds": 5},
    )
    model.fit(
        train_df[["x"]], train_df[["pending_at", "resolved_at"]], train_df["y"], cutoff,
        val_df[["x"]], val_df[["pending_at", "resolved_at"]], val_df["y"], cutoff,
    )
    return model, hold_df, rate


def test_predict_hazard_shape_and_range(scenario_a):
    model, hold_df, rate, _, _, _ = scenario_a
    hazard = model.predict_hazard(hold_df[["x"]])
    assert hazard.shape == (len(hold_df), len(SCENARIO_A_EDGES) - 1)
    assert np.all((hazard >= 0.0) & (hazard <= 1.0))


def test_predict_survival_grid_is_nonincreasing(scenario_a):
    model, hold_df, rate, _, _, _ = scenario_a
    survival = model.predict_survival_grid(hold_df[["x"]])
    assert np.all(np.diff(survival, axis=1) <= 1e-9)


@pytest.mark.parametrize("bad_q", [0.0, 1.0, -0.1, 1.5])
def test_predict_quantile_rejects_invalid_q(scenario_a, bad_q):
    model, hold_df, rate, _, _, _ = scenario_a
    with pytest.raises(ValueError, match="q must be in"):
        model.predict_quantile(hold_df[["x"]], bad_q)


def test_predict_quantile_monotonic_in_q(scenario_a):
    model, hold_df, rate, _, _, _ = scenario_a
    p50 = model.predict_quantile(hold_df[["x"]], 0.5)
    p90 = model.predict_quantile(hold_df[["x"]], 0.9)
    assert np.all(p90 >= p50)


def test_predict_quantile_recovers_finite_grid_quantiles(scenario_a):
    """The whole point of the constant-hazard-within-bin interpolation:
    for a genuinely exponential population with quantiles landing inside
    the finite bin grid, predict_quantile should recover them closely."""
    model, hold_df, rate, _, _, _ = scenario_a
    p50 = model.predict_quantile(hold_df[["x"]], 0.5)
    p90 = model.predict_quantile(hold_df[["x"]], 0.9)
    true_p50 = -math.log(0.5) / rate
    true_p90 = -math.log(0.1) / rate
    assert abs(p50.mean() - true_p50) / true_p50 < 0.25
    assert abs(p90.mean() - true_p90) / true_p90 < 0.25


def test_predict_quantile_tail_recovers_beyond_last_edge(scenario_b):
    """Heavy-tailed population: p95 should mostly fall past the last
    finite edge and be recovered via the exponential tail_rate_."""
    model, hold_df, rate = scenario_b
    p95 = model.predict_quantile(hold_df[["x"]], 0.95)
    true_p95 = -math.log(0.05) / rate
    t_last_s = 480 * 60
    assert (p95 > t_last_s).mean() > 0.5
    assert abs(p95.mean() - true_p95) / true_p95 < 0.3


def test_predict_quantile_degenerate_tail_returns_inf(scenario_a):
    """A zero (degenerate) tail rate means the model has no basis for
    extrapolating past the last finite edge -- deep quantiles reaching
    the tail must come back as inf, not a silently wrong finite number."""
    model, hold_df, rate, _, _, _ = scenario_a
    original_rate = model.tail_rate_
    try:
        model.tail_rate_ = 0.0
        p999 = model.predict_quantile(hold_df[["x"]], 0.999)
        assert np.isinf(p999).any()
    finally:
        model.tail_rate_ = original_rate


def test_predict_quantile_matches_hand_computed_crossing_time():
    """White-box check that bypasses LightGBM entirely: stub predict_hazard
    with a fixed, known hazard matrix and assert predict_quantile's output
    matches the closed-form constant-hazard-within-bin crossing time
    computed independently right here (not by calling into predict_quantile
    or any other production code path), to machine precision.

    The rest of this module only checks predict_quantile against loose
    (25-30%) tolerances on real LightGBM output, which validates the
    end-to-end pipeline but would not reliably catch a regression in the
    interpolation arithmetic itself -- e.g. an off-by-one in which bin
    boundary is used, a sign flip, or a revert to the old (rejected) buggy
    linear-in-S(t) interpolation, which was within striking distance of
    passing a 25-30% check for some inputs.

    Bin grid: edges_minutes=[0, 10, 20, inf] -> edges_s=[0, 600, 1200, inf]:
    two finite bins of width 600s each, terminal bin starting at
    t_last=1200s. Fixed per-row hazard [h0, h1, h_term] = [0.2, 0.5, 0.9]
    (h_term is irrelevant -- predict_quantile never reads the terminal
    bin's own hazard, only tail_rate_) gives finite survival
    S(600) = 1 - h0 = 0.8, S(1200) = S(600) * (1 - h1) = 0.8 * 0.5 = 0.4.
    """
    edges_minutes = [0, 10, 20, math.inf]
    width = 600.0
    t_last = 1200.0
    h0, h1 = 0.2, 0.5
    s0 = 1.0 - h0          # S(600) = 0.8
    s1 = s0 * (1.0 - h1)   # S(1200) = 0.4

    model = DiscreteHazardModel(edges_minutes=edges_minutes)
    model.predict_hazard = lambda X: np.array([[h0, h1, 0.9]])
    X = pd.DataFrame({"x": [0.0]})

    # (a) mid-bin finite quantile: s_star=0.9 lands strictly inside bin 0
    # (between S(0)=1.0 and S(600)=0.8), not on a boundary.
    s_star_a = 0.9
    lam0 = -math.log(s0 / 1.0) / width
    expected_a = 0.0 + math.log(1.0 / s_star_a) / lam0
    got_a = model.predict_quantile(X, 1.0 - s_star_a)[0]
    assert got_a == pytest.approx(expected_a, rel=1e-9)

    # (c) exact boundary: s_star = S(600) = 0.8 -- continuity check, must
    # land exactly on the boundary time 600.0, not drift into bin 1.
    s_star_c = s0
    expected_c = 600.0
    got_c = model.predict_quantile(X, 1.0 - s_star_c)[0]
    assert got_c == pytest.approx(expected_c, rel=1e-9)

    # (b) tail quantile: s_star=0.1 falls below S(1200)=0.4, so it must be
    # read off the exponential tail using a tail_rate_ set directly here
    # (bypassing fit_exponential_tail_rate entirely).
    model.tail_rate_ = 0.001
    s_star_b = 0.1
    expected_b = t_last - math.log(s_star_b / s1) / model.tail_rate_
    got_b = model.predict_quantile(X, 1.0 - s_star_b)[0]
    assert got_b == pytest.approx(expected_b, rel=1e-9)
