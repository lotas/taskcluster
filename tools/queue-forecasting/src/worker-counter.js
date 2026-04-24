import taskcluster from '@taskcluster/client';
import { createPool } from './db.js';

const SAMPLE_INTERVAL_MS  = 5 * 60 * 1000;      // 5 min
const REFRESH_POOLS_MS    = 24 * 60 * 60 * 1000; // daily
const DB_BATCH_SIZE       = 500;

const rootUrl = process.env.TASKCLUSTER_ROOT_URL;
if (!rootUrl) {
  console.error('TASKCLUSTER_ROOT_URL is required');
  process.exit(1);
}

// Anonymous clients (no credentials — these APIs are public)
const workerManager = new taskcluster.WorkerManager({ rootUrl });
const pool = createPool(process.env.DATABASE_URL);


// ---------------------------------------------------------------------------
// TC API helpers
// ---------------------------------------------------------------------------

/**
 * Fetch all worker pool stats via the paginated listWorkerPoolsStats endpoint.
 * Returns { stats: Array<{ task_queue_id, running_workers, existing_capacity }>, pages }.
 *
 * API: GET /api/worker-manager/v1/worker-pools/stats
 * Response shape (per generated schema):
 *   { workerPoolsStats: [{ workerPoolId, runningCount, currentCapacity, ... }],
 *     continuationToken? }
 *
 * Notes on field mapping:
 *   - workerPoolId     → task_queue_id  (same format: providerId/workerType)
 *   - runningCount     → running_workers (workers in "running" state)
 *   - currentCapacity  → existing_capacity (total capacity not "stopped";
 *                        the spec assumed "existingCapacity" but the actual
 *                        field from the schema is "currentCapacity")
 */
async function fetchDynamicPoolStats() {
  const stats = [];
  let token = undefined;
  let pages = 0;
  do {
    const query = { limit: 100 };
    if (token) query.continuationToken = token;
    const resp = await workerManager.listWorkerPoolsStats(query);
    for (const s of resp.workerPoolsStats || []) {
      stats.push({
        task_queue_id:     s.workerPoolId,
        running_workers:   s.runningCount   ?? null,
        existing_capacity: s.currentCapacity ?? null,
      });
    }
    token = resp.continuationToken;
    pages += 1;
  } while (token);
  return { stats, pages };
}


/**
 * Fetch all worker pool configs (for the dimension refresh).
 * Provides providerId per pool so we can classify dynamic vs. static.
 *
 * API: GET /api/worker-manager/v1/worker-pools
 * Response shape: { workerPools: [{ workerPoolId, providerId, ... }], continuationToken? }
 */
async function fetchDynamicPoolConfigs() {
  const pools = [];
  let token = undefined;
  do {
    const query = { limit: 100 };
    if (token) query.continuationToken = token;
    const resp = await workerManager.listWorkerPools(query);
    for (const p of resp.workerPools || []) {
      pools.push({
        task_queue_id: p.workerPoolId,
        provider_type: p.providerId ?? null,
      });
    }
    token = resp.continuationToken;
  } while (token);
  return pools;
}


// ---------------------------------------------------------------------------
// DB helpers
// ---------------------------------------------------------------------------

/**
 * Derive "busy worker" counts from our existing task_runs table.
 * A run is in-flight iff started_at IS NOT NULL AND resolved_at IS NULL.
 * Returns Map<task_queue_id, count>.
 */
async function claimedTasksByQueue(now) {
  const res = await pool.query(`
    SELECT t.task_queue_id, COUNT(*)::int AS claimed
    FROM queue_forecast_task_runs r
    JOIN queue_forecast_tasks t ON r.task_id = t.task_id
    WHERE r.started_at IS NOT NULL
      AND r.resolved_at IS NULL
      AND r.started_at <= $1
    GROUP BY t.task_queue_id
  `, [now]);
  const m = new Map();
  for (const row of res.rows) {
    if (row.task_queue_id) m.set(row.task_queue_id, row.claimed);
  }
  return m;
}


/**
 * Batch upsert worker count rows.
 * Idempotent: ON CONFLICT (task_queue_id, sampled_at) DO UPDATE.
 */
async function upsertWorkerCounts(rows, source) {
  if (rows.length === 0) return;
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    for (let i = 0; i < rows.length; i += DB_BATCH_SIZE) {
      const chunk = rows.slice(i, i + DB_BATCH_SIZE);
      const values = [];
      const params = [];
      chunk.forEach((r, k) => {
        const base = k * 6;
        values.push(`($${base + 1}, $${base + 2}, $${base + 3}, $${base + 4}, $${base + 5}, $${base + 6})`);
        params.push(r.sampled_at, r.task_queue_id, r.running_workers, r.claimed_tasks, r.existing_capacity, source);
      });
      await client.query(`
        INSERT INTO queue_forecast_worker_counts
          (sampled_at, task_queue_id, running_workers, claimed_tasks, existing_capacity, source)
        VALUES ${values.join(', ')}
        ON CONFLICT (task_queue_id, sampled_at) DO UPDATE SET
          running_workers   = EXCLUDED.running_workers,
          claimed_tasks     = EXCLUDED.claimed_tasks,
          existing_capacity = EXCLUDED.existing_capacity,
          source            = EXCLUDED.source
      `, params);
    }
    await client.query('COMMIT');
  } catch (err) {
    await client.query('ROLLBACK');
    throw err;
  } finally {
    client.release();
  }
}


