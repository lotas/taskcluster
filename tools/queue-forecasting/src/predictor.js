import { createPool } from './db.js';
import { normalizeMetadataName, pendingBucket, PENDING_BUCKET_SQL, writeLineWithBackpressure } from './utils.js';

const pool = createPool(process.env.DATABASE_URL);

// --- Wait-time bucket helpers ---

const WAIT_BUCKETS = [
  { name: '<1m',   lo: 0,    hi: 60       },
  { name: '1-5m',  lo: 60,   hi: 300      },
  { name: '5-30m', lo: 300,  hi: 1800     },
  { name: '30m+',  lo: 1800, hi: Infinity },
];

function waitBucket(actualSeconds) {
  for (const b of WAIT_BUCKETS) {
    if (actualSeconds >= b.lo && actualSeconds < b.hi) return b.name;
  }
  return null;
}

// --- Duration Prediction (hierarchical fallback) ---
//
// predictDuration() is the single-task prediction function intended for real-time
// use (e.g., a future API endpoint that predicts duration for a newly pending task).
// It queries with a per-task asOfDate, which matters when tasks have different
// pending times. Currently unused — the backtest path below uses bulk-loaded
// statistics (predictDurationFromStats) for performance at scale.
//
// TODO: Wire this into a real-time prediction API or CLI single-task mode.

const DURATION_BY_METADATA_NAME = `
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY r.run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY r.run_duration_s) AS p90,
       count(*) AS sample_size
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.run_duration_s IS NOT NULL
  AND t.metadata_name = $1
  AND r.reason_resolved = 'completed'
  AND r.resolved_at < $2
  AND r.resolved_at > $2::timestamptz - INTERVAL '7 days';
`;

const DURATION_BY_NORMALIZED_NAME = `
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY r.run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY r.run_duration_s) AS p90,
       count(*) AS sample_size
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.run_duration_s IS NOT NULL
  AND t.normalized_name = $1
  AND r.reason_resolved = 'completed'
  AND r.resolved_at < $2
  AND r.resolved_at > $2::timestamptz - INTERVAL '7 days';
`;

const DURATION_BY_KIND_AND_TEST_TYPE = `
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY r.run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY r.run_duration_s) AS p90,
       count(*) AS sample_size
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.run_duration_s IS NOT NULL
  AND t.tags->>'kind' = $1
  AND t.tags->>'test-type' = $2
  AND r.reason_resolved = 'completed'
  AND r.resolved_at < $3
  AND r.resolved_at > $3::timestamptz - INTERVAL '7 days';
`;

const DURATION_BY_TASK_QUEUE_ID = `
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY r.run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY r.run_duration_s) AS p90,
       count(*) AS sample_size
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.run_duration_s IS NOT NULL
  AND t.task_queue_id = $1
  AND r.reason_resolved = 'completed'
  AND r.resolved_at < $2
  AND r.resolved_at > $2::timestamptz - INTERVAL '7 days';
`;

const DURATION_BY_SCHEDULER_ID = `
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY r.run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY r.run_duration_s) AS p90,
       count(*) AS sample_size
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.run_duration_s IS NOT NULL
  AND t.scheduler_id = $1
  AND r.reason_resolved = 'completed'
  AND r.resolved_at < $2
  AND r.resolved_at > $2::timestamptz - INTERVAL '7 days';
`;

const DURATION_GLOBAL = `
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY r.run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY r.run_duration_s) AS p90,
       count(*) AS sample_size
FROM queue_forecast_task_runs r
WHERE r.run_duration_s IS NOT NULL
  AND r.reason_resolved = 'completed'
  AND r.resolved_at < $1
  AND r.resolved_at > $1::timestamptz - INTERVAL '7 days';
`;

const MIN_SAMPLE_SIZE = 5;

async function predictDuration(task, asOfDate) {
  const tryLevel = async (label, sql, params) => {
    const res = await pool.query(sql, params);
    const row = res.rows[0];
    if (row && parseInt(row.sample_size, 10) >= MIN_SAMPLE_SIZE) {
      return { level: label, p50: row.p50, p90: row.p90, sample_size: parseInt(row.sample_size, 10) };
    }
    return null;
  };

  // Level 1: metadata_name exact match
  if (task.metadata_name) {
    const r = await tryLevel('metadata_name', DURATION_BY_METADATA_NAME, [task.metadata_name, asOfDate]);
    if (r) return r;
  }

  // Level 2: normalized_name (skip if same as metadata_name)
  const normName = task.normalized_name || normalizeMetadataName(task.metadata_name);
  if (normName && normName !== task.metadata_name) {
    const r = await tryLevel('normalized_name', DURATION_BY_NORMALIZED_NAME, [normName, asOfDate]);
    if (r) return r;
  }

  // Level 3: tags kind + test-type (handle tags as string or object)
  const parsedTags = parseTags(task.tags);
  const kind = parsedTags?.kind;
  const testType = parsedTags?.['test-type'];
  if (kind && testType) {
    const r = await tryLevel('kind+test-type', DURATION_BY_KIND_AND_TEST_TYPE, [kind, testType, asOfDate]);
    if (r) return r;
  }

  // Level 4: task_queue_id
  if (task.task_queue_id) {
    const r = await tryLevel('task_queue_id', DURATION_BY_TASK_QUEUE_ID, [task.task_queue_id, asOfDate]);
    if (r) return r;
  }

  // Level 5: scheduler_id
  if (task.scheduler_id) {
    const r = await tryLevel('scheduler_id', DURATION_BY_SCHEDULER_ID, [task.scheduler_id, asOfDate]);
    if (r) return r;
  }

  // Level 6: global median
  const r = await tryLevel('global', DURATION_GLOBAL, [asOfDate]);
  if (r) return r;

  return null;
}

