"""Task-group ETA, edge-free baselines (group-eta-plan.md P1).

Predicts when a task group's *original graph* finishes, at the moment the graph
is known (decision task resolved + PRED_DELAY), using only data we already
collect. No dependency edges needed.

Definitions
  t0       = min(task_created) over the group (≈ decision task creation).
  t_pred   = decision-task resolved_at + PRED_DELAY (fallback t0 + FALLBACK_DELAY
             when the group has no task whose id == task_group_id).
  known    = tasks with task_created <= t_pred (the original graph). Later tasks
             (actions, retriggers) are excluded from the label and reported.
  label    = max(last_resolved over known tasks) - t0, in seconds ("span").

Estimators (p50 and p90 of span)
  B0   history: span quantiles by (repo_family, known-size bucket), fallbacks
       family -> global.
  B1c  offset-max, comonotone: per-task offset = last_resolved - t0 by
       (family, normalized_name) [fallback family]; group q = max_i Q_i(q).
  B1i  offset-max, independent: group CDF = prod_i F_i(x) on a log grid.
  B1c* B1c with a per-family multiplicative calibration fitted on the last
       CAL_DAYS of the training window (split-conformal style).

Subcommands
  extract   cache per-day task rows to <data-dir>/tasks/<day>.parquet
  eval      walk-forward over test days; writes report.md + predictions.parquet

Usage (host, DB on 127.0.0.1:5433; DATABASE_URL from ../.env with host rewritten):
  python -m scripts.group_eta_baselines extract --from 2026-09-20 --to 2026-11-14 --data-dir ~/qf-group-eta
  python -m scripts.group_eta_baselines eval --data-dir ~/qf-group-eta \
      --test-days 2026-10-20..2026-10-28,2026-11-03..2026-11-13
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

PRED_DELAY_S = 120
FALLBACK_DELAY_S = 300
TRAIN_DAYS = 14
CAL_DAYS = 4
MIN_KEY_SAMPLES = 20
MIN_CAL_SIZE_SAMPLES = 30
MIN_GROUP_TASKS = 5
SIZE_BUCKETS = [5, 10, 50, 200, 1000, np.inf]
GRID = np.geomspace(30, 4 * 86400, 400)  # seconds, 30s .. 4d

# Collection-quality exclusions (memory: collector outage 10-29..11-02, Oct 4 gap,
# Sept 27..Oct 3 thinned). Groups whose t0 falls on these days are dropped from
# both training and testing.
DEFAULT_EXCLUDED = (
    [dt.date(2026, 9, 27) + dt.timedelta(days=i) for i in range(8)]
    + [dt.date(2026, 10, 29) + dt.timedelta(days=i) for i in range(5)]
)

EXTRACT_SQL = """
SELECT t.task_group_id,
       coalesce(t.repo_family, 'unknown') AS repo_family,
       coalesce(t.normalized_name, '?') AS normalized_name,
       extract(epoch FROM t.task_created)::float8 AS created,
       (t.task_id = t.task_group_id) AS is_gid,
       t.dependency_count,
       r.n_runs, r.n_unresolved, r.first_pending, r.last_resolved, r.last_reason
FROM queue_forecast_tasks t
LEFT JOIN LATERAL (
  SELECT count(*)::int AS n_runs,
         count(*) FILTER (WHERE resolved_at IS NULL)::int AS n_unresolved,
         extract(epoch FROM min(pending_at))::float8 AS first_pending,
         extract(epoch FROM max(resolved_at))::float8 AS last_resolved,
         (array_agg(reason_resolved ORDER BY run_id DESC))[1] AS last_reason
  FROM queue_forecast_task_runs r WHERE r.task_id = t.task_id
) r ON true
WHERE t.task_created >= %s AND t.task_created < %s
  AND t.task_group_id IS NOT NULL
