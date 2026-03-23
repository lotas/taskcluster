import { createPool } from './db.js';
import { normalizeMetadataName } from './utils.js';

const pool = createPool(process.env.DATABASE_URL || 'postgresql://postgres@host.docker.internal:5433/forecasting');

const MIN_SAMPLE_SIZE = 5;

async function findPendingTask() {
  const res = await pool.query(`
    SELECT task_id, run_id, metadata_name, normalized_name, task_queue_id,
           tags, scheduler_id, image_name, scheduled, started, queue_pending
    FROM task_events
    WHERE scheduled IS NOT NULL
      AND started IS NULL
      AND resolved IS NULL
    ORDER BY scheduled DESC
    LIMIT 1
  `);
  if (res.rows.length === 0) {
    // Fallback: find a recently scheduled task (even if started)
    const fallback = await pool.query(`
      SELECT task_id, run_id, metadata_name, normalized_name, task_queue_id,
             tags, scheduler_id, image_name, scheduled, started, queue_pending
      FROM task_events
      WHERE scheduled IS NOT NULL AND metadata_name IS NOT NULL
      ORDER BY scheduled DESC
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
      sql: `SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY run_duration_s) AS p50,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY run_duration_s) AS p90,
                   count(*) AS sample_size
            FROM task_events
            WHERE run_id IS NOT NULL AND run_duration_s IS NOT NULL
              AND metadata_name = $1 AND reason_resolved = 'completed'
              AND resolved < $2 AND resolved > $2::timestamptz - INTERVAL '7 days'`,
      params: [task.metadata_name, asOfDate],
    });
  }

  // Level 2: normalized_name
  const normName = task.normalized_name || normalizeMetadataName(task.metadata_name);
  if (normName && normName !== task.metadata_name) {
    levels.push({
      label: 'normalized_name',
      sql: `SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY run_duration_s) AS p50,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY run_duration_s) AS p90,
                   count(*) AS sample_size
            FROM task_events
            WHERE run_id IS NOT NULL AND run_duration_s IS NOT NULL
              AND normalized_name = $1 AND reason_resolved = 'completed'
              AND resolved < $2 AND resolved > $2::timestamptz - INTERVAL '7 days'`,
      params: [normName, asOfDate],
    });
  }

  // Level 3: tags kind + test-type
  const tags = parseTags(task.tags);
  if (tags?.kind && tags?.['test-type']) {
    levels.push({
      label: 'kind+test-type',
      sql: `SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY run_duration_s) AS p50,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY run_duration_s) AS p90,
                   count(*) AS sample_size
            FROM task_events
            WHERE run_id IS NOT NULL AND run_duration_s IS NOT NULL
              AND tags->>'kind' = $1 AND tags->>'test-type' = $2
              AND reason_resolved = 'completed'
              AND resolved < $3 AND resolved > $3::timestamptz - INTERVAL '7 days'`,
      params: [tags.kind, tags['test-type'], asOfDate],
    });
  }

  // Level 4: task_queue_id
  if (task.task_queue_id) {
    levels.push({
      label: 'task_queue_id',
      sql: `SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY run_duration_s) AS p50,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY run_duration_s) AS p90,
                   count(*) AS sample_size
            FROM task_events
            WHERE run_id IS NOT NULL AND run_duration_s IS NOT NULL
              AND task_queue_id = $1 AND reason_resolved = 'completed'
              AND resolved < $2 AND resolved > $2::timestamptz - INTERVAL '7 days'`,
      params: [task.task_queue_id, asOfDate],
    });
  }

  // Level 5: image_name
  if (task.image_name) {
    levels.push({
      label: 'image_name',
      sql: `SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY run_duration_s) AS p50,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY run_duration_s) AS p90,
                   count(*) AS sample_size
            FROM task_events
            WHERE run_id IS NOT NULL AND run_duration_s IS NOT NULL
              AND image_name = $1 AND reason_resolved = 'completed'
              AND resolved < $2 AND resolved > $2::timestamptz - INTERVAL '7 days'`,
      params: [task.image_name, asOfDate],
    });
  }

  // Level 6: scheduler_id
  if (task.scheduler_id) {
    levels.push({
      label: 'scheduler_id',
      sql: `SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY run_duration_s) AS p50,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY run_duration_s) AS p90,
                   count(*) AS sample_size
            FROM task_events
            WHERE run_id IS NOT NULL AND run_duration_s IS NOT NULL
              AND scheduler_id = $1 AND reason_resolved = 'completed'
              AND resolved < $2 AND resolved > $2::timestamptz - INTERVAL '7 days'`,
      params: [task.scheduler_id, asOfDate],
    });
  }

  // Level 7: global
  levels.push({
    label: 'global',
    sql: `SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY run_duration_s) AS p50,
                 percentile_cont(0.9) WITHIN GROUP (ORDER BY run_duration_s) AS p90,
                 count(*) AS sample_size
          FROM task_events
          WHERE run_id IS NOT NULL AND run_duration_s IS NOT NULL
            AND reason_resolved = 'completed'
            AND resolved < $1 AND resolved > $1::timestamptz - INTERVAL '7 days'`,
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
  const isPending = !task.started;

  console.log(`Task: ${task.task_id} (run ${task.run_id ?? 'none'})`);
  console.log(`  Name:       ${task.metadata_name || '(not enriched)'}`);
  console.log(`  Queue:      ${task.task_queue_id || '(unknown)'}`);
  console.log(`  Scheduler:  ${task.scheduler_id || '(unknown)'}`);
  console.log(`  Tags:       ${tags ? JSON.stringify(tags) : '(none)'}`);
  console.log(`  Scheduled:  ${task.scheduled}`);
  console.log(`  Status:     ${isPending ? 'PENDING (not started)' : `started at ${task.started}`}`);
  if (task.queue_pending != null) {
    console.log(`  Queue depth: ${task.queue_pending} pending at schedule time`);
  }

  console.log('\n--- Prediction ---');
  const prediction = await predictWithFallback(task);
  if (!prediction) {
    console.log('  No prediction possible (insufficient historical data).');
    return;
  }

  console.log(`  Match level:  ${prediction.level}`);
  console.log(`  Sample size:  ${prediction.sample_size} completed runs (last 7 days)`);
  console.log(`  p50 duration: ${fmt(prediction.p50)}`);
  console.log(`  p90 duration: ${fmt(prediction.p90)}`);
}

try {
  await run();
} catch (err) {
  console.error('Prediction failed:', err);
} finally {
  await pool.end();
}
