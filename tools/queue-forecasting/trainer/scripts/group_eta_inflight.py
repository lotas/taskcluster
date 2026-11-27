"""Task-group ETA, in-flight re-estimates (group-eta-plan.md B2), offline eval.

The static estimators (group_eta_baselines.py) predict once, at t_pred. B2
re-estimates while the group runs: at checkpoint tau = t_pred + c,

  - tasks of the original graph that resolved by tau drop out;
  - every still-running task i contributes its offset quantile conditioned on
    offset > e (e = tau - t0):  Q_i(q | > e) = F_i^-1(F_i(e) + q (1 - F_i(e))),
    with F_i the per-(family, name) offset CDF from fit_b1 (family fallback);
  - group quantile = max over running tasks (comonotone, like B1c).

B2* = B2 with per-(checkpoint, family) multiplicative factors on REMAINING time
(r50 hits the median, r90 covers 90%), fitted on the last g.CAL_DAYS days of the
training window with the same (full-window) B1 tables. The comonotone max of many
conditional p90s is wide (remaining p90 ~4-7x actual in a 3-day smoke test).

When history has (almost) no mass above e (F_i(e) >= TAIL_F), the task is past
everything seen in training; it gets e * TAIL_MULT[q] instead.

Compared at the same checkpoint against static B1c floored at e (the honest
"static" answer mid-flight: never claim a finish in the past). Scored on
remaining time R = span - e, for groups still running at tau.

"Resolved by tau" uses the task's final resolution (last run). A task that
resolved and was later rerun counts as running: at tau that is either true
(rerun pending) or slightly conservative.

Usage (host; same cache as group_eta_baselines):
  python -m scripts.group_eta_inflight --data-dir ~/qf-group-eta --test-days 2026-10-08..2026-11-20
"""
from __future__ import annotations

import argparse
import datetime as dt
import time
from pathlib import Path

import numpy as np
import pandas as pd

from scripts import group_eta_baselines as g

CHECKPOINTS_MIN = (15, 30, 60, 120)
TAIL_F = 0.995
TAIL_MULT = {0.5: 1.1, 0.9: 1.5}
MIN_REMAINING_S = 60.0  # floor for remaining-time predictions/labels in ratio metrics


def _cdf_stack(model):
    """(keys -> row index, CDF matrix [n_keys, len(GRID)]) incl. family fallbacks."""
    q, cdf = g._lookup_tables(model)
    keys = list(cdf.keys())
    return {k: i for i, k in enumerate(keys)}, np.stack([cdf[k] for k in keys]), q


def conditional_quantiles(F: np.ndarray, e: np.ndarray, q: float) -> np.ndarray:
    """Per row: q-quantile of the CDF row F[i] (on g.GRID) conditioned on value > e[i]."""
    j = np.searchsorted(g.GRID, e, side="right") - 1
    Fe = np.where(j >= 0, F[np.arange(len(F)), np.clip(j, 0, None)], 0.0)
    u = Fe + q * (1.0 - Fe)
    idx = (F >= u[:, None]).argmax(axis=1)
    out = g.GRID[idx]
    out = np.maximum(out, e)  # grid resolution: never below elapsed
    tail = Fe >= TAIL_F
    out[tail] = e[tail] * TAIL_MULT[q]
    return out


def predict_b2(model, known: pd.DataFrame, groups: pd.DataFrame, c_s: float) -> pd.DataFrame:
    """B2 at tau = t_pred + c_s for each group. known: one row per known task with
    gid, repo_family, normalized_name, t0, last_resolved. Returns p50/p90 spans
    (from t0) indexed by gid, only for groups with >= 1 task running at tau."""
    kidx, CDF, q = _cdf_stack(model)
    tau = known["gid"].map(groups["t_pred"]).to_numpy() + c_s
    running = known["last_resolved"].to_numpy() > tau
    rk = g._resolve_keys(model, known)
    have = rk.isin(kidx.keys()).to_numpy()
    sel = running & have
    if not sel.any():
        return pd.DataFrame(columns=["p50", "p90"], dtype=float)
    k = known[sel]
    e = (tau - known["t0"].to_numpy())[sel]
    rows = rk[sel].map(kidx).to_numpy()
    out = {"gid": k["gid"].to_numpy()}
    for qq, name in ((0.5, "p50"), (0.9, "p90")):
        vals = np.empty(len(k))
        for s in range(0, len(k), 50000):
            sl = slice(s, s + 50000)
            vals[sl] = conditional_quantiles(CDF[rows[sl]], e[sl], qq)
        out[name] = vals
    return pd.DataFrame(out).groupby("gid").max()