"""


# ---------------------------------------------------------------- extract ---

def _days(a: dt.date, b: dt.date):
    d = a
    while d <= b:
        yield d
        d += dt.timedelta(days=1)


def extract(data_dir: Path, start: dt.date, end: dt.date, force: bool) -> None:
    import psycopg

    out = data_dir / "tasks"
    out.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute("SET statement_timeout = '900s'")
        for d in _days(start, end):
            path = out / f"{d.isoformat()}.parquet"
            if path.exists() and not force:
                continue
            t = time.time()
            with conn.cursor() as cur:
                cur.execute(EXTRACT_SQL, (d, d + dt.timedelta(days=1)))
                cols = [c.name for c in cur.description]
                df = pd.DataFrame(cur.fetchall(), columns=cols)
            for c in ("task_group_id", "repo_family", "normalized_name", "last_reason"):
                df[c] = df[c].astype("category")
            tmp = path.with_suffix(".tmp")
            df.to_parquet(tmp, index=False)
            tmp.rename(path)
            print(f"[extract] {d}: {len(df):,} tasks in {time.time() - t:.0f}s", flush=True)


# ------------------------------------------------------------------ build ---

def load_tasks(data_dir: Path) -> pd.DataFrame:
    files = sorted((data_dir / "tasks").glob("*.parquet"))
    if not files:
        sys.exit(f"no extracted days under {data_dir}/tasks")
    parts = [pd.read_parquet(f) for f in files]
    for c in ("task_group_id", "repo_family", "normalized_name", "last_reason"):
        cats = pd.api.types.union_categoricals([p[c] for p in parts]).categories
        for p in parts:
            p[c] = pd.Categorical(p[c].astype(object), categories=cats)
    return pd.concat(parts, ignore_index=True)


def build_groups(tasks: pd.DataFrame, data_end_ts: float, excluded: set[dt.date]):
    """Return (groups, known_tasks). groups indexed by task_group_id codes."""
    tasks = tasks.copy()
    tasks["gid"] = tasks["task_group_id"].cat.codes
    g = tasks.groupby("gid", observed=True)
    t0 = g["created"].min()
    dec = tasks.loc[tasks["is_gid"], ["gid", "last_resolved"]].drop_duplicates("gid").set_index("gid")["last_resolved"]
    t_pred = pd.Series(t0 + FALLBACK_DELAY_S, index=t0.index)
    has_dec = dec.reindex(t0.index).notna()
    t_pred[has_dec] = dec.reindex(t0.index)[has_dec] + PRED_DELAY_S

    tasks["t0"] = tasks["gid"].map(t0)
    tasks["t_pred"] = tasks["gid"].map(t_pred)
    known_mask = tasks["created"] <= tasks["t_pred"]
    known = tasks[known_mask].copy()
    late = tasks[~known_mask].groupby("gid", observed=True).size()

    kg = known.groupby("gid", observed=True)
    groups = pd.DataFrame({
        "t0": t0,
        "t_pred": t_pred,
        "has_decision": has_dec,
        "n_known": kg.size(),
        "n_unresolved": kg["n_unresolved"].sum(),
        "n_no_runs": kg["n_runs"].apply(lambda s: int((s.fillna(0) == 0).sum())),
        "end": kg["last_resolved"].max(),
        "repo_family": kg["repo_family"].agg(lambda s: s.mode().iat[0]),
        "n_late": late,
    })
    groups["n_late"] = groups["n_late"].fillna(0).astype(int)
    groups["span"] = groups["end"] - groups["t0"]
    groups["day"] = pd.to_datetime(groups["t0"], unit="s", utc=True).dt.date
    first_day = pd.to_datetime(tasks["created"].min(), unit="s", utc=True).date()

    ok = (
        (groups["n_known"] >= MIN_GROUP_TASKS)
        & (groups["n_unresolved"] == 0)
        & (groups["n_no_runs"] == 0)
        & groups["span"].notna() & (groups["span"] > 0)
        & ~groups["day"].isin(excluded)
        & (groups["day"] > first_day)              # group may predate extraction
        & (groups["t0"] < data_end_ts - 3 * 86400)  # labels need time to settle
    )
    groups["usable"] = ok
    known = known[known["gid"].isin(groups.index[ok])]
    known = known.assign(offset=known["last_resolved"] - known["t0"])
    return groups, known


# ------------------------------------------------------------- estimators ---

def size_bucket(n):
    return pd.cut(n, SIZE_BUCKETS, right=False, labels=False)


def fit_b0(train_groups: pd.DataFrame):
    tg = train_groups.assign(sb=size_bucket(train_groups["n_known"]))
    by_fs = tg.groupby(["repo_family", "sb"], observed=True)["span"]
    fs = by_fs.quantile([0.5, 0.9]).unstack()
    fs = fs[by_fs.size() >= MIN_KEY_SAMPLES]
    fam = tg.groupby("repo_family", observed=True)["span"].quantile([0.5, 0.9]).unstack()
    glob = tg["span"].quantile([0.5, 0.9])
    return fs, fam, glob


def predict_b0(model, groups: pd.DataFrame) -> pd.DataFrame:
    fs, fam, glob = model
    sb = size_bucket(groups["n_known"])
    out = pd.DataFrame(index=groups.index, columns=[0.5, 0.9], dtype=float)
    keys = list(zip(groups["repo_family"], sb))
    out[:] = np.array([fs.loc[k].values if k in fs.index else [np.nan, np.nan] for k in keys])
    miss = out[0.5].isna()
    out.loc[miss] = fam.reindex(groups.loc[miss, "repo_family"]).values
    miss = out[0.5].isna()
    out.loc[miss, 0.5], out.loc[miss, 0.9] = glob[0.5], glob[0.9]
    return out.rename(columns={0.5: "p50", 0.9: "p90"})


def _key(df: pd.DataFrame) -> pd.Series:
    return df["repo_family"].astype(str) + "\x1f" + df["normalized_name"].astype(str)


def fit_b1(train_known: pd.DataFrame):
    """Per-key empirical CDF on GRID + quantiles; key = family|name, fallback family."""
    tk = train_known.assign(key=_key(train_known))
    log_off = np.log(np.clip(tk["offset"].to_numpy(), 1.0, None))
    tk = tk.assign(lo=log_off)
    counts = tk.groupby("key").size()
    good = counts.index[counts >= MIN_KEY_SAMPLES]

    def table(df, by):
        q = df.groupby(by)["offset"].quantile([0.5, 0.9]).unstack()
        cdf = {}
        lg = np.log(GRID)
        for k, s in df.groupby(by)["lo"]:
            v = np.sort(s.to_numpy())
            cdf[k] = np.searchsorted(v, lg, side="right") / len(v)
        return q, cdf

    q_key, cdf_key = table(tk[tk["key"].isin(good)], "key")
    q_fam, cdf_fam = table(tk, "repo_family")
    return {"q_key": q_key, "cdf_key": cdf_key, "q_fam": q_fam, "cdf_fam": cdf_fam}


def _resolve_keys(model, known: pd.DataFrame) -> pd.Series:
    key = _key(known)
    fam = "F\x1e" + known["repo_family"].astype(str)
    return key.where(key.isin(model["q_key"].index), fam)


def _lookup_tables(model):
    q = pd.concat([model["q_key"], model["q_fam"].rename(index=lambda k: "F\x1e" + str(k))])
    cdf = dict(model["cdf_key"])
    cdf.update({"F\x1e" + str(k): v for k, v in model["cdf_fam"].items()})
    return q, cdf


def predict_b1(model, known: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (B1c, B1i) predictions indexed by gid."""
    if known.empty:
        empty = pd.DataFrame(columns=["p50", "p90"], dtype=float)
        return empty, empty.copy()
    rk = _resolve_keys(model, known)
    q, cdf = _lookup_tables(model)
    # Unknown family entirely -> drop those tasks (group falls back to its other tasks).
    have = rk.isin(q.index)
    kk = known[have].assign(rk=rk[have])
    if kk.empty:
        empty = pd.DataFrame(columns=["p50", "p90"], dtype=float)
        return empty, empty.copy()

    qv = q.reindex(kk["rk"])
    b1c = pd.DataFrame({"p50": qv[0.5].to_numpy(), "p90": qv[0.9].to_numpy(), "gid": kk["gid"].to_numpy()})
    b1c = b1c.groupby("gid").max()

    pairs = kk.groupby(["gid", "rk"], observed=True).size().reset_index(name="n")
    keys = pairs["rk"].unique()
    kidx = {k: i for i, k in enumerate(keys)}
    logF = np.log(np.clip(np.stack([cdf[k] for k in keys]), 1e-9, 1.0))
    gids = pairs["gid"].unique()
    gidx = pd.Series(np.arange(len(gids)), index=gids)
    acc = np.zeros((len(gids), len(GRID)))
    rows_g = gidx[pairs["gid"]].to_numpy()
    rows_k = pairs["rk"].map(kidx).to_numpy()
    n = pairs["n"].to_numpy()[:, None]
    for s in range(0, len(pairs), 20000):
        sl = slice(s, s + 20000)
        np.add.at(acc, rows_g[sl], n[sl] * logF[rows_k[sl]])
    Fg = np.exp(acc)
    b1i = pd.DataFrame({"p50": _invert(Fg, 0.5), "p90": _invert(Fg, 0.9)}, index=gids)
    return b1c, b1i