/**
 * Refresh the worker pool dimension table (runs daily).
 * - All pools returned by WorkerManager.listWorkerPools are classified as 'dynamic'.
 * - Any task_queue_id seen in queue_forecast_tasks but NOT in the dynamic list
 *   is classified as 'static' (these are self-hosted / provisioner-managed workers
 *   that do not appear in worker-manager).
 */
async function refreshPoolDimension() {
  console.log('[worker-counter] refreshing pool dimension...');
  const dynamicPools = await fetchDynamicPoolConfigs();
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    const now = new Date();
    for (const p of dynamicPools) {
      await client.query(`
        INSERT INTO queue_forecast_worker_pools (task_queue_id, pool_kind, provider_type, refreshed_at)
        VALUES ($1, 'dynamic', $2, $3)
        ON CONFLICT (task_queue_id) DO UPDATE SET
          pool_kind     = 'dynamic',
          provider_type = EXCLUDED.provider_type,
          refreshed_at  = EXCLUDED.refreshed_at
      `, [p.task_queue_id, p.provider_type, now]);
    }
    // Queues present in task data but absent from worker-manager → 'static'.
    // Cast $1 to timestamptz explicitly — without it Postgres infers TEXT
    // from the `SELECT DISTINCT ..., $1` context and rejects the INSERT.
    await client.query(`
      INSERT INTO queue_forecast_worker_pools (task_queue_id, pool_kind, provider_type, refreshed_at)
      SELECT DISTINCT t.task_queue_id, 'static', NULL::text, $1::timestamptz
      FROM queue_forecast_tasks t
      WHERE t.task_queue_id IS NOT NULL
        AND NOT EXISTS (
          SELECT 1 FROM queue_forecast_worker_pools wp
          WHERE wp.task_queue_id = t.task_queue_id AND wp.pool_kind = 'dynamic'
        )
      ON CONFLICT (task_queue_id) DO UPDATE SET
        refreshed_at = EXCLUDED.refreshed_at
      WHERE queue_forecast_worker_pools.pool_kind != 'dynamic'
    `, [now]);
    await client.query('COMMIT');
    console.log(`[worker-counter] pool dimension refreshed: ${dynamicPools.length} dynamic pools`);
  } catch (err) {
    await client.query('ROLLBACK');
    throw err;
  } finally {
    client.release();
  }
}


// ---------------------------------------------------------------------------
// Main sample loop
// ---------------------------------------------------------------------------

/**
 * Take a single worker-count snapshot.
 * Failures are caught and logged — the loop continues regardless.
 */
async function sampleOnce() {
  const start = Date.now();
  const now = new Date();
  try {
    const [dynStats, claimedMap] = await Promise.all([
      fetchDynamicPoolStats(),
      claimedTasksByQueue(now),
    ]);

    // Build one row per queue — union of dynamic pool IDs and queues with
    // in-flight tasks in our local DB.
    const byQueue = new Map();

    for (const s of dynStats.stats) {
      byQueue.set(s.task_queue_id, {
        sampled_at:        now,
        task_queue_id:     s.task_queue_id,
        running_workers:   s.running_workers,
        claimed_tasks:     claimedMap.get(s.task_queue_id) ?? 0,
        existing_capacity: s.existing_capacity,
      });
    }

    for (const [qid, claimed] of claimedMap.entries()) {
      if (!byQueue.has(qid)) {
        byQueue.set(qid, {
          sampled_at:        now,
          task_queue_id:     qid,
          running_workers:   null, // static pool; no live worker count from TC API
          claimed_tasks:     claimed,
          existing_capacity: null,
        });
      }
    }

    const rows = Array.from(byQueue.values());
    await upsertWorkerCounts(rows, 'tc_api');

    const ms = Date.now() - start;
    console.log(
      `[worker-counter] sample: ${rows.length} rows ` +
      `(dyn=${dynStats.stats.length} pages=${dynStats.pages} claimed_queues=${claimedMap.size}) ` +
      `in ${ms}ms`,
    );
  } catch (err) {
    // Failure-isolated: log and keep the interval alive.
    console.error(`[worker-counter] sample failed: ${err.stack || err.message}`);
  }
}


// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

async function main() {
  console.log(`[worker-counter] starting, rootUrl=${rootUrl}`);

  // Warm up pool dimension before the first sample so classification rows exist.
  try {
    await refreshPoolDimension();
  } catch (e) {
    console.error('[worker-counter] initial pool refresh failed:', e.stack || e.message);
  }

  await sampleOnce();

  const sampleTimer  = setInterval(sampleOnce,           SAMPLE_INTERVAL_MS);
  const refreshTimer = setInterval(refreshPoolDimension, REFRESH_POOLS_MS);

  const shutdown = async (signal) => {
    console.log(`[worker-counter] received ${signal}, shutting down`);
    clearInterval(sampleTimer);
    clearInterval(refreshTimer);
    try {
      await pool.end();
    } catch (e) {
      console.error('[worker-counter] error closing pool:', e.message);
    }
    process.exit(0);
  };

  process.on('SIGINT',  () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));
}

main().catch(err => {
  console.error('[worker-counter] fatal:', err);
  process.exit(1);
});
