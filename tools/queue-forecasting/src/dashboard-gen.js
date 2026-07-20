/**
 * Dashboard generator — queries Postgres + reads trainer manifests,
 * writes a static index.html + status.html to OUTPUT_DIR every INTERVAL_MS.
 */
import pg from 'pg';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = path.join(__dirname, '..');
const OUTPUT_DIR = process.env.DASHBOARD_OUTPUT_DIR || path.join(PROJECT_ROOT, 'dashboard-out');
const MODELS_DIR = path.join(PROJECT_ROOT, 'trainer', 'data', 'models');
const INTERVAL_MS = parseInt(process.env.DASHBOARD_INTERVAL_MS || '900000', 10); // 15 min
const AGGREGATIONS_WINDOW_DAYS = parseInt(process.env.AGGREGATIONS_WINDOW_DAYS || '30', 10);
const DAILY_HEALTH_WINDOW_DAYS = parseInt(process.env.DAILY_HEALTH_WINDOW_DAYS || '60', 10);

// Per-query timing: generate() fans out ~22 queries through a pool capped at
// max:3, so a single query slowing down as tables grow can stretch the whole
// Promise.all to many minutes with zero visibility into which one is at
// fault (observed live 2026-07-15: dashboard stale for 3.5+ days, generate()
// silent for 9+ minutes, no way to tell which of the 22 queries was slow).
function timed(label, promise) {
  const t0 = Date.now();
  return promise.then(
    (result) => {
      console.log(`[dashboard-gen] ${label}: ${((Date.now() - t0) / 1000).toFixed(1)}s`);
      return result;
    },
    (err) => {
      console.log(`[dashboard-gen] ${label} FAILED after ${((Date.now() - t0) / 1000).toFixed(1)}s: ${err.message}`);
      throw err;
    },
  );
}

function formatInterval(ms) {
  const totalSeconds = Math.round(ms / 1000);
  if (totalSeconds % 3600 === 0) return `${totalSeconds / 3600}h`;
  if (totalSeconds % 60 === 0) return `${totalSeconds / 60}m`;
  return `${totalSeconds}s`;
}
const DATABASE_URL = process.env.DATABASE_URL || 'postgresql://postgres@localhost:5433/forecasting';

// ─── DB Queries ──────────────────────────────────────────────────────────────

async function queryTableHealth(pool) {
  const { rows } = await pool.query(`
    SELECT 'queue_forecast_tasks' AS table_name,
           count(*)::bigint AS row_count,
           max(enriched_at) AS newest_ts
      FROM queue_forecast_tasks
    UNION ALL
    SELECT 'queue_forecast_task_runs',
           count(*)::bigint,
           greatest(max(pending_at), max(started_at), max(resolved_at))
      FROM queue_forecast_task_runs
    UNION ALL
    SELECT 'queue_forecast_worker_counts',
           count(*)::bigint,
           max(sampled_at)
      FROM queue_forecast_worker_counts
    UNION ALL
    SELECT 'queue_forecast_daily_health',
           count(*)::bigint,
           max(computed_at)
      FROM queue_forecast_daily_health
    UNION ALL
    SELECT 'queue_forecast_run_predictions',
           count(*)::bigint,
           max(predicted_at)
      FROM queue_forecast_run_predictions
    UNION ALL
    SELECT 'queue_forecast_worker_pools',
           count(*)::bigint,
           max(refreshed_at)
      FROM queue_forecast_worker_pools
    ORDER BY table_name;
  `);
  return rows;
}

async function queryFreshness(pool) {
  const { rows } = await pool.query(`
    SELECT
      now() AS checked_at,
      (SELECT max(pending_at)  FROM queue_forecast_task_runs) AS latest_pending,
      (SELECT max(started_at)  FROM queue_forecast_task_runs) AS latest_started,
      (SELECT max(resolved_at) FROM queue_forecast_task_runs) AS latest_resolved,
      (SELECT max(enriched_at) FROM queue_forecast_tasks)     AS latest_enriched,
      (SELECT max(sampled_at)  FROM queue_forecast_worker_counts) AS latest_worker_sample,
      (SELECT max(computed_at) FROM queue_forecast_daily_health)  AS latest_daily_health;
  `);
  return rows[0];
}

async function queryIngestion(pool) {
  const { rows } = await pool.query(`
    WITH windows(label, span) AS (
      VALUES
        ('5m',  interval '5 minutes'),
        ('15m', interval '15 minutes'),
        ('1h',  interval '1 hour'),
        ('6h',  interval '6 hours'),
        ('24h', interval '24 hours')
    )
    SELECT
      w.label,
      count(*) FILTER (WHERE r.pending_at  >= now() - w.span) AS pending_rows,
      count(*) FILTER (WHERE r.started_at  >= now() - w.span) AS started_rows,
      count(*) FILTER (WHERE r.resolved_at >= now() - w.span) AS resolved_rows
    FROM windows w
    CROSS JOIN queue_forecast_task_runs r
    GROUP BY w.label, w.span
    ORDER BY w.span;
  `);
  return rows;
}

async function queryOpenIssues(pool) {
  const { rows } = await pool.query(`
    SELECT 'unenriched_tasks' AS metric, count(*)::bigint AS value
      FROM queue_forecast_tasks WHERE metadata_name IS NULL
    UNION ALL
    SELECT 'unresolved_runs', count(*)::bigint
      FROM queue_forecast_task_runs WHERE resolved_at IS NULL
    UNION ALL
    SELECT 'unresolved_runs_older_than_2h', count(*)::bigint
      FROM queue_forecast_task_runs
     WHERE resolved_at IS NULL AND pending_at < now() - interval '2 hours'
    UNION ALL
    SELECT 'worker_samples_last_30m', count(DISTINCT sampled_at)::bigint
      FROM queue_forecast_worker_counts WHERE sampled_at >= now() - interval '30 minutes'
    UNION ALL
    SELECT 'daily_health_rows_last_7d', count(*)::bigint
      FROM queue_forecast_daily_health WHERE sample_date >= current_date - 7
    ORDER BY metric;
  `);
  return rows;
}

async function queryDailyHealth(pool) {
  const { rows } = await pool.query(`
    SELECT
      sample_date,
      n_total, n_completed, n_failed, n_exception,
      completion_rate, exception_rate, stuck_pending_rate,
      wait_p99_s, run_p99_s,
      total_capacity_p50, total_running_p50, utilization_p50,
      n_worker_samples,
      flag_exception_spike, flag_stuck_pending_spike,
      flag_wait_p99_spike, flag_volume_anomaly, flag_low_completion,
      flag_capacity_drop, flag_capacity_spike, flag_low_utilization,
      flag_sampler_offline,
      is_anomalous, anomaly_reasons
    FROM queue_forecast_daily_health
    WHERE sample_date >= current_date - $1::int
    ORDER BY sample_date DESC;
  `, [DAILY_HEALTH_WINDOW_DAYS]);
  return rows;
}

async function queryRecentResolutions(pool) {
  const { rows } = await pool.query(`
    SELECT reason_resolved, count(*) AS n
      FROM queue_forecast_task_runs
     WHERE resolved_at >= now() - interval '1 hour'
     GROUP BY reason_resolved
     ORDER BY n DESC
     LIMIT 10;
  `);
  return rows;
}

async function queryTodayHourly(pool) {
  const { rows } = await pool.query(`
    WITH hours AS (
      SELECT generate_series(
        date_trunc('day', now() AT TIME ZONE 'UTC'),
        date_trunc('hour', now() AT TIME ZONE 'UTC'),
        interval '1 hour'
      ) AS hour_start
    )
    SELECT
      to_char(h.hour_start AT TIME ZONE 'UTC', 'HH24:00') AS hour,
      count(*) FILTER (WHERE r.pending_at  >= h.hour_start AND r.pending_at  < h.hour_start + interval '1 hour') AS pending,
      count(*) FILTER (WHERE r.started_at  >= h.hour_start AND r.started_at  < h.hour_start + interval '1 hour') AS started,
      count(*) FILTER (WHERE r.resolved_at >= h.hour_start AND r.resolved_at < h.hour_start + interval '1 hour') AS resolved,
      count(*) FILTER (WHERE r.resolved_at >= h.hour_start AND r.resolved_at < h.hour_start + interval '1 hour'
                        AND r.reason_resolved = 'completed') AS completed,
      count(*) FILTER (WHERE r.resolved_at >= h.hour_start AND r.resolved_at < h.hour_start + interval '1 hour'
                        AND r.reason_resolved = 'failed') AS failed
    FROM hours h
    CROSS JOIN queue_forecast_task_runs r
    WHERE r.pending_at >= date_trunc('day', now() AT TIME ZONE 'UTC')
       OR r.started_at >= date_trunc('day', now() AT TIME ZONE 'UTC')
       OR r.resolved_at >= date_trunc('day', now() AT TIME ZONE 'UTC')
    GROUP BY h.hour_start
    ORDER BY h.hour_start;
  `);
  return rows;
}

async function queryPredictorHealth(pool) {
  const { rows } = await pool.query(`
    WITH recent AS (
      SELECT
        predicted_at,
        wait_model_version,
        duration_model_version,
        (input_features->>'enriched_at_predict')::boolean    AS enriched,
        input_features->'baselines'->'wait'->>'level'        AS wait_level,
        input_features->'baselines'->'duration'->>'level'    AS dur_level,
        (input_features->'baselines'->'wait'->>'p50')        AS wait_bl_p50,
        (input_features->'baselines'->'duration'->>'p50')    AS dur_bl_p50
      FROM queue_forecast_run_predictions
      WHERE predicted_at >= now() - interval '1 hour'
    ),
    counts AS (
      SELECT
        (SELECT count(*) FROM queue_forecast_run_predictions
           WHERE predicted_at >= now() - interval '5 minutes')  AS preds_5m,
        (SELECT count(*) FROM recent)                            AS preds_1h,
        (SELECT count(*) FROM queue_forecast_run_predictions
           WHERE predicted_at >= now() - interval '24 hours')   AS preds_24h,
        (SELECT max(predicted_at) FROM queue_forecast_run_predictions) AS last_predicted_at,
        (SELECT count(*) FROM queue_forecast_task_runs r
           WHERE r.resolved_at IS NULL
             AND NOT EXISTS (
               SELECT 1 FROM queue_forecast_run_predictions p
                WHERE p.task_id = r.task_id AND p.run_id = r.run_id))  AS catchup_backlog
    ),
    wait_versions AS (
      SELECT json_agg(json_build_object('version', wait_model_version, 'n', n)
                      ORDER BY n DESC) AS versions
      FROM (SELECT wait_model_version, count(*) AS n FROM recent
            GROUP BY wait_model_version) v
    ),
    dur_versions AS (
      SELECT json_agg(json_build_object('version', duration_model_version, 'n', n)
                      ORDER BY n DESC) AS versions
      FROM (SELECT duration_model_version, count(*) AS n FROM recent
            GROUP BY duration_model_version) v
    ),
    wait_coverage AS (
      SELECT json_agg(json_build_object('level', coalesce(wait_level, '(null)'), 'n', n)
                      ORDER BY n DESC) AS coverage
      FROM (SELECT wait_level, count(*) AS n FROM recent
            GROUP BY wait_level) c
    ),
    dur_coverage AS (
      SELECT json_agg(json_build_object('level', coalesce(dur_level, '(null)'), 'n', n)
                      ORDER BY n DESC) AS coverage
      FROM (SELECT dur_level, count(*) AS n FROM recent
            GROUP BY dur_level) c
    ),
    rates AS (
      SELECT
        avg(CASE WHEN enriched IS TRUE THEN 1.0 ELSE 0.0 END)                AS enriched_rate,
        avg(CASE WHEN wait_bl_p50 IS NULL THEN 1.0 ELSE 0.0 END)             AS wait_nan_rate,
        avg(CASE WHEN dur_bl_p50  IS NULL THEN 1.0 ELSE 0.0 END)             AS dur_nan_rate
      FROM recent
    )
    SELECT
      counts.preds_5m, counts.preds_1h, counts.preds_24h,
      counts.last_predicted_at, counts.catchup_backlog,
      wait_versions.versions  AS wait_versions,
      dur_versions.versions   AS duration_versions,
      wait_coverage.coverage  AS wait_coverage,
      dur_coverage.coverage   AS duration_coverage,
      rates.enriched_rate, rates.wait_nan_rate, rates.dur_nan_rate
    FROM counts, wait_versions, dur_versions, wait_coverage, dur_coverage, rates;
  `);
  return rows[0];
}