def remaining_factors(pred: pd.DataFrame, groups: pd.DataFrame, e: pd.Series) -> pd.DataFrame:
    """Per-family (+ __global__) factors on remaining time; pred/e indexed by gid."""
    R = np.fmax(groups.loc[pred.index, "span"] - e.loc[pred.index], MIN_REMAINING_S)
    r = pd.DataFrame({"fam": groups.loc[pred.index, "repo_family"].astype(str),
                      "a50": R / np.fmax(pred["p50"] - e.loc[pred.index], MIN_REMAINING_S),
                      "a90": R / np.fmax(pred["p90"] - e.loc[pred.index], MIN_REMAINING_S)}).dropna()
    f = r.groupby("fam").agg(n=("a50", "size"), f50=("a50", "median"), f90=("a90", lambda x: x.quantile(0.9)))
    f.loc["__global__"] = [len(r), r["a50"].median(), r["a90"].quantile(0.9)]
    return f


def apply_remaining_factors(pred: pd.DataFrame, groups: pd.DataFrame, e: pd.Series, f: pd.DataFrame) -> pd.DataFrame:
    fam = groups.loc[pred.index, "repo_family"].astype(str)
    fam = fam.where(fam.isin(f.index[f["n"] >= g.MIN_KEY_SAMPLES]), "__global__")
    ff = f.reindex(fam)
    ee = e.loc[pred.index].to_numpy()
    r50 = np.fmax(pred["p50"].to_numpy() - ee, MIN_REMAINING_S) * ff["f50"].to_numpy()
    r90 = np.fmax(pred["p90"].to_numpy() - ee, MIN_REMAINING_S) * ff["f90"].to_numpy()
    return pd.DataFrame({"p50": ee + r50, "p90": ee + np.fmax(r90, r50)}, index=pred.index)


def b2_live(model, known: pd.DataFrame, groups: pd.DataFrame, c_s: float):
    """(B2 predictions, elapsed) for the groups still running at t_pred + c_s."""
    tau = groups["t_pred"] + c_s
    e = tau - groups["t0"]
    live = groups.index[(groups["end"] > tau).to_numpy()]
    pred = predict_b2(model, known[known["gid"].isin(live)], groups, c_s).reindex(live).dropna()
    return pred, e


def fit_b2_factors(b1, cal_groups: pd.DataFrame, cal_known: pd.DataFrame) -> dict[int, pd.DataFrame]:
    """B2* remaining-time factors per checkpoint (minutes -> per-family frame).

    cal_groups: completed groups (t0, t_pred, end, span, repo_family); cal_known: their
    known tasks (gid, repo_family, normalized_name, t0, last_resolved). Same B1 tables
    as prediction (in-sample on the calibration days, as in the offline eval)."""
    out = {}
    for c in CHECKPOINTS_MIN:
        cp, ce = b2_live(b1, cal_known, cal_groups, c * 60.0)
        out[c] = remaining_factors(cp, cal_groups, ce)
    return out


def predict_b2_star(b1, factors: pd.DataFrame, groups: pd.DataFrame, known: pd.DataFrame, c_s: float) -> pd.DataFrame:
    """B2* spans (from t0), floored at elapsed, for groups with >= 1 task running at
    tau = t_pred + c_s. groups: t0, t_pred, repo_family (indexed by gid). known: gid,
    repo_family, normalized_name, t0, last_resolved (inf = not resolved yet)."""
    raw = predict_b2(b1, known, groups, c_s).dropna()
    if raw.empty:
        return raw
    e = groups["t_pred"] + c_s - groups["t0"]
    out = apply_remaining_factors(raw, groups, e, factors)
    ee = e.loc[out.index].to_numpy()
    return out.assign(p50=np.fmax(out["p50"].to_numpy(), ee), p90=np.fmax(out["p90"].to_numpy(), ee))


