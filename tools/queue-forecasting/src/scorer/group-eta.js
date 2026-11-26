/**
 * Edge-free task-group ETA estimators (B0, B1c, B1c*), ported from
 * trainer/scripts/group_eta_baselines.py (predict_b0, predict_b1's B1c branch,
 * apply_calibration) and trainer/scripts/group_eta_live.py (live_frames).
 *
 * The model is the JSON subset written by `group_eta_live.py fit|export-json`
 * (format "group-eta-tables-v1"). Everything here is pure: no DB, no network.
 *
 * Semantics kept from the Python side:
 *  - known tasks are those created <= t_pred; a group needs >= min_group_tasks;
 *  - group repo_family = mode of the known tasks' families (ties: smallest);
 *  - t0 = earliest created; estimates are spans from t0;
 *  - B0: (family, size bucket) -> family -> global quantile lookup;
 *  - B1c: max over tasks of the per-task offset quantiles, key family\x1fname
 *    falling back to the family table; tasks of an unknown family are dropped;
 *  - B1c*: B1c on the calibration-split fit, times per-family factors (family
 *    needs n >= min_key_samples, else __global__), then p90 = max(p90, p50).
 */

export const MODEL_FORMAT = 'group-eta-tables-v1';

/**
 * Default estimator wherever one estimate is shown (dashboard, API `recommended`).
 * B1c since 2026-11-24: offline try 10-08..11-20 w2x 0.774 / cov90 0.937 / pinball 40.0 min,
 * better than B1c* (0.731 / 0.889 / 47.1) in every week; live week 1 0.754 / 0.918
 * (group-eta-plan.md "Update 2026-11-24"). Fallbacks if it is not live.
 */
export const ESTIMATOR_PREFERENCE = ['B1c', 'B1c*', 'B0'];

/**
 * Prefix of the B2* in-flight re-estimates ('B2*+15m' ... 'B2*+120m', trainer
 * scripts/group_eta_live.py --b2). Same table, but a re-estimate at t_pred + c for
 * still-running groups, scored on remaining time (weekly report §6b). Internal
 * shadow only: kept out of static scorecards, the API and the playground.
 */
export const INFLIGHT_PREFIX = 'B2*+';
export const isStaticEstimator = name => !name.startsWith(INFLIGHT_PREFIX);

/** First preferred estimator present in `names`, else the first name, else null. */
export function pickEstimator(names) {
  for (const e of ESTIMATOR_PREFERENCE) if (names.includes(e)) return e;
  return names[0] ?? null;
}

export function validateModel(m) {
  if (!m || m.format !== MODEL_FORMAT) throw new Error(`group-eta model: expected format ${MODEL_FORMAT}`);
  for (const k of ['b0', 'b1_full', 'b1_fit', 'cal_c', 'params']) {
    if (!m[k]) throw new Error(`group-eta model: missing ${k}`);
  }
  return m;
}

/** pd.cut(n, SIZE_BUCKETS, right=False, labels=False); null bounds are +inf. */
export function sizeBucket(n, buckets) {
  for (let i = 0; i + 1 < buckets.length; i++) {
    const lo = buckets[i];
    const hi = buckets[i + 1] == null ? Infinity : buckets[i + 1];
    if (n >= lo && n < hi) return i;
  }
  return null;
}

/** pandas Series.mode().iat[0] for strings: most frequent, ties -> sorted first. */
export function mode(values) {
  const counts = new Map();
  for (const v of values) counts.set(v, (counts.get(v) || 0) + 1);
  let best = null;
  let bestN = -1;
  for (const [v, n] of counts) {
    if (n > bestN || (n === bestN && v < best)) { best = v; bestN = n; }
  }
  return best;
}

const fin = (x) => typeof x === 'number' && Number.isFinite(x);

/**
 * @param {object} model  parsed live_model.json
 * @param {number} tPred  epoch seconds
 * @param {{repoFamily?: string|null, normalizedName?: string|null, created: number}[]} tasks
 * @returns {{t0, tPred, nKnown, repoFamily, estimates: {estimator, p50_s, p90_s}[]} | {skipped: string}}
 */
export function predictGroup(model, tPred, tasks) {
  const known = [];
  for (const t of tasks) {
    if (!fin(t.created) || t.created > tPred) continue;
    known.push({
      fam: t.repoFamily || 'unknown',      // LIVE_TASKS_SQL coalesce(repo_family, 'unknown')
      name: t.normalizedName || '?',       // coalesce(normalized_name, '?')
      created: t.created,
    });
  }
  const p = model.params;
  if (known.length < p.min_group_tasks) {
    return { skipped: `fewer than ${p.min_group_tasks} tasks were known at prediction time (${known.length})` };
  }
  const t0 = Math.min(...known.map(t => t.created));
  const fam = mode(known.map(t => t.fam));
  const nKnown = known.length;

  const estimates = [];
  const push = (estimator, r) => {
    if (r && fin(r[0]) && fin(r[1])) estimates.push({ estimator, p50_s: r[0], p90_s: r[1] });
  };
  push('B0', predictB0(model.b0, fam, nKnown, p.size_buckets));
  push('B1c', predictB1c(model.b1_full, known));
  const c = predictB1c(model.b1_fit, known);
  push('B1c*', c && calibrate(model.cal_c, fam, c, p.min_key_samples));
  return { t0, tPred, nKnown, repoFamily: fam, estimates };
}

function predictB0(b0, fam, n, buckets) {
  const sb = sizeBucket(n, buckets);
  const byFs = sb == null ? null : b0.fs[fam]?.[String(sb)];
  if (byFs && fin(byFs[0])) return byFs;
  const byFam = b0.fam[fam];
  if (byFam && fin(byFam[0])) return byFam;
  return b0.glob;
}

function predictB1c(tables, known) {
  let p50 = -Infinity;
  let p90 = -Infinity;
  let any = false;
  for (const t of known) {
    const q = tables.q_key[`${t.fam}\x1f${t.name}`] ?? tables.q_fam[t.fam];
    if (!q) continue; // unknown family: dropped, as in predict_b1
    any = true;
    // groupby('gid').max() skips NaN per column
    if (fin(q[0]) && q[0] > p50) p50 = q[0];
    if (fin(q[1]) && q[1] > p90) p90 = q[1];
  }
  if (!any) return null;
  return [p50 === -Infinity ? NaN : p50, p90 === -Infinity ? NaN : p90];
}

function calibrate(cal, fam, [p50, p90], minKeySamples) {
  const f = cal[fam] && cal[fam].n >= minKeySamples ? cal[fam] : cal.__global__;
  if (!f) return null;
  // A NaN factor is exported as JSON null, and p50 * null is 0 in JS. Restore
  // NaN so the result matches Python (NaN, then dropped by the finite check).
  const c50 = p50 * (fin(f.f50) ? f.f50 : NaN);
  const c90 = p90 * (fin(f.f90) ? f.f90 : NaN);
  // np.fmax: NaN loses to a number
  const out90 = !fin(c90) ? c50 : !fin(c50) ? c90 : Math.max(c90, c50);
  return [c50, out90];
}
