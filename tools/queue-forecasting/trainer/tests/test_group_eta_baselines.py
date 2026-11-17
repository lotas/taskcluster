import datetime as dt

import numpy as np
import pandas as pd
import pytest

from scripts import group_eta_baselines as g

DAY0 = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc).timestamp()


def _tasks(rows):
    df = pd.DataFrame(rows, columns=["task_group_id", "repo_family", "normalized_name", "created",
                                     "is_gid", "n_runs", "n_unresolved", "last_resolved"])
    df["dependency_count"] = None
    df["first_pending"] = df["created"]
    df["last_reason"] = "completed"
    for c in ("task_group_id", "repo_family", "normalized_name", "last_reason"):
        df[c] = df[c].astype("category")
    return df


def _group(gid, t0, n=6, dec_dur=100, offsets=None, fam="try", late=False, unresolved=False):
    offsets = offsets or [1000 + 100 * i for i in range(n - 1)]
    rows = [(gid, fam, "decision", t0, True, 1, 0, t0 + dec_dur)]
    for i, off in enumerate(offsets):
        rows.append((gid, fam, f"task{i}", t0 + 60, False, 1, int(unresolved and i == 0),
                     None if (unresolved and i == 0) else t0 + off))
    if late:
        rows.append((gid, fam, "retrigger", t0 + dec_dur + g.PRED_DELAY_S + 600, False, 1, 0, t0 + 99999))
    return rows


def test_build_groups_label_excludes_late_tasks_and_sets_t_pred():
    t0 = DAY0 + 2 * 86400
    rows = _group("G1", t0, late=True) + _group("Z", DAY0 + 10, n=6)  # Z pins first_day
    groups, known = g.build_groups(_tasks(rows), data_end_ts=t0 + 10 * 86400, excluded=set())
    gi = groups.loc[groups.index[groups["t0"] == t0][0]]
    assert gi["t_pred"] == t0 + 100 + g.PRED_DELAY_S
    assert gi["n_known"] == 6 and gi["n_late"] == 1
    assert gi["span"] == 1400  # max offset of known tasks, not the late retrigger
    assert gi["usable"]


def test_build_groups_drops_unresolved_excluded_and_unsettled():
    first = _group("Z", DAY0 + 10)
    unres = _group("U", DAY0 + 2 * 86400, unresolved=True)
    excl = _group("E", DAY0 + 3 * 86400)
    recent = _group("R", DAY0 + 9.5 * 86400)
    groups, _ = g.build_groups(_tasks(first + unres + excl + recent), data_end_ts=DAY0 + 10 * 86400,
                               excluded={dt.date(2026, 10, 4)})
    usable_t0 = set(groups.loc[groups["usable"], "t0"])
    assert usable_t0 == set()  # Z is on first_day, U unresolved, E excluded, R too recent


def test_fallback_t_pred_without_decision_task():
    rows = _group("Z", DAY0 + 10)
    t0 = DAY0 + 2 * 86400
    no_dec = [("N", "try", f"t{i}", t0, False, 1, 0, t0 + 500 + i) for i in range(6)]
    groups, _ = g.build_groups(_tasks(rows + no_dec), DAY0 + 10 * 86400, set())
    row = groups[groups["t0"] == t0].iloc[0]
    assert not row["has_decision"] and row["t_pred"] == t0 + g.FALLBACK_DELAY_S


def _known(gid, names_offsets, fam="try"):
    return pd.DataFrame({"gid": gid, "repo_family": fam,
                         "normalized_name": [n for n, _ in names_offsets],
                         "offset": [float(o) for _, o in names_offsets]})


def test_b1_comonotone_is_max_of_task_quantiles_and_independent_is_larger():
    rng = np.random.default_rng(0)
    train = pd.concat([
        _known(i, [("a", rng.uniform(100, 200)), ("b", rng.uniform(1000, 2000))]) for i in range(200)
    ], ignore_index=True)
    m = g.fit_b1(train)
    test = _known(999, [("a", 0), ("b", 0), ("b", 0), ("b", 0)])
    b1c, b1i = g.predict_b1(m, test)
    qb = train.loc[train["normalized_name"] == "b", "offset"].quantile([0.5, 0.9])
    assert b1c.loc[999, "p50"] == pytest.approx(qb[0.5])
    assert b1c.loc[999, "p90"] == pytest.approx(qb[0.9])
    # max of 3 iid draws of b has higher quantiles than one draw
    assert b1i.loc[999, "p50"] > qb[0.5] * 0.98
    assert b1i.loc[999, "p90"] >= b1i.loc[999, "p50"]


