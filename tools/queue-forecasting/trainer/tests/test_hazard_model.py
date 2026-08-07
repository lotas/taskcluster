import math

import numpy as np
import pandas as pd
import pytest

import src.hazard_model as hazard_model_module
from src.hazard_labels import DEFAULT_BIN_EDGES_MINUTES, build_bin_risk_and_labels
from src.hazard_model import FALLBACK_BOOST_ROUNDS, DiscreteHazardModel

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


def _thin_val_setup(n=300):
    """Bins are [0,5)[5,10)[10,inf) minutes. Training waits span all three
    (60s / 400s / 700s); every validation row resolves in 60s, so bins 1
    and 2 have an empty validation risk set but a populated training one --
    the real shape of a quiet weekend validation day."""
    pending_at = pd.Series(pd.Timestamp("2026-01-01T00:00:00Z") + pd.to_timedelta(np.arange(n), unit="s"))
    resolved_at = pd.Series(pd.NaT, index=range(n))
    meta = pd.DataFrame({"pending_at": pending_at, "resolved_at": resolved_at})
    X = pd.DataFrame({"x": np.arange(n, dtype=float) % 7})
    cutoff = pending_at.max() + pd.Timedelta(hours=1)
    y_train = pd.Series(np.select(
        [np.arange(n) % 3 == 0, np.arange(n) % 3 == 1], [60.0, 400.0], default=700.0))
    y_val = pd.Series(np.full(n, 60.0))
    return X, meta, y_train, y_val, cutoff


def test_capacity_drops_for_small_risk_sets():
    """Bins above the row threshold keep the configured capacity; bins below
    it drop to the small-bin tier. Guards the 2026-08-06 finding that a flat
    num_leaves=63 drove bin 6 to val AUC 0.479 (worse than chance)."""
    m = DiscreteHazardModel(params={"num_leaves": 63, "min_data_in_leaf": 50})
    assert m._capacity_for(2_802_380) == (63, 50)   # bin 0
    assert m._capacity_for(237_894) == (63, 50)     # bin 2, just above threshold
    assert m._capacity_for(125_126) == (31, 100)    # bin 3, just below
    assert m._capacity_for(14_797) == (31, 100)     # bin 6


def test_capacity_tiers_are_configurable():
    m = DiscreteHazardModel(params={
        "num_leaves": 63, "min_data_in_leaf": 50,
        "small_bin_threshold_rows": 1000,
        "small_bin_num_leaves": 7, "small_bin_min_data_in_leaf": 500,
    })
    assert m._capacity_for(5000) == (63, 50)
    assert m._capacity_for(999) == (7, 500)


def test_fit_records_capacity_actually_used_per_bin():
    """bin_fit must report the capacity that ran, not the config's headline
    values -- otherwise a manifest silently misdescribes the small bins."""
    X, meta, y_train, y_val, cutoff = _thin_val_setup()
    m = DiscreteHazardModel(
        edges_minutes=[0, 5, 10, math.inf],
        params={"n_estimators": 5, "num_leaves": 63, "min_data_in_leaf": 50,
                "small_bin_threshold_rows": 200, "small_bin_num_leaves": 7,
                "small_bin_min_data_in_leaf": 500},
    )
    m.fit(X, meta, y_train, cutoff, X, meta, y_val, cutoff)
    by_bin = {b["bin"]: b for b in m.bin_fit_}
    assert by_bin[0]["n_train_rows"] >= 200 and by_bin[0]["num_leaves"] == 63
    assert by_bin[2]["n_train_rows"] < 200 and by_bin[2]["num_leaves"] == 7
    assert by_bin[2]["min_data_in_leaf"] == 500


def test_fit_degrades_instead_of_failing_on_thin_validation_risk_set():
    """A validation risk set too thin to early-stop on is a normal data
    condition -- a quiet weekend validation day leaves the later bins with
    nothing, and for the terminal bin predict_quantile never even reads the
    resulting booster. Abandoning an otherwise-complete run (~40min of data
    loading) over it is the wrong trade. Train the bin at a fixed round
    count and record the degradation instead."""
    X, meta, y_train, y_val, cutoff = _thin_val_setup()
    model = DiscreteHazardModel(edges_minutes=[0, 5, 10, math.inf], params={"n_estimators": 5})
    model.fit(X, meta, y_train, cutoff, X, meta, y_val, cutoff)

    assert len(model.boosters) == 3
    assert [d["bin"] for d in model.degraded_bins_] == [1, 2]
    assert all(d["n_val_rows"] == 0 for d in model.degraded_bins_)
    # Still a usable model: quantiles come out finite and ordered.
    p50, p90 = model.predict_quantile(X, 0.5), model.predict_quantile(X, 0.9)
    assert np.all(np.isfinite(p50)) and np.all(p90 >= p50)


