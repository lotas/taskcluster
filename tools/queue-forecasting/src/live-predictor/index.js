/**
 * Live Predictor service.
 *
 * Lifecycle (order matters):
 *   1. Connect to Postgres (pool for queries, dedicated client for LISTEN).
 *   2. Load model bundles from trainer/data/models/<latest>/ (wait + duration).
 *   3. Refresh the baseline stats once, then schedule hourly refresh.
 *   4. LISTEN queue_forecast_task_pending — MUST happen BEFORE catch-up so
 *      notifications committed during catch-up are not lost.
 *   5. Run startup catch-up over currently-unresolved rows that don't yet
 *      have a prediction (keyset-paginated until exhausted).
 *   6. On each NOTIFY: dispatch predictAndStore with a concurrency cap.
 *
 * On unhandled errors in the LISTEN connection, exit non-zero so the
 * container's restart policy revives us.
 */

import pg from 'pg';
import path from 'path';
import { fileURLToPath } from 'url';
import { findLatestModelDir, loadBundle } from './model-loader.js';
import { BaselineStats } from './baseline-stats.js';
import { predictAndStore } from './predict.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = path.join(__dirname, '..', '..');
const MODELS_ROOT = path.join(PROJECT_ROOT, 'trainer', 'data', 'models');

const WAIT_STEM      = 'wait_time_residual_throughput_filtered_baseline';
const DURATION_STEM  = 'run_duration_residual';
const CHANNEL        = 'queue_forecast_task_pending';
const MAX_INFLIGHT   = 4;   // cap concurrent predictions to keep DB load bounded

// Catch-up covers currently-unresolved rows that have no prediction yet.
// Uses the existing partial index idx_qf_task_runs_unresolved
// (`(pending_at) WHERE resolved_at IS NULL`) for an index-only scan.
//
// Keyset pagination over (pending_at, task_id, run_id) ensures each row is
// visited at most once per startup, so a permanently failing row (e.g. a
// corrupt task row that always throws inside predictAndStore) doesn't block
// the loop — we advance past it and move on. The cursor starts before all
// real rows; each batch shifts it forward to the last row seen.
const CATCH_UP_BATCH = 500;
const CATCH_UP_SQL = `
SELECT r.task_id, r.run_id, r.pending_at
FROM queue_forecast_task_runs r
WHERE r.resolved_at IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM queue_forecast_run_predictions p
    WHERE p.task_id = r.task_id AND p.run_id = r.run_id
  )
  AND (r.pending_at, r.task_id, r.run_id) > ($1::timestamptz, $2::text, $3::int)
ORDER BY r.pending_at, r.task_id, r.run_id
LIMIT ${CATCH_UP_BATCH}
;`;

function log(...args) {
  console.log(new Date().toISOString(), '[live-predictor]', ...args);
}

function logErr(...args) {
  console.error(new Date().toISOString(), '[live-predictor]', ...args);
}

/**
 * Simple semaphore-style concurrency cap. Returns a promise that resolves
 * when there's a free slot, and a release() to call when done.
 */
function makeLimiter(max) {
  let inFlight = 0;
  const waiters = [];
  function tryRelease() {
    if (waiters.length && inFlight < max) {
      inFlight++;
      const w = waiters.shift();
      w();
    }
  }
  return async function acquire() {
    if (inFlight < max) {
      inFlight++;
    } else {
      await new Promise((r) => waiters.push(r));
    }
    return function release() {
      inFlight--;
      tryRelease();
    };
  };
}