async function queryRecentResolved(pool) {
  const { rows } = await pool.query(`
    SELECT
      p.predicted_at,
      r.resolved_at,
      p.task_id,
      p.run_id,
      t.task_queue_id,
      p.wait_p50_s, p.wait_p90_s,
      r.wait_duration_s AS actual_wait_s,
      p.run_p50_s,  p.run_p90_s,
      r.run_duration_s  AS actual_run_s,
      r.reason_resolved
    FROM queue_forecast_run_predictions p
    JOIN queue_forecast_task_runs r USING (task_id, run_id)
    JOIN queue_forecast_tasks      t USING (task_id)
    WHERE r.started_at IS NOT NULL
      AND r.resolved_at IS NOT NULL
    ORDER BY r.resolved_at DESC
    LIMIT 50;
  `);
  return rows;
}

async function queryRecentUnresolved(pool) {
  const { rows } = await pool.query(`
    SELECT
      p.predicted_at,
      p.task_id,
      p.run_id,
      t.task_queue_id,
      p.wait_p50_s, p.wait_p90_s,
      p.run_p50_s,  p.run_p90_s,
      CASE WHEN r.started_at IS NULL THEN 'pending' ELSE 'running' END AS state,
      r.pending_at,
      EXTRACT(EPOCH FROM (now() - r.pending_at)) AS age_pending_s
    FROM queue_forecast_run_predictions p
    JOIN queue_forecast_task_runs r USING (task_id, run_id)
    JOIN queue_forecast_tasks      t USING (task_id)
    WHERE r.resolved_at IS NULL
    ORDER BY p.predicted_at DESC
    LIMIT 50;
  `);
  return rows;
}

// Returns a SQL fragment with eight COUNT(*) FILTER (...) expressions that
// classify each resolved row's actual vs p50/p90 into good/warn/bad bands.
// Emits two flavors: "_all_" covers every resolved row, "_done_" restricts
// to reason_resolved = 'completed' (the trainer's headline slice).
// `act`, `p50`, `p90` are column expressions; aliases are prefixed by `prefix`.
function bandCountsSql(act, p50, p90, prefix) {
  const eligible = `${act} IS NOT NULL AND ${p50} IS NOT NULL AND ${p90} IS NOT NULL`;
  const done = `r.reason_resolved = 'completed'`;
  return `
    count(*) FILTER (WHERE ${eligible})                                                    AS ${prefix}_all_n,
    count(*) FILTER (WHERE ${eligible} AND ${act} <= ${p50})                                AS ${prefix}_all_good,
    count(*) FILTER (WHERE ${eligible} AND ${act} >  ${p50} AND ${act} <= ${p90})           AS ${prefix}_all_warn,
    count(*) FILTER (WHERE ${eligible} AND ${act} >  ${p90})                                AS ${prefix}_all_bad,
    count(*) FILTER (WHERE ${eligible} AND ${done})                                         AS ${prefix}_done_n,
    count(*) FILTER (WHERE ${eligible} AND ${done} AND ${act} <= ${p50})                    AS ${prefix}_done_good,
    count(*) FILTER (WHERE ${eligible} AND ${done} AND ${act} >  ${p50} AND ${act} <= ${p90}) AS ${prefix}_done_warn,
    count(*) FILTER (WHERE ${eligible} AND ${done} AND ${act} >  ${p90})                    AS ${prefix}_done_bad
  `;
}

async function queryAggregationsOverall(pool, windowDays) {
  const { rows } = await pool.query(`
    SELECT
      ${bandCountsSql('r.wait_duration_s', 'p.wait_p50_s', 'p.wait_p90_s', 'wait')},
      ${bandCountsSql('r.run_duration_s',  'p.run_p50_s',  'p.run_p90_s',  'run')},
      min(r.resolved_at) AS window_first_resolved,
      max(r.resolved_at) AS window_last_resolved
    FROM queue_forecast_run_predictions p
    JOIN queue_forecast_task_runs r USING (task_id, run_id)
    WHERE r.resolved_at IS NOT NULL
      AND r.resolved_at >= now() - ($1 || ' days')::interval;
  `, [windowDays]);
  return rows[0];
}

async function queryAggregationsByDay(pool, windowDays) {
  const { rows } = await pool.query(`
    SELECT
      date_trunc('day', r.resolved_at AT TIME ZONE 'UTC')::date AS day,
      ${bandCountsSql('r.wait_duration_s', 'p.wait_p50_s', 'p.wait_p90_s', 'wait')},
      ${bandCountsSql('r.run_duration_s',  'p.run_p50_s',  'p.run_p90_s',  'run')}
    FROM queue_forecast_run_predictions p
    JOIN queue_forecast_task_runs r USING (task_id, run_id)
    WHERE r.resolved_at IS NOT NULL
      AND r.resolved_at >= now() - ($1 || ' days')::interval
    GROUP BY day
    ORDER BY day DESC;
  `, [windowDays]);
  return rows;
}

async function queryAggregationsByQueue(pool, windowDays) {
  const { rows } = await pool.query(`
    SELECT
      t.task_queue_id,
      ${bandCountsSql('r.wait_duration_s', 'p.wait_p50_s', 'p.wait_p90_s', 'wait')},
      ${bandCountsSql('r.run_duration_s',  'p.run_p50_s',  'p.run_p90_s',  'run')}
    FROM queue_forecast_run_predictions p
    JOIN queue_forecast_task_runs r USING (task_id, run_id)
    JOIN queue_forecast_tasks      t USING (task_id)
    WHERE r.resolved_at IS NOT NULL
      AND r.resolved_at >= now() - ($1 || ' days')::interval
    GROUP BY t.task_queue_id
    ORDER BY (count(*) FILTER (WHERE r.wait_duration_s IS NOT NULL AND p.wait_p50_s IS NOT NULL AND p.wait_p90_s IS NOT NULL)) DESC,
             t.task_queue_id ASC;
  `, [windowDays]);
  return rows;
}

// Generic "by single dimension" aggregation. `dimSql` is the SELECT expression
// (column or CASE) producing the dimension value; `joinSql` is any extra JOIN
// the dimSql needs (e.g. `JOIN queue_forecast_tasks t USING (task_id)`).
async function queryAggByDim(pool, windowDays, dimSql, joinSql = '') {
  const { rows } = await pool.query(`
    SELECT
      ${dimSql} AS dim,
      ${bandCountsSql('r.wait_duration_s', 'p.wait_p50_s', 'p.wait_p90_s', 'wait')},
      ${bandCountsSql('r.run_duration_s',  'p.run_p50_s',  'p.run_p90_s',  'run')}
    FROM queue_forecast_run_predictions p
    JOIN queue_forecast_task_runs r USING (task_id, run_id)
    ${joinSql}
    WHERE r.resolved_at IS NOT NULL
      AND r.resolved_at >= now() - ($1 || ' days')::interval
    GROUP BY dim
    ORDER BY (count(*) FILTER (WHERE r.wait_duration_s IS NOT NULL AND p.wait_p50_s IS NOT NULL AND p.wait_p90_s IS NOT NULL)) DESC,
             dim ASC NULLS LAST;
  `, [windowDays]);
  return rows;
}

function queryAggregationsByReason(pool, windowDays) {
  return queryAggByDim(pool, windowDays, `COALESCE(r.reason_resolved, '(null)')`);
}

function queryAggregationsByScheduler(pool, windowDays) {
  return queryAggByDim(
    pool, windowDays,
    `COALESCE(t.scheduler_id, '(null)')`,
    `JOIN queue_forecast_tasks t USING (task_id)`,
  );
}

function queryAggregationsByPriority(pool, windowDays) {
  return queryAggByDim(pool, windowDays, `COALESCE(r.priority_at_pending, '(null)')`);
}

function queryAggregationsByWaitBaselineLevel(pool, windowDays) {
  return queryAggByDim(
    pool, windowDays,
    `COALESCE(p.input_features->'baselines'->'wait'->>'level', '(none)')`,
  );
}

function queryAggregationsByRunBaselineLevel(pool, windowDays) {
  return queryAggByDim(
    pool, windowDays,
    `COALESCE(p.input_features->'baselines'->'duration'->>'level', '(none)')`,
  );
}

// Sample-size buckets: log-scaled bands. '(none)' means no baseline was used.
const SAMPLE_BUCKET_SQL = `
  CASE
    WHEN $S IS NULL                  THEN '(none)'
    WHEN $S < 10                     THEN '<10'
    WHEN $S < 100                    THEN '10-99'
    WHEN $S < 1000                   THEN '100-999'
    WHEN $S < 10000                  THEN '1k-10k'
    ELSE                                  '10k+'
  END
`;

function queryAggregationsByWaitBaselineSampleSize(pool, windowDays) {
  const expr = SAMPLE_BUCKET_SQL.replaceAll(
    '$S',
    `NULLIF(p.input_features->'baselines'->'wait'->>'sample_size','')::bigint`,
  );
  return queryAggByDim(pool, windowDays, expr);
}

function queryAggregationsByRunBaselineSampleSize(pool, windowDays) {
  const expr = SAMPLE_BUCKET_SQL.replaceAll(
    '$S',
    `NULLIF(p.input_features->'baselines'->'duration'->>'sample_size','')::bigint`,
  );
  return queryAggByDim(pool, windowDays, expr);
}

const WAIT_DURATION_BUCKET_SQL = `
  CASE
    WHEN r.wait_duration_s IS NULL THEN '(no wait)'
    WHEN r.wait_duration_s < 60    THEN '<1m'
    WHEN r.wait_duration_s < 300   THEN '1-5m'
    WHEN r.wait_duration_s < 1800  THEN '5-30m'
    ELSE                                '30m+'
  END
`;

const RUN_DURATION_BUCKET_SQL = `
  CASE
    WHEN r.run_duration_s IS NULL THEN '(no run)'
    WHEN r.run_duration_s < 60    THEN '<1m'
    WHEN r.run_duration_s < 300   THEN '1-5m'
    WHEN r.run_duration_s < 1800  THEN '5-30m'
    WHEN r.run_duration_s < 3600  THEN '30-60m'
    ELSE                               '60m+'
  END
`;

// Wait buckets ordered chronologically rather than by sample size, so the
// row order communicates the duration progression. Completed-only fields
// are still present in the same query — render code shows both flavors.
// Wrapped in a subquery so the ORDER BY's CASE can reference `dim` — output
// column aliases aren't resolvable inside arbitrary ORDER BY expressions
// (only as standalone sort keys), so the inner subquery materializes `dim`.
async function queryAggregationsByWaitBucket(pool, windowDays) {
  const { rows } = await pool.query(`
    SELECT * FROM (
      SELECT
        ${WAIT_DURATION_BUCKET_SQL} AS dim,
        ${bandCountsSql('r.wait_duration_s', 'p.wait_p50_s', 'p.wait_p90_s', 'wait')},
        ${bandCountsSql('r.run_duration_s',  'p.run_p50_s',  'p.run_p90_s',  'run')}
      FROM queue_forecast_run_predictions p
      JOIN queue_forecast_task_runs r USING (task_id, run_id)
      WHERE r.resolved_at IS NOT NULL
        AND r.resolved_at >= now() - ($1 || ' days')::interval
      GROUP BY 1
    ) s
    ORDER BY CASE s.dim
      WHEN '<1m'       THEN 1
      WHEN '1-5m'      THEN 2
      WHEN '5-30m'     THEN 3
      WHEN '30m+'      THEN 4
      WHEN '(no wait)' THEN 9
      ELSE 8 END;
  `, [windowDays]);
  return rows;
}

// First-class tail miss-rate table: of the completed runs in each actual-wait
// bucket where we emitted a wait p90, how many exceeded it (actual > p90). The
// 30m+ row is the headline tail metric we gate on (<35% experimental / <30%
// broad). Wrapped in a subquery so ORDER BY's CASE can reference `dim` (same
// reason as queryAggregationsByWaitBucket). Counts are returned raw; the render
// step computes bad/n so it can guard n=0.
async function queryWaitTailMissRate(pool, windowDays) {
  const eligible = `r.reason_resolved = 'completed' AND r.wait_duration_s IS NOT NULL AND p.wait_p90_s IS NOT NULL`;
  const { rows } = await pool.query(`
    SELECT * FROM (
      SELECT
        ${WAIT_DURATION_BUCKET_SQL} AS dim,
        count(*) FILTER (WHERE ${eligible})                                AS n,
        count(*) FILTER (WHERE ${eligible} AND r.wait_duration_s > p.wait_p90_s) AS bad
      FROM queue_forecast_run_predictions p
      JOIN queue_forecast_task_runs r USING (task_id, run_id)
      WHERE r.resolved_at IS NOT NULL
        AND r.resolved_at >= now() - ($1 || ' days')::interval
      GROUP BY 1
    ) s
    ORDER BY CASE s.dim
      WHEN '<1m'       THEN 1
      WHEN '1-5m'      THEN 2
      WHEN '5-30m'     THEN 3
      WHEN '30m+'      THEN 4
      WHEN '(no wait)' THEN 9
      ELSE 8 END;
  `, [windowDays]);
  return rows;
}

