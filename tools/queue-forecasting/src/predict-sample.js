import { createPool } from './db.js';
import { normalizeMetadataName, pendingBucket, PENDING_BUCKET_SQL } from './utils.js';

const pool = createPool(process.env.DATABASE_URL || 'postgresql://postgres@host.docker.internal:5433/forecasting');

const MIN_SAMPLE_SIZE = 5;

async function findPendingTask() {
  const res = await pool.query(`
    SELECT t.task_id, r.run_id, t.metadata_name, t.normalized_name, t.task_queue_id,
           t.tags, t.scheduler_id, r.priority_at_pending, r.pending_at, r.started_at, r.queue_pending
    FROM queue_forecast_task_runs r
    JOIN queue_forecast_tasks t ON r.task_id = t.task_id
    WHERE r.pending_at IS NOT NULL
      AND r.started_at IS NULL
      AND r.resolved_at IS NULL
    ORDER BY r.pending_at DESC
    LIMIT 1
  `);
  if (res.rows.length === 0) {
    // Fallback: find a recently scheduled task (even if started)
    const fallback = await pool.query(`
      SELECT t.task_id, r.run_id, t.metadata_name, t.normalized_name, t.task_queue_id,
             t.tags, t.scheduler_id, r.priority_at_pending, r.pending_at, r.started_at, r.queue_pending
      FROM queue_forecast_task_runs r
      JOIN queue_forecast_tasks t ON r.task_id = t.task_id
      WHERE r.pending_at IS NOT NULL AND t.metadata_name IS NOT NULL
      ORDER BY r.pending_at DESC
      LIMIT 1
    `);
    return fallback.rows[0] || null;
  }
  return res.rows[0];
}

function parseTags(tags) {
  if (!tags) return null;
  if (typeof tags === 'object') return tags;
  try { return JSON.parse(tags); } catch { return null; }
}

async function predictWithFallback(task) {
  const asOfDate = new Date().toISOString();
  const levels = [];

  // Level 1: metadata_name
  if (task.metadata_name) {
    levels.push({
      label: 'metadata_name',
      sql: `SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY r.run_duration_s) AS p50,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY r.run_duration_s) AS p90,
                   count(*) AS sample_size
            FROM queue_forecast_task_runs r
            JOIN queue_forecast_tasks t ON r.task_id = t.task_id
            WHERE r.run_duration_s IS NOT NULL
              AND t.metadata_name = $1 AND r.reason_resolved = 'completed'
              AND r.resolved_at < $2 AND r.resolved_at > $2::timestamptz - INTERVAL '7 days'`,
      params: [task.metadata_name, asOfDate],
    });
  }

  // Level 2: normalized_name
  const normName = task.normalized_name || normalizeMetadataName(task.metadata_name);
  if (normName && normName !== task.metadata_name) {
    levels.push({
      label: 'normalized_name',
      sql: `SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY r.run_duration_s) AS p50,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY r.run_duration_s) AS p90,
                   count(*) AS sample_size
            FROM queue_forecast_task_runs r
            JOIN queue_forecast_tasks t ON r.task_id = t.task_id
            WHERE r.run_duration_s IS NOT NULL
              AND t.normalized_name = $1 AND r.reason_resolved = 'completed'
              AND r.resolved_at < $2 AND r.resolved_at > $2::timestamptz - INTERVAL '7 days'`,
      params: [normName, asOfDate],
    });
  }

  // Level 3: tags kind + test-type
  const tags = parseTags(task.tags);
  if (tags?.kind && tags?.['test-type']) {
    levels.push({
      label: 'kind+test-type',
      sql: `SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY r.run_duration_s) AS p50,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY r.run_duration_s) AS p90,
                   count(*) AS sample_size
            FROM queue_forecast_task_runs r
            JOIN queue_forecast_tasks t ON r.task_id = t.task_id
            WHERE r.run_duration_s IS NOT NULL
              AND t.tags->>'kind' = $1 AND t.tags->>'test-type' = $2
              AND r.reason_resolved = 'completed'
              AND r.resolved_at < $3 AND r.resolved_at > $3::timestamptz - INTERVAL '7 days'`,
      params: [tags.kind, tags['test-type'], asOfDate],
    });
  }

  // Level 4: task_queue_id
  if (task.task_queue_id) {
    levels.push({
      label: 'task_queue_id',
      sql: `SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY r.run_duration_s) AS p50,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY r.run_duration_s) AS p90,
                   count(*) AS sample_size
            FROM queue_forecast_task_runs r
            JOIN queue_forecast_tasks t ON r.task_id = t.task_id
            WHERE r.run_duration_s IS NOT NULL
              AND t.task_queue_id = $1 AND r.reason_resolved = 'completed'
              AND r.resolved_at < $2 AND r.resolved_at > $2::timestamptz - INTERVAL '7 days'`,
      params: [task.task_queue_id, asOfDate],
    });
  }

  // Level 5: scheduler_id
  if (task.scheduler_id) {
    levels.push({
      label: 'scheduler_id',
      sql: `SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY r.run_duration_s) AS p50,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY r.run_duration_s) AS p90,
                   count(*) AS sample_size
            FROM queue_forecast_task_runs r
            JOIN queue_forecast_tasks t ON r.task_id = t.task_id
            WHERE r.run_duration_s IS NOT NULL
              AND t.scheduler_id = $1 AND r.reason_resolved = 'completed'
              AND r.resolved_at < $2 AND r.resolved_at > $2::timestamptz - INTERVAL '7 days'`,
      params: [task.scheduler_id, asOfDate],
    });
  }

  // Level 6: global
  levels.push({
    label: 'global',
    sql: `SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY r.run_duration_s) AS p50,
                 percentile_cont(0.9) WITHIN GROUP (ORDER BY r.run_duration_s) AS p90,
                 count(*) AS sample_size
          FROM queue_forecast_task_runs r
          WHERE r.run_duration_s IS NOT NULL
            AND r.reason_resolved = 'completed'
            AND r.resolved_at < $1 AND r.resolved_at > $1::timestamptz - INTERVAL '7 days'`,
    params: [asOfDate],
  });

  for (const level of levels) {
    const res = await pool.query(level.sql, level.params);
    const row = res.rows[0];
    if (row && parseInt(row.sample_size, 10) >= MIN_SAMPLE_SIZE) {
      return {
        level: level.label,
        p50: parseFloat(row.p50),
        p90: parseFloat(row.p90),
        sample_size: parseInt(row.sample_size, 10),
      };
    }
  }
  return null;
}

