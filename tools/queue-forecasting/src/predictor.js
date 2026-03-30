import { createPool } from './db.js';
import { normalizeMetadataName, pendingBucket, PENDING_BUCKET_SQL } from './utils.js';

const pool = createPool(process.env.DATABASE_URL);

// --- Duration Prediction (hierarchical fallback) ---
//
// predictDuration() is the single-task prediction function intended for real-time
// use (e.g., a future API endpoint that predicts duration for a newly pending task).
// It queries with a per-task asOfDate, which matters when tasks have different
// scheduled times. Currently unused — the backtest path below uses bulk-loaded
// statistics (predictDurationFromStats) for performance at scale.
//
// TODO: Wire this into a real-time prediction API or CLI single-task mode.

const DURATION_BY_METADATA_NAME = `
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY run_duration_s) AS p90,
       count(*) AS sample_size
FROM task_events
WHERE run_id IS NOT NULL
  AND run_duration_s IS NOT NULL
  AND metadata_name = $1
  AND reason_resolved = 'completed'
  AND resolved < $2
  AND resolved > $2::timestamptz - INTERVAL '7 days';
`;

const DURATION_BY_NORMALIZED_NAME = `
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY run_duration_s) AS p90,
       count(*) AS sample_size
FROM task_events
WHERE run_id IS NOT NULL
  AND run_duration_s IS NOT NULL
  AND normalized_name = $1
  AND reason_resolved = 'completed'
  AND resolved < $2
  AND resolved > $2::timestamptz - INTERVAL '7 days';
`;

const DURATION_BY_KIND_AND_TEST_TYPE = `
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY run_duration_s) AS p90,
       count(*) AS sample_size
FROM task_events
WHERE run_id IS NOT NULL
  AND run_duration_s IS NOT NULL
  AND tags->>'kind' = $1
  AND tags->>'test-type' = $2
  AND reason_resolved = 'completed'
  AND resolved < $3
  AND resolved > $3::timestamptz - INTERVAL '7 days';
`;

const DURATION_BY_TASK_QUEUE_ID = `
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY run_duration_s) AS p90,
       count(*) AS sample_size
FROM task_events
WHERE run_id IS NOT NULL
  AND run_duration_s IS NOT NULL
  AND task_queue_id = $1
  AND reason_resolved = 'completed'
  AND resolved < $2
  AND resolved > $2::timestamptz - INTERVAL '7 days';
`;

const DURATION_BY_IMAGE_NAME = `
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY run_duration_s) AS p90,
       count(*) AS sample_size
FROM task_events
WHERE run_id IS NOT NULL
  AND run_duration_s IS NOT NULL
  AND image_name = $1
  AND reason_resolved = 'completed'
  AND resolved < $2
  AND resolved > $2::timestamptz - INTERVAL '7 days';
`;

const DURATION_BY_SCHEDULER_ID = `
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY run_duration_s) AS p90,
       count(*) AS sample_size
FROM task_events
WHERE run_id IS NOT NULL
  AND run_duration_s IS NOT NULL
  AND scheduler_id = $1
  AND reason_resolved = 'completed'
  AND resolved < $2
  AND resolved > $2::timestamptz - INTERVAL '7 days';
`;

const DURATION_GLOBAL = `
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY run_duration_s) AS p90,
       count(*) AS sample_size
FROM task_events
WHERE run_id IS NOT NULL
  AND run_duration_s IS NOT NULL
  AND reason_resolved = 'completed'
  AND resolved < $1
  AND resolved > $1::timestamptz - INTERVAL '7 days';
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

  // Level 5: image_name
  if (task.image_name) {
    const r = await tryLevel('image_name', DURATION_BY_IMAGE_NAME, [task.image_name, asOfDate]);
    if (r) return r;
  }

  // Level 6: scheduler_id
  if (task.scheduler_id) {
    const r = await tryLevel('scheduler_id', DURATION_BY_SCHEDULER_ID, [task.scheduler_id, asOfDate]);
    if (r) return r;
  }

  // Level 7: global median
  const r = await tryLevel('global', DURATION_GLOBAL, [asOfDate]);
  if (r) return r;

  return null;
}

// --- Bulk Statistics Queries (for backtest) ---

const BULK_STATS_BY_METADATA_NAME = `
SELECT metadata_name AS key,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY run_duration_s) AS p90,
       count(*) AS sample_size
