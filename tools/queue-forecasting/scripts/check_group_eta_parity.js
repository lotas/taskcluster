// Compare src/scorer/group-eta.js against the Python live shadow rows.
//   node scripts/check_group_eta_parity.js [model.json] [nGroups]
// Uses the groups predicted by the same model_version as the JSON; rebuilds
// each group's known tasks from queue_forecast_tasks exactly like
// trainer/scripts/group_eta_live.py (created in [t0, t_pred]).
import fs from 'fs';
import pg from 'pg';
import { predictGroup, validateModel } from '../src/scorer/group-eta.js';

const file = process.argv[2] || 'trainer/data/group_eta/live_model.json';
const n = parseInt(process.argv[3] || '50', 10);
const model = validateModel(JSON.parse(fs.readFileSync(file, 'utf8')));
const pool = new pg.Pool({ connectionString: process.env.DATABASE_URL, max: 1 });

const groups = await pool.query(`
  SELECT task_group_id, extract(epoch FROM min(t0))::float8 AS t0, extract(epoch FROM min(t_pred))::float8 AS t_pred,
         json_object_agg(estimator, json_build_array(p50_s, p90_s, n_known)) AS est
  FROM queue_forecast_group_predictions_shadow
  WHERE model_version = $1 GROUP BY 1 ORDER BY max(predicted_at) DESC LIMIT $2`, [model.version, n]);
let checked = 0, bad = 0, drift = 0;
for (const g of groups.rows) {
  const t = await pool.query(`
    SELECT coalesce(repo_family, 'unknown') AS repo_family, coalesce(normalized_name, '?') AS normalized_name,
           extract(epoch FROM task_created)::float8 AS created
    FROM queue_forecast_tasks
    WHERE task_group_id = $1 AND task_created >= to_timestamp($2) AND task_created <= to_timestamp($3)`,
  [g.task_group_id, g.t0, g.t_pred]);
  const out = predictGroup(model, g.t_pred, t.rows.map(r => ({ repoFamily: r.repo_family, normalizedName: r.normalized_name, created: r.created })));
  for (const [est, [p50, p90, nk]] of Object.entries(g.est)) {
    if (!model.estimators.includes(est)) continue;
    const e = out.estimates?.find(x => x.estimator === est);
    if (out.nKnown !== nk) { drift++; continue; } // tasks landed in the DB after the cron predicted
    checked++;
    const ok = e && Math.abs(e.p50_s - p50) < 1e-6 * Math.max(1, p50) && Math.abs(e.p90_s - p90) < 1e-6 * Math.max(1, p90) && out.nKnown === nk;
    if (!ok) { bad++; console.log('MISMATCH', g.task_group_id, est, { py: [p50, p90, nk], js: e ? [e.p50_s, e.p90_s, out.nKnown] : out }); }
  }
}
console.log(`${model.version}: ${groups.rows.length} groups, ${checked} estimates compared, ${bad} mismatches; ${drift} skipped (known-task count changed since the cron ran)`);
await pool.end();
process.exit(bad ? 1 : 0);