// --- Bulk Statistics Queries (for backtest) ---

const BULK_STATS_BY_METADATA_NAME = `
SELECT t.metadata_name AS key,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY r.run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY r.run_duration_s) AS p90,
       count(*) AS sample_size
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.run_duration_s IS NOT NULL
  AND t.metadata_name IS NOT NULL
  AND r.reason_resolved = 'completed'
  AND r.resolved_at < $1::date
  AND r.resolved_at > $1::date - INTERVAL '7 days'
GROUP BY t.metadata_name;
`;

const BULK_STATS_BY_KIND_TEST_TYPE = `
SELECT (t.tags->>'kind') || '|' || (t.tags->>'test-type') AS key,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY r.run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY r.run_duration_s) AS p90,
       count(*) AS sample_size
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.run_duration_s IS NOT NULL
  AND t.tags IS NOT NULL
  AND t.tags->>'kind' IS NOT NULL
  AND t.tags->>'test-type' IS NOT NULL
  AND r.reason_resolved = 'completed'
  AND r.resolved_at < $1::date
  AND r.resolved_at > $1::date - INTERVAL '7 days'
GROUP BY t.tags->>'kind', t.tags->>'test-type';
`;

const BULK_STATS_BY_NORMALIZED_NAME = `
SELECT t.normalized_name AS key,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY r.run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY r.run_duration_s) AS p90,
       count(*) AS sample_size
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.run_duration_s IS NOT NULL
  AND t.normalized_name IS NOT NULL
  AND r.reason_resolved = 'completed'
  AND r.resolved_at < $1::date
  AND r.resolved_at > $1::date - INTERVAL '7 days'
GROUP BY t.normalized_name;
`;

const BULK_STATS_BY_TASK_QUEUE_ID = `
SELECT t.task_queue_id AS key,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY r.run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY r.run_duration_s) AS p90,
       count(*) AS sample_size
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.run_duration_s IS NOT NULL
  AND t.task_queue_id IS NOT NULL
  AND r.reason_resolved = 'completed'
  AND r.resolved_at < $1::date
  AND r.resolved_at > $1::date - INTERVAL '7 days'
GROUP BY t.task_queue_id;
`;

const BULK_STATS_BY_SCHEDULER_ID = `
SELECT t.scheduler_id AS key,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY r.run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY r.run_duration_s) AS p90,
       count(*) AS sample_size
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.run_duration_s IS NOT NULL
  AND t.scheduler_id IS NOT NULL
  AND r.reason_resolved = 'completed'
  AND r.resolved_at < $1::date
  AND r.resolved_at > $1::date - INTERVAL '7 days'
GROUP BY t.scheduler_id;
`;

async function loadBulkStats(date) {
  const [byName, byNormName, byKindType, byQueue, byScheduler, globalRes] = await Promise.all([
    pool.query(BULK_STATS_BY_METADATA_NAME, [date]),
    pool.query(BULK_STATS_BY_NORMALIZED_NAME, [date]),
    pool.query(BULK_STATS_BY_KIND_TEST_TYPE, [date]),
    pool.query(BULK_STATS_BY_TASK_QUEUE_ID, [date]),
    pool.query(BULK_STATS_BY_SCHEDULER_ID, [date]),
    pool.query(DURATION_GLOBAL, [date]),
  ]);

  const toMap = (rows) => {
    const m = new Map();
    for (const r of rows) {
      if (parseInt(r.sample_size, 10) >= MIN_SAMPLE_SIZE) {
        m.set(r.key, { p50: r.p50, p90: r.p90, sample_size: parseInt(r.sample_size, 10) });
      }
    }
    return m;
  };

  const globalRow = globalRes.rows[0];
  const globalStats = (globalRow && parseInt(globalRow.sample_size, 10) >= MIN_SAMPLE_SIZE)
    ? { p50: globalRow.p50, p90: globalRow.p90, sample_size: parseInt(globalRow.sample_size, 10) }
    : null;

  return {
    byMetadataName: toMap(byName.rows),
    byNormalizedName: toMap(byNormName.rows),
    byKindTestType: toMap(byKindType.rows),
    byTaskQueueId: toMap(byQueue.rows),
    bySchedulerId: toMap(byScheduler.rows),
    global: globalStats,
  };
}

