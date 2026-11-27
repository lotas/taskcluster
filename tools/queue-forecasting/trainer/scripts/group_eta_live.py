"""Live shadow task-group ETA (group-eta-plan.md P2). Internal, not user-facing.

  fit      extract any missing days into the cache, fit all edge-free estimators
           on the last TRAIN_DAYS of completed groups, save <data-dir>/live_model.pkl
           and its quantile-table subset <data-dir>/live_model.json
  export-json  rewrite live_model.json from the existing live_model.pkl (no refit)
  predict  for groups whose decision task resolved in (now - lookback, now - 120s],
           predict with the saved model and insert into
           queue_forecast_group_predictions_shadow (idempotent: ON CONFLICT DO NOTHING).
           B2* (default on, --no-b2 to skip): also re-estimate groups at each checkpoint tau = t_pred + c
           (c in group_eta_inflight.CHECKPOINTS_MIN) that fell in (now - lookback,
           now - 120s], as estimator 'B2*+<c>m' (group_eta_inflight.py, B2*).

B2* rows keep the static t_pred (it defines the original graph = the label); the
checkpoint is tau = t_pred + c from the estimator name. They are computed AT tau
(task resolution state reconstructed from resolved_at), so a late cron run gives
the same answer. Only groups with a task still running at tau get a row.

Same estimator code as scripts/group_eta_baselines.py (fit_all / predict_all), so
offline eval and live shadow agree by construction.

Run in the trainer container (see scripts/group_eta_live.sh).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import pickle
import time
from pathlib import Path

import numpy as np
import pandas as pd

from scripts import group_eta_baselines as g
from scripts import group_eta_inflight as inflight

MAX_DECISION_HOURS = 6
DEFAULT_DATA_DIR = Path(os.environ.get("QF_GROUP_ETA_DIR", "data/group_eta"))

# Groups whose decision task resolved in the window. The decision task is the
# task whose id == task_group_id (97% of try groups); others are skipped live.
LIVE_GROUPS_SQL = """
SELECT t.task_group_id,
       extract(epoch FROM t.task_created)::float8 AS dec_created,
       extract(epoch FROM max(r.resolved_at))::float8 AS dec_resolved
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t USING (task_id)
WHERE r.resolved_at > now() - make_interval(mins => %(lookback)s)
  AND r.resolved_at <= now() - make_interval(secs => %(delay)s)
  AND t.task_id = t.task_group_id
  AND NOT EXISTS (SELECT 1 FROM queue_forecast_group_predictions_shadow s
                  WHERE s.task_group_id = t.task_group_id)
GROUP BY 1, 2
-- A decision task that resolves hours after creation (e.g. never ran and hit its
-- deadline) is not a fresh graph; it would also stretch LIVE_TASKS_SQL's scan
-- window to a day+ and time out (seen 2026-11-19 23:10Z).
HAVING max(r.resolved_at) - min(t.task_created) < make_interval(hours => %(max_dec_hours)s)
"""

# Known tasks: created in [min t0, max t_pred]; uses idx_qf_tasks_task_created.
LIVE_TASKS_SQL = """
SELECT task_group_id, coalesce(repo_family, 'unknown') AS repo_family,
       coalesce(normalized_name, '?') AS normalized_name,
       extract(epoch FROM task_created)::float8 AS created
FROM queue_forecast_tasks
WHERE task_created >= to_timestamp(%(lo)s) AND task_created <= to_timestamp(%(hi)s)
  AND task_group_id = ANY(%(gids)s)
"""

INSERT_SQL = """
INSERT INTO queue_forecast_group_predictions_shadow
  (t0, t_pred, p50_s, p90_s, n_known, task_group_id, estimator, repo_family, model_version)
