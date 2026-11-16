import numpy as np
import pandas as pd

from scripts import group_eta_baselines as g
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