def test_b1_unknown_name_falls_back_to_family():
    train = pd.concat([_known(i, [("a", 100 + i)]) for i in range(50)], ignore_index=True)
    m = g.fit_b1(train)
    b1c, _ = g.predict_b1(m, _known(7, [("never-seen", 0)]))
    assert b1c.loc[7, "p50"] == pytest.approx(train["offset"].quantile(0.5))


def test_calibration_scales_to_target_coverage():
    rng = np.random.default_rng(1)
    n = 1000
    groups = pd.DataFrame({"repo_family": ["try"] * n, "span": rng.uniform(100, 300, n)})
    pred = pd.DataFrame({"p50": np.full(n, 100.0), "p90": np.full(n, 100.0)})
    f = g.fit_calibration(pred, groups)
    out = g.apply_calibration(pred, groups, f)
    assert np.mean(groups["span"] <= out["p90"]) == pytest.approx(0.9, abs=0.01)
    assert np.median(groups["span"] / out["p50"]) == pytest.approx(1.0, abs=0.01)


def test_b0_fallbacks():
    tg = pd.DataFrame({"repo_family": ["try"] * 30 + ["autoland"] * 3,
                       "n_known": [20] * 30 + [20] * 3,
                       "span": list(range(100, 130)) + [5000, 6000, 7000]})
    m = g.fit_b0(tg)
    q = pd.DataFrame({"repo_family": ["try", "autoland", "try"], "n_known": [20, 20, 5000]})
    p = g.predict_b0(m, q)
    assert p.loc[0, "p50"] == pytest.approx(np.quantile(range(100, 130), 0.5))
    assert p.loc[1, "p50"] == pytest.approx(6000)  # family fallback (bucket too small)
    assert p.loc[2, "p50"] == pytest.approx(np.quantile(range(100, 130), 0.5))  # try family fallback


def test_metrics():
    m = g.metrics(np.array([100.0, 200.0]), np.array([100.0, 50.0]), np.array([150.0, 150.0]))
    assert m["w2x"] == 0.5 and m["cov90"] == 0.5
    assert m["mae_min"] == pytest.approx(75 / 60)


def test_parse_days():
    assert g.parse_days("2026-10-30..2026-11-01,2026-11-05") == [
        dt.date(2026, 10, 30), dt.date(2026, 10, 31), dt.date(2026, 11, 1), dt.date(2026, 11, 5)]


def test_md_table():
    out = g.md(pd.DataFrame({"a": [1], "b": ["x"]}))
    assert out.splitlines() == ["| a | b |", "|---|---|", "| 1 | x |"]


def test_fit_all_calibrates_on_last_present_days_across_gap():
    rng = np.random.default_rng(5)
    rows = _group("Z", DAY0 + 10)
    for i in range(300):
        day = 1 + i // 30          # days 1..10
        if day >= 7:
            day += 5               # gap: days 7..11 missing, like an excluded outage
        rows += _group(f"G{i}", DAY0 + day * 86400 + (i % 30) * 2000, offsets=list(rng.uniform(600, 3000, 6)))
    groups, known = g.build_groups(_tasks(rows), DAY0 + 60 * 86400, set())
    use = groups[groups["usable"]]
    d0 = DAY0 + 17 * 86400
    model = g.fit_all(use[use["end"] < d0], known, d0)  # must not raise on the gap
    assert model["cal_c"].loc["try", "n"] > 0


def test_predict_b1_empty_known():
    train = pd.concat([_known(i, [("a", 100 + i)]) for i in range(50)], ignore_index=True)
    c, i = g.predict_b1(g.fit_b1(train), _known(1, [("a", 0)]).iloc[0:0])
    assert c.empty and i.empty
