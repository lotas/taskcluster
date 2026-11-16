"""Live shadow task-group ETA (group-eta-plan.md P2). Internal, not user-facing.

  fit      extract any missing days into the cache, fit all edge-free estimators
           on the last TRAIN_DAYS of completed groups, save <data-dir>/live_model.pkl
  predict  for groups whose decision task resolved in (now - lookback, now - 120s],
           predict with the saved model and insert into
           queue_forecast_group_predictions_shadow (idempotent: ON CONFLICT DO NOTHING)

Same estimator code as scripts/group_eta_baselines.py (fit_all / predict_all), so
offline eval and live shadow agree by construction.

Run in the trainer container (see scripts/group_eta_live.sh).
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import pickle
import time
from pathlib import Path

import pandas as pd

from scripts import group_eta_baselines as g

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
    groups, known = g.build_groups(tasks, data_end_ts=d0 + 3 * 86400, excluded=excluded)
    use = groups[groups["usable"]]
    tr = use[(use["t0"] >= d0 - g.TRAIN_DAYS * 86400) & (use["end"] < d0)]
    model = g.fit_all(tr, known, d0)
    model["version"] = f"edgefree-v1-{today.isoformat()}"
    tmp = data_dir / "live_model.pkl.tmp"
    with open(tmp, "wb") as f:
        pickle.dump(model, f)
    tmp.rename(data_dir / "live_model.pkl")
    print(f"[group-eta fit] {model['version']}: {len(tr):,} training groups, {time.time() - t:.0f}s", flush=True)


def predict(data_dir: Path, lookback_min: int, estimators: list[str]) -> None:
    import psycopg

    with open(data_dir / "live_model.pkl", "rb") as f:
        model = pickle.load(f)
    age_days = (time.time() - model["trained_before"]) / 86400
    if age_days > 3:
        print(f"[group-eta predict] WARNING model is {age_days:.1f} days old ({model['version']})")

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute("SET statement_timeout = '120s'")
        dec = pd.DataFrame(conn.execute(LIVE_GROUPS_SQL, {"lookback": lookback_min, "delay": g.PRED_DELAY_S}).fetchall(),
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
    p = sub.add_parser("predict")
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    p.add_argument("--lookback-min", type=int, default=30)
    p.add_argument("--estimators", default=",".join(g.ESTIMATORS))
    a = ap.parse_args(argv)
    if a.cmd == "fit":
        fit(a.data_dir.expanduser(), set(g.DEFAULT_EXCLUDED))
    else:
        predict(a.data_dir.expanduser(), a.lookback_min, a.estimators.split(","))


if __name__ == "__main__":
    main()