async function predictWaitWithFallback(task) {
  const asOfDate = new Date().toISOString();
  const bucket = pendingBucket(task.queue_pending);
  const levels = [];

  // Level 1: queue + pending bucket (skip when queue_pending unknown)
  if (task.task_queue_id && bucket != null) {
    levels.push({
      label: 'queue+bucket',
      sql: `SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p50,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p90,
                   count(*) AS sample_size
            FROM queue_forecast_task_runs r
            JOIN queue_forecast_tasks t ON r.task_id = t.task_id
            WHERE r.wait_duration_s IS NOT NULL
              AND t.task_queue_id = $1 AND ${PENDING_BUCKET_SQL} = $2
              AND r.started_at IS NOT NULL
              AND r.resolved_at < $3 AND r.resolved_at > $3::timestamptz - INTERVAL '7 days'`,
      params: [task.task_queue_id, bucket, asOfDate],
    });
  }

  // Level 2: queue only
  if (task.task_queue_id) {
    levels.push({
      label: 'queue',
      sql: `SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p50,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p90,
                   count(*) AS sample_size
            FROM queue_forecast_task_runs r
            JOIN queue_forecast_tasks t ON r.task_id = t.task_id
            WHERE r.wait_duration_s IS NOT NULL
              AND t.task_queue_id = $1
              AND r.started_at IS NOT NULL
              AND r.resolved_at < $2 AND r.resolved_at > $2::timestamptz - INTERVAL '7 days'`,
      params: [task.task_queue_id, asOfDate],
    });
  }

  // Level 3: priority + pending bucket (skip when queue_pending unknown)
  if (task.priority_at_pending != null && bucket != null) {
    levels.push({
      label: 'priority+bucket',
      sql: `SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p50,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p90,
                   count(*) AS sample_size
            FROM queue_forecast_task_runs r
            WHERE r.wait_duration_s IS NOT NULL
              AND r.priority_at_pending = $1 AND ${PENDING_BUCKET_SQL} = $2
              AND r.started_at IS NOT NULL
              AND r.resolved_at < $3 AND r.resolved_at > $3::timestamptz - INTERVAL '7 days'`,
      params: [task.priority_at_pending, bucket, asOfDate],
    });
  }

  // Level 4: global
  levels.push({
    label: 'global',
    sql: `SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p50,
                 percentile_cont(0.9) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p90,
                 count(*) AS sample_size
          FROM queue_forecast_task_runs r
          WHERE r.wait_duration_s IS NOT NULL
            AND r.started_at IS NOT NULL
            AND r.resolved_at < $1 AND r.resolved_at > $1::timestamptz - INTERVAL '7 days'`,
    params: [asOfDate],
  });

  for (const level of levels) {
    const res = await pool.query(level.sql, level.params);
    const row = res.rows[0];
    if (row && parseInt(row.sample_size, 10) >= MIN_SAMPLE_SIZE) {
      return {
        level: level.label,
        p50: parseFloat(row.p50),
        p90: parseFloat(row.p90),
        sample_size: parseInt(row.sample_size, 10),
      };
    }
  }
  return null;
}