FROM task_events
WHERE run_id IS NOT NULL
  AND run_duration_s IS NOT NULL
  AND metadata_name IS NOT NULL
  AND reason_resolved = 'completed'
  AND resolved < $1::date
  AND resolved > $1::date - INTERVAL '7 days'
GROUP BY metadata_name;
`;

const BULK_STATS_BY_KIND_TEST_TYPE = `
SELECT (tags->>'kind') || '|' || (tags->>'test-type') AS key,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY run_duration_s) AS p90,
       count(*) AS sample_size
FROM task_events
WHERE run_id IS NOT NULL
  AND run_duration_s IS NOT NULL
  AND tags IS NOT NULL
  AND tags->>'kind' IS NOT NULL
  AND tags->>'test-type' IS NOT NULL
  AND reason_resolved = 'completed'
  AND resolved < $1::date
  AND resolved > $1::date - INTERVAL '7 days'
GROUP BY tags->>'kind', tags->>'test-type';
`;

const BULK_STATS_BY_NORMALIZED_NAME = `
SELECT normalized_name AS key,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY run_duration_s) AS p90,
       count(*) AS sample_size
FROM task_events
WHERE run_id IS NOT NULL
  AND run_duration_s IS NOT NULL
  AND normalized_name IS NOT NULL
  AND reason_resolved = 'completed'
  AND resolved < $1::date
  AND resolved > $1::date - INTERVAL '7 days'
GROUP BY normalized_name;
`;

const BULK_STATS_BY_TASK_QUEUE_ID = `
SELECT task_queue_id AS key,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY run_duration_s) AS p90,
       count(*) AS sample_size
FROM task_events
WHERE run_id IS NOT NULL
  AND run_duration_s IS NOT NULL
  AND task_queue_id IS NOT NULL
  AND reason_resolved = 'completed'
  AND resolved < $1::date
  AND resolved > $1::date - INTERVAL '7 days'
GROUP BY task_queue_id;
`;

const BULK_STATS_BY_IMAGE_NAME = `
SELECT image_name AS key,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY run_duration_s) AS p90,
       count(*) AS sample_size
FROM task_events
WHERE run_id IS NOT NULL
  AND run_duration_s IS NOT NULL
  AND image_name IS NOT NULL
  AND reason_resolved = 'completed'
  AND resolved < $1::date
  AND resolved > $1::date - INTERVAL '7 days'
GROUP BY image_name;
`;

const BULK_STATS_BY_SCHEDULER_ID = `
SELECT scheduler_id AS key,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY run_duration_s) AS p90,
       count(*) AS sample_size
FROM task_events
WHERE run_id IS NOT NULL
  AND run_duration_s IS NOT NULL
  AND scheduler_id IS NOT NULL
  AND reason_resolved = 'completed'
  AND resolved < $1::date
  AND resolved > $1::date - INTERVAL '7 days'
GROUP BY scheduler_id;
`;

async function loadBulkStats(date) {
  const [byName, byNormName, byKindType, byQueue, byImage, byScheduler, globalRes] = await Promise.all([
    pool.query(BULK_STATS_BY_METADATA_NAME, [date]),
    pool.query(BULK_STATS_BY_NORMALIZED_NAME, [date]),
    pool.query(BULK_STATS_BY_KIND_TEST_TYPE, [date]),
    pool.query(BULK_STATS_BY_TASK_QUEUE_ID, [date]),
    pool.query(BULK_STATS_BY_IMAGE_NAME, [date]),
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
    byImageName: toMap(byImage.rows),
    bySchedulerId: toMap(byScheduler.rows),
    global: globalStats,
  };
}

// --- Wait Time Prediction (queue-aware) ---

const BULK_WAIT_STATS_BY_QUEUE_AND_BUCKET = `
SELECT task_queue_id || '|' || ${PENDING_BUCKET_SQL} AS key,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY wait_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY wait_duration_s) AS p90,
       count(*) AS sample_size
FROM task_events
WHERE run_id IS NOT NULL
  AND wait_duration_s IS NOT NULL
  AND reason_resolved = 'completed'
  AND resolved < $1::date
  AND resolved > $1::date - INTERVAL '7 days'