def evaluate(data_dir: Path, test_days, excluded, label: str = "ran") -> pd.DataFrame:
    t = time.time()
    tasks = g.load_tasks(data_dir)
    groups, known = g.build_groups(tasks, tasks["created"].max(), excluded, label)
    del tasks
    use = groups[groups["usable"]]
    print(f"[inflight] {len(use):,} usable groups ({time.time() - t:.0f}s)", flush=True)
    rows = []
    for d in test_days:
        if d in excluded:
            continue
        d0 = g._ts(d)
        test = use[(use["t0"] >= d0) & (use["t0"] < d0 + 86400)]
        tr = use[(use["t0"] >= d0 - g.TRAIN_DAYS * 86400) & (use["end"] < d0)]
        if len(test) == 0 or len(tr) < 100:
            continue
        model = g.fit_b1(known[known["gid"].isin(tr.index)])
        cal_days = sorted(tr["day"].unique())[-g.CAL_DAYS:]
        cal = tr[tr["day"].isin(cal_days)]
        ck = known[known["gid"].isin(cal.index)]
        tk = known[known["gid"].isin(test.index)]
        static, _ = g.predict_b1(model, tk)
        static = static.reindex(test.index)
        factors = fit_b2_factors(model, cal, ck)
        for c in CHECKPOINTS_MIN:
            c_s = c * 60.0
            tau = test["t_pred"] + c_s
            e = tau - test["t0"]
            live = test.index[(test["end"] > tau).to_numpy()]
            if len(live) == 0:
                continue
            b2 = predict_b2(model, tk[tk["gid"].isin(live)], test, c_s).reindex(live)
            b2s = predict_b2_star(model, factors[c], test, tk[tk["gid"].isin(live)], c_s).reindex(live)
            base = pd.DataFrame({"day": d, "gid": live, "c_min": c,
                                 "repo_family": test.loc[live, "repo_family"].astype(str).to_numpy(),
                                 "n_known": test.loc[live, "n_known"].to_numpy(),
                                 "span": test.loc[live, "span"].to_numpy(),
                                 "e": e.loc[live].to_numpy()})
            for name, p in (("B1c-static", static.loc[live]), ("B2", b2), ("B2*", b2s)):
                rows.append(base.assign(estimator=name,
                                        p50=np.fmax(p["p50"].to_numpy(), base["e"]),
                                        p90=np.fmax(p["p90"].to_numpy(), base["e"])))
        print(f"[inflight] {d}: test={len(test)} ({time.time() - t:.0f}s)", flush=True)
    P = pd.concat(rows, ignore_index=True).dropna(subset=["p50", "p90"])
    return P


def remaining_metrics(x: pd.DataFrame) -> dict:
    R = np.fmax(x["span"] - x["e"], MIN_REMAINING_S)
    r50 = np.fmax(x["p50"] - x["e"], MIN_REMAINING_S)
    r90 = np.fmax(x["p90"] - x["e"], MIN_REMAINING_S)
    m = g.metrics(R, r50, r90)
    m["med_remaining_min"] = m.pop("med_span_min")
    return m


def table(P: pd.DataFrame, by) -> pd.DataFrame:
    out = []
    for k, x in P.groupby(by, observed=True):
        out.append({**dict(zip(by, k)), **remaining_metrics(x)})
    return pd.DataFrame(out).round(3)


def write_report(path: Path, P: pd.DataFrame, test_days) -> None:
    T = P[P["repo_family"] == "try"].assign(
        size=pd.cut(P["n_known"], g.SIZE_BUCKETS, right=False).astype(str))
    lines = [
        f"# Group ETA in-flight re-estimates (B2) — {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%MZ}",
        "",
        f"Test days {test_days[0]}..{test_days[-1]}. Checkpoint tau = t_pred + c; groups still running at tau only.",
        "Metrics are on REMAINING time (span - elapsed), floored at 60 s. B1c-static = the t_pred B1c estimate floored at elapsed.",
        "",
        "## All families, by checkpoint", "", g.md(table(P, ["c_min", "estimator"])), "",
        "## try, by checkpoint", "", g.md(table(T, ["c_min", "estimator"])), "",
        "## try, by size bucket (all checkpoints)", "", g.md(table(T, ["size", "estimator"])), "",
        "## try 200+ tasks, by checkpoint", "", g.md(table(T[T["n_known"] >= 200], ["c_min", "estimator"])), "",
    ]
    path.write_text("\n".join(lines))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", required=True, type=Path)
    ap.add_argument("--test-days", required=True)
    ap.add_argument("--label", choices=g.LABEL_MODES, default="ran",
                    help="'ran': blocked (never-ran, deadline-exceeded) tasks don't set the group end")
    ap.add_argument("--out-dir", type=Path, help="report/predictions dir (default: --data-dir)")
    a = ap.parse_args(argv)
    data_dir = a.data_dir.expanduser()
    out_dir = a.out_dir.expanduser() if a.out_dir else data_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    days = g.parse_days(a.test_days)
    P = evaluate(data_dir, days, set(g.DEFAULT_EXCLUDED), a.label)
    P.to_parquet(out_dir / "inflight_predictions.parquet", index=False)
    write_report(out_dir / "report-inflight.md", P, days)
    print(f"[inflight] wrote {out_dir / 'report-inflight.md'}")


if __name__ == "__main__":
    main()