def _invert(F: np.ndarray, q: float) -> np.ndarray:
    """First grid point where the (monotone) CDF reaches q; last point if never."""
    idx = (F >= q).argmax(axis=1)
    idx[~(F >= q).any(axis=1)] = len(GRID) - 1
    return GRID[idx]


def fit_calibration(pred: pd.DataFrame, groups: pd.DataFrame) -> pd.DataFrame:
    """Per-family multiplicative factors so p50 hits the median and p90 covers 90%."""
    r = pd.DataFrame({"fam": groups["repo_family"],
                      "r50": groups["span"] / pred["p50"],
                      "r90": groups["span"] / pred["p90"]}).dropna()
    f = r.groupby("fam", observed=True).agg(n=("r50", "size"), f50=("r50", "median"),
                                            f90=("r90", lambda s: s.quantile(0.9)))
    glob = pd.Series({"n": len(r), "f50": r["r50"].median(), "f90": r["r90"].quantile(0.9)})
    f.loc["__global__"] = glob
    return f


def _size_key(groups: pd.DataFrame) -> pd.Series:
    """'<family>|<size bucket index>' calibration key."""
    return groups["repo_family"].astype(str) + "|" + size_bucket(groups["n_known"]).astype(str)


def fit_calibration_sized(pred: pd.DataFrame, groups: pd.DataFrame) -> pd.DataFrame:
    """Per-family factors plus per-(family x size bucket) factors where the bucket has
    >= MIN_CAL_SIZE_SAMPLES calibration groups (B1c**). Large try groups are
    under-covered by the family-only factor (group-eta-plan.md P1 weak spot)."""
    f = fit_calibration(pred, groups)
    g = groups.loc[pred.index]
    r = pd.DataFrame({"k": _size_key(g), "r50": g["span"] / pred["p50"],
                      "r90": g["span"] / pred["p90"]}).dropna()
    fs = r.groupby("k").agg(n=("r50", "size"), f50=("r50", "median"),
                            f90=("r90", lambda s: s.quantile(0.9)))
    return pd.concat([f, fs[fs["n"] >= MIN_CAL_SIZE_SAMPLES]])