GROUP BY task_queue_id, ${PENDING_BUCKET_SQL};
`;

const BULK_WAIT_STATS_BY_QUEUE = `
SELECT task_queue_id AS key,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY wait_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY wait_duration_s) AS p90,
       count(*) AS sample_size
FROM task_events
WHERE run_id IS NOT NULL
  AND wait_duration_s IS NOT NULL
  AND task_queue_id IS NOT NULL
  AND reason_resolved = 'completed'
  AND resolved < $1::date
  AND resolved > $1::date - INTERVAL '7 days'
GROUP BY task_queue_id;
`;

const BULK_WAIT_STATS_BY_PRIORITY_AND_BUCKET = `
SELECT priority || '|' || ${PENDING_BUCKET_SQL} AS key,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY wait_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY wait_duration_s) AS p90,
       count(*) AS sample_size
FROM task_events
WHERE run_id IS NOT NULL
  AND wait_duration_s IS NOT NULL
  AND reason_resolved = 'completed'
  AND resolved < $1::date
  AND resolved > $1::date - INTERVAL '7 days'
GROUP BY priority, ${PENDING_BUCKET_SQL};
`;

const WAIT_GLOBAL = `
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY wait_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY wait_duration_s) AS p90,
       count(*) AS sample_size
FROM task_events
WHERE run_id IS NOT NULL
  AND wait_duration_s IS NOT NULL
  AND reason_resolved = 'completed'
  AND resolved < $1::date
  AND resolved > $1::date - INTERVAL '7 days';
`;

async function loadBulkWaitStats(date) {
  const [byQueueBucket, byQueue, byPriorityBucket, globalRes] = await Promise.all([
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
    byQueueAndBucket: toMap(byQueueBucket.rows),
    byQueue: toMap(byQueue.rows),
    byPriorityAndBucket: toMap(byPriorityBucket.rows),
    global: globalStats,
  };
}

function predictWaitFromStats(task, waitStats) {
  const bucket = pendingBucket(task.queue_pending);

  // Level 1: queue + pending bucket
  if (task.task_queue_id) {
    const s = waitStats.byQueueAndBucket.get(`${task.task_queue_id}|${bucket}`);
    if (s) return { level: 'queue+bucket', ...s };
  }

  // Level 2: queue only
  if (task.task_queue_id) {
    const s = waitStats.byQueue.get(task.task_queue_id);
    if (s) return { level: 'queue', ...s };
  }

  // Level 3: priority + pending bucket
  if (task.priority != null) {
    const s = waitStats.byPriorityAndBucket.get(`${task.priority}|${bucket}`);
    if (s) return { level: 'priority+bucket', ...s };
  }

  // Level 4: global
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

  // Level 5: image_name
  if (task.image_name) {
    const s = stats.byImageName.get(task.image_name);
    if (s) return { level: 'image_name', ...s };
  }

  // Level 6: scheduler_id
  if (task.scheduler_id) {
    const s = stats.bySchedulerId.get(task.scheduler_id);
    if (s) return { level: 'scheduler_id', ...s };
  }

  // Level 7: global median
  if (stats.global) return { level: 'global', ...stats.global };

  return null;
}

// --- Backtest ---

const RESOLVED_TASKS_SQL = `
SELECT task_id, run_id, metadata_name, normalized_name, task_queue_id, tags,
       scheduler_id, image_name, priority, queue_pending,
       scheduled, started, resolved,
       run_duration_s, wait_duration_s,
       reason_resolved
FROM task_events
WHERE run_id IS NOT NULL
  AND resolved IS NOT NULL
  AND reason_resolved = 'completed'
  AND resolved >= $1::date
  AND resolved < $1::date + INTERVAL '1 day';
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

// --- CLI ---

const args = process.argv.slice(2);
let date = null;

for (let i = 0; i < args.length; i++) {
  if (args[i] === '--date' && args[i + 1]) {
    date = args[i + 1];
    i++;
  }
}

if (!date || !/^\d{4}-\d{2}-\d{2}$/.test(date) || isNaN(new Date(date).getTime())) {
  console.error('Usage: node src/predictor.js --date YYYY-MM-DD');
  process.exit(1);
}

try {
  await runBacktest(date);
} finally {
  await pool.end();
}