// --- Wait Time Prediction (queue-aware) ---

const BULK_WAIT_STATS_BY_QUEUE_PRIORITY_AND_BUCKET = `
SELECT t.task_queue_id || '|' || r.priority_at_pending || '|' || ${PENDING_BUCKET_SQL} AS key,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p90,
       count(*) AS sample_size
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.wait_duration_s IS NOT NULL
  AND t.task_queue_id IS NOT NULL
  AND r.priority_at_pending IS NOT NULL
  AND r.started_at IS NOT NULL
  AND r.resolved_at < $1::date
  AND r.resolved_at > $1::date - INTERVAL '7 days'
GROUP BY t.task_queue_id, r.priority_at_pending, ${PENDING_BUCKET_SQL};
`;

const BULK_WAIT_STATS_BY_QUEUE_AND_BUCKET = `
SELECT t.task_queue_id || '|' || ${PENDING_BUCKET_SQL} AS key,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p90,
       count(*) AS sample_size
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.wait_duration_s IS NOT NULL
  AND r.started_at IS NOT NULL
  AND r.resolved_at < $1::date
  AND r.resolved_at > $1::date - INTERVAL '7 days'
GROUP BY t.task_queue_id, ${PENDING_BUCKET_SQL};
`;

const BULK_WAIT_STATS_BY_QUEUE = `
SELECT t.task_queue_id AS key,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p90,
       count(*) AS sample_size
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.wait_duration_s IS NOT NULL
  AND t.task_queue_id IS NOT NULL
  AND r.started_at IS NOT NULL
  AND r.resolved_at < $1::date
  AND r.resolved_at > $1::date - INTERVAL '7 days'
GROUP BY t.task_queue_id;
`;

const BULK_WAIT_STATS_BY_PRIORITY_AND_BUCKET = `
SELECT r.priority_at_pending || '|' || ${PENDING_BUCKET_SQL} AS key,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p90,
       count(*) AS sample_size
FROM queue_forecast_task_runs r
WHERE r.wait_duration_s IS NOT NULL
  AND r.started_at IS NOT NULL
  AND r.resolved_at < $1::date
  AND r.resolved_at > $1::date - INTERVAL '7 days'
GROUP BY r.priority_at_pending, ${PENDING_BUCKET_SQL};
`;

const WAIT_GLOBAL = `
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p90,
       count(*) AS sample_size
FROM queue_forecast_task_runs r
WHERE r.wait_duration_s IS NOT NULL
  AND r.started_at IS NOT NULL
  AND r.resolved_at < $1::date
  AND r.resolved_at > $1::date - INTERVAL '7 days';
`;

async function loadBulkWaitStats(date) {
  const [byQueuePriorityBucket, byQueueBucket, byQueue, byPriorityBucket, globalRes] = await Promise.all([
    pool.query(BULK_WAIT_STATS_BY_QUEUE_PRIORITY_AND_BUCKET, [date]),
    pool.query(BULK_WAIT_STATS_BY_QUEUE_AND_BUCKET, [date]),
    pool.query(BULK_WAIT_STATS_BY_QUEUE, [date]),
    pool.query(BULK_WAIT_STATS_BY_PRIORITY_AND_BUCKET, [date]),
    pool.query(WAIT_GLOBAL, [date]),
  ]);

  const toMap = (rows) => {
    const m = new Map();
    for (const r of rows) {
      if (parseInt(r.sample_size, 10) >= MIN_SAMPLE_SIZE) {
        m.set(r.key, { p50: r.p50, p90: r.p90, sample_size: parseInt(r.sample_size, 10) });
      }
    }
    return m;
  };

  const globalRow = globalRes.rows[0];
  const globalStats = (globalRow && parseInt(globalRow.sample_size, 10) >= MIN_SAMPLE_SIZE)
    ? { p50: globalRow.p50, p90: globalRow.p90, sample_size: parseInt(globalRow.sample_size, 10) }
    : null;

  return {
    byQueuePriorityAndBucket: toMap(byQueuePriorityBucket.rows),
    byQueueAndBucket: toMap(byQueueBucket.rows),
    byQueue: toMap(byQueue.rows),
    byPriorityAndBucket: toMap(byPriorityBucket.rows),
    global: globalStats,
  };
}