def apply_calibration(pred: pd.DataFrame, groups: pd.DataFrame, f: pd.DataFrame) -> pd.DataFrame:
    ok = f.index[f["n"] >= MIN_KEY_SAMPLES]
    fam = groups["repo_family"].astype(str).where(
        groups["repo_family"].astype(str).isin(ok), "__global__")
    # Size-aware tables (B1c**) also carry 'family|bucket' rows; prefer them when present.
    if ok.astype(str).str.contains("|", regex=False).any():
        sk = _size_key(groups)
        fam = sk.where(sk.isin(ok), fam)
    ff = f.reindex(fam.reindex(pred.index))
    p50 = pred["p50"].to_numpy() * ff["f50"].to_numpy()
    p90 = pred["p90"].to_numpy() * ff["f90"].to_numpy()
    # Factors are fitted independently; keep the quantiles ordered.
    return pd.DataFrame({"p50": p50, "p90": np.fmax(p90, p50)}, index=pred.index)


# ---------------------------------------------------------------- metrics ---

def metrics(y: np.ndarray, p50: np.ndarray, p90: np.ndarray) -> dict:
    y, p50, p90 = map(np.asarray, (y, p50, p90))
    pin = np.where(y >= p90, 0.9 * (y - p90), 0.1 * (p90 - y))
    return {
        "n": len(y),
        "med_span_min": float(np.median(y) / 60) if len(y) else np.nan,
        "mae_min": float(np.mean(np.abs(y - p50)) / 60),
        "w2x": float(np.mean((y >= p50 / 2) & (y <= p50 * 2))),
        "cov90": float(np.mean(y <= p90)),
        "pinball90_min": float(np.mean(pin) / 60),
    }