VALUES (to_timestamp(%s), to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING
"""

B2_PREFIX = "B2*+"

# Checkpoints due: static rows (any non-B2 estimator; all share t0/t_pred) whose
# t_pred + c fell in the window and that have no B2* row for c yet. predicted_at >=
# t_pred, so the predicted_at bound (index) cannot drop a due checkpoint.
B2_DUE_SQL = """
SELECT s.task_group_id, extract(epoch FROM s.t0)::float8 AS t0,
       extract(epoch FROM s.t_pred)::float8 AS t_pred, s.repo_family, s.n_known, c.c_min
FROM (SELECT DISTINCT ON (task_group_id) task_group_id, t0, t_pred, repo_family, n_known
      FROM queue_forecast_group_predictions_shadow
      WHERE predicted_at > now() - make_interval(mins => %(lookback)s + %(max_c)s)
        AND left(estimator, 4) <> 'B2*+'
      ORDER BY task_group_id, predicted_at) s
CROSS JOIN unnest(%(checkpoints)s::int[]) AS c(c_min)
WHERE s.t_pred + make_interval(mins => c.c_min) >  now() - make_interval(mins => %(lookback)s)
  AND s.t_pred + make_interval(mins => c.c_min) <= now() - make_interval(secs => %(delay)s)
  AND NOT EXISTS (SELECT 1 FROM queue_forecast_group_predictions_shadow b
                  WHERE b.task_group_id = s.task_group_id AND b.estimator = 'B2*+' || c.c_min || 'm')
"""

# Original-graph tasks of the due groups with their resolution state. Final state per
# task, like the offline extract: a task counts as resolved at tau iff it has runs, none
# unresolved, and the last resolution is <= tau.
B2_TASKS_SQL = """
SELECT t.task_group_id, coalesce(t.repo_family, 'unknown') AS repo_family,
       coalesce(t.normalized_name, '?') AS normalized_name,
       extract(epoch FROM t.task_created)::float8 AS created,
       r.n_runs, r.n_unresolved, r.last_resolved
FROM queue_forecast_tasks t
LEFT JOIN LATERAL (
  SELECT count(*)::int AS n_runs,
         count(*) FILTER (WHERE resolved_at IS NULL)::int AS n_unresolved,
         extract(epoch FROM max(resolved_at))::float8 AS last_resolved
  FROM queue_forecast_task_runs r WHERE r.task_id = t.task_id
) r ON true
WHERE t.task_group_id = ANY(%(gids)s)
  AND t.task_created >= to_timestamp(%(lo)s) AND t.task_created <= to_timestamp(%(hi)s)
"""


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def fit(data_dir: Path, excluded: set[dt.date]) -> None:
    t = time.time()
    today = _today()
    # Cache needs TRAIN_DAYS of groups plus one day of lead-in (groups straddling
    # the first day are dropped). Yesterday is re-extracted: it was partial before.
    start = today - dt.timedelta(days=g.TRAIN_DAYS + 2)
    yday = today - dt.timedelta(days=1)
    g.extract(data_dir, start, yday - dt.timedelta(days=1), force=False)
    g.extract(data_dir, yday, yday, force=True)

    tasks = g.load_tasks(data_dir)
    tasks = tasks[tasks["created"] >= g._ts(start)]
    d0 = g._ts(today)
    # data_end = now: groups need 3 days to count as settled in eval, but live we
    # want the freshest window; completion-before-d0 already guarantees labels.
    # Label 'ran' since 2026-11-27 (group-eta-plan.md G4): blocked tasks don't set the end.
    groups, known = g.build_groups(tasks, data_end_ts=d0 + 3 * 86400, excluded=excluded, label="ran")
    use = groups[groups["usable"]]
    tr = use[(use["t0"] >= d0 - g.TRAIN_DAYS * 86400) & (use["end"] < d0)]
    model = g.fit_all(tr, known, d0)
    # B2*: factors on the last CAL_DAYS present days, with the full-window B1 tables
    # (exactly the offline eval in group_eta_inflight.evaluate).
    cal_days = sorted(tr["day"].unique())[-g.CAL_DAYS:]
    cal = tr[tr["day"].isin(cal_days)]
    model["b2_factors"] = inflight.fit_b2_factors(model["b1_full"], cal, known[known["gid"].isin(cal.index)])
    model["version"] = f"edgefree-v1-{today.isoformat()}"
    tmp = data_dir / "live_model.pkl.tmp"
    with open(tmp, "wb") as f:
        pickle.dump(model, f)
    tmp.rename(data_dir / "live_model.pkl")
    write_json(model, data_dir)
    print(f"[group-eta fit] {model['version']}: {len(tr):,} training groups, {time.time() - t:.0f}s", flush=True)


def predict(data_dir: Path, lookback_min: int, estimators: list[str], b2: bool = False) -> None:
    import psycopg

    with open(data_dir / "live_model.pkl", "rb") as f:
        model = pickle.load(f)
    age_days = (time.time() - model["trained_before"]) / 86400
    if age_days > 3:
        print(f"[group-eta predict] WARNING model is {age_days:.1f} days old ({model['version']})")

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute("SET statement_timeout = '120s'")
        predict_static(conn, model, lookback_min, estimators)
        if b2:
            if "b2_factors" not in model:
                print(f"[group-eta predict] B2*: model {model['version']} has no b2_factors (refit needed), skipped")
            else:
                predict_inflight(conn, model, lookback_min)


def predict_static(conn, model: dict, lookback_min: int, estimators: list[str]) -> None:
    dec = pd.DataFrame(conn.execute(LIVE_GROUPS_SQL, {"lookback": lookback_min, "delay": g.PRED_DELAY_S,
                                                   "max_dec_hours": MAX_DECISION_HOURS}).fetchall(),
                       columns=["task_group_id", "dec_created", "dec_resolved"])
    if dec.empty:
        print("[group-eta predict] no new groups")
        return
    dec["t_pred"] = dec["dec_resolved"] + g.PRED_DELAY_S
    params = {"lo": float(dec["dec_created"].min()) - 3600, "hi": float(dec["t_pred"].max()),
              "gids": dec["task_group_id"].tolist()}
    tasks = pd.DataFrame(conn.execute(LIVE_TASKS_SQL, params).fetchall(),
                         columns=["task_group_id", "repo_family", "normalized_name", "created"])
    groups, known = live_frames(dec, tasks)
    if groups.empty:
        print(f"[group-eta predict] {len(dec)} decision tasks, no group with >= {g.MIN_GROUP_TASKS} known tasks")
        return
    preds = g.predict_all(model, groups, known)
    rows = []
    for est in estimators:
        p = preds[est]
        for gid, r in groups.iterrows():
            p50, p90 = p.at[gid, "p50"], p.at[gid, "p90"]
            if pd.isna(p50) or pd.isna(p90):
                continue
            rows.append((float(r["t0"]), float(r["t_pred"]), float(p50), float(p90), int(r["n_known"]),
                         gid, est, r["repo_family"], model["version"]))
    with conn.cursor() as cur:
        cur.executemany(INSERT_SQL, rows)
    conn.commit()
    print(f"[group-eta predict] {len(groups)} groups, {len(rows)} rows ({model['version']})", flush=True)


def predict_inflight(conn, model: dict, lookback_min: int) -> None:
    cps = list(inflight.CHECKPOINTS_MIN)
    due = pd.DataFrame(conn.execute(B2_DUE_SQL, {"lookback": lookback_min, "max_c": max(cps), "checkpoints": cps,
                                                 "delay": g.PRED_DELAY_S}).fetchall(),
                       columns=["task_group_id", "t0", "t_pred", "repo_family", "n_known", "c_min"])
    if due.empty:
        print("[group-eta predict] B2*: no checkpoints due")
        return
    hdr = due.drop_duplicates("task_group_id").set_index("task_group_id")
    tasks = pd.DataFrame(conn.execute(B2_TASKS_SQL, {"gids": hdr.index.tolist(), "lo": float(hdr["t0"].min()),
                                                     "hi": float(hdr["t_pred"].max())}).fetchall(),
                         columns=["task_group_id", "repo_family", "normalized_name", "created",
                                  "n_runs", "n_unresolved", "last_resolved"])
    rows = b2_rows(model, due, tasks)
    with conn.cursor() as cur:
        cur.executemany(INSERT_SQL, rows)
    conn.commit()
    print(f"[group-eta predict] B2*: {len(due)} checkpoints due, {len(rows)} rows "
          f"(others had no running task at tau)", flush=True)


def b2_frames(due: pd.DataFrame, tasks: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(groups, known) for inflight.predict_b2_star from B2_DUE_SQL / B2_TASKS_SQL rows.

    known = original-graph tasks (created in [t0, t_pred]); last_resolved = inf while
    the task has no runs or an unresolved run."""
    groups = due.drop_duplicates("task_group_id").set_index("task_group_id")[["t0", "t_pred", "repo_family", "n_known"]]
    k = tasks.merge(groups[["t0", "t_pred"]], left_on="task_group_id", right_index=True)
    k = k[(k["created"] >= k["t0"]) & (k["created"] <= k["t_pred"])]
    pending = (k["n_runs"].fillna(0) == 0) | (k["n_unresolved"].fillna(0) > 0) | k["last_resolved"].isna()
    k = k.assign(last_resolved=k["last_resolved"].where(~pending, np.inf))
    known = k.rename(columns={"task_group_id": "gid"})[["gid", "repo_family", "normalized_name", "t0", "last_resolved"]]
    return groups, known.reset_index(drop=True)


def b2_rows(model: dict, due: pd.DataFrame, tasks: pd.DataFrame) -> list[tuple]:
    groups, known = b2_frames(due, tasks)
    rows = []
    for c, d in due.groupby("c_min"):
        c = int(c)
        gids = d["task_group_id"].unique()
        gg = groups.loc[gids]
        p = inflight.predict_b2_star(model["b1_full"], model["b2_factors"][c], gg,
                                     known[known["gid"].isin(gids)], c * 60.0)
        for gid, r in p.iterrows():
            h = gg.loc[gid]
            rows.append((float(h["t0"]), float(h["t_pred"]), float(r["p50"]), float(r["p90"]), int(h["n_known"]),
                         gid, f"{B2_PREFIX}{c}m", h["repo_family"], model["version"]))
    return rows


JSON_ESTIMATORS = ("B0", "B1c", "B1c*")


def _f(x):
    x = float(x)
    return None if math.isnan(x) else x


def _qtable(df: pd.DataFrame) -> dict:
    """quantile frame (index -> columns 0.5, 0.9) as {str(key): [p50, p90]}."""
    return {str(k): [_f(r[0.5]), _f(r[0.9])] for k, r in df.iterrows()}


def model_json(model: dict) -> dict:
    """The subset of a fitted model that B0, B1c and B1c* need, as plain JSON.

    Consumed by src/scorer/group-eta.js (the playground scorer). The per-key CDFs
    (B1i only) are left out; they are what makes the pickle ~200 MB.
    """
    fs, fam, glob = model["b0"]
    b0_fs: dict = {}
    for (family, sb), r in fs.iterrows():
        b0_fs.setdefault(str(family), {})[str(int(sb))] = [_f(r[0.5]), _f(r[0.9])]
    cal = model["cal_c"]
    return {
        "format": "group-eta-tables-v1",
        "version": model["version"],
        "trained_before": float(model["trained_before"]),
        "estimators": list(JSON_ESTIMATORS),
        "params": {
            "size_buckets": [b if math.isfinite(b) else None for b in g.SIZE_BUCKETS],
            "min_key_samples": g.MIN_KEY_SAMPLES,
            "min_group_tasks": g.MIN_GROUP_TASKS,
            "pred_delay_s": g.PRED_DELAY_S,
        },
        "b0": {"fs": b0_fs, "fam": _qtable(fam), "glob": [_f(glob[0.5]), _f(glob[0.9])]},
        "b1_full": {"q_key": _qtable(model["b1_full"]["q_key"]), "q_fam": _qtable(model["b1_full"]["q_fam"])},
        "b1_fit": {"q_key": _qtable(model["b1_fit"]["q_key"]), "q_fam": _qtable(model["b1_fit"]["q_fam"])},
        "cal_c": {str(k): {"n": int(r["n"]), "f50": _f(r["f50"]), "f90": _f(r["f90"])} for k, r in cal.iterrows()},
    }


def write_json(model: dict, data_dir: Path) -> Path:
    out = data_dir / "live_model.json"
    tmp = data_dir / "live_model.json.tmp"
    with open(tmp, "w") as f:
        json.dump(model_json(model), f, separators=(",", ":"), allow_nan=False)
    tmp.rename(out)
    return out


def export_json(data_dir: Path) -> None:
    with open(data_dir / "live_model.pkl", "rb") as f:
        model = pickle.load(f)
    out = write_json(model, data_dir)
    print(f"[group-eta export-json] {model['version']} -> {out} ({out.stat().st_size / 1e6:.1f} MB)", flush=True)


def live_frames(dec: pd.DataFrame, tasks: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build (groups, known) in the shapes predict_all expects, from live rows."""
    tasks = tasks.merge(dec[["task_group_id", "t_pred"]], on="task_group_id")
    tasks = tasks[tasks["created"] <= tasks["t_pred"]]
    kg = tasks.groupby("task_group_id")
    groups = pd.DataFrame({
        "t0": kg["created"].min(),
        "t_pred": kg["t_pred"].first(),
        "n_known": kg.size(),
        "repo_family": kg["repo_family"].agg(lambda s: s.mode().iat[0]),
    })
    groups = groups[groups["n_known"] >= g.MIN_GROUP_TASKS]
    known = tasks[tasks["task_group_id"].isin(groups.index)].rename(columns={"task_group_id": "gid"})
    return groups, known[["gid", "repo_family", "normalized_name"]]


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fit")
    f.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    e = sub.add_parser("export-json")
    e.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    p = sub.add_parser("predict")
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    p.add_argument("--lookback-min", type=int, default=30)
    # B1i/B1i* dropped from live 2026-11-20: independence assumption over-covers
    # (offline try cov90 0.996, w2x 0.52). Still computed offline for reference.
    p.add_argument("--estimators", default="B0,B1c,B1c*")
    # On since 2026-11-26 (G3 live shadow); --no-b2 to skip.
    p.add_argument("--b2", action=argparse.BooleanOptionalAction, default=True,
                   help="also write B2* in-flight re-estimates (estimators 'B2*+<c>m')")
    a = ap.parse_args(argv)
    if a.cmd == "fit":
        fit(a.data_dir.expanduser(), set(g.DEFAULT_EXCLUDED))
    elif a.cmd == "export-json":
        export_json(a.data_dir.expanduser())
    else:
        predict(a.data_dir.expanduser(), a.lookback_min, a.estimators.split(","), a.b2)


if __name__ == "__main__":
    main()