function predictWaitFromStats(task, waitStats) {
  const bucket = pendingBucket(task.queue_pending);

  // Level 1: queue + priority + pending bucket — the most specific. Wait time
  // in a deep queue is dominated by priority, so a priority-blind baseline
  // badly mis-anchors high/low-priority tasks. Skip when priority/bucket unknown.
  if (task.task_queue_id && task.priority_at_pending != null && bucket != null) {
    const s = waitStats.byQueuePriorityAndBucket.get(`${task.task_queue_id}|${task.priority_at_pending}|${bucket}`);
    if (s) return { level: 'queue+priority+bucket', ...s };
  }

  // Level 2: queue + pending bucket (skip when queue_pending unknown)
  if (task.task_queue_id && bucket != null) {
    const s = waitStats.byQueueAndBucket.get(`${task.task_queue_id}|${bucket}`);
    if (s) return { level: 'queue+bucket', ...s };
  }

  // Level 3: queue only
  if (task.task_queue_id) {
    const s = waitStats.byQueue.get(task.task_queue_id);
    if (s) return { level: 'queue', ...s };
  }

  // Level 4: priority + pending bucket (skip when queue_pending unknown)
  if (task.priority_at_pending != null && bucket != null) {
    const s = waitStats.byPriorityAndBucket.get(`${task.priority_at_pending}|${bucket}`);
    if (s) return { level: 'priority+bucket', ...s };
  }

  // Level 5: global
  if (waitStats.global) return { level: 'global', ...waitStats.global };

  return null;
}

function parseTags(tags) {
  if (!tags) return null;
  if (typeof tags === 'object') return tags;
  try { return JSON.parse(tags); } catch { return null; }
}

function predictDurationFromStats(task, stats) {
  // Level 1: metadata_name exact match
  if (task.metadata_name) {
    const s = stats.byMetadataName.get(task.metadata_name);
    if (s) return { level: 'metadata_name', ...s };
  }

  // Level 2: normalized_name (skip if same as metadata_name)
  const normName = task.normalized_name || normalizeMetadataName(task.metadata_name);
  if (normName && normName !== task.metadata_name) {
    const s = stats.byNormalizedName.get(normName);
    if (s) return { level: 'normalized_name', ...s };
  }

  // Level 3: tags kind + test-type (handle tags as string or object)
  const tags = parseTags(task.tags);
  const kind = tags?.kind;
  const testType = tags?.['test-type'];
  if (kind && testType) {
    const s = stats.byKindTestType.get(`${kind}|${testType}`);
    if (s) return { level: 'kind+test-type', ...s };
  }

  // Level 4: task_queue_id
  if (task.task_queue_id) {
    const s = stats.byTaskQueueId.get(task.task_queue_id);
    if (s) return { level: 'task_queue_id', ...s };
  }

  // Level 5: scheduler_id
  if (task.scheduler_id) {
    const s = stats.bySchedulerId.get(task.scheduler_id);
    if (s) return { level: 'scheduler_id', ...s };
  }

  // Level 6: global median
  if (stats.global) return { level: 'global', ...stats.global };

  return null;
}

// --- Pending-eval-date mode ---
// Evaluate the baseline on runs whose pending_at falls on day D
// (UTC), using percentile history restricted to rows resolved strictly
// before D 00:00Z. This is the apples-to-apples mode used for
// model comparison. See trainer-spec.md §"Evaluation protocol".

const RESOLVED_TASKS_SQL_PENDING = `
SELECT r.task_id, r.run_id, t.metadata_name, t.normalized_name, t.task_queue_id, t.tags,
       t.scheduler_id, r.priority_at_pending, r.queue_pending,
       r.pending_at, r.started_at, r.resolved_at,
       r.run_duration_s, r.wait_duration_s,
       r.reason_resolved
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.pending_at >= $1::timestamptz
  AND r.pending_at <  $1::timestamptz + INTERVAL '1 day'
  AND r.reason_resolved = 'completed';
`;

// Rewrite existing bulk-stats SQL for pending-eval-date mode.
// Swaps the two `resolved_at` bounds for a single `resolved_at < $cutoff`
// plus `resolved_at > $cutoff - INTERVAL '7 days'`. Same 7-day lookback
// window but anchored on the feature-available cutoff instant instead
// of the eval-resolve date.
//
// When `excludeDates` is non-empty, also injects an extra clause excluding
// any history rows whose `resolved_at::date` is in the given set. Empty
// or undefined `excludeDates` MUST yield the exact original SQL string
// (idempotence with pre-flag callers).
function toPendingHistorySql(sql, excludeDates) {
  let out = sql
    .replace(/r\.resolved_at < \$1::date/g, 'r.resolved_at < $1::timestamptz')
    .replace(/r\.resolved_at > \$1::date - INTERVAL '7 days'/g,
             "r.resolved_at > $1::timestamptz - INTERVAL '7 days'");
  if (excludeDates && excludeDates.length > 0) {
    out = out.replace(
      /r\.resolved_at > \$1::timestamptz - INTERVAL '7 days'/g,
      "r.resolved_at > $1::timestamptz - INTERVAL '7 days'\n  AND r.resolved_at::date <> ALL($2::date[])"
    );
  }
  return out;
}

