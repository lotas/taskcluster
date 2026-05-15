/**
 * Live Predictor service.
 *
 * Lifecycle:
 *   1. Connect to Postgres pool.
 *   2. Load model bundles from trainer/data/models/<latest>/ (wait + duration).
 *   3. Refresh the baseline stats once, then schedule hourly refresh.
 *   4. Poll loop: every POLL_INTERVAL_MS, run a keyset-paginated sweep over
 *      currently-unresolved unpredicted rows and predict each one. Each batch
 *      is awaited before re-querying so the NOT EXISTS check sees just-
 *      predicted rows on the next iteration; the cursor advances past every
 *      attempted row so a permanently-failing row can't stall the loop.
 *
 * Polling replaces an earlier LISTEN/NOTIFY design that proved unreliable in
 * practice (notifications stopped flowing after catch-up despite the listener
 * connection appearing healthy). Polling is the simpler model: at the cost of
 * a few seconds of latency per prediction, we get a single code path with no
 * notification-loss failure mode.
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
const MAX_INFLIGHT   = 4;
const POLL_INTERVAL_MS = Number(process.env.LIVE_PREDICTOR_POLL_MS || 5000);

// Each poll cycle keyset-paginates through unpredicted unresolved rows.
// Uses the existing partial index idx_qf_task_runs_unresolved
// (`(pending_at) WHERE resolved_at IS NULL`) for an index-only scan.
//
// Cursor advances past every row seen, so a row that keeps failing inside
// predictAndStore is attempted once per cycle (cursor reset between cycles)
// rather than blocking forward progress within a cycle.
const BATCH_SIZE = 500;
const POLL_SQL = `
SELECT r.task_id, r.run_id, r.pending_at
FROM queue_forecast_task_runs r
WHERE r.resolved_at IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM queue_forecast_run_predictions p
    WHERE p.task_id = r.task_id AND p.run_id = r.run_id
  )
  AND (r.pending_at, r.task_id, r.run_id) > ($1::timestamptz, $2::text, $3::int)
ORDER BY r.pending_at, r.task_id, r.run_id
LIMIT ${BATCH_SIZE}
;`;

function log(...args) {
  console.log(new Date().toISOString(), '[live-predictor]', ...args);
}

function logErr(...args) {
  console.error(new Date().toISOString(), '[live-predictor]', ...args);
}

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

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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

  async function processOne(taskId, runId) {
    const release = await acquire();
    try {
      const r = await predictAndStore({ pool, bundles, baselineStats: stats, taskId, runId });
      if (r.inserted) {
        log(`predicted ${taskId}/${runId}${r.enriched ? '' : ' (cold-start)'}`);
      } else if (r.reason !== 'duplicate') {
        log(`skipped ${taskId}/${runId}: ${r.reason}`);
      }
    } catch (err) {
      logErr(`predict failed for ${taskId}/${runId}:`, err.message);
    } finally {
      release();
    }
  }

  // ──────────────────────────────────────────────────────────────────────
  // Polling loop.
  // ──────────────────────────────────────────────────────────────────────
  let shuttingDown = false;
  process.on('SIGTERM', async () => {
    if (shuttingDown) return;
    shuttingDown = true;
    log('SIGTERM received, shutting down');
    stats.stopPeriodicRefresh();
    await pool.end().catch(() => {});
    process.exit(0);
  });

  log(`polling every ${POLL_INTERVAL_MS}ms`);
  while (!shuttingDown) {
    try {
      let cursorPendingAt = new Date(0);
      let cursorTaskId    = '';
      let cursorRunId     = -1;
      let cycleCount      = 0;

      while (!shuttingDown) {
        const batch = await pool.query(POLL_SQL, [cursorPendingAt, cursorTaskId, cursorRunId]);
        if (batch.rowCount === 0) break;
        await Promise.all(batch.rows.map(r => processOne(r.task_id, r.run_id)));
        const last = batch.rows[batch.rows.length - 1];
        cursorPendingAt = last.pending_at;
        cursorTaskId    = last.task_id;
        cursorRunId     = last.run_id;
        cycleCount += batch.rowCount;
      }
      if (cycleCount > 0) log(`cycle complete: ${cycleCount} rows processed`);
    } catch (err) {
      logErr('poll cycle failed:', err.message);
    }
    await sleep(POLL_INTERVAL_MS);
  }
}

main().catch((err) => {
  logErr('fatal:', err);
  process.exit(1);
});