# ------------------------------------------------------- fit / predict all ---

ESTIMATORS = ("B0", "B1c", "B1i", "B1c*", "B1i*", "B1c**")


def fit_all(train_groups: pd.DataFrame, known: pd.DataFrame, d0: float) -> dict:
    """Fit every estimator from groups that completed before d0.

    B1c/B1i use the full window; the calibrated variants refit B1 on the window
    minus its last CAL_DAYS and fit per-family factors on those held-out days.
    """
    # Calibrate on the last CAL_DAYS *present* days (excluded days leave gaps).
    days = sorted(train_groups["day"].unique())
    cal_days = set(days[-CAL_DAYS:]) if len(days) > CAL_DAYS else set(days[-1:])
    is_cal = train_groups["day"].isin(cal_days)
    fit_g, cal_g = train_groups[~is_cal], train_groups[is_cal]
    b1_fit = fit_b1(known[known["gid"].isin(fit_g.index)])
    c_cal, i_cal = predict_b1(b1_fit, known[known["gid"].isin(cal_g.index)])
    return {
        "b0": fit_b0(train_groups),
        "b1_full": fit_b1(known[known["gid"].isin(train_groups.index)]),
        "b1_fit": b1_fit,
        "cal_c": fit_calibration(c_cal, cal_g.loc[c_cal.index]),
        "cal_i": fit_calibration(i_cal, cal_g.loc[i_cal.index]),
        "cal_cs": fit_calibration_sized(c_cal, cal_g),
        "trained_before": d0,
        "n_train_groups": len(train_groups),
    }