async function loadBulkStatsForCutoff(cutoff, excludeDates = []) {
  const q = [
    BULK_STATS_BY_METADATA_NAME,
    BULK_STATS_BY_NORMALIZED_NAME,
    BULK_STATS_BY_KIND_TEST_TYPE,
    BULK_STATS_BY_TASK_QUEUE_ID,
    BULK_STATS_BY_SCHEDULER_ID,
    DURATION_GLOBAL,
  ].map(sql => toPendingHistorySql(sql, excludeDates));

  const params = excludeDates.length > 0 ? [cutoff, excludeDates] : [cutoff];
  const [byName, byNormName, byKindType, byQueue, byScheduler, globalRes] = await Promise.all(
    q.map(sql => pool.query(sql, params))
  );

  const toMap = (rows) => {
    const m = new Map();
    for (const r of rows) {
      if (parseInt(r.sample_size, 10) >= MIN_SAMPLE_SIZE) {
        m.set(r.key, { p50: r.p50, p90: r.p90, sample_size: parseInt(r.sample_size, 10) });
      }
    }
    return m;
  };

  const g = globalRes.rows[0];
  const globalStats = (g && parseInt(g.sample_size, 10) >= MIN_SAMPLE_SIZE)
    ? { p50: g.p50, p90: g.p90, sample_size: parseInt(g.sample_size, 10) }
    : null;

  return {
    byMetadataName: toMap(byName.rows),
    byNormalizedName: toMap(byNormName.rows),
    byKindTestType: toMap(byKindType.rows),
    byTaskQueueId: toMap(byQueue.rows),
    bySchedulerId: toMap(byScheduler.rows),
    global: globalStats,
  };
}

async function loadBulkWaitStatsForCutoff(cutoff, excludeDates = []) {
  const q = [
    BULK_WAIT_STATS_BY_QUEUE_PRIORITY_AND_BUCKET,
    BULK_WAIT_STATS_BY_QUEUE_AND_BUCKET,
    BULK_WAIT_STATS_BY_QUEUE,
    BULK_WAIT_STATS_BY_PRIORITY_AND_BUCKET,
    WAIT_GLOBAL,
  ].map(sql => toPendingHistorySql(sql, excludeDates));

  const params = excludeDates.length > 0 ? [cutoff, excludeDates] : [cutoff];
  const [byQueuePriorityBucket, byQueueBucket, byQueue, byPriorityBucket, globalRes] = await Promise.all(
    q.map(sql => pool.query(sql, params))
  );

  const toMap = (rows) => {
    const m = new Map();
    for (const r of rows) {
      if (parseInt(r.sample_size, 10) >= MIN_SAMPLE_SIZE) {
        m.set(r.key, { p50: r.p50, p90: r.p90, sample_size: parseInt(r.sample_size, 10) });
      }
    }
    return m;
  };

  const g = globalRes.rows[0];
  const globalStats = (g && parseInt(g.sample_size, 10) >= MIN_SAMPLE_SIZE)
    ? { p50: g.p50, p90: g.p90, sample_size: parseInt(g.sample_size, 10) }
    : null;

  return {
    byQueuePriorityAndBucket: toMap(byQueuePriorityBucket.rows),
    byQueueAndBucket: toMap(byQueueBucket.rows),
    byQueue: toMap(byQueue.rows),
    byPriorityAndBucket: toMap(byPriorityBucket.rows),
    global: globalStats,
  };
}

// --- Export Baseline Predictions ---

async function runExportBaselinePredictions({ fromDate, toDate, outputPath, excludeDates = [] }) {
  // Validate args
  const from = new Date(`${fromDate}T00:00:00Z`);
  const to   = new Date(`${toDate}T00:00:00Z`);
  if (!(from < to)) {
    throw new Error(`--from (${fromDate}) must be strictly before --to (${toDate})`);
  }

  const fs = await import('node:fs');
  const out = fs.createWriteStream(outputPath, { flags: 'w' });
  let cumulative = 0;

  const ROW_SQL = `
    SELECT r.task_id, r.run_id, t.metadata_name, t.normalized_name, t.task_queue_id, t.tags,
           t.scheduler_id, r.priority_at_pending, r.queue_pending,
           r.pending_at, r.started_at, r.resolved_at,
           r.run_duration_s, r.wait_duration_s,
           r.reason_resolved
    FROM queue_forecast_task_runs r
    JOIN queue_forecast_tasks t ON r.task_id = t.task_id
    WHERE r.pending_at >= $1::timestamptz
      AND r.pending_at <  $1::timestamptz + INTERVAL '1 day';
  `;

  for (let d = new Date(from); d < to; d.setUTCDate(d.getUTCDate() + 1)) {
    const cutoff = d.toISOString();           // e.g. "2026-04-15T00:00:00.000Z"
    const dayStr = cutoff.slice(0, 10);        // "2026-04-15"
    const [stats, waitStats, rowsRes] = await Promise.all([
      loadBulkStatsForCutoff(cutoff, excludeDates),
      loadBulkWaitStatsForCutoff(cutoff, excludeDates),
      pool.query(ROW_SQL, [cutoff]),
    ]);
    let dayCount = 0;
    for (const row of rowsRes.rows) {
      const blD = predictDurationFromStats(row, stats);
      const blW = predictWaitFromStats(row, waitStats);
      await writeLineWithBackpressure(out, JSON.stringify({
        task_id: row.task_id,
        run_id:  row.run_id,
        pending_at: row.pending_at instanceof Date ? row.pending_at.toISOString() : row.pending_at,
        bl_duration_p50: blD ? parseFloat(blD.p50) : null,
        bl_duration_p90: blD ? parseFloat(blD.p90) : null,
        bl_wait_p50:     blW ? parseFloat(blW.p50) : null,
        bl_wait_p90:     blW ? parseFloat(blW.p90) : null,
      }) + '\n');
      dayCount++;
    }
    cumulative += dayCount;
    process.stderr.write(`[export] ${dayStr}: ${dayCount.toLocaleString()} rows (cumulative ${cumulative.toLocaleString()})\n`);
  }

  await new Promise((resolve, reject) => out.end(err => err ? reject(err) : resolve()));
  console.log(`Exported ${cumulative.toLocaleString()} baseline-prediction rows to ${outputPath}`);
}