function fmt(seconds) {
  if (seconds < 60) return `${seconds.toFixed(0)}s`;
  if (seconds < 3600) return `${(seconds / 60).toFixed(1)}m`;
  return `${(seconds / 3600).toFixed(2)}h`;
}

async function run() {
  console.log('=== Predict Sample ===\n');

  const task = await findPendingTask();
  if (!task) {
    console.log('No tasks found in database.');
    return;
  }

  const tags = parseTags(task.tags);
  const isPending = !task.started_at;

  console.log(`Task: ${task.task_id} (run ${task.run_id ?? 'none'})`);
  console.log(`  Name:       ${task.metadata_name || '(not enriched)'}`);
  console.log(`  Queue:      ${task.task_queue_id || '(unknown)'}`);
  console.log(`  Scheduler:  ${task.scheduler_id || '(unknown)'}`);
  console.log(`  Tags:       ${tags ? JSON.stringify(tags) : '(none)'}`);
  console.log(`  Pending at: ${task.pending_at}`);
  console.log(`  Status:     ${isPending ? 'PENDING (not started)' : `started at ${task.started_at}`}`);
  if (task.queue_pending != null) {
    console.log(`  Queue depth: ${task.queue_pending} pending at schedule time`);
  }

  console.log('\n--- Duration Prediction ---');
  const prediction = await predictWithFallback(task);
  if (!prediction) {
    console.log('  No prediction possible (insufficient historical data).');
  } else {
    console.log(`  Match level:  ${prediction.level}`);
    console.log(`  Sample size:  ${prediction.sample_size} completed runs (last 7 days)`);
    console.log(`  p50 duration: ${fmt(prediction.p50)}`);
    console.log(`  p90 duration: ${fmt(prediction.p90)}`);
  }

  console.log('\n--- Wait Time Prediction ---');
  const bucket = pendingBucket(task.queue_pending);
  console.log(`  Pending bucket: ${bucket} (queue_pending=${task.queue_pending ?? 'null'})`);
  const waitPrediction = await predictWaitWithFallback(task);
  if (!waitPrediction) {
    console.log('  No wait prediction possible (insufficient historical data).');
  } else {
    console.log(`  Match level:  ${waitPrediction.level}`);
    console.log(`  Sample size:  ${waitPrediction.sample_size} completed runs (last 7 days)`);
    console.log(`  p50 wait:     ${fmt(waitPrediction.p50)}`);
    console.log(`  p90 wait:     ${fmt(waitPrediction.p90)}`);

    if (isPending && prediction) {
      const pendingTime = new Date(task.pending_at);
      const predictedStart = new Date(pendingTime.getTime() + waitPrediction.p50 * 1000);
      const predictedCompletion = new Date(predictedStart.getTime() + prediction.p50 * 1000);
      console.log('\n--- Combined Estimate (pending task) ---');
      console.log(`  Predicted start:      ${predictedStart.toISOString()} (pending + wait p50)`);
      console.log(`  Predicted completion: ${predictedCompletion.toISOString()} (start + duration p50)`);
      console.log(`  Total time:           ${fmt(waitPrediction.p50 + prediction.p50)}`);
    }
  }
}

try {
  await run();
} catch (err) {
  console.error('Prediction failed:', err);
} finally {
  await pool.end();
}
