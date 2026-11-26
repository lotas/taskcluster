import numpy as np
import pandas as pd

from scripts import group_eta_baselines as g
from scripts import group_eta_inflight as inflight
from scripts import group_eta_live as live
from tests.test_group_eta_baselines import DAY0, _group, _tasks


def test_live_frames_filters_late_and_small_groups():
    dec = pd.DataFrame({"task_group_id": ["A", "B"], "dec_created": [0.0, 0.0],
                        "dec_resolved": [100.0, 100.0], "t_pred": [220.0, 220.0]})
    tasks = pd.DataFrame({
        "task_group_id": ["A"] * 7 + ["B"] * 3,
        "repo_family": "try", "normalized_name": [f"n{i}" for i in range(10)],
        "created": [0, 60, 60, 60, 60, 60, 999, 0, 60, 60],
    })
    groups, known = live.live_frames(dec, tasks)
    assert list(groups.index) == ["A"]
    assert groups.loc["A", "n_known"] == 6  # the task created at 999 is after t_pred
    assert groups.loc["A", "t0"] == 0
    assert set(known.columns) == {"gid", "repo_family", "normalized_name"}


def test_fit_all_predict_all_round_trip():
    rng = np.random.default_rng(3)
    rows = _group("Z", DAY0 + 10)
    for i in range(400):
        t0 = DAY0 + 86400 + i * 3000
        rows += _group(f"G{i}", t0, offsets=list(rng.uniform(600, 3000, 8)))
    groups, known = g.build_groups(_tasks(rows), DAY0 + 40 * 86400, set())
    use = groups[groups["usable"]]
    d0 = DAY0 + 16 * 86400
    model = g.fit_all(use[use["end"] < d0], known, d0)
    test = use.iloc[-20:]
    out = g.predict_all(model, test, known[known["gid"].isin(test.index)])
    assert set(out) == set(g.ESTIMATORS)
    for p in out.values():
        assert p.notna().all().all()
        assert (p["p90"] >= p["p50"] * 0.999).all()


def _b2_model(seed=5):
    """fit_all + B2* factors on synthetic groups (8 tasks, offsets U(600, 3000) s)."""
    rng = np.random.default_rng(seed)
    rows = _group("Z", DAY0 + 10)
    for i in range(400):
        rows += _group(f"G{i}", DAY0 + 86400 + i * 3000, offsets=list(rng.uniform(600, 3000, 8)))
    groups, known = g.build_groups(_tasks(rows), DAY0 + 40 * 86400, set())
    use = groups[groups["usable"]]
    d0 = DAY0 + 16 * 86400
    tr = use[use["end"] < d0]
    model = g.fit_all(tr, known, d0)
    cal = tr[tr["day"].isin(sorted(tr["day"].unique())[-g.CAL_DAYS:])]
    model["b2_factors"] = inflight.fit_b2_factors(model["b1_full"], cal, known[known["gid"].isin(cal.index)])
    model["version"] = "test"
    return model


def test_b2_frames_original_graph_and_pending_state():
    due = pd.DataFrame({"task_group_id": ["A", "A"], "t0": [0.0, 0.0], "t_pred": [220.0, 220.0],
                        "repo_family": "try", "n_known": 4, "c_min": [15, 30]})
    tasks = pd.DataFrame({
        "task_group_id": "A", "repo_family": "try", "normalized_name": ["d", "a", "b", "c", "late"],
        "created": [0.0, 60, 60, 60, 999],
        "n_runs": [1, 1, 0, 2, 1], "n_unresolved": [0, 0, 0, 1, 0],
        "last_resolved": [100.0, 1500.0, None, 800.0, 2000.0],
    })
    groups, known = live.b2_frames(due, tasks)
    assert list(groups.index) == ["A"]
    assert list(known["normalized_name"]) == ["d", "a", "b", "c"]  # 'late' created after t_pred
    assert list(known["last_resolved"]) == [100.0, 1500.0, np.inf, np.inf]  # no runs / rerun pending


def test_b2_rows_only_running_groups_floored_at_elapsed():
    model = _b2_model()
    assert set(model["b2_factors"]) == set(inflight.CHECKPOINTS_MIN)
    t0, t_pred = 0.0, 220.0
    due = pd.DataFrame({"task_group_id": ["R", "R", "D"], "t0": t0, "t_pred": t_pred,
                        "repo_family": "try", "n_known": 6, "c_min": [15, 60, 15]})
    rows = []
    for gid, last in (("R", 5000.0), ("D", 900.0)):  # R: one task runs past +60 min; D: all done by +15
        rows.append((gid, "try", "decision", t0, 1, 0, 100.0))
        for i in range(5):
            rows.append((gid, "try", f"task{i}", 60.0, 1, 0, last if i == 0 else 700.0))
    tasks = pd.DataFrame(rows, columns=["task_group_id", "repo_family", "normalized_name", "created",
                                        "n_runs", "n_unresolved", "last_resolved"])
    out = live.b2_rows(model, due, tasks)
    by_est = {r[6]: r for r in out}
    assert set(by_est) == {"B2*+15m", "B2*+60m"}
    assert all(r[5] == "R" and r[1] == t_pred and r[4] == 6 for r in out)  # static t_pred and n_known kept
    for c in (15, 60):
        _, _, p50, p90, *_ = by_est[f"B2*+{c}m"]
        e = t_pred + c * 60 - t0
        assert e <= p50 <= p90


def test_b2_star_live_matches_offline_helper():
    """b2_rows == inflight.predict_b2_star on the same frames (live and eval share code)."""
    model = _b2_model()
    due = pd.DataFrame({"task_group_id": ["R"], "t0": 0.0, "t_pred": 220.0, "repo_family": "try",
                        "n_known": 6, "c_min": [30]})
    tasks = pd.DataFrame({"task_group_id": "R", "repo_family": "try",
                          "normalized_name": ["decision"] + [f"task{i}" for i in range(5)],
                          "created": [0.0] + [60.0] * 5, "n_runs": 1, "n_unresolved": 0,
                          "last_resolved": [100.0, 5000.0, 4000.0, 700, 700, 700]})
    (row,) = live.b2_rows(model, due, tasks)
    groups, known = live.b2_frames(due, tasks)
    ref = inflight.predict_b2_star(model["b1_full"], model["b2_factors"][30], groups, known, 1800.0)
    assert (row[2], row[3]) == (ref.at["R", "p50"], ref.at["R", "p90"])