// --- Backtest ---

const RESOLVED_TASKS_SQL = `
SELECT r.task_id, r.run_id, t.metadata_name, t.normalized_name, t.task_queue_id, t.tags,
       t.scheduler_id, r.priority_at_pending, r.queue_pending,
       r.pending_at, r.started_at, r.resolved_at,
       r.run_duration_s, r.wait_duration_s,
       r.reason_resolved
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.resolved_at IS NOT NULL
  AND r.reason_resolved = 'completed'
  AND r.resolved_at >= $1::date
  AND r.resolved_at < $1::date + INTERVAL '1 day';
`;

async function runBacktest(date) {
  console.log(`\n=== Backtest for ${date} ===\n`);

  // Bulk-load all statistics in parallel
  const [stats, waitStats, tasksRes] = await Promise.all([
    loadBulkStats(date),
    loadBulkWaitStats(date),
    pool.query(RESOLVED_TASKS_SQL, [date]),
  ]);
  const tasks = tasksRes.rows;

  if (tasks.length === 0) {
    console.log('No completed tasks found for this date.');
    return;
  }

  console.log(`Found ${tasks.length} completed runs\n`);

  let predictions = 0;
  let totalError = 0;
  let within2x = 0;
  const levelCounts = {};

  for (const task of tasks) {
    if (task.run_duration_s == null) continue;

    const prediction = predictDurationFromStats(task, stats);
    if (!prediction) continue;

    predictions++;
    const actual = parseFloat(task.run_duration_s);
    const predicted = parseFloat(prediction.p50);
    const error = Math.abs(predicted - actual);
    totalError += error;

    if (predicted > 0 && actual > 0) {
      const ratio = Math.max(predicted / actual, actual / predicted);
      if (ratio <= 2) within2x++;
    }

    levelCounts[prediction.level] = (levelCounts[prediction.level] || 0) + 1;
  }

  if (predictions > 0) {
    const mae = totalError / predictions;
    const within2xPct = ((within2x / predictions) * 100).toFixed(1);

    console.log('--- Duration Prediction Accuracy ---');
    console.log(`  Tasks evaluated:       ${predictions}`);
    console.log(`  Mean Absolute Error:   ${mae.toFixed(1)}s`);
    console.log(`  Within 2x of actual:   ${within2xPct}%`);
    console.log(`  Prediction levels used:`);
    for (const [level, count] of Object.entries(levelCounts)) {
      console.log(`    ${level}: ${count}`);
    }
  } else {
    console.log('No duration predictions could be made (insufficient historical data).');
  }

  // --- Wait Time Evaluation ---
  let waitPredictions = 0;
  let waitTotalError = 0;
  let waitWithin2x = 0;
  const waitLevelCounts = {};

  for (const task of tasks) {
    if (task.wait_duration_s == null) continue;

    const prediction = predictWaitFromStats(task, waitStats);
    if (!prediction) continue;

    waitPredictions++;
    const actual = parseFloat(task.wait_duration_s);
    const predicted = parseFloat(prediction.p50);
    const error = Math.abs(predicted - actual);
    waitTotalError += error;

    if (predicted > 0 && actual > 0) {
      const ratio = Math.max(predicted / actual, actual / predicted);
      if (ratio <= 2) waitWithin2x++;
    }

    waitLevelCounts[prediction.level] = (waitLevelCounts[prediction.level] || 0) + 1;
  }

  if (waitPredictions > 0) {
    const waitMae = waitTotalError / waitPredictions;
    const waitWithin2xPct = ((waitWithin2x / waitPredictions) * 100).toFixed(1);

    console.log('\n--- Wait Time Prediction Accuracy ---');
    console.log(`  Tasks evaluated:       ${waitPredictions}`);
    console.log(`  Mean Absolute Error:   ${waitMae.toFixed(1)}s`);
    console.log(`  Within 2x of actual:   ${waitWithin2xPct}%`);
    console.log(`  Prediction levels used:`);
    for (const [level, count] of Object.entries(waitLevelCounts)) {
      console.log(`    ${level}: ${count}`);
    }
  } else {
    console.log('\nNo wait time predictions could be made (insufficient historical data).');
  }
}

