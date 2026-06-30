import { createPool } from './db.js';

// ---------------------------------------------------------------------------
// Retention
//
// The time-series tables grow unbounded — collector/worker-counter/live-
// predictor only ever INSERT. This loop deletes rows older than RETENTION_DAYS
// so the Postgres volume stays bounded.
//
// Deletes are batched (one committed statement per batch) so a tick never
// holds a long transaction or generates a huge WAL spike, and so the collector
// keeps writing throughout.
//
// `queue_forecast_task_runs` is intentionally NOT listed: it has an
// `ON DELETE CASCADE` FK to `queue_forecast_tasks`, so deleting an old task
// removes its runs automatically. The other two tables age out on their own
// timestamp columns.
//
// Sizing note: a single training run reads `lookback_days + holdout + val`
// back from its as-of date (38d for run_duration), and a walk-forward sweep
// slides that back across cohorts — worst realistic reach ≈ 55d. 60d is the
// default so sweeps never request data that's been pruned.
// ---------------------------------------------------------------------------

const RETENTION_DAYS       = Number(process.env.RETENTION_DAYS ?? 60);
const INTERVAL_SECONDS     = Number(process.env.RETENTION_INTERVAL_SECONDS ?? 86400); // daily
const BATCH_SIZE           = Number(process.env.RETENTION_BATCH_SIZE ?? 50000);
const MAX_BATCHES_PER_TICK = Number(process.env.RETENTION_MAX_BATCHES ?? 100000);     // runaway guard

if (!process.env.DATABASE_URL) {
  console.error('[retention] DATABASE_URL is required');
  process.exit(1);
}

const pool = createPool(process.env.DATABASE_URL);

// Fixed allow-list (never user input) — table/column names are interpolated
// into SQL below, so they must stay literals defined here. Order matters:
// `tasks` first so its cascade clears matching `task_runs` rows.
const TARGETS = [
  { table: 'queue_forecast_tasks',           column: 'task_created' },
  { table: 'queue_forecast_run_predictions', column: 'predicted_at' },
  { table: 'queue_forecast_worker_counts',   column: 'sampled_at' },
];

// Batched delete by physical row id (ctid). The cutoff filter uses each
// table's timestamp index; the LIMIT keeps every statement small.
async function trimTarget({ table, column }, days, batchSize) {
  const sql = `
    DELETE FROM ${table}
    WHERE ctid IN (
      SELECT ctid FROM ${table}
      WHERE ${column} < now() - make_interval(days => $1)
      LIMIT $2
    )`;

  let deleted = 0;
  let batches = 0;
  for (;;) {
    const { rowCount } = await pool.query(sql, [days, batchSize]);
    deleted += rowCount;
    batches += 1;
    if (rowCount === 0) break;
    if (batches >= MAX_BATCHES_PER_TICK) {
      console.warn(
        `[retention] ${table}: hit MAX_BATCHES (${MAX_BATCHES_PER_TICK}) ` +
        `after ${deleted} rows — remaining backlog will clear on the next tick`,
      );
      break;
    }
  }
  return { deleted, batches };
}

let inTick = false;

async function tick() {
  if (inTick) {
    console.warn('[retention] previous tick still running, skipping');
    return;
  }
  inTick = true;
  console.log(`[retention] tick: pruning rows older than ${RETENTION_DAYS}d (batch ${BATCH_SIZE})`);
  try {
    for (const target of TARGETS) {
      try {
        const { deleted, batches } = await trimTarget(target, RETENTION_DAYS, BATCH_SIZE);
        console.log(`[retention] ${target.table}: deleted ${deleted} row(s) in ${batches} batch(es)`);
      } catch (e) {
        // One table failing must not stop the others or kill the loop.
        console.error(`[retention] ${target.table} failed:`, e.stack || e.message);
      }
    }
  } finally {
    inTick = false;
  }
}

async function main() {
  console.log(
    `[retention] starting: retention=${RETENTION_DAYS}d interval=${INTERVAL_SECONDS}s`,
  );

  await tick();

  const timer = setInterval(() => {
    tick().catch(e => console.error('[retention] tick raised:', e.stack || e.message));
  }, INTERVAL_SECONDS * 1000);

  const shutdown = async (signal) => {
    console.log(`[retention] received ${signal}, shutting down`);
    clearInterval(timer);
    try {
      await pool.end();
    } catch (e) {
      console.error('[retention] error closing pool:', e.message);
    }
    process.exit(0);
  };

  process.on('SIGINT',  () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));
}

main().catch(err => {
  console.error('[retention] fatal:', err);
  process.exit(1);
});
