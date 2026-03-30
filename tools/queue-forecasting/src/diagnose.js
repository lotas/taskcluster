import { createPool } from './db.js';

const pool = createPool(process.env.DATABASE_URL || 'postgresql://postgres@host.docker.internal:5433/forecasting');

async function run() {
  console.log('=== Database Diagnostics ===\n');

  // 1. Check tags column type
  console.log('--- Column Types ---');
  const colRes = await pool.query(`
    SELECT column_name, data_type, udt_name
    FROM information_schema.columns
    WHERE table_name = 'task_events'
    ORDER BY ordinal_position
  `);
  for (const row of colRes.rows) {
    const flag = row.column_name === 'tags' ? ' <---' : '';
    console.log(`  ${row.column_name}: ${row.data_type} (${row.udt_name})${flag}`);
  }

  // 2. Try the failing query
  console.log('\n--- JSONB Operator Test ---');
  try {
    const testRes = await pool.query(`SELECT tags->>'kind' AS kind FROM task_events LIMIT 1`);
    console.log(`  tags->>'kind' works: ${JSON.stringify(testRes.rows[0])}`);
  } catch (err) {
    console.log(`  tags->>'kind' FAILS: ${err.message}`);
  }

  // 3. Row counts
  console.log('\n--- Row Counts ---');
  const countRes = await pool.query(`SELECT count(*) AS total FROM task_events`);
  console.log(`  Total rows: ${countRes.rows[0].total}`);

  const enrichedRes = await pool.query(`SELECT count(*) AS n FROM task_events WHERE metadata_name IS NOT NULL`);
  console.log(`  Enriched (has metadata_name): ${enrichedRes.rows[0].n}`);

  const resolvedRes = await pool.query(`SELECT count(*) AS n FROM task_events WHERE resolved IS NOT NULL`);
  console.log(`  Resolved: ${resolvedRes.rows[0].n}`);

  const withDuration = await pool.query(`SELECT count(*) AS n FROM task_events WHERE run_duration_s IS NOT NULL`);
  console.log(`  With run_duration_s: ${withDuration.rows[0].n}`);

  const completedRes = await pool.query(`SELECT count(*) AS n FROM task_events WHERE reason_resolved = 'completed'`);
  console.log(`  Completed: ${completedRes.rows[0].n}`);

  // 4. Field completeness
  console.log('\n--- Field Completeness (non-null counts) ---');
  const fields = [
    'tags', 'task_queue_id', 'scheduler_id', 'normalized_name',
    'image_name', 'queue_pending', 'run_id',
  ];
  for (const f of fields) {
    const res = await pool.query(`SELECT count(*) AS n FROM task_events WHERE ${f} IS NOT NULL`);
    console.log(`  ${f}: ${res.rows[0].n}`);
  }

  // 5. Tags content sample
  console.log('\n--- Tags Sample (first 5 non-null) ---');
  const tagSample = await pool.query(`SELECT tags FROM task_events WHERE tags IS NOT NULL LIMIT 5`);
  for (const row of tagSample.rows) {
    const val = row.tags;
    console.log(`  type=${typeof val}, value=${JSON.stringify(val).slice(0, 120)}`);
  }

  // 6. Temporal range
  console.log('\n--- Temporal Range ---');
  const rangeRes = await pool.query(`
    SELECT min(scheduled) AS earliest_scheduled, max(scheduled) AS latest_scheduled,
           min(resolved) AS earliest_resolved, max(resolved) AS latest_resolved
    FROM task_events
  `);
  const r = rangeRes.rows[0];
  console.log(`  Scheduled: ${r.earliest_scheduled} to ${r.latest_scheduled}`);
  console.log(`  Resolved:  ${r.earliest_resolved} to ${r.latest_resolved}`);

  // 7. Resolution reasons breakdown
  console.log('\n--- Resolution Reasons ---');
  const reasonRes = await pool.query(`
    SELECT reason_resolved, count(*) AS n
    FROM task_events
    WHERE reason_resolved IS NOT NULL
    GROUP BY reason_resolved
    ORDER BY n DESC
  `);
  for (const row of reasonRes.rows) {
    console.log(`  ${row.reason_resolved}: ${row.n}`);
  }

  // ========== Queue Pending Analysis ==========
  console.log('\n\n=== Queue Pending Analysis ===\n');

  // 8. Overall NULL rate
  console.log('--- Overall NULL Rate ---');
  const overallRes = await pool.query(`
    SELECT
      count(*) AS total,
      count(queue_pending) AS non_null,
      round(100.0 * count(queue_pending) / NULLIF(count(*), 0), 2) AS pct_non_null
    FROM task_events
  `);
  const ov = overallRes.rows[0];
  console.log(`  Total rows: ${ov.total}`);
  console.log(`  queue_pending NOT NULL: ${ov.non_null} (${ov.pct_non_null}%)`);
  console.log(`  queue_pending IS NULL: ${ov.total - ov.non_null}`);

  // 9. NULL rate among completed runs
  console.log('\n--- NULL Rate Among Completed Runs ---');
  const completedNullRes = await pool.query(`
    SELECT
      count(*) AS total,
      count(queue_pending) AS non_null,
      round(100.0 * count(queue_pending) / NULLIF(count(*), 0), 2) AS pct_non_null
    FROM task_events
    WHERE run_id IS NOT NULL AND reason_resolved = 'completed'
  `);
  const cv = completedNullRes.rows[0];
  console.log(`  Completed runs: ${cv.total}`);
  console.log(`  queue_pending NOT NULL: ${cv.non_null} (${cv.pct_non_null}%)`);
  console.log(`  queue_pending IS NULL: ${cv.total - cv.non_null}`);

  // 10. NULL by temporal bucket (daily)
  console.log('\n--- NULL Rate by Day ---');
  const dailyRes = await pool.query(`
    SELECT
      date_trunc('day', scheduled)::date AS day,
      count(*) AS total,
      count(*) - count(queue_pending) AS null_count,
      round(100.0 * count(queue_pending) / NULLIF(count(*), 0), 2) AS pct_non_null
    FROM task_events
    WHERE scheduled IS NOT NULL
    GROUP BY 1
    ORDER BY 1
  `);
  for (const row of dailyRes.rows) {
    console.log(`  ${row.day}: ${row.null_count}/${row.total} NULL (${row.pct_non_null}% non-null)`);
  }

  // 11. NULL by task_queue_id (top 20 queues)
  console.log('\n--- NULL Rate by Task Queue (top 20) ---');
  const queueRes = await pool.query(`
    SELECT
      task_queue_id,
      count(*) AS total,
      count(*) - count(queue_pending) AS null_count,
      round(100.0 * count(queue_pending) / NULLIF(count(*), 0), 2) AS pct_non_null
    FROM task_events
    WHERE task_queue_id IS NOT NULL
    GROUP BY task_queue_id
    ORDER BY total DESC
    LIMIT 20
  `);
  for (const row of queueRes.rows) {
    console.log(`  ${row.task_queue_id}: ${row.null_count}/${row.total} NULL (${row.pct_non_null}% non-null)`);
  }

  // 12. NULL vs event flow — missed pending events (tasks that actually ran but lack queue_pending)
  console.log('\n--- Missed Pending Events (NULL queue_pending on tasks that ran) ---');
  const missedRes = await pool.query(`
    SELECT
      count(*) AS total,
      count(*) FILTER (WHERE started IS NOT NULL) AS started_no_pending,
      count(*) FILTER (WHERE reason_resolved = 'completed') AS completed_no_pending,
      count(*) FILTER (WHERE started IS NULL AND resolved IS NOT NULL) AS resolved_never_started
    FROM task_events
    WHERE queue_pending IS NULL AND scheduled IS NOT NULL
  `);
  const mv = missedRes.rows[0];
  console.log(`  Scheduled but NULL queue_pending: ${mv.total}`);
  console.log(`    actually started (worker picked up): ${mv.started_no_pending}`);
  console.log(`    completed successfully: ${mv.completed_no_pending}`);
  console.log(`    resolved without starting (canceled/deadline): ${mv.resolved_never_started}`);

  // 13. queue_pending value distribution (histogram using pendingBucket boundaries)
  console.log('\n--- queue_pending Value Distribution ---');
  const distRes = await pool.query(`
    SELECT
      CASE
        WHEN queue_pending = 0 THEN 'empty (0)'
        WHEN queue_pending BETWEEN 1 AND 5 THEN 'low (1-5)'
        WHEN queue_pending BETWEEN 6 AND 20 THEN 'moderate (6-20)'
        WHEN queue_pending BETWEEN 21 AND 50 THEN 'busy (21-50)'
        WHEN queue_pending BETWEEN 51 AND 200 THEN 'heavy (51-200)'
        WHEN queue_pending BETWEEN 201 AND 500 THEN 'very-heavy (201-500)'
        WHEN queue_pending BETWEEN 501 AND 1500 THEN 'overloaded (501-1500)'
        WHEN queue_pending > 1500 THEN 'saturated (>1500)'
      END AS bucket,
      count(*) AS n,
      min(queue_pending) AS min_val,
      max(queue_pending) AS max_val,
      round(avg(queue_pending), 1) AS avg_val
    FROM task_events
    WHERE queue_pending IS NOT NULL
    GROUP BY 1
    ORDER BY min_val
  `);
  for (const row of distRes.rows) {
    console.log(`  ${row.bucket}: ${row.n} rows (min=${row.min_val}, max=${row.max_val}, avg=${row.avg_val})`);
  }
}

try {
  await run();
} catch (err) {
  console.error('Diagnostic failed:', err);
} finally {
  await pool.end();
}