async function runPendingEvalBacktest(dateStr, excludeDates = []) {
  const cutoff = `${dateStr}T00:00:00Z`;

  console.log(`\n=== Pending-eval-date backtest for ${dateStr} ===\n`);
  console.log(`  Eval cohort: pending_at ∈ [${cutoff}, +1d), reason_resolved='completed'`);
  console.log(`  History cutoff: resolved_at < ${cutoff} (7-day trailing window)\n`);

  const [stats, waitStats, tasksRes] = await Promise.all([
    loadBulkStatsForCutoff(cutoff, excludeDates),
    loadBulkWaitStatsForCutoff(cutoff, excludeDates),
    pool.query(RESOLVED_TASKS_SQL_PENDING, [cutoff]),
  ]);
  const tasks = tasksRes.rows;
  console.log(`Found ${tasks.length} completed runs pending on ${dateStr}\n`);

  const agg = {
    duration: { n: 0, mae: { eligible_n: 0, sum_abs_error: 0 }, within_2x: { eligible_n: 0, hit_n: 0 } },
    wait: {
      n: 0,
      mae: { eligible_n: 0, sum_abs_error: 0 },
      within_2x: { eligible_n: 0, hit_n: 0 },
      buckets: Object.fromEntries(
        WAIT_BUCKETS.map(b => [b.name, {
          mae: { eligible_n: 0, sum_abs_error: 0 },
          within_2x: { eligible_n: 0, hit_n: 0 },
        }])
      ),
    },
  };

  for (const task of tasks) {
    // Duration
    if (task.run_duration_s != null) {
      const p = predictDurationFromStats(task, stats);
      if (p) {
        const actual = parseFloat(task.run_duration_s);
        const predicted = parseFloat(p.p50);
        agg.duration.n++;
        agg.duration.mae.eligible_n++;
        agg.duration.mae.sum_abs_error += Math.abs(predicted - actual);
        if (predicted > 0 && actual > 0) {
          agg.duration.within_2x.eligible_n++;
          const ratio = Math.max(predicted / actual, actual / predicted);
          if (ratio <= 2) agg.duration.within_2x.hit_n++;
        }
      }
    }

    // Wait — cohort must match the Python trainer's wait_time.yaml filter:
    //   started_at IS NOT NULL AND wait_duration_s IS NOT NULL
    //     AND wait_duration_s >= 0 AND queue_pending IS NOT NULL
    // so the baseline and trainer evaluate on the same rows.
    if (task.wait_duration_s != null
        && task.queue_pending != null
        && parseFloat(task.wait_duration_s) >= 0) {
      const p = predictWaitFromStats(task, waitStats);
      if (p) {
        const actual = parseFloat(task.wait_duration_s);
        const predicted = parseFloat(p.p50);
        agg.wait.n++;
        agg.wait.mae.eligible_n++;
        agg.wait.mae.sum_abs_error += Math.abs(predicted - actual);
        if (predicted > 0 && actual > 0) {
          agg.wait.within_2x.eligible_n++;
          const ratio = Math.max(predicted / actual, actual / predicted);
          if (ratio <= 2) agg.wait.within_2x.hit_n++;
        }
        const bucket = waitBucket(actual);
        if (bucket) {
          const b = agg.wait.buckets[bucket];
          b.mae.eligible_n++;
          b.mae.sum_abs_error += Math.abs(predicted - actual);
          if (predicted > 0 && actual > 0) {
            b.within_2x.eligible_n++;
            const ratio = Math.max(predicted / actual, actual / predicted);
            if (ratio <= 2) b.within_2x.hit_n++;
          }
        }
      }
    }
  }

  const pct = (num, den) => den > 0 ? (num / den * 100).toFixed(1) + '%' : 'n/a';
  const avg = (num, den) => den > 0 ? (num / den).toFixed(1) + 's' : 'n/a';
  console.log('--- Duration ---');
  console.log(`  n=${agg.duration.n} MAE=${avg(agg.duration.mae.sum_abs_error, agg.duration.mae.eligible_n)} within_2x=${pct(agg.duration.within_2x.hit_n, agg.duration.within_2x.eligible_n)}`);
  console.log('--- Wait ---');
  console.log(`  n=${agg.wait.n} MAE=${avg(agg.wait.mae.sum_abs_error, agg.wait.mae.eligible_n)} within_2x=${pct(agg.wait.within_2x.hit_n, agg.wait.within_2x.eligible_n)}`);
  console.log('--- Wait by bucket ---');
  for (const b of WAIT_BUCKETS) {
    const bd = agg.wait.buckets[b.name];
    const mae = bd.mae.eligible_n ? (bd.mae.sum_abs_error / bd.mae.eligible_n).toFixed(1) + 's' : 'n/a';
    const w2x = bd.within_2x.eligible_n ? (bd.within_2x.hit_n / bd.within_2x.eligible_n * 100).toFixed(1) + '%' : 'n/a';
    console.log(`  ${b.name.padEnd(6)} n=${bd.mae.eligible_n} MAE=${mae} within_2x=${w2x}`);
  }

  return { dateStr, cutoff, agg };
}