async function queryAggregationsByRunBucket(pool, windowDays) {
  const { rows } = await pool.query(`
    SELECT * FROM (
      SELECT
        ${RUN_DURATION_BUCKET_SQL} AS dim,
        ${bandCountsSql('r.wait_duration_s', 'p.wait_p50_s', 'p.wait_p90_s', 'wait')},
        ${bandCountsSql('r.run_duration_s',  'p.run_p50_s',  'p.run_p90_s',  'run')}
      FROM queue_forecast_run_predictions p
      JOIN queue_forecast_task_runs r USING (task_id, run_id)
      WHERE r.resolved_at IS NOT NULL
        AND r.resolved_at >= now() - ($1 || ' days')::interval
      GROUP BY 1
    ) s
    ORDER BY CASE s.dim
      WHEN '<1m'      THEN 1
      WHEN '1-5m'     THEN 2
      WHEN '5-30m'    THEN 3
      WHEN '30-60m'   THEN 4
      WHEN '60m+'     THEN 5
      WHEN '(no run)' THEN 9
      ELSE 8 END;
  `, [windowDays]);
  return rows;
}

// Top names with actual > p90, restricted to completed-only since that's the
// slice we calibrate against. Grouped on normalized_name so chunked variants
// (`*-chunk-1`, `*-chunk-2`, ...) collapse into one row — same grouping the
// trainer uses for the per-task baselines.
async function queryTopMissesByName(pool, windowDays, side, limit = 25) {
  const cols = side === 'wait'
    ? { act: 'r.wait_duration_s', p50: 'p.wait_p50_s', p90: 'p.wait_p90_s' }
    : { act: 'r.run_duration_s',  p50: 'p.run_p50_s',  p90: 'p.run_p90_s'  };
  const eligible = `${cols.act} IS NOT NULL AND ${cols.p50} IS NOT NULL AND ${cols.p90} IS NOT NULL AND r.reason_resolved = 'completed'`;
  const { rows } = await pool.query(`
    SELECT
      COALESCE(t.normalized_name, '(null)') AS task_name,
      count(*) FILTER (WHERE ${eligible})                                  AS n_eligible,
      count(*) FILTER (WHERE ${eligible} AND ${cols.act} > ${cols.p90})     AS misses,
      percentile_cont(0.5) WITHIN GROUP (ORDER BY ${cols.act})
        FILTER (WHERE ${eligible} AND ${cols.act} > ${cols.p90})            AS miss_actual_p50,
      percentile_cont(0.9) WITHIN GROUP (ORDER BY ${cols.act})
        FILTER (WHERE ${eligible} AND ${cols.act} > ${cols.p90})            AS miss_actual_p90,
      percentile_cont(0.5) WITHIN GROUP (ORDER BY ${cols.p90})
        FILTER (WHERE ${eligible} AND ${cols.act} > ${cols.p90})            AS miss_pred_p90
    FROM queue_forecast_run_predictions p
    JOIN queue_forecast_task_runs r USING (task_id, run_id)
    JOIN queue_forecast_tasks      t USING (task_id)
    WHERE r.resolved_at IS NOT NULL
      AND r.resolved_at >= now() - ($1 || ' days')::interval
    GROUP BY 1
    HAVING count(*) FILTER (WHERE ${eligible} AND ${cols.act} > ${cols.p90}) > 0
    ORDER BY misses DESC, n_eligible DESC
    LIMIT $2;
  `, [windowDays, limit]);
  return rows;
}

// ─── Manifest Loading ────────────────────────────────────────────────────────

function loadLatestManifests() {
  if (!fs.existsSync(MODELS_DIR)) return [];
  const dates = fs.readdirSync(MODELS_DIR)
    .filter(d => /^\d{4}-\d{2}-\d{2}$/.test(d))
    .sort()
    .reverse();

  const latest = dates[0];
  if (!latest) return [];

  const dir = path.join(MODELS_DIR, latest);
  const manifests = [];
  for (const f of fs.readdirSync(dir)) {
    if (f.endsWith('_manifest.json')) {
      try {
        const data = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8'));
        data._filename = f;
        data._date_dir = latest;
        manifests.push(data);
      } catch { /* skip broken files */ }
    }
  }
  return manifests;
}

// ─── Markdown to HTML (minimal, no deps) ─────────────────────────────────────