def predict_all(model: dict, groups: pd.DataFrame, known: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """groups: indexed by gid with repo_family, n_known. known: gid, repo_family, normalized_name."""
    out = {"B0": predict_b0(model["b0"], groups)}
    out["B1c"], out["B1i"] = predict_b1(model["b1_full"], known)
    c, i = predict_b1(model["b1_fit"], known)
    out["B1c*"] = apply_calibration(c, groups, model["cal_c"])
    out["B1i*"] = apply_calibration(i, groups, model["cal_i"])
    if "cal_cs" in model:  # pickles fitted before B1c** existed lack it
        out["B1c**"] = apply_calibration(c, groups, model["cal_cs"])
    return {k: v.reindex(groups.index) for k, v in out.items()}


# ------------------------------------------------------------------- eval ---

def parse_days(spec: str) -> list[dt.date]:
    out = []
    for part in spec.split(","):
        if ".." in part:
            a, b = part.split("..")
            out += list(_days(dt.date.fromisoformat(a), dt.date.fromisoformat(b)))
        else:
            out.append(dt.date.fromisoformat(part))
    return out


def _ts(d: dt.date) -> float:
    return dt.datetime.combine(d, dt.time(), tzinfo=dt.timezone.utc).timestamp()


def evaluate(data_dir: Path, test_days: list[dt.date], excluded: set[dt.date]) -> None:
    t = time.time()
    tasks = load_tasks(data_dir)
    data_end = tasks["created"].max()
    groups, known = build_groups(tasks, data_end, excluded)
    del tasks
    use = groups[groups["usable"]]
    print(f"[eval] {len(groups):,} groups, {len(use):,} usable; built in {time.time() - t:.0f}s", flush=True)

    preds = []
    for d in test_days:
        if d in excluded:
            continue
        d0 = _ts(d)
        test = use[(use["t0"] >= d0) & (use["t0"] < d0 + 86400)]
        # Training: groups that started in the prior window AND finished before the
        # test day starts (labels must be observable at prediction time).
        tr = use[(use["t0"] >= d0 - TRAIN_DAYS * 86400) & (use["end"] < d0)]
        if len(test) == 0 or len(tr) < 100:
            print(f"[eval] {d}: skipped (test={len(test)}, train={len(tr)})")
            continue
        model = fit_all(tr, known, d0)
        out = predict_all(model, test, known[known["gid"].isin(test.index)])
        for name, p in out.items():
            p = p.reindex(test.index)
            preds.append(pd.DataFrame({
                "day": d, "gid": test.index, "estimator": name,
                "repo_family": test["repo_family"].astype(str).to_numpy(),
                "n_known": test["n_known"].to_numpy(), "span": test["span"].to_numpy(),
                "p50": p["p50"].to_numpy(), "p90": p["p90"].to_numpy()}))
        print(f"[eval] {d}: test={len(test)} train={len(tr)} ({time.time() - t:.0f}s)", flush=True)

    P = pd.concat(preds, ignore_index=True).dropna(subset=["p50", "p90"])
    P.to_parquet(data_dir / "predictions.parquet", index=False)
    write_report(data_dir / "report.md", P, groups, test_days, excluded)
    print(f"[eval] wrote {data_dir / 'report.md'}")


def md(df: pd.DataFrame) -> str:
    """Minimal markdown table (avoids a tabulate dependency)."""
    cols = list(df.columns)
    out = ["| " + " | ".join(map(str, cols)) + " |", "|" + "---|" * len(cols)]
    out += ["| " + " | ".join(str(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join(out)


def _table(P: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    rows = []
    for k, x in P.groupby(by, observed=True):
        rows.append({**dict(zip(by, k if isinstance(k, tuple) else (k,))),
                     **metrics(x["span"], x["p50"], x["p90"])})
    return pd.DataFrame(rows).round(3)


def write_report(path: Path, P: pd.DataFrame, groups: pd.DataFrame, test_days, excluded) -> None:
    u = groups[groups["usable"]]
    lines = [
        f"# Group ETA edge-free baselines — {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%MZ}",
        "",
        f"Test days: {test_days[0]}..{test_days[-1]} (excluding {sorted(d.isoformat() for d in excluded if test_days[0] <= d <= test_days[-1])}).",
        f"Prediction at decision resolved + {PRED_DELAY_S}s; label = original-graph completion - t0.",
        f"Groups: {len(groups):,} total, {len(u):,} usable (>= {MIN_GROUP_TASKS} known tasks, all resolved). "
        f"Share of usable groups with decision task: {u['has_decision'].mean():.3f}; "
        f"with late (post-t_pred) tasks: {(u['n_late'] > 0).mean():.3f}.",
        "",
        "Proposed internal bars (try): cov90 in [0.85, 0.93], w2x >= 0.70.",
        "",
        "## All families",
        "",
        md(_table(P, ["estimator"])),
        "",
        "## By family",
        "",
        md(_table(P, ["repo_family", "estimator"])),
        "",
        "## try, by known-size bucket",
        "",
    ]
    T = P[P["repo_family"] == "try"].assign(
        size=pd.cut(P["n_known"], SIZE_BUCKETS, right=False).astype(str))
    lines += [md(_table(T, ["size", "estimator"])), ""]
    lines += ["## try, per day (B1c* only)", "",
              md(_table(T[T["estimator"] == "B1c*"], ["day"])), ""]
    path.write_text("\n".join(lines))


# ------------------------------------------------------------------- main ---

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract")
    e.add_argument("--from", dest="start", required=True, type=dt.date.fromisoformat)
    e.add_argument("--to", dest="end", required=True, type=dt.date.fromisoformat)
    e.add_argument("--data-dir", required=True, type=Path)
    e.add_argument("--force", action="store_true")
    v = sub.add_parser("eval")
    v.add_argument("--data-dir", required=True, type=Path)
    v.add_argument("--test-days", required=True)
    v.add_argument("--exclude", default="", help="extra excluded days, same syntax as --test-days")
    a = ap.parse_args(argv)
    if a.cmd == "extract":
        extract(a.data_dir.expanduser(), a.start, a.end, a.force)
    else:
        excl = set(DEFAULT_EXCLUDED) | (set(parse_days(a.exclude)) if a.exclude else set())
        evaluate(a.data_dir.expanduser(), parse_days(a.test_days), excl)


if __name__ == "__main__":
    main()