function buildOutputJson(result) {
  const { dateStr, cutoff, agg } = result;
  const nextDay = new Date(new Date(cutoff).getTime() + 24 * 3600 * 1000).toISOString();
  return {
    mode: 'pending-eval-date',
    eval_date: dateStr,
    eval_window: {
      pending_start: cutoff,
      pending_end:   nextDay,
    },
    history_cutoff: cutoff,
    history_lookback_days: 7,
    slice: "reason_resolved = 'completed'",
    duration: {
      n: agg.duration.n,
      mae: {
        eligible_n:    agg.duration.mae.eligible_n,
        sum_abs_error: agg.duration.mae.sum_abs_error,
      },
      within_2x: {
        eligible_n: agg.duration.within_2x.eligible_n,
        hit_n:      agg.duration.within_2x.hit_n,
      },
    },
    wait: {
      n: agg.wait.n,
      mae: {
        eligible_n:    agg.wait.mae.eligible_n,
        sum_abs_error: agg.wait.mae.sum_abs_error,
      },
      within_2x: {
        eligible_n: agg.wait.within_2x.eligible_n,
        hit_n:      agg.wait.within_2x.hit_n,
      },
      buckets: agg.wait.buckets,
    },
  };
}

// --- CLI ---

const args = process.argv.slice(2);
let date = null;
let pendingEvalDate = null;
let outputJson = null;
let exportBaselinePredictions = false;
let fromDate = null;
let toDate = null;
let excludeDates = [];

for (let i = 0; i < args.length; i++) {
  if (args[i] === '--date' && args[i + 1]) { date = args[i + 1]; i++; continue; }
  if (args[i] === '--pending-eval-date' && args[i + 1]) { pendingEvalDate = args[i + 1]; i++; continue; }
  if (args[i] === '--output-json' && args[i + 1]) { outputJson = args[i + 1]; i++; continue; }
  if (args[i] === '--output'      && args[i + 1]) { outputJson = args[i + 1]; i++; continue; }
  if (args[i] === '--export-baseline-predictions') { exportBaselinePredictions = true; continue; }
  if (args[i] === '--from' && args[i + 1]) { fromDate = args[i + 1]; i++; continue; }
  if (args[i] === '--to'   && args[i + 1]) { toDate   = args[i + 1]; i++; continue; }
  if (args[i] === '--exclude-dates' && args[i + 1]) {
    excludeDates = args[i + 1].split(',').map(s => s.trim()).filter(Boolean);
    for (const d of excludeDates) {
      if (!/^\d{4}-\d{2}-\d{2}$/.test(d)) {
        console.error(`Invalid --exclude-dates entry: ${d}`);
        process.exit(1);
      }
    }
    i++;
    continue;
  }
}

if (excludeDates.length > 0) {
  process.stderr.write(`[predictor] excluding ${excludeDates.length} anomalous date(s) from history: ${excludeDates.join(',')}\n`);
}

const isValidDate = (s) => /^\d{4}-\d{2}-\d{2}$/.test(s) && !isNaN(new Date(s).getTime());

if (!date && !pendingEvalDate && !exportBaselinePredictions) {
  console.error('Usage:');
  console.error('  node src/predictor.js --date YYYY-MM-DD');
  console.error('  node src/predictor.js --pending-eval-date YYYY-MM-DD [--output-json path]');
  console.error('  node src/predictor.js --export-baseline-predictions --from YYYY-MM-DD --to YYYY-MM-DD --output path.ndjson');
  process.exit(1);
}

if (date && !isValidDate(date)) {
  console.error(`Invalid --date: ${date}`);
  process.exit(1);
}
if (pendingEvalDate && !isValidDate(pendingEvalDate)) {
  console.error(`Invalid --pending-eval-date: ${pendingEvalDate}`);
  process.exit(1);
}

try {
  if (exportBaselinePredictions) {
    if (!fromDate || !toDate || !outputJson) {
      console.error('Usage: --export-baseline-predictions --from YYYY-MM-DD --to YYYY-MM-DD --output path.ndjson');
      process.exit(1);
    }
    if (!isValidDate(fromDate) || !isValidDate(toDate)) {
      console.error(`Invalid date range: ${fromDate} .. ${toDate}`);
      process.exit(1);
    }
    await runExportBaselinePredictions({ fromDate, toDate, outputPath: outputJson, excludeDates });
  } else if (pendingEvalDate) {
    const result = await runPendingEvalBacktest(pendingEvalDate, excludeDates);
    if (outputJson) {
      const fs = await import('node:fs/promises');
      await fs.writeFile(outputJson, JSON.stringify(buildOutputJson(result), null, 2));
      console.log(`\nWrote baseline JSON: ${outputJson}`);
    }
  } else {
    await runBacktest(date);
  }
} finally {
  await pool.end();
}