def test_fit_still_raises_on_empty_training_risk_set_when_val_is_thin():
    """Degrading on a thin *validation* set must not soften the training-side
    guard -- with no training rows there is nothing to fit at all."""
    n = 200
    pending_at = pd.Series(pd.Timestamp("2026-01-01T00:00:00Z") + pd.to_timedelta(np.arange(n), unit="s"))
    meta = pd.DataFrame({"pending_at": pending_at, "resolved_at": pd.Series(pd.NaT, index=range(n))})
    X = pd.DataFrame({"x": np.zeros(n)})
    cutoff = pending_at.max() + pd.Timedelta(hours=1)
    y = pd.Series(np.full(n, 60.0))
    model = DiscreteHazardModel(edges_minutes=[0, 5, 10, math.inf], params={"n_estimators": 5})
    with pytest.raises(RuntimeError, match="bin 1 has an empty training risk set"):
        model.fit(X, meta, y, cutoff, X, meta, y, cutoff)


def test_thin_validation_threshold_is_configurable_and_borrows_best_iteration():
    """A bin below min_val_rows_for_early_stop is trained at the median
    best_iteration of the bins that did early-stop, not at n_estimators."""
    X, meta, y_train, y_val, cutoff = _thin_val_setup()
    model = DiscreteHazardModel(
        edges_minutes=[0, 5, 10, math.inf],
        params={"n_estimators": 5, "min_val_rows_for_early_stop": 10},
    )
    model.fit(X, meta, y_train, cutoff, X, meta, y_val, cutoff)
    borrowed = {d["rounds"] for d in model.degraded_bins_}
    assert borrowed and borrowed != {FALLBACK_BOOST_ROUNDS}, (
        "should borrow bin 0's observed best_iteration, not the no-information fallback")


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


def test_save_load_round_trip(tmp_path, scenario_a):
    model, hold_df, rate, _, _, _ = scenario_a
    p50_before = model.predict_quantile(hold_df[["x"]], 0.5)
    p90_before = model.predict_quantile(hold_df[["x"]], 0.9)

    model_dir = tmp_path / "hazard_model"
    model.save(model_dir)
    loaded = DiscreteHazardModel.load(model_dir)

    assert loaded.edges_minutes == model.edges_minutes
    assert loaded.feature_names_ == model.feature_names_
    assert loaded.tail_rate_ == model.tail_rate_

    p50_after = loaded.predict_quantile(hold_df[["x"]], 0.5)
    p90_after = loaded.predict_quantile(hold_df[["x"]], 0.9)
    assert np.allclose(p50_before, p50_after)
    assert np.allclose(p90_before, p90_after)
    assert np.allclose(model.predict_hazard(hold_df[["x"]]), loaded.predict_hazard(hold_df[["x"]]))


def test_save_raises_before_fit(tmp_path):
    model = DiscreteHazardModel(edges_minutes=[0, 5, math.inf])
    with pytest.raises(RuntimeError, match="model not fit"):
        model.save(tmp_path / "unfit_model")


def test_save_clears_stale_artifacts_from_prior_save(tmp_path, scenario_a):
    """A second save() into the same directory (e.g. a smaller model
    replacing a larger one) must not leave the prior save's files behind --
    otherwise an interrupted re-save could leave load() able to silently
    combine new boosters with a stale meta.json/older boosters."""
    model, _, _, _, _, _ = scenario_a          # 5 bins
    small = DiscreteHazardModel(edges_minutes=[0, 10, math.inf])
    small.boosters = model.boosters[:2]
    small.feature_names_ = model.feature_names_
    small.tail_rate_ = model.tail_rate_
    d = tmp_path / "hz"
    model.save(d)
    small.save(d)
    assert sorted(p.name for p in d.iterdir()) == ["bin_0.lgb", "bin_1.lgb", "meta.json"]
    assert len(DiscreteHazardModel.load(d).boosters) == 2


def test_predict_hazard_is_column_order_invariant():
    """LightGBM does not reorder a DataFrame by column name -- predict_hazard
    must explicitly reorder X to match feature_names_, or a column-reordered
    caller (e.g. a loaded model used from a different process) silently
    mispredicts with no error."""
    rng = np.random.default_rng(1)
    n = 300
    X = pd.DataFrame({"a": rng.normal(size=n), "b": rng.normal(size=n)})
    y = pd.Series(np.where(X["a"] + X["b"] > 0, rng.exponential(200, n), rng.exponential(2000, n)))
    pending_at = pd.Series(pd.Timestamp("2026-01-01T00:00:00Z") + pd.to_timedelta(rng.uniform(0, 5 * 86400, n), unit="s"))
    meta = pd.DataFrame({"pending_at": pending_at, "resolved_at": pd.Series(pd.NaT, index=range(n))})
    cutoff = pending_at.max() + pd.Timedelta(hours=6)

    model = DiscreteHazardModel(edges_minutes=[0, 5, 15, math.inf], params={"n_estimators": 15, "min_data_in_leaf": 10, "early_stopping_rounds": 5})
    model.fit(X.iloc[:200], meta.iloc[:200], y.iloc[:200], cutoff, X.iloc[200:], meta.iloc[200:], y.iloc[200:], cutoff)

    correct_order = model.predict_hazard(X[["a", "b"]])
    reordered = model.predict_hazard(X[["b", "a"]])
    assert np.allclose(correct_order, reordered)