function markdownToHtml(md) {
  let html = '';
  const lines = md.split('\n');
  let inCode = false;
  let inTable = false;
  let inList = false;
  let tableRows = [];

  function flushTable() {
    if (!tableRows.length) return;
    html += '<table><thead><tr>';
    const headers = tableRows[0];
    for (const h of headers) html += `<th>${inlineFormat(h.trim())}</th>`;
    html += '</tr></thead><tbody>';
    for (let i = 2; i < tableRows.length; i++) {
      html += '<tr>';
      for (const c of tableRows[i]) html += `<td>${inlineFormat(c.trim())}</td>`;
      html += '</tr>';
    }
    html += '</tbody></table>\n';
    tableRows = [];
    inTable = false;
  }

  function flushList() {
    if (inList) { html += '</ul>\n'; inList = false; }
  }

  function inlineFormat(text) {
    return text
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1'); // strip links
  }

  for (const line of lines) {
    // Code blocks
    if (line.startsWith('```')) {
      if (inTable) flushTable();
      if (inList) flushList();
      if (inCode) { html += '</code></pre>\n'; inCode = false; }
      else { html += '<pre><code>'; inCode = true; }
      continue;
    }
    if (inCode) {
      html += line.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;') + '\n';
      continue;
    }

    // Table rows
    if (line.includes('|') && line.trim().startsWith('|')) {
      if (inList) flushList();
      const cells = line.split('|').slice(1, -1);
      if (cells.every(c => /^[\s:-]+$/.test(c))) {
        // separator row
        tableRows.push(cells);
        inTable = true;
        continue;
      }
      tableRows.push(cells);
      inTable = true;
      continue;
    } else if (inTable) {
      flushTable();
    }

    // Empty line
    if (!line.trim()) {
      if (inList) flushList();
      continue;
    }

    // Headers
    const hMatch = line.match(/^(#{1,6})\s+(.+)/);
    if (hMatch) {
      if (inList) flushList();
      const level = hMatch[1].length;
      html += `<h${level}>${inlineFormat(hMatch[2])}</h${level}>\n`;
      continue;
    }

    // List items
    if (/^\s*[-*]\s/.test(line) || /^\s*\d+\.\s/.test(line)) {
      if (!inList) { html += '<ul>\n'; inList = true; }
      const text = line.replace(/^\s*[-*]\s+/, '').replace(/^\s*\d+\.\s+/, '');
      html += `<li>${inlineFormat(text)}</li>\n`;
      continue;
    }

    // Blockquote
    if (line.startsWith('>')) {
      if (inList) flushList();
      html += `<blockquote>${inlineFormat(line.slice(1).trim())}</blockquote>\n`;
      continue;
    }

    // Horizontal rule
    if (/^---+$/.test(line.trim())) {
      if (inList) flushList();
      html += '<hr>\n';
      continue;
    }

    // Paragraph
    if (inList) flushList();
    html += `<p>${inlineFormat(line)}</p>\n`;
  }

  if (inCode) html += '</code></pre>\n';
  if (inTable) flushTable();
  if (inList) flushList();

  return html;
}

// ─── HTML Helpers ────────────────────────────────────────────────────────────

function esc(s) {
  if (s == null) return '—';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function fmtNum(n) {
  if (n == null) return '—';
  return Number(n).toLocaleString('en-US');
}

function fmtPct(v) {
  if (v == null || Number.isNaN(Number(v))) return '—';
  return (Number(v) * 100).toFixed(1) + '%';
}

function fmtDuration(s) {
  if (s == null || Number.isNaN(Number(s))) return '—';
  s = Number(s);
  if (s < 60) return s.toFixed(1) + 's';
  if (s < 3600) return (s / 60).toFixed(1) + 'm';
  return (s / 3600).toFixed(1) + 'h';
}

function fmtAge(ts) {
  if (!ts) return '—';
  const ms = Date.now() - new Date(ts).getTime();
  const s = ms / 1000;
  if (s < 60) return Math.round(s) + 's ago';
  if (s < 3600) return Math.round(s / 60) + 'm ago';
  if (s < 86400) return (s / 3600).toFixed(1) + 'h ago';
  return (s / 86400).toFixed(1) + 'd ago';
}

function healthBadge(ok, warnText = 'WARN', okText = 'OK') {
  if (ok) return `<span class="badge badge-ok">${okText}</span>`;
  return `<span class="badge badge-warn">${warnText}</span>`;
}

function flagDot(val) {
  return val ? '<span class="dot dot-red" title="flagged"></span>' : '<span class="dot dot-dim"></span>';
}

// Render a task_id + run_id as a Taskcluster inspector link.
function taskLink(taskId, runId) {
  if (!taskId) return '—';
  const url = `https://firefox-ci-tc.services.mozilla.com/tasks/${encodeURIComponent(taskId)}`;
  const runLabel = runId != null ? ` · run ${esc(String(runId))}` : '';
  return `<a class="task-link" href="${url}" target="_blank" rel="noopener">${esc(taskId)}${runLabel}</a>`;
}

// Pick a color class for a predicted-vs-actual cell. Returns '' (neutral) when
// the comparison can't be made (any input null / non-finite).
function colorForActual(actual, p50, p90) {
  if (actual == null || p50 == null || p90 == null) return '';
  const a = Number(actual);
  const lo = Number(p50);
  const hi = Number(p90);
  if (!Number.isFinite(a) || !Number.isFinite(lo) || !Number.isFinite(hi)) return '';
  if (a <= lo) return 'cell-good';
  if (a <= hi) return 'cell-warn';
  return 'cell-bad';
}

// Renders four <td> cells (n, good%, warn%, bad%) for one band-stats row.
function bandCells(n, good, warn, bad) {
  const nn = Number(n) || 0;
  if (nn === 0) {
    return `<td class="r">0</td><td class="r">—</td><td class="r">—</td><td class="r">—</td>`;
  }
  const pct = (x) => `${(100 * Number(x) / nn).toFixed(1)}%`;
  return `<td class="r">${fmtNum(nn)}</td>` +
         `<td class="r cell-good">${pct(good)}</td>` +
         `<td class="r cell-warn">${pct(warn)}</td>` +
         `<td class="r cell-bad">${pct(bad)}</td>`;
}

// ─── Section Renderers ───────────────────────────────────────────────────────

function renderTableHealth(rows) {
  let html = `<table><thead><tr>
    <th>Table</th><th class="r">Rows</th><th>Newest</th><th>Age</th>
  </tr></thead><tbody>`;
  for (const r of rows) {
    html += `<tr>
      <td>${esc(r.table_name)}</td>
      <td class="r">${fmtNum(r.row_count)}</td>
      <td class="ts">${r.newest_ts ? new Date(r.newest_ts).toISOString().replace('T', ' ').slice(0, 19) + 'Z' : '—'}</td>
      <td>${fmtAge(r.newest_ts)}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  return html;
}

function renderFreshness(f) {
  const thresholds = {
    pending_at: 30 * 60 * 1000,
    started_at: 30 * 60 * 1000,
    resolved_at: 30 * 60 * 1000,
    enriched_at: 30 * 60 * 1000,
    worker_sample: 30 * 60 * 1000,
    daily_health: 2 * 60 * 60 * 1000,
  };
  const items = [
    ['pending_at', f.latest_pending],
    ['started_at', f.latest_started],
    ['resolved_at', f.latest_resolved],
    ['enriched_at', f.latest_enriched],
    ['worker_sample', f.latest_worker_sample],
    ['daily_health', f.latest_daily_health],
  ];
  let html = '<table><thead><tr><th>Stream</th><th>Latest</th><th>Age</th><th></th></tr></thead><tbody>';
  for (const [name, ts] of items) {
    const ageMs = ts ? Date.now() - new Date(ts).getTime() : null;
    const stale = ageMs != null && ageMs > (thresholds[name] || 30 * 60 * 1000);
    html += `<tr>
      <td>${esc(name)}</td>
      <td class="ts">${ts ? new Date(ts).toISOString().replace('T', ' ').slice(0, 19) + 'Z' : '—'}</td>
      <td>${fmtAge(ts)}</td>
      <td>${ts ? healthBadge(!stale) : healthBadge(false, 'NO DATA')}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  return html;
}

function renderIngestion(rows) {
  let html = `<table><thead><tr>
    <th>Window</th><th class="r">Pending</th><th class="r">Started</th><th class="r">Resolved</th>
  </tr></thead><tbody>`;
  for (const r of rows) {
    html += `<tr>
      <td>${esc(r.label)}</td>
      <td class="r">${fmtNum(r.pending_rows)}</td>
      <td class="r">${fmtNum(r.started_rows)}</td>
      <td class="r">${fmtNum(r.resolved_rows)}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  return html;
}

function renderOpenIssues(rows) {
  let html = '<table><thead><tr><th>Metric</th><th class="r">Value</th></tr></thead><tbody>';
  for (const r of rows) {
    const warn = r.metric === 'unresolved_runs_older_than_2h' && Number(r.value) > 100;
    html += `<tr>
      <td>${esc(r.metric)}</td>
      <td class="r${warn ? ' text-warn' : ''}">${fmtNum(r.value)}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  return html;
}

function renderResolutions(rows) {
  let html = '<table><thead><tr><th>Reason</th><th class="r">Count (1h)</th></tr></thead><tbody>';
  for (const r of rows) {
    html += `<tr><td>${esc(r.reason_resolved)}</td><td class="r">${fmtNum(r.n)}</td></tr>`;
  }
  html += '</tbody></table>';
  return html;
}

function renderTodayHourly(rows) {
  if (!rows.length) return '<p class="muted">No data for today yet.</p>';
  // Totals row
  let totPending = 0, totStarted = 0, totResolved = 0, totCompleted = 0, totFailed = 0;
  for (const r of rows) {
    totPending += Number(r.pending); totStarted += Number(r.started);
    totResolved += Number(r.resolved); totCompleted += Number(r.completed); totFailed += Number(r.failed);
  }

  let html = `<div class="today-summary">
    <span class="today-stat"><strong>${fmtNum(totPending)}</strong> pending</span>
    <span class="today-stat"><strong>${fmtNum(totResolved)}</strong> resolved</span>
    <span class="today-stat"><strong>${fmtNum(totCompleted)}</strong> completed</span>
    <span class="today-stat"><strong>${fmtNum(totFailed)}</strong> failed</span>
  </div>`;

  html += `<table><thead><tr>
    <th>Hour (UTC)</th><th class="r">Pending</th><th class="r">Started</th>
    <th class="r">Resolved</th><th class="r">Completed</th><th class="r">Failed</th>
    <th>Activity</th>
  </tr></thead><tbody>`;
  const maxPending = Math.max(...rows.map(r => Number(r.pending)), 1);
  for (const r of rows) {
    const pct = (Number(r.pending) / maxPending * 100).toFixed(0);
    html += `<tr>
      <td>${esc(r.hour)}</td>
      <td class="r">${fmtNum(r.pending)}</td>
      <td class="r">${fmtNum(r.started)}</td>
      <td class="r">${fmtNum(r.resolved)}</td>
      <td class="r">${fmtNum(r.completed)}</td>
      <td class="r">${fmtNum(r.failed)}</td>
      <td><div class="bar" style="width:${pct}%"></div></td>
    </tr>`;
  }
  html += '</tbody></table>';
  return html;
}

function renderPredictorHealth(h) {
  if (!h) return '<p class="muted">No predictor data yet.</p>';

  const lastAge = h.last_predicted_at ? Date.now() - new Date(h.last_predicted_at).getTime() : null;
  const lastOk = lastAge != null && lastAge < 5 * 60 * 1000;

  const countersHtml = `<div class="pred-counters">
    <span class="pred-counter"><strong>${fmtNum(h.preds_5m)}</strong> predictions (5m)</span>
    <span class="pred-counter"><strong>${fmtNum(h.preds_1h)}</strong> predictions (1h)</span>
    <span class="pred-counter"><strong>${fmtNum(h.preds_24h)}</strong> predictions (24h)</span>
    <span class="pred-counter"><strong>${fmtAge(h.last_predicted_at)}</strong> since last
      ${h.last_predicted_at ? healthBadge(lastOk) : healthBadge(false, 'NO DATA')}</span>
    <span class="pred-counter"><strong>${fmtNum(h.catchup_backlog)}</strong> catch-up backlog</span>
  </div>`;

  function renderVersionList(versions, kind) {
    if (!versions || !versions.length) return `<p class="muted">No ${kind} predictions in last hour.</p>`;
    const rows = versions.map(v =>
      `<tr><td class="mono">${esc(v.version || '(null)')}</td><td class="r">${fmtNum(v.n)}</td></tr>`
    ).join('');
    return `<table><thead><tr><th>${kind} model version</th><th class="r">Count (1h)</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  function renderCoverageList(coverage, kind) {
    if (!coverage || !coverage.length) return `<p class="muted">No ${kind} coverage data.</p>`;
    const rows = coverage.map(c =>
      `<tr><td>${esc(c.level)}</td><td class="r">${fmtNum(c.n)}</td></tr>`
    ).join('');
    return `<table><thead><tr><th>${kind} baseline level</th><th class="r">Count (1h)</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  const ratesHtml = `<table><thead><tr><th>Rate (1h)</th><th class="r">Value</th></tr></thead><tbody>
    <tr><td>enriched-at-predict</td><td class="r">${fmtPct(h.enriched_rate)}</td></tr>
    <tr><td>NaN wait baseline</td><td class="r">${fmtPct(h.wait_nan_rate)}</td></tr>
    <tr><td>NaN duration baseline</td><td class="r">${fmtPct(h.dur_nan_rate)}</td></tr>
  </tbody></table>`;

  return `
    ${countersHtml}
    <div class="grid">
      <div class="section">
        <h3>Active model versions</h3>
        ${renderVersionList(h.wait_versions, 'wait')}
        ${renderVersionList(h.duration_versions, 'duration')}
      </div>
      <div class="section">
        <h3>Baseline coverage</h3>
        ${renderCoverageList(h.wait_coverage, 'wait')}
        ${renderCoverageList(h.duration_coverage, 'duration')}
      </div>
    </div>
    <div class="section">
      <h3>Quality rates</h3>
      ${ratesHtml}
    </div>
  `;
}

function renderResolvedSample(rows) {
  const banner = `<div class="caveat-banner">
    <strong>Survival-biased sample.</strong> Only tasks that have already finished since
    the predictor went live on 2026-05-15 appear below, so longer-running predictions are
    under-represented. Use this to spot-check that predictions look directionally sensible,
    not to judge accuracy. Aggregate calibration metrics will be added once enough resolved
    data has accumulated.
  </div>`;

  if (!rows.length) return banner + '<p class="muted">No resolved predictions yet.</p>';

  const tsCol = (ts) => ts ? new Date(ts).toISOString().replace('T', ' ').slice(0, 19) + 'Z' : '—';
  let html = banner + `<table><thead><tr>
    <th>Resolved</th><th>Task</th><th>Queue</th>
    <th class="r">Wait p50</th><th class="r">Wait p90</th><th class="r">Actual wait</th>
    <th class="r">Run p50</th><th class="r">Run p90</th><th class="r">Actual run</th>
  </tr></thead><tbody>`;
  for (const r of rows) {
    const waitClass = colorForActual(r.actual_wait_s, r.wait_p50_s, r.wait_p90_s);
    const runClass  = colorForActual(r.actual_run_s,  r.run_p50_s,  r.run_p90_s);
    html += `<tr>
      <td class="ts">${tsCol(r.resolved_at)}</td>
      <td class="mono">${taskLink(r.task_id, r.run_id)}</td>
      <td>${esc(r.task_queue_id)}</td>
      <td class="r">${fmtDuration(r.wait_p50_s)}</td>
      <td class="r">${fmtDuration(r.wait_p90_s)}</td>
      <td class="r ${waitClass}">${fmtDuration(r.actual_wait_s)}</td>
      <td class="r">${fmtDuration(r.run_p50_s)}</td>
      <td class="r">${fmtDuration(r.run_p90_s)}</td>
      <td class="r ${runClass}">${fmtDuration(r.actual_run_s)}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  return html;
}

function renderUnresolvedSample(rows) {
  if (!rows.length) return '<p class="muted">No unresolved predictions.</p>';
  const tsCol = (ts) => ts ? new Date(ts).toISOString().replace('T', ' ').slice(0, 19) + 'Z' : '—';
  let html = `<table><thead><tr>
    <th>Predicted</th><th>Task</th><th>Queue</th>
    <th class="r">Wait p50</th><th class="r">Wait p90</th>
    <th class="r">Run p50</th><th class="r">Run p90</th>
    <th>State</th><th class="r">Age</th>
  </tr></thead><tbody>`;
  for (const r of rows) {
    html += `<tr>
      <td class="ts">${tsCol(r.predicted_at)}</td>
      <td class="mono">${taskLink(r.task_id, r.run_id)}</td>
      <td>${esc(r.task_queue_id)}</td>
      <td class="r">${fmtDuration(r.wait_p50_s)}</td>
      <td class="r">${fmtDuration(r.wait_p90_s)}</td>
      <td class="r">${fmtDuration(r.run_p50_s)}</td>
      <td class="r">${fmtDuration(r.run_p90_s)}</td>
      <td>${esc(r.state)}</td>
      <td class="r">${fmtDuration(r.age_pending_s)}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  return html;
}

function renderManifestsTabs(manifests) {
  if (!manifests.length) return '<p class="muted">No training manifests found.</p>';

  // Tab buttons
  let html = '<div class="tabs">';
  manifests.forEach((m, i) => {
    const name = m._filename?.replace('_manifest.json', '') || `model-${i}`;
    const active = i === 0 ? ' active' : '';
    html += `<button class="tab-btn${active}" onclick="switchTab('manifest', ${i}, this)">${esc(name)}</button>`;
  });
  html += '</div>';

  // Tab panels
  manifests.forEach((m, i) => {
    const ev = m.evaluation?.primary || {};
    const agg = ev.aggregate || {};
    const baseAgg = ev.baseline_aggregate || {};
    const lgbOnlyAgg = ev.lightgbm_only_aggregate || {};
    const w = m.windows || {};
    const isResidual = !!lgbOnlyAgg.mae_s;
    const display = i === 0 ? 'block' : 'none';

    html += `<div class="tab-panel manifest-panel" id="manifest-${i}" style="display:${display}">`;
    html += `<div class="manifest-card">`;
    html += `<div class="meta">trained ${fmtAge(m.trained_at)} · as_of ${esc(w.as_of_date?.slice(0, 10))} · ${esc(m.model_type)}</div>`;
    html += `<div class="meta">train: ${fmtNum(w.train?.rows)} rows (${esc(w.train?.start?.slice(0, 10))}..${esc(w.train?.end?.slice(0, 10))}) · val: ${fmtNum(w.val?.rows)} · holdout: ${fmtNum(w.holdout?.rows)}</div>`;

    // Aggregate metrics
    html += '<table class="metrics"><thead><tr><th></th>';
    if (isResidual) {
      html += '<th class="r">Baseline</th><th class="r">LGB-only</th><th class="r">Residual</th>';
    } else {
      html += '<th class="r">Model</th><th class="r">Baseline</th><th class="r">Delta</th>';
    }
    html += '</tr></thead><tbody>';

    if (isResidual) {
      html += `<tr><td>MAE</td>
        <td class="r">${fmtDuration(baseAgg.mae_s)}</td>
        <td class="r">${fmtDuration(lgbOnlyAgg.mae_s)}</td>
        <td class="r">${fmtDuration(agg.mae_s)}</td></tr>`;
      html += `<tr><td>within-2x</td>
        <td class="r">${fmtPct(baseAgg.within_2x_rate)}</td>
        <td class="r">${fmtPct(lgbOnlyAgg.within_2x_rate)}</td>
        <td class="r">${fmtPct(agg.within_2x_rate)}</td></tr>`;
      if (agg.p90_coverage_rate != null) {
        html += `<tr><td>p90 coverage</td>
          <td class="r">—</td>
          <td class="r">${fmtPct(lgbOnlyAgg.p90_coverage_rate)}</td>
          <td class="r">${fmtPct(agg.p90_coverage_rate)}</td></tr>`;
      }
    } else {
      const maeDelta = (baseAgg.mae_s && agg.mae_s)
        ? ((agg.mae_s - baseAgg.mae_s) / baseAgg.mae_s * 100).toFixed(1) + '%'
        : '—';
      html += `<tr><td>MAE</td>
        <td class="r">${fmtDuration(agg.mae_s)}</td>
        <td class="r">${fmtDuration(baseAgg.mae_s)}</td>
        <td class="r">${esc(maeDelta)}</td></tr>`;
      html += `<tr><td>within-2x</td>
        <td class="r">${fmtPct(agg.within_2x_rate)}</td>
        <td class="r">${fmtPct(baseAgg.within_2x_rate)}</td>
        <td class="r">—</td></tr>`;
      if (agg.p90_coverage_rate != null) {
        html += `<tr><td>p90 coverage</td>
          <td class="r">${fmtPct(agg.p90_coverage_rate)}</td>
          <td class="r">—</td>
          <td class="r">—</td></tr>`;
      }
    }
    html += '</tbody></table>';

    // Per-bucket breakdown
    const buckets = ev.buckets_aggregate;
    const baseBuckets = ev.baseline_buckets_aggregate;
    if (buckets && Object.keys(buckets).length > 0) {
      html += '<table class="metrics"><thead><tr><th>Bucket</th><th class="r">n</th><th class="r">MAE</th><th class="r">Base MAE</th><th class="r">w/in 2x</th><th class="r">Base w/in 2x</th></tr></thead><tbody>';
      for (const name of ['<1m', '1-5m', '5-30m', '30m+']) {
        const b = buckets[name] || {};
        const bb = (baseBuckets || {})[name] || {};
        html += `<tr>
          <td>${esc(name)}</td>
          <td class="r">${fmtNum(b.mae?.eligible_n)}</td>
          <td class="r">${fmtDuration(b.mae_s)}</td>
          <td class="r">${fmtDuration(bb.mae_s)}</td>
          <td class="r">${fmtPct(b.within_2x_rate)}</td>
          <td class="r">${fmtPct(bb.within_2x_rate)}</td>
        </tr>`;
      }
      html += '</tbody></table>';
    }

    // Per-day holdout breakdown
    const perDay = ev.per_day;
    if (perDay && Object.keys(perDay).length > 0) {
      html += '<details><summary class="meta" style="cursor:pointer;margin-top:8px">Per-day holdout breakdown</summary>';
      html += '<table class="metrics"><thead><tr><th>Day</th><th class="r">n</th><th class="r">MAE</th><th class="r">w/in 2x</th><th class="r">p90 cov</th></tr></thead><tbody>';
      for (const [day, d] of Object.entries(perDay).sort()) {
        const n = d.mae?.eligible_n || 0;
        const mae = n ? d.mae.sum_abs_error / n : NaN;
        const w2x = d.within_2x?.eligible_n ? d.within_2x.hit_n / d.within_2x.eligible_n : NaN;
        const p90 = d.p90_coverage?.eligible_n ? d.p90_coverage.covered_n / d.p90_coverage.eligible_n : NaN;
        html += `<tr>
          <td>${esc(day)}</td>
          <td class="r">${fmtNum(n)}</td>
          <td class="r">${fmtDuration(mae)}</td>
          <td class="r">${fmtPct(w2x)}</td>
          <td class="r">${fmtPct(p90)}</td>
        </tr>`;
      }
      html += '</tbody></table></details>';
    }

    html += '</div></div>';
  });
  return html;
}

function loadWalkForwardSummary() {
  const csvPath = path.join(PROJECT_ROOT, 'walk_forward_summary.csv');
  if (!fs.existsSync(csvPath)) return [];
  const lines = fs.readFileSync(csvPath, 'utf8').trim().split('\n');
  if (lines.length < 2) return [];
  const headers = lines[0].split(',');
  return lines.slice(1).map(line => {
    const vals = line.split(',');
    const row = {};
    headers.forEach((h, i) => { row[h] = vals[i]; });
    return row;
  });
}

function renderWalkForwardSummary(rows) {
  if (!rows.length) return '<p class="muted">No walk_forward_summary.csv found.</p>';

  // Group by config, preserving insertion order
  const byConfig = {};
  for (const r of rows) {
    if (!byConfig[r.config]) byConfig[r.config] = [];
    byConfig[r.config].push(r);
  }
  const configs = Object.keys(byConfig);

  let html = '<div class="tabs">';
  configs.forEach((cfg, i) => {
    const active = i === 0 ? ' active' : '';
    html += `<button class="tab-btn${active}" onclick="switchTab('wf', ${i}, this)">${esc(cfg)}</button>`;
  });
  html += '</div>';

  configs.forEach((cfg, i) => {
    const cfgRows = byConfig[cfg];
    const display = i === 0 ? 'block' : 'none';
    html += `<div class="tab-panel wf-panel" id="wf-${i}" style="display:${display}">`;
    html += `<table><thead><tr>
      <th>Cohort</th>
      <th class="r">Baseline MAE</th>
      <th class="r">Model MAE</th>
      <th class="r">Δ MAE %</th>
      <th class="r">Model within-2x</th>
      <th class="r">Δ within-2x pp</th>
      <th class="r">p90 cov</th>
      <th class="r">Hold rows</th>
    </tr></thead><tbody>`;
    for (const r of cfgRows) {
      const deltaMae = parseFloat(r.delta_mae_pct);
      const deltaW2x = parseFloat(r.delta_within_2x_pp);
      const deltaMaeStyle = !isNaN(deltaMae)
        ? (deltaMae < 0 ? ' style="color:var(--green)"' : ' style="color:var(--red)"') : '';
      const deltaW2xStyle = !isNaN(deltaW2x)
        ? (deltaW2x > 0 ? ' style="color:var(--green)"' : ' style="color:var(--red)"') : '';
      html += `<tr>
        <td>${esc(r.cohort_as_of)}</td>
        <td class="r">${fmtDuration(r.baseline_mae)}</td>
        <td class="r">${fmtDuration(r.model_mae)}</td>
        <td class="r"${deltaMaeStyle}>${!isNaN(deltaMae) ? deltaMae.toFixed(1) + '%' : '—'}</td>
        <td class="r">${fmtPct(r.model_within_2x)}</td>
        <td class="r"${deltaW2xStyle}>${!isNaN(deltaW2x) ? (deltaW2x > 0 ? '+' : '') + deltaW2x.toFixed(1) + 'pp' : '—'}</td>
        <td class="r">${fmtPct(r.p90_coverage)}</td>
        <td class="r">${fmtNum(r.hold_rows)}</td>
      </tr>`;
    }
    html += '</tbody></table></div>';
  });
  return html;
}

function renderDailyHealth(rows) {
  if (!rows.length) return '<p class="muted">No daily health data.</p>';
  let html = `<table class="health-table"><thead><tr>
    <th>Date</th><th class="r">Total</th><th class="r">Comp%</th><th class="r">Exc%</th>
    <th class="r">Wait p99</th><th class="r">Cap p50</th><th class="r">Util</th>
    <th>Exc</th><th>Stuck</th><th>Wait</th><th>Vol</th><th>Comp</th>
    <th>Cap&darr;</th><th>Cap&uarr;</th><th>Util&darr;</th><th>Samp</th>
    <th>Anomaly</th>
  </tr></thead><tbody>`;
  for (const r of rows) {
    const cls = r.is_anomalous ? ' class="row-anomaly"' : '';
    html += `<tr${cls}>
      <td>${esc(r.sample_date?.toISOString?.() ? r.sample_date.toISOString().slice(0, 10) : r.sample_date)}</td>
      <td class="r">${fmtNum(r.n_total)}</td>
      <td class="r">${fmtPct(r.completion_rate)}</td>
      <td class="r">${fmtPct(r.exception_rate)}</td>
      <td class="r">${fmtDuration(r.wait_p99_s)}</td>
      <td class="r">${fmtNum(r.total_capacity_p50)}</td>
      <td class="r">${fmtPct(r.utilization_p50)}</td>
      <td>${flagDot(r.flag_exception_spike)}</td>
      <td>${flagDot(r.flag_stuck_pending_spike)}</td>
      <td>${flagDot(r.flag_wait_p99_spike)}</td>
      <td>${flagDot(r.flag_volume_anomaly)}</td>
      <td>${flagDot(r.flag_low_completion)}</td>
      <td>${flagDot(r.flag_capacity_drop)}</td>
      <td>${flagDot(r.flag_capacity_spike)}</td>
      <td>${flagDot(r.flag_low_utilization)}</td>
      <td>${flagDot(r.flag_sampler_offline)}</td>
      <td>${r.is_anomalous
        ? `<span class="badge badge-warn" title="${esc(r.anomaly_reasons?.join(', '))}">${r.anomaly_reasons?.length || 0} flag${(r.anomaly_reasons?.length || 0) === 1 ? '' : 's'}</span>`
        : '<span class="badge badge-ok">OK</span>'}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  return html;
}

// ─── Daily Health Trend Charts ──────────────────────────────────────────────
// Simplistic inline-SVG line charts (no charting deps). One series per metric,
// tabbed via the same switchTab() mechanism as the manifest/walk-forward tabs.
// Anomalous days get a marker so a visible step/spike can be checked against
// the flags already surfaced in the table below.

const CHART_W = 760, CHART_H = 220;
const CHART_PAD = { top: 14, right: 14, bottom: 26, left: 54 };

function fmtAxisValue(unit, v) {
  if (unit === 'pct') return (v * 100).toFixed(0) + '%';
  if (unit === 'duration') return fmtDur(v);
  return fmtNum(Math.round(v));
}

function chartDateStr(r) {
  return r.sample_date?.toISOString?.()
    ? r.sample_date.toISOString().slice(0, 10)
    : String(r.sample_date).slice(0, 10);
}

// `points`: ascending-by-date [{date, value, anomalous}]. `value` may be null
// (rendered as a gap in the line). `clamp` optionally bounds the y-axis
// (e.g. [0,1] for rate metrics) after the usual 10% padding.
function renderLineChart(points, unit, clamp) {
  const plotW = CHART_W - CHART_PAD.left - CHART_PAD.right;
  const plotH = CHART_H - CHART_PAD.top - CHART_PAD.bottom;

  const finiteValues = points.map(p => p.value).filter(v => v != null && Number.isFinite(v));
  if (finiteValues.length === 0) return '<p class="muted">No data.</p>';

  let yMin = Math.min(...finiteValues);
  let yMax = Math.max(...finiteValues);
  if (yMin === yMax) { yMin -= 1; yMax += 1; }
  const pad = (yMax - yMin) * 0.1;
  yMin -= pad; yMax += pad;
  if (clamp) {
    yMin = Math.max(clamp[0], yMin);
    yMax = Math.min(clamp[1], yMax);
  }

  const n = points.length;
  const xAt = (i) => CHART_PAD.left + (n === 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const yAt = (v) => CHART_PAD.top + plotH - ((v - yMin) / (yMax - yMin)) * plotH;

  const coords = points.map((p, i) => ({
    x: xAt(i),
    y: (p.value != null && Number.isFinite(p.value)) ? yAt(p.value) : null,
    date: p.date,
    value: p.value,
    anomalous: !!p.anomalous,
  }));

  let d = '';
  let started = false;
  for (const c of coords) {
    if (c.y == null) { started = false; continue; }
    d += (started ? ' L ' : ' M ') + c.x.toFixed(1) + ' ' + c.y.toFixed(1);
    started = true;
  }

  const TICKS = 4;
  let gridSvg = '';
  for (let t = 0; t <= TICKS; t++) {
    const v = yMin + (yMax - yMin) * (t / TICKS);
    const y = yAt(v);
    gridSvg += `<line x1="${CHART_PAD.left}" y1="${y.toFixed(1)}" x2="${CHART_W - CHART_PAD.right}" y2="${y.toFixed(1)}" class="chart-grid"/>`;
    gridSvg += `<text x="${CHART_PAD.left - 8}" y="${(y + 3).toFixed(1)}" class="chart-axis-label" text-anchor="end">${esc(fmtAxisValue(unit, v))}</text>`;
  }

  const xTickCount = Math.min(6, n);
  let xLabelsSvg = '';
  for (let t = 0; t < xTickCount; t++) {
    const i = Math.round(t * (n - 1) / (xTickCount - 1 || 1));
    xLabelsSvg += `<text x="${coords[i].x.toFixed(1)}" y="${CHART_H - 6}" class="chart-axis-label" text-anchor="middle">${esc(points[i].date.slice(5))}</text>`;
  }

  let anomSvg = '';
  for (const c of coords) {
    if (c.anomalous && c.y != null) {
      anomSvg += `<circle cx="${c.x.toFixed(1)}" cy="${c.y.toFixed(1)}" r="3" class="chart-anom-dot"/>`;
    }
  }

  let lastSvg = '';
  for (let i = coords.length - 1; i >= 0; i--) {
    if (coords[i].y != null) {
      lastSvg = `<circle cx="${coords[i].x.toFixed(1)}" cy="${coords[i].y.toFixed(1)}" r="3.5" class="chart-last-dot"/>`;
      break;
    }
  }

  const jsonAttr = JSON.stringify(coords)
    .replace(/&/g, '&amp;')
    .replace(/'/g, '&#39;');

  return `
  <div class="chart-wrap">
    <svg class="chart-svg" viewBox="0 0 ${CHART_W} ${CHART_H}" preserveAspectRatio="none"
         data-points='${jsonAttr}' data-unit="${unit}"
         onmousemove="chartHover(event, this)" onmouseleave="chartHoverEnd(this)">
      ${gridSvg}
      <path d="${d}" class="chart-line"/>
      ${anomSvg}
      ${lastSvg}
      ${xLabelsSvg}
      <line class="chart-crosshair" x1="0" y1="${CHART_PAD.top}" x2="0" y2="${CHART_PAD.top + plotH}" style="display:none"/>
      <circle class="chart-hover-dot" r="4" style="display:none"/>
    </svg>
    <div class="chart-tooltip" style="display:none"></div>
  </div>`;
}

const DAILY_HEALTH_CHART_METRICS = [
  { key: 'utilization', label: 'Utilization',   unit: 'pct',      clamp: [0, 1], get: r => r.utilization_p50 },
  { key: 'total',        label: 'Total Tasks',   unit: 'count',   clamp: null,   get: r => r.n_total },
  { key: 'completed',    label: 'Completed %',   unit: 'pct',     clamp: [0, 1], get: r => r.completion_rate },
  { key: 'exceptions',   label: 'Exceptions %',  unit: 'pct',     clamp: [0, 1], get: r => r.exception_rate },
  { key: 'waitp99',      label: 'Wait p99',      unit: 'duration', clamp: null,  get: r => r.wait_p99_s },
  { key: 'capp50',       label: 'Cap p50',       unit: 'count',   clamp: null,   get: r => r.total_capacity_p50 },
];

function renderDailyHealthCharts(rows) {
  if (!rows.length) return '<p class="muted">No daily health data.</p>';
  const asc = [...rows].sort((a, b) => chartDateStr(a) < chartDateStr(b) ? -1 : 1);

  let html = '<div class="tabs">';
  DAILY_HEALTH_CHART_METRICS.forEach((m, i) => {
    const active = i === 0 ? ' active' : '';
    html += `<button class="tab-btn${active}" onclick="switchTab('healthchart', ${i}, this)">${esc(m.label)}</button>`;
  });
  html += '</div>';

  DAILY_HEALTH_CHART_METRICS.forEach((m, i) => {
    const points = asc.map(r => ({
      date: chartDateStr(r),
      value: m.get(r) != null ? Number(m.get(r)) : null,
      anomalous: r.is_anomalous,
    }));
    const display = i === 0 ? 'block' : 'none';
    html += `<div class="tab-panel healthchart-panel" id="healthchart-${i}" style="display:${display}">`;
    html += renderLineChart(points, m.unit, m.clamp);
    html += '</div>';
  });
  return html;
}

// Pull the {n,good,warn,bad} group for one flavor (`all` or `done`) out of a
// row, given a wait/run prefix. Lets the band renderer stay flavor-agnostic.
function pickBand(row, prefix, flavor) {
  return {
    n:    row[`${prefix}_${flavor}_n`],
    good: row[`${prefix}_${flavor}_good`],
    warn: row[`${prefix}_${flavor}_warn`],
    bad:  row[`${prefix}_${flavor}_bad`],
  };
}

function bandCellsFor(row, prefix, flavor) {
  const b = pickBand(row, prefix, flavor);
  return bandCells(b.n, b.good, b.warn, b.bad);
}

// Renders a single overall-style two-row table for one flavor ('all' / 'done').
function renderOverallTable(row, flavor) {
  return `<table><thead><tr>
    <th>Side</th><th class="r">n</th><th class="r">good (&le;p50)</th><th class="r">warn (p50–p90)</th><th class="r">bad (&gt;p90)</th>
  </tr></thead><tbody>
    <tr><td>wait</td>${bandCellsFor(row, 'wait', flavor)}</tr>
    <tr><td>run</td>${bandCellsFor(row,  'run',  flavor)}</tr>
  </tbody></table>`;
}

function renderAggregationsOverall(row) {
  if (!row) return '<p class="muted">No data.</p>';
  return `
    <h3>All resolved</h3>
    ${renderOverallTable(row, 'all')}
    <h3>Completed only (reason_resolved = 'completed')</h3>
    ${renderOverallTable(row, 'done')}
  `;
}

// Generic "by-dimension" renderer: one row per dim value, 8 numeric cells
// (4 for wait + 4 for run) for the given flavor. Used for by-day, by-queue,
// and every new breakdown table.
function renderByDimTable(rows, dimLabel, flavor, dimValueFn) {
  if (!rows.length) return '<p class="muted">No resolved predictions in window.</p>';
  let html = `<table><thead><tr>
    <th>${dimLabel}</th>
    <th class="r">wait n</th><th class="r">wait good</th><th class="r">wait warn</th><th class="r">wait bad</th>
    <th class="r">run n</th><th class="r">run good</th><th class="r">run warn</th><th class="r">run bad</th>
  </tr></thead><tbody>`;
  for (const r of rows) {
    html += `<tr><td>${esc(dimValueFn(r))}</td>` +
            bandCellsFor(r, 'wait', flavor) +
            bandCellsFor(r, 'run',  flavor) +
            `</tr>`;
  }
  html += '</tbody></table>';
  return html;
}

function renderBothFlavors(rows, dimLabel, dimValueFn) {
  return `
    <h3>All resolved</h3>
    ${renderByDimTable(rows, dimLabel, 'all', dimValueFn)}
    <h3>Completed only</h3>
    ${renderByDimTable(rows, dimLabel, 'done', dimValueFn)}
  `;
}

const dayValue = (r) =>
  r.day?.toISOString?.() ? r.day.toISOString().slice(0, 10) : String(r.day);
const dimValue = (r) => r.dim;

function renderAggregationsByDay(rows) {
  return renderBothFlavors(rows, 'Day (UTC)', dayValue);
}

function renderAggregationsByQueue(rows) {
  return renderBothFlavors(rows, 'task_queue_id', (r) => r.task_queue_id);
}

// reason_resolved is the dimension that splits all-resolved vs completed, so
// a "completed-only" flavor would be tautological — render the all-resolved
// flavor only.
function renderAggregationsByReason(rows) {
  if (!rows.length) return '<p class="muted">No resolved predictions in window.</p>';
  return renderByDimTable(rows, 'reason_resolved', 'all', dimValue);
}

function renderAggregationsByScheduler(rows) {
  return renderBothFlavors(rows, 'scheduler_id', dimValue);
}

function renderAggregationsByPriority(rows) {
  return renderBothFlavors(rows, 'priority_at_pending', dimValue);
}

function renderAggregationsByBaselineLevel(rows, side) {
  return renderBothFlavors(rows, `${side} baseline level`, dimValue);
}

function renderAggregationsByBaselineSampleSize(rows, side) {
  return renderBothFlavors(rows, `${side} baseline sample size`, dimValue);
}

function renderAggregationsByWaitBucket(rows) {
  return renderBothFlavors(rows, 'actual wait bucket', dimValue);
}

function renderAggregationsByRunBucket(rows) {
  return renderBothFlavors(rows, 'actual run bucket', dimValue);
}

// Tail miss-rate table. One row per actual-wait bucket; the 30m+ row is the
// metric we gate on, so it's highlighted and annotated against the two gates:
// <35% (experimental) and <30% (broad). Miss rate = bad/n, '—' when n=0.
function renderWaitTailMissRate(rows) {
  if (!rows.length) return '<p class="muted">No resolved predictions in window.</p>';
  let html = `<table><thead><tr>
    <th>actual wait bucket</th><th class="r">n</th><th class="r">misses (&gt;p90)</th><th class="r">miss rate</th>
  </tr></thead><tbody>`;
  for (const r of rows) {
    const n = Number(r.n) || 0;
    const bad = Number(r.bad) || 0;
    const rate = n > 0 ? bad / n : null;
    const isTail = r.dim === '30m+';
    // Gate only the headline tail row; <30% is good, <35% is the experimental
    // ceiling (warn), above is a miss.
    let rateCell = `<td class="r">${fmtPct(rate)}</td>`;
    if (isTail && rate != null) {
      const cls = rate < 0.30 ? 'cell-good' : rate < 0.35 ? 'cell-warn' : 'cell-bad';
      rateCell = `<td class="r ${cls}">${fmtPct(rate)}</td>`;
    }
    const dimCell = isTail
      ? `<td><b>${esc(r.dim)}</b></td>`
      : `<td>${esc(r.dim)}</td>`;
    html += `<tr>${dimCell}<td class="r">${fmtNum(n)}</td><td class="r">${fmtNum(bad)}</td>${rateCell}</tr>`;
  }
  html += '</tbody></table>';
  return html;
}

function fmtDur(s) {
  if (s == null) return '—';
  const x = Number(s);
  if (!Number.isFinite(x)) return '—';
  if (x < 60)   return `${x.toFixed(0)}s`;
  if (x < 3600) return `${(x / 60).toFixed(1)}m`;
  return `${(x / 3600).toFixed(1)}h`;
}

function renderTopMisses(rows, side) {
  if (!rows.length) {
    return '<p class="muted">No misses (actual &gt; p90) in window.</p>';
  }
  let html = `<table><thead><tr>
    <th>task name (normalized)</th>
    <th class="r">misses</th><th class="r">n eligible</th><th class="r">miss rate</th>
    <th class="r">miss ${side} actual p50</th><th class="r">miss ${side} actual p90</th><th class="r">miss predicted p90 (median)</th>
  </tr></thead><tbody>`;
  for (const r of rows) {
    const n = Number(r.n_eligible) || 0;
    const misses = Number(r.misses) || 0;
    const rate = n === 0 ? '—' : `${(100 * misses / n).toFixed(1)}%`;
    html += `<tr>
      <td>${esc(r.task_name)}</td>
      <td class="r">${fmtNum(misses)}</td>
      <td class="r">${fmtNum(n)}</td>
      <td class="r">${rate}</td>
      <td class="r">${fmtDur(r.miss_actual_p50)}</td>
      <td class="r">${fmtDur(r.miss_actual_p90)}</td>
      <td class="r">${fmtDur(r.miss_pred_p90)}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  return html;
}

// ─── CSS ─────────────────────────────────────────────────────────────────────

const CSS = `
  :root {
    --bg: #0d1117; --bg2: #161b22; --bg3: #21262d;
    --fg: #e6edf3; --fg2: #8b949e; --fg3: #484f58;
    --green: #3fb950; --yellow: #d29922; --red: #f85149;
    --blue: #58a6ff; --border: #30363d;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', 'Consolas', monospace;
    font-size: 13px; line-height: 1.5;
    background: var(--bg); color: var(--fg);
    padding: 20px; max-width: 1400px; margin: 0 auto;
  }
  h1 { font-size: 20px; font-weight: 600; margin-bottom: 4px; color: var(--fg); }
  h2 {
    font-size: 14px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.05em; color: var(--blue); margin: 28px 0 12px; padding-bottom: 6px;
    border-bottom: 1px solid var(--border);
  }
  h3 { font-size: 13px; font-weight: 600; color: var(--fg); margin-bottom: 4px; }
  .header-meta { color: var(--fg2); font-size: 12px; margin-bottom: 20px; }
  .header-meta a { color: var(--blue); text-decoration: none; }
  .header-meta a:hover { text-decoration: underline; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 16px; }
  th, td { padding: 5px 10px; text-align: left; border-bottom: 1px solid var(--border); }
  th { color: var(--fg2); font-weight: 500; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; }
  td { color: var(--fg); }
  .r { text-align: right; }
  .ts { font-size: 12px; color: var(--fg2); }
  .meta { font-size: 12px; color: var(--fg2); margin-bottom: 8px; }
  .muted { color: var(--fg3); }
  .text-warn { color: var(--yellow); }
  .badge {
    display: inline-block; padding: 1px 8px; border-radius: 10px;
    font-size: 11px; font-weight: 600;
  }
  .badge-ok { background: rgba(63,185,80,0.15); color: var(--green); }
  .badge-warn { background: rgba(210,153,34,0.15); color: var(--yellow); }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; }
  .dot-red { background: var(--red); }
  .dot-dim { background: var(--fg3); opacity: 0.3; }
  .manifest-card {
    background: var(--bg2); border: 1px solid var(--border); border-radius: 6px;
    padding: 14px 16px; margin-bottom: 12px;
  }
  .manifest-card table { margin-top: 8px; }
  .metrics th, .metrics td { padding: 4px 10px; }
  .row-anomaly { background: rgba(248,81,73,0.08); }
  .health-table th, .health-table td { padding: 4px 6px; font-size: 12px; }
  .health-table th { font-size: 10px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
  .section { margin-bottom: 12px; }
  footer { margin-top: 32px; padding-top: 12px; border-top: 1px solid var(--border); color: var(--fg3); font-size: 11px; }
  /* Tabs */
  .tabs { display: flex; gap: 4px; margin-bottom: 12px; flex-wrap: wrap; }
  .tab-btn {
    background: var(--bg3); border: 1px solid var(--border); color: var(--fg2);
    padding: 4px 12px; border-radius: 6px 6px 0 0; cursor: pointer;
    font-family: inherit; font-size: 12px; border-bottom: none;
  }
  .tab-btn:hover { color: var(--fg); background: var(--bg2); }
  .tab-btn.active { background: var(--bg2); color: var(--blue); border-color: var(--blue); border-bottom: 1px solid var(--bg2); }
  .tab-panel { display: none; }
  /* Today */
  .today-summary { display: flex; gap: 24px; margin-bottom: 12px; flex-wrap: wrap; }
  .today-stat { color: var(--fg2); font-size: 13px; }
  .today-stat strong { color: var(--fg); font-size: 16px; }
  .bar { height: 12px; background: var(--blue); border-radius: 2px; opacity: 0.6; min-width: 1px; }
  /* Predictions page */
  .cell-good { background: rgba(63,185,80,0.10);  color: var(--green); }
  .cell-warn { background: rgba(210,153,34,0.10); color: var(--yellow); }
  .cell-bad  { background: rgba(248,81,73,0.10);  color: var(--red); }
  .caveat-banner {
    background: rgba(210,153,34,0.08); border-left: 3px solid var(--yellow);
    padding: 10px 14px; margin: 8px 0 16px; color: var(--fg2); font-size: 12px;
    line-height: 1.5;
  }
  .caveat-banner strong { color: var(--yellow); }
  .pred-counters { display: flex; gap: 24px; margin-bottom: 12px; flex-wrap: wrap; }
  .pred-counter { color: var(--fg2); font-size: 13px; }
  .pred-counter strong { color: var(--fg); font-size: 16px; }
  .mono { font-family: inherit; font-size: 12px; color: var(--fg2); }
  .task-link { color: var(--blue); text-decoration: none; }
  .task-link:hover { text-decoration: underline; }
  .chart-wrap { position: relative; margin-bottom: 16px; }
  .chart-svg { width: 100%; height: auto; display: block; }
  .chart-grid { stroke: var(--border); stroke-width: 1; }
  .chart-axis-label { fill: var(--fg2); font-size: 10px; font-family: inherit; }
  .chart-line { fill: none; stroke: var(--blue); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
  .chart-anom-dot { fill: var(--yellow); }
  .chart-last-dot { fill: var(--blue); stroke: var(--bg); stroke-width: 1.5; }
  .chart-crosshair { stroke: var(--fg3); stroke-width: 1; }
  .chart-hover-dot { fill: var(--fg); stroke: var(--bg); stroke-width: 1.5; }
  .chart-tooltip {
    position: absolute; pointer-events: none; z-index: 10;
    background: var(--bg3); border: 1px solid var(--border); border-radius: 4px;
    padding: 6px 8px; font-size: 12px; color: var(--fg); white-space: nowrap;
  }
  .chart-tooltip-anom { color: var(--yellow); }
`;

const TAB_JS = `
function switchTab(group, idx, btn) {
  document.querySelectorAll('.' + group + '-panel').forEach(p => p.style.display = 'none');
  document.getElementById(group + '-' + idx).style.display = 'block';
  btn.parentElement.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}

function chartUnitFormat(unit, v) {
  if (v == null) return '—';
  if (unit === 'pct') return (v * 100).toFixed(1) + '%';
  if (unit === 'duration') {
    if (v < 60) return v.toFixed(1) + 's';
    if (v < 3600) return (v / 60).toFixed(1) + 'm';
    return (v / 3600).toFixed(1) + 'h';
  }
  return Math.round(v).toLocaleString('en-US');
}

function chartHover(evt, svg) {
  const pts = JSON.parse(svg.dataset.points);
  const unit = svg.dataset.unit;
  const rect = svg.getBoundingClientRect();
  const vb = svg.viewBox.baseVal;
  const mouseX = (evt.clientX - rect.left) * (vb.width / rect.width);

  let nearest = pts[0], bestDist = Infinity;
  for (const p of pts) {
    const dist = Math.abs(p.x - mouseX);
    if (dist < bestDist) { bestDist = dist; nearest = p; }
  }

  const crosshair = svg.querySelector('.chart-crosshair');
  const dot = svg.querySelector('.chart-hover-dot');
  crosshair.setAttribute('x1', nearest.x);
  crosshair.setAttribute('x2', nearest.x);
  crosshair.style.display = 'block';

  const tooltip = svg.parentElement.querySelector('.chart-tooltip');
  if (nearest.value == null) {
    dot.style.display = 'none';
    tooltip.innerHTML = '<strong>' + nearest.date + '</strong><br>No data';
  } else {
    dot.setAttribute('cx', nearest.x);
    dot.setAttribute('cy', nearest.y);
    dot.style.display = 'block';
    tooltip.innerHTML = '<strong>' + nearest.date + '</strong><br>' + chartUnitFormat(unit, nearest.value) +
      (nearest.anomalous ? ' <span class="chart-tooltip-anom">&#9679; anomaly</span>' : '');
  }
  tooltip.style.display = 'block';

  const wrapRect = svg.parentElement.getBoundingClientRect();
  const mouseXInWrap = evt.clientX - wrapRect.left;
  const maxLeft = wrapRect.width - 150;
  tooltip.style.left = Math.min(mouseXInWrap + 12, Math.max(0, maxLeft)) + 'px';
  tooltip.style.top = Math.max(0, evt.clientY - wrapRect.top - 36) + 'px';
}

function chartHoverEnd(svg) {
  svg.querySelector('.chart-crosshair').style.display = 'none';
  svg.querySelector('.chart-hover-dot').style.display = 'none';
  svg.parentElement.querySelector('.chart-tooltip').style.display = 'none';
}
`;

// ─── Page Builders ───────────────────────────────────────────────────────────

function renderPage({ title, h1Html, headerMetaHtml, bodyHtml, extraStyle = '', extraScript = '' }) {
  const now = new Date().toISOString().replace('T', ' ').slice(0, 19) + 'Z';
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="900">
<title>${title}</title>
<style>${CSS}${extraStyle}</style>
</head>
<body>
<h1>${h1Html}</h1>
<div class="header-meta">${headerMetaHtml}</div>
${bodyHtml}
<footer>queue-forecasting · generated ${now}</footer>
${extraScript ? `<script>${extraScript}</script>` : ''}
</body>
</html>`;
}

function buildPage(data) {
  const now = new Date().toISOString().replace('T', ' ').slice(0, 19) + 'Z';
  const headerMetaHtml = `Generated ${now} · refreshes every ${formatInterval(INTERVAL_MS)} · <a href="status.html">Project Status</a> · <a href="predictions.html">Predictions</a> · <a href="aggregations.html">Aggregations</a>`;
  const bodyHtml = `
<h2>Today (UTC)</h2>
${data.todayHourly}

<h2>Data Collection Health</h2>
<div class="grid">
  <div class="section">
    <h3>Table Sizes</h3>
    ${data.tableHealth}
  </div>
  <div class="section">
    <h3>Stream Freshness</h3>
    ${data.freshness}
  </div>
</div>
<div class="grid">
  <div class="section">
    <h3>Ingestion Windows</h3>
    ${data.ingestion}
  </div>
  <div class="section">
    <h3>Open Issues</h3>
    ${data.openIssues}
  </div>
</div>
<div class="section">
  <h3>Recent Resolutions (1h)</h3>
  ${data.resolutions}
</div>

<h2>Training Results</h2>
<div class="meta">${data.manifestsDir}</div>
${data.manifests}

<h2>Walk-Forward Evaluation</h2>
${data.walkForward}

<h2>Daily Health (last ${DAILY_HEALTH_WINDOW_DAYS}d)</h2>
${data.dailyHealthCharts}
${data.dailyHealth}
`;
  return renderPage({
    title: 'Queue Forecasting Dashboard',
    h1Html: 'Queue Forecasting Dashboard',
    headerMetaHtml,
    bodyHtml,
    extraScript: TAB_JS,
  });
}

function buildStatusPage(mdContent) {
  const body = markdownToHtml(mdContent);
  const extraStyle = `
  /* Markdown overrides */
  .md-body h1 { font-size: 22px; margin: 24px 0 8px; color: var(--fg); }
  .md-body h2 { font-size: 16px; margin: 24px 0 8px; text-transform: none; letter-spacing: normal; }
  .md-body h3 { font-size: 14px; margin: 20px 0 6px; }
  .md-body h4 { font-size: 13px; margin: 16px 0 4px; color: var(--blue); }
  .md-body h5, .md-body h6 { font-size: 12px; margin: 12px 0 4px; color: var(--fg2); }
  .md-body p { margin: 6px 0; }
  .md-body ul { margin: 6px 0 6px 20px; }
  .md-body li { margin: 2px 0; }
  .md-body table { margin: 8px 0 16px; }
  .md-body pre {
    background: var(--bg3); border: 1px solid var(--border); border-radius: 4px;
    padding: 10px; overflow-x: auto; margin: 8px 0;
  }
  .md-body code { background: var(--bg3); padding: 1px 4px; border-radius: 3px; font-size: 12px; }
  .md-body pre code { background: none; padding: 0; }
  .md-body blockquote {
    border-left: 3px solid var(--border); padding: 4px 12px; margin: 8px 0;
    color: var(--fg2); font-style: italic;
  }
  .md-body hr { border: none; border-top: 1px solid var(--border); margin: 20px 0; }
  .md-body strong { color: var(--fg); }`;
  const now = new Date().toISOString().replace('T', ' ').slice(0, 19) + 'Z';
  return renderPage({
    title: 'Project Status — Queue Forecasting',
    h1Html: '<a href="/" style="color:var(--fg);text-decoration:none">&larr;</a> Project Status',
    headerMetaHtml: `Rendered from trainer-phase2-decision.md · generated ${now} · <a href="/">Back to Dashboard</a>`,
    bodyHtml: `<div class="md-body">\n${body}\n</div>`,
    extraStyle,
  });
}

function buildPredictionsPage(data) {
  const now = new Date().toISOString().replace('T', ' ').slice(0, 19) + 'Z';
  const bodyHtml = `
<h2>Predictor Health</h2>
${data.predictorHealth}

<h2>Last 50 Resolved (predicted vs actual)</h2>
${data.resolvedSample}

<h2>Last 50 Unresolved (in flight)</h2>
${data.unresolvedSample}
`;
  return renderPage({
    title: 'Predictions — Queue Forecasting',
    h1Html: '<a href="/" style="color:var(--fg);text-decoration:none">&larr;</a> Predictions',
    headerMetaHtml: `Generated ${now} · refreshes every ${formatInterval(INTERVAL_MS)} · <a href="/">Back to Dashboard</a> · <a href="aggregations.html">Aggregations</a>`,
    bodyHtml,
  });
}

function buildAggregationsPage(data) {
  const now = new Date().toISOString().replace('T', ' ').slice(0, 19) + 'Z';
  const bodyHtml = `
<div class="meta">Window: last ${AGGREGATIONS_WINDOW_DAYS} days · ${data.overallMeta}</div>
<div class="meta">Bands: good (actual &le; p50) · warn (p50 &lt; actual &le; p90) · bad (actual &gt; p90). Each section shows two flavors: <b>all resolved</b> matches the existing aggregation page; <b>completed only</b> restricts to <code>reason_resolved = 'completed'</code>, matching the trainer's headline slice.</div>

<h2>Overall</h2>
${data.overall}

<h2>By Day (UTC)</h2>
${data.byDay}

<h2>By Worker Pool (task_queue_id)</h2>
${data.byQueue}

<h2>By reason_resolved</h2>
<div class="meta">Single flavor: this dimension is what separates "all" from "completed".</div>
${data.byReason}

<h2>By scheduler_id</h2>
${data.byScheduler}

<h2>By priority_at_pending</h2>
${data.byPriority}

<h2>By Wait Baseline Level</h2>
<div class="meta">Hierarchical fallback level used by the wait baseline (queue+bucket → queue → priority+bucket → global). <code>(none)</code> means no baseline applied.</div>
${data.byWaitBaselineLevel}

<h2>By Wait Baseline Sample-Size Bucket</h2>
${data.byWaitBaselineSample}

<h2>By Run Baseline Level</h2>
<div class="meta">Hierarchical fallback level used by the duration baseline (metadata_name → normalized_name → kind+test-type → task_queue_id → scheduler_id → global).</div>
${data.byRunBaselineLevel}

<h2>By Run Baseline Sample-Size Bucket</h2>
${data.byRunBaselineSample}

<h2>By Actual Wait Bucket</h2>
${data.byWaitBucket}

<h2>Wait p90 Miss Rate by Actual Wait Bucket</h2>
<div class="meta">Of completed runs with an emitted wait p90, the share whose actual wait exceeded the p90 (a tail miss). The <b>30m+</b> row is the headline tail metric we gate on: <span class="text-warn">&lt;35% experimental</span>, &lt;30% broad.</div>
${data.waitTailMissRate}

<h2>By Actual Run Bucket</h2>
${data.byRunBucket}

<h2>Top Wait Misses (actual &gt; p90, completed only)</h2>
<div class="meta">Highest-volume task names contributing to wait p90 misses.</div>
${data.topWaitMisses}

<h2>Top Run Misses (actual &gt; p90, completed only)</h2>
<div class="meta">Highest-volume task names contributing to run p90 misses.</div>
${data.topRunMisses}
`;
  const tocStyle = `
  .toc {
    position: fixed; top: 20px; left: 16px; width: 230px;
    max-height: calc(100vh - 40px); overflow-y: auto;
    background: var(--bg2); border: 1px solid var(--border); border-radius: 6px;
    padding: 10px 0; font-size: 12px; z-index: 100;
    scrollbar-width: thin; scrollbar-color: var(--bg3) transparent;
  }
  .toc-title {
    padding: 2px 14px 8px; color: var(--fg2); font-size: 11px;
    text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 1px solid var(--border);
    margin-bottom: 4px;
  }
  .toc a {
    display: block; padding: 3px 14px; color: var(--fg2); text-decoration: none;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    border-left: 2px solid transparent;
  }
  .toc a:hover { color: var(--fg); background: var(--bg3); }
  .toc a.active { color: var(--blue); border-left-color: var(--blue); background: rgba(88,166,255,0.06); }
  body.has-toc { margin-left: 260px; margin-right: 16px; }
  @media (max-width: 1200px) { .toc { display: none; } body.has-toc { margin-left: auto; margin-right: auto; } }`;

  const tocScript = `
  (function() {
    var hh = document.querySelectorAll('h2');
    if (!hh.length) return;
    var nav = document.createElement('nav');
    nav.className = 'toc';
    nav.innerHTML = '<div class="toc-title">Sections</div>';
    for (var i = 0; i < hh.length; i++) {
      hh[i].id = 'sec-' + i;
      var a = document.createElement('a');
      a.href = '#sec-' + i;
      a.textContent = hh[i].textContent;
      a.title = hh[i].textContent;
      nav.appendChild(a);
    }
    document.body.insertBefore(nav, document.body.firstChild);
    document.body.classList.add('has-toc');
    var links = nav.querySelectorAll('a');
    function update() {
      var cur = 0;
      for (var i = 0; i < hh.length; i++) { if (hh[i].getBoundingClientRect().top <= 60) cur = i; }
      for (var i = 0; i < links.length; i++) links[i].classList.toggle('active', i === cur);
    }
    window.addEventListener('scroll', update, { passive: true });
    update();
  })();`;

  return renderPage({
    title: 'Aggregations — Queue Forecasting',
    h1Html: '<a href="/" style="color:var(--fg);text-decoration:none">&larr;</a> Prediction Aggregations',
    headerMetaHtml: `Generated ${now} · refreshes every ${formatInterval(INTERVAL_MS)} · <a href="/">Back to Dashboard</a> · <a href="predictions.html">Predictions</a>`,
    bodyHtml,
    extraStyle: tocStyle,
    extraScript: tocScript,
  });
}

// ─── Main Loop ───────────────────────────────────────────────────────────────

async function generate() {
  const pool = new pg.Pool({ connectionString: DATABASE_URL, max: 3 });
  try {
    const [
      tableHealth, freshness, ingestion, openIssues, dailyHealth, resolutions, todayHourly,
      predictorHealth, recentResolved, recentUnresolved,
      aggOverall, aggByDay, aggByQueue,
      aggByReason, aggByScheduler, aggByPriority,
      aggByWaitBaselineLevel, aggByWaitBaselineSample,
      aggByRunBaselineLevel,  aggByRunBaselineSample,
      aggByWaitBucket, aggByRunBucket,
      waitTailMissRate,
      topWaitMisses, topRunMisses,
    ] = await Promise.all([
      timed('queryTableHealth', queryTableHealth(pool)),
      timed('queryFreshness', queryFreshness(pool)),
      timed('queryIngestion', queryIngestion(pool)),
      timed('queryOpenIssues', queryOpenIssues(pool)),
      timed('queryDailyHealth', queryDailyHealth(pool)),
      timed('queryRecentResolutions', queryRecentResolutions(pool)),
      timed('queryTodayHourly', queryTodayHourly(pool)),
      timed('queryPredictorHealth', queryPredictorHealth(pool)),
      timed('queryRecentResolved', queryRecentResolved(pool)),
      timed('queryRecentUnresolved', queryRecentUnresolved(pool)),
      timed('queryAggregationsOverall', queryAggregationsOverall(pool, AGGREGATIONS_WINDOW_DAYS)),
      timed('queryAggregationsByDay', queryAggregationsByDay(pool, AGGREGATIONS_WINDOW_DAYS)),
      timed('queryAggregationsByQueue', queryAggregationsByQueue(pool, AGGREGATIONS_WINDOW_DAYS)),
      timed('queryAggregationsByReason', queryAggregationsByReason(pool, AGGREGATIONS_WINDOW_DAYS)),
      timed('queryAggregationsByScheduler', queryAggregationsByScheduler(pool, AGGREGATIONS_WINDOW_DAYS)),
      timed('queryAggregationsByPriority', queryAggregationsByPriority(pool, AGGREGATIONS_WINDOW_DAYS)),
      timed('queryAggregationsByWaitBaselineLevel', queryAggregationsByWaitBaselineLevel(pool, AGGREGATIONS_WINDOW_DAYS)),
      timed('queryAggregationsByWaitBaselineSampleSize', queryAggregationsByWaitBaselineSampleSize(pool, AGGREGATIONS_WINDOW_DAYS)),
      timed('queryAggregationsByRunBaselineLevel', queryAggregationsByRunBaselineLevel(pool, AGGREGATIONS_WINDOW_DAYS)),
      timed('queryAggregationsByRunBaselineSampleSize', queryAggregationsByRunBaselineSampleSize(pool, AGGREGATIONS_WINDOW_DAYS)),
      timed('queryAggregationsByWaitBucket', queryAggregationsByWaitBucket(pool, AGGREGATIONS_WINDOW_DAYS)),
      timed('queryAggregationsByRunBucket', queryAggregationsByRunBucket(pool, AGGREGATIONS_WINDOW_DAYS)),
      timed('queryWaitTailMissRate', queryWaitTailMissRate(pool, AGGREGATIONS_WINDOW_DAYS)),
      timed('queryTopMissesByName(wait)', queryTopMissesByName(pool, AGGREGATIONS_WINDOW_DAYS, 'wait')),
      timed('queryTopMissesByName(run)', queryTopMissesByName(pool, AGGREGATIONS_WINDOW_DAYS, 'run')),
    ]);

    const manifests = loadLatestManifests();
    const latestDir = manifests.length ? manifests[0]._date_dir : 'none';
    const wfRows = loadWalkForwardSummary();

    const html = buildPage({
      tableHealth: renderTableHealth(tableHealth),
      freshness: renderFreshness(freshness),
      ingestion: renderIngestion(ingestion),
      openIssues: renderOpenIssues(openIssues),
      resolutions: renderResolutions(resolutions),
      manifests: renderManifestsTabs(manifests),
      manifestsDir: `Latest: trainer/data/models/${latestDir}/`,
      dailyHealth: renderDailyHealth(dailyHealth),
      dailyHealthCharts: renderDailyHealthCharts(dailyHealth),
      todayHourly: renderTodayHourly(todayHourly),
      walkForward: renderWalkForwardSummary(wfRows),
    });

    fs.mkdirSync(OUTPUT_DIR, { recursive: true });
    fs.writeFileSync(path.join(OUTPUT_DIR, 'index.html'), html);

    const predictionsHtml = buildPredictionsPage({
      predictorHealth:  renderPredictorHealth(predictorHealth),
      resolvedSample:   renderResolvedSample(recentResolved),
      unresolvedSample: renderUnresolvedSample(recentUnresolved),
    });
    fs.writeFileSync(path.join(OUTPUT_DIR, 'predictions.html'), predictionsHtml);

    const aggregationsHtml = buildAggregationsPage({
      overall:     renderAggregationsOverall(aggOverall),
      overallMeta: aggOverall && aggOverall.window_first_resolved
        ? `data range ${new Date(aggOverall.window_first_resolved).toISOString().slice(0, 10)} → ${new Date(aggOverall.window_last_resolved).toISOString().slice(0, 10)}`
        : 'no resolved predictions in window',
      byDay:                  renderAggregationsByDay(aggByDay),
      byQueue:                renderAggregationsByQueue(aggByQueue),
      byReason:               renderAggregationsByReason(aggByReason),
      byScheduler:            renderAggregationsByScheduler(aggByScheduler),
      byPriority:             renderAggregationsByPriority(aggByPriority),
      byWaitBaselineLevel:    renderAggregationsByBaselineLevel(aggByWaitBaselineLevel, 'wait'),
      byWaitBaselineSample:   renderAggregationsByBaselineSampleSize(aggByWaitBaselineSample, 'wait'),
      byRunBaselineLevel:     renderAggregationsByBaselineLevel(aggByRunBaselineLevel, 'run'),
      byRunBaselineSample:    renderAggregationsByBaselineSampleSize(aggByRunBaselineSample, 'run'),
      byWaitBucket:           renderAggregationsByWaitBucket(aggByWaitBucket),
      waitTailMissRate:       renderWaitTailMissRate(waitTailMissRate),
      byRunBucket:            renderAggregationsByRunBucket(aggByRunBucket),
      topWaitMisses:          renderTopMisses(topWaitMisses, 'wait'),
      topRunMisses:           renderTopMisses(topRunMisses, 'run'),
    });
    fs.writeFileSync(path.join(OUTPUT_DIR, 'aggregations.html'), aggregationsHtml);

    // Status page from markdown
    const mdPath = path.join(PROJECT_ROOT, 'trainer-phase2-decision.md');
    if (fs.existsSync(mdPath)) {
      const md = fs.readFileSync(mdPath, 'utf8');
      fs.writeFileSync(path.join(OUTPUT_DIR, 'status.html'), buildStatusPage(md));
    }

    console.log(`[${new Date().toISOString()}] dashboard written to ${OUTPUT_DIR}/`);
  } finally {
    await pool.end();
  }
}

async function loop() {
  // eslint-disable-next-line no-constant-condition
  while (true) {
    try {
      await generate();
    } catch (err) {
      console.error(`[${new Date().toISOString()}] dashboard generation failed:`, err.message);
    }
    await new Promise(r => setTimeout(r, INTERVAL_MS));
  }
}

if (process.argv.includes('--once')) {
  generate().catch(err => { console.error(err); process.exit(1); });
} else {
  loop();
}