async function main() {
  const databaseUrl = process.env.DATABASE_URL;
  if (!databaseUrl) throw new Error('DATABASE_URL is required');

  const modelDir = findLatestModelDir(MODELS_ROOT);
  if (!modelDir) throw new Error(`No model directories under ${MODELS_ROOT}`);
  log('loading bundles from', modelDir);

  const [waitBundle, durationBundle] = await Promise.all([
    loadBundle(modelDir, WAIT_STEM),
    loadBundle(modelDir, DURATION_STEM),
  ]);
  log('wait model:',     waitBundle.schema.model_version,     'artifact', waitBundle.artifact_hash);
  log('duration model:', durationBundle.schema.model_version, 'artifact', durationBundle.artifact_hash);

  const pool = new pg.Pool({ connectionString: databaseUrl, max: 10 });

  // Anomaly-filter flag is driven by each bundle's feature_schema.anomaly_filter
  // block (mirroring the trainer config). Defaults: wait filtered, duration not.
  const filterFromSchema = (schema) => {
    const af = schema?.anomaly_filter;
    if (!af || !af.enabled) return false;
    return ['baseline', 'both'].includes(af.mode);
  };
  const stats = new BaselineStats(pool, {
    waitFilterAnomalous:     filterFromSchema(waitBundle.schema),
    durationFilterAnomalous: filterFromSchema(durationBundle.schema),
  });
  log(`baseline filter — wait: ${stats._opts.waitFilterAnomalous}, ` +
      `duration: ${stats._opts.durationFilterAnomalous}`);
  await stats.refresh();
  log('baseline stats refreshed');
  stats.startPeriodicRefresh();

  const bundles = { wait: waitBundle, duration: durationBundle };
  const acquire = makeLimiter(MAX_INFLIGHT);

  async function processOne(taskId, runId, source) {
    const release = await acquire();
    try {
      const r = await predictAndStore({ pool, bundles, baselineStats: stats, taskId, runId });
      if (r.inserted) {
        log(`predicted ${taskId}/${runId} via ${source}${r.enriched ? '' : ' (cold-start)'}`);
      } else if (r.reason !== 'duplicate') {
        log(`skipped ${taskId}/${runId} via ${source}: ${r.reason}`);
      }
    } catch (err) {
      logErr(`predict failed for ${taskId}/${runId} (${source}):`, err.message);
    } finally {
      release();
    }
  }

  // ────────────────────────────────────────────────────────────────────
  // STEP A: Set up LISTEN BEFORE catch-up.
  //
  // NOTIFY events committed between catch-up start and LISTEN registration
  // would be lost otherwise — postgres only delivers notifications to
  // sessions that are LISTENing at commit time. We set up LISTEN first,
  // then run catch-up; any events that arrive during catch-up are queued
  // on the listener and processed after, and duplicates between catch-up
  // and NOTIFY are handled by INSERT ... ON CONFLICT DO NOTHING.
  // ────────────────────────────────────────────────────────────────────
  const listener = new pg.Client({ connectionString: databaseUrl });
  listener.on('error', (err) => {
    logErr('listener error, exiting for restart:', err.message);
    process.exit(1);
  });
  listener.on('notification', (msg) => {
    if (msg.channel !== CHANNEL) return;
    let payload;
    try {
      payload = JSON.parse(msg.payload);
    } catch {
      logErr('bad NOTIFY payload, ignoring:', msg.payload);
      return;
    }
    if (!payload.task_id || payload.run_id === undefined) return;
    processOne(payload.task_id, payload.run_id, 'notify');
  });
  await listener.connect();
  await listener.query(`LISTEN ${CHANNEL}`);
  log(`LISTENing on ${CHANNEL}`);

  // ────────────────────────────────────────────────────────────────────
  // STEP B: Catch-up for currently-unresolved unpredicted rows.
  // Keyset cursor starts before all real rows; each batch advances it to
  // the last row seen, so every row is attempted at most once and a
  // permanently failing row never stalls the loop.
  // ────────────────────────────────────────────────────────────────────
  log('running catch-up');
  let totalCatchUp = 0;
  let cursorPendingAt = new Date(0);
  let cursorTaskId    = '';
  let cursorRunId     = -1;

  while (true) {
    const batch = await pool.query(CATCH_UP_SQL, [cursorPendingAt, cursorTaskId, cursorRunId]);
    if (batch.rowCount === 0) break;
    log(`catch-up batch: ${batch.rowCount} unresolved unpredicted rows`);
    await Promise.all(batch.rows.map(r => processOne(r.task_id, r.run_id, 'catch-up')));
    const last = batch.rows[batch.rows.length - 1];
    cursorPendingAt = last.pending_at;
    cursorTaskId    = last.task_id;
    cursorRunId     = last.run_id;
    totalCatchUp += batch.rowCount;
  }
  log(`catch-up complete: ${totalCatchUp} rows processed`);

  // Keep the process alive — Node won't exit while LISTEN is open.
  process.on('SIGTERM', async () => {
    log('SIGTERM received, shutting down');
    stats.stopPeriodicRefresh();
    await listener.end();
    await pool.end();
    process.exit(0);
  });
}

main().catch((err) => {
  logErr('fatal:', err);
  process.exit(1);
});
