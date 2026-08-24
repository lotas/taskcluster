import { createPool, upsertTask, upsertTaskRun, enrichTask, getUnenrichedTaskIds } from '../src/db.js';
import { normalizeMetadataName, extractImageName } from '../src/utils.js';
import { assertDisposableDatabaseUrl } from './smoke-guard.js';

// No default. See smoke-guard.js — this file issues unqualified DELETEs.
// It reads SMOKE_DATABASE_URL, never DATABASE_URL, so a shell that already has
// DATABASE_URL exported for operational work cannot silently arm it.
const DATABASE_URL = assertDisposableDatabaseUrl(process.env.SMOKE_DATABASE_URL);
const pool = createPool(DATABASE_URL);

let passed = 0;
let failed = 0;

function assert(condition, message) {
  if (!condition) {
    console.error(`  FAIL: ${message}`);
    failed++;
  } else {
    console.log(`  PASS: ${message}`);
    passed++;
  }
}

async function resetTables() {
  await pool.query('DELETE FROM queue_forecast_task_runs');
  await pool.query('DELETE FROM queue_forecast_tasks');
}

async function getTaskRow(taskId) {
  const res = await pool.query(
    'SELECT * FROM queue_forecast_tasks WHERE task_id = $1',
    [taskId],
  );
  return res.rows[0] || null;
}

async function getRunRows(taskId) {
  const res = await pool.query(
    'SELECT * FROM queue_forecast_task_runs WHERE task_id = $1 ORDER BY run_id',
    [taskId],
  );
  return res.rows;
}

// Test 1: Task upsert idempotency
async function testTaskUpsertIdempotency() {
  console.log('\nTest 1: Task upsert idempotency');
  await resetTables();

  await upsertTask(pool, {
    task_id: 'task-1',
    task_queue_id: 'gecko-t/linux',
  });
  await upsertTask(pool, {
    task_id: 'task-1',
    task_queue_id: 'gecko-t/linux',
  });

  const task = await getTaskRow('task-1');
  assert(task !== null, 'Task row exists');
  assert(task.task_queue_id === 'gecko-t/linux', 'task_queue_id preserved');
}

// Test 2: Run upsert idempotency
async function testRunUpsertIdempotency() {
  console.log('\nTest 2: Run upsert idempotency');
  await resetTables();

  await upsertTask(pool, { task_id: 'task-2', task_queue_id: 'gecko-t/linux' });
  await upsertTaskRun(pool, {
    task_id: 'task-2', run_id: 0,
    pending_at: '2026-03-18T10:00:00Z',
  });
  await upsertTaskRun(pool, {
    task_id: 'task-2', run_id: 0,
    pending_at: '2026-03-18T10:00:00Z',
  });

  const runs = await getRunRows('task-2');
  assert(runs.length === 1, 'Single run row after duplicate upsert');
}

// Test 3: Out-of-order events (COALESCE merge)
async function testOutOfOrderEvents() {
  console.log('\nTest 3: Out-of-order events');
  await resetTables();

  await upsertTask(pool, { task_id: 'task-3' });
  // task-running arrives first
  await upsertTaskRun(pool, {
    task_id: 'task-3', run_id: 0,
    started_at: '2026-03-18T10:05:00Z',
  });
  // task-pending arrives second
  await upsertTaskRun(pool, {
    task_id: 'task-3', run_id: 0,
    pending_at: '2026-03-18T10:00:00Z',
    reason_created: 'scheduled',
  });

  const runs = await getRunRows('task-3');
  assert(runs.length === 1, 'Single run row after out-of-order events');
  assert(runs[0].started_at !== null, 'started_at preserved from first event');
  assert(runs[0].pending_at !== null, 'pending_at merged from second event');
}

// Test 4: original_priority immutability (first-write-wins)
async function testOriginalPriorityImmutability() {
  console.log('\nTest 4: original_priority immutability');
  await resetTables();

  await upsertTask(pool, {
    task_id: 'task-4',
    original_priority: 'medium',
  });
  await upsertTask(pool, {
    task_id: 'task-4',
    original_priority: 'high',
  });

  const task = await getTaskRow('task-4');
  assert(task.original_priority === 'medium', 'original_priority stays medium (first write wins)');
}

// Test 5: priority_at_pending immutability (first-write-wins)
async function testPriorityAtPendingImmutability() {
  console.log('\nTest 5: priority_at_pending immutability');
  await resetTables();

  await upsertTask(pool, { task_id: 'task-5' });
  await upsertTaskRun(pool, {
    task_id: 'task-5', run_id: 0,
    priority_at_pending: 'medium',
    pending_at: '2026-03-18T10:00:00Z',
  });
  // Later event tries to overwrite priority
  await upsertTaskRun(pool, {
    task_id: 'task-5', run_id: 0,
    priority_at_pending: 'high',
    started_at: '2026-03-18T10:05:00Z',
  });

  const runs = await getRunRows('task-5');
  assert(runs[0].priority_at_pending === 'medium', 'priority_at_pending stays medium (first write wins)');
  assert(runs[0].started_at !== null, 'started_at still merged from second event');
}

// Test 6: Task + run separation
async function testTaskRunSeparation() {
  console.log('\nTest 6: Task + run separation');
  await resetTables();

  await upsertTask(pool, {
    task_id: 'task-6',
    task_queue_id: 'gecko-t/linux',
    scheduler_id: 'gecko-level-3',
  });
  await upsertTaskRun(pool, {
    task_id: 'task-6', run_id: 0,
    pending_at: '2026-03-18T10:00:00Z',
  });
  await upsertTaskRun(pool, {
    task_id: 'task-6', run_id: 1,
    pending_at: '2026-03-18T10:10:00Z',
  });

  const task = await getTaskRow('task-6');
  const runs = await getRunRows('task-6');
  assert(task !== null, 'Task row exists');
  assert(runs.length === 2, 'Two run rows exist');
  assert(task.task_queue_id === 'gecko-t/linux', 'Task-level field on task row');
  assert(runs[0].pending_at !== null, 'Run-level field on run row');
}

// Test 7: Wait/run duration calculation
async function testDurationCalculation() {
  console.log('\nTest 7: Wait/run duration calculation');
  await resetTables();

  await upsertTask(pool, { task_id: 'task-7' });
  await upsertTaskRun(pool, {
    task_id: 'task-7', run_id: 0,
    pending_at: '2026-03-18T10:00:00Z',
    started_at: '2026-03-18T10:02:00Z',
    resolved_at: '2026-03-18T10:07:00Z',
    wait_duration_s: 120,
    run_duration_s: 300,
  });

  const runs = await getRunRows('task-7');
  assert(parseFloat(runs[0].wait_duration_s) === 120, 'wait_duration_s = 120s');
  assert(parseFloat(runs[0].run_duration_s) === 300, 'run_duration_s = 300s');
}

// Test 8: Enrichment update
async function testEnrichmentUpdate() {
  console.log('\nTest 8: Enrichment update');
  await resetTables();

  await upsertTask(pool, {
    task_id: 'task-8',
    task_queue_id: 'gecko-t/linux',
  });
  await upsertTaskRun(pool, {
    task_id: 'task-8', run_id: 0,
    pending_at: '2026-03-18T10:00:00Z',
  });

  await enrichTask(pool, 'task-8', {
    metadata_name: 'test-linux/debug-mochitest-1',
    normalized_name: 'test-linux/debug-mochitest-1',
    tags: { kind: 'test', 'test-type': 'mochitest' },
    task_created: '2026-03-18T09:55:00Z',
    original_priority: 'medium',
    task_queue_id: 'gecko-t/linux',
    task_group_id: 'group-8',
    scheduler_id: 'gecko-level-3',
    project_id: 'mozilla-central',
    max_run_time_s: 3600,
    repo_family: 'central',
    repo_family_source: 'route',
    repo_family_evidence: 'index.gecko.v2.mozilla-central',
    repo_family_derivation_version: 1,
  });

  const task = await getTaskRow('task-8');
  assert(task.metadata_name === 'test-linux/debug-mochitest-1', 'metadata_name enriched');
  assert(task.normalized_name === 'test-linux/debug-mochitest-1', 'normalized_name enriched');
  assert(task.scheduler_id === 'gecko-level-3', 'scheduler_id enriched');
  assert(task.project_id === 'mozilla-central', 'project_id enriched');
  assert(task.max_run_time_s === 3600, 'max_run_time_s enriched');
  assert(task.repo_family === 'central', 'repo_family enriched');
  assert(task.repo_family_source === 'route', 'repo_family_source enriched');
  assert(task.repo_family_evidence === 'index.gecko.v2.mozilla-central', 'repo_family_evidence enriched');
  assert(task.repo_family_derivation_version === 1, 'repo_family_derivation_version enriched');
  assert(task.enriched_at !== null, 'enriched_at set');
}

// Test 9: enriched_at only set once
async function testEnrichedAtOnce() {
  console.log('\nTest 9: enriched_at only set once');
  await resetTables();

  await upsertTask(pool, { task_id: 'task-9' });

  const before = await getTaskRow('task-9');
  assert(before.enriched_at === null, 'enriched_at is NULL before enrichment');

  await enrichTask(pool, 'task-9', { metadata_name: 'test-1' });
  const after1 = await getTaskRow('task-9');
  const firstEnrichedAt = after1.enriched_at;
  assert(firstEnrichedAt !== null, 'enriched_at set after first enrichment');

  // Small delay to ensure timestamps would differ
  await new Promise(r => setTimeout(r, 10));
  await enrichTask(pool, 'task-9', { metadata_name: 'test-2' });
  const after2 = await getTaskRow('task-9');
  assert(
    after2.enriched_at.getTime() === firstEnrichedAt.getTime(),
    'enriched_at unchanged after second enrichment (COALESCE preserves first)',
  );
}

// Test 10: FK constraint — run requires task
async function testFkConstraint() {
  console.log('\nTest 10: FK constraint — run requires task');
  await resetTables();

  let fkViolation = false;
  try {
    await upsertTaskRun(pool, {
      task_id: 'nonexistent-task', run_id: 0,
      pending_at: '2026-03-18T10:00:00Z',
    });
  } catch (err) {
    if (err.code === '23503') fkViolation = true; // foreign_key_violation
  }
  assert(fkViolation, 'FK violation when inserting run for nonexistent task');
}

// Test 11: Backfill query returns unenriched tasks
async function testBackfillQuery() {
  console.log('\nTest 11: Backfill query returns unenriched tasks');
  await resetTables();

  await upsertTask(pool, {
    task_id: 'task-11',
    task_queue_id: 'gecko-t/linux',
  });

  const taskIds = await getUnenrichedTaskIds(pool);
  assert(taskIds.includes('task-11'), 'Unenriched task included in backfill query');

  await enrichTask(pool, 'task-11', { metadata_name: 'test-enriched' });
  const taskIds2 = await getUnenrichedTaskIds(pool);
  assert(!taskIds2.includes('task-11'), 'Enriched task excluded from backfill query');
}

// Test 12: queue_pending capture and COALESCE preservation
async function testQueuePendingCapture() {
  console.log('\nTest 12: queue_pending capture');
  await resetTables();

  await upsertTask(pool, { task_id: 'task-qp' });
  await upsertTaskRun(pool, {
    task_id: 'task-qp', run_id: 0,
    queue_pending: 42,
    pending_at: '2026-03-20T10:00:00Z',
  });

  const runs = await getRunRows('task-qp');
  assert(runs[0].queue_pending === 42, 'queue_pending persisted');

  // Second upsert without queue_pending should not overwrite
  await upsertTaskRun(pool, {
    task_id: 'task-qp', run_id: 0,
    started_at: '2026-03-20T10:01:00Z',
  });

  const runs2 = await getRunRows('task-qp');
  assert(runs2[0].queue_pending === 42, 'queue_pending preserved by COALESCE');
}

// Test 13: COALESCE merge on task fields
async function testTaskCoalesceMerge() {
  console.log('\nTest 13: COALESCE merge on task fields');
  await resetTables();

  await upsertTask(pool, {
    task_id: 'task-13',
    task_queue_id: 'gecko-t/linux',
  });
  await upsertTask(pool, {
    task_id: 'task-13',
    scheduler_id: 'gecko-level-3',
  });

  const task = await getTaskRow('task-13');
  assert(task.task_queue_id === 'gecko-t/linux', 'task_queue_id preserved from first upsert');
  assert(task.scheduler_id === 'gecko-level-3', 'scheduler_id merged from second upsert');
}

// Test 14: CASCADE delete
async function testCascadeDelete() {
  console.log('\nTest 14: CASCADE delete');
  await resetTables();

  await upsertTask(pool, { task_id: 'task-14' });
  await upsertTaskRun(pool, { task_id: 'task-14', run_id: 0, pending_at: '2026-03-18T10:00:00Z' });
  await upsertTaskRun(pool, { task_id: 'task-14', run_id: 1, pending_at: '2026-03-18T10:10:00Z' });

  await pool.query('DELETE FROM queue_forecast_tasks WHERE task_id = $1', ['task-14']);
  const runs = await getRunRows('task-14');
  assert(runs.length === 0, 'Run rows deleted by CASCADE when task deleted');
}

// Test 15: normalizeMetadataName
async function testNormalizeMetadataName() {
  console.log('\nTest 15: normalizeMetadataName');

  assert(
    normalizeMetadataName('mozci classify autoland@193d2dd1a0b3') === 'mozci classify autoland',
    'Strips @<hex-hash> suffix',
  );
  assert(
    normalizeMetadataName('wpt-chrome-canary-testharness-7') === 'wpt-chrome-canary-testharness-7',
    'Keeps shard number unchanged',
  );
  assert(
    normalizeMetadataName('Fuzzing task linux-pool10 - 1/4') === 'Fuzzing task linux-pool10 - 1/4',
    'Keeps fraction unchanged',
  );
  assert(
    normalizeMetadataName(null) === null,
    'Returns null for null input',
  );
  assert(
    normalizeMetadataName('build@abcdef1234567890abcdef') === 'build',
    'Strips long hex hash',
  );
  assert(
    normalizeMetadataName('task-name@short') === 'task-name@short',
    'Keeps @suffix if not a hex hash >=12 chars',
  );
}

// Test 16: extractImageName (utility still works, even though column dropped)
async function testExtractImageName() {
  console.log('\nTest 16: extractImageName');

  assert(
    extractImageName({ payload: { image: 'ubuntu:22.04' } }) === 'ubuntu:22.04',
    'Returns string image directly',
  );
  assert(
    extractImageName({ payload: { image: { namespace: 'docker-image.v2.gecko-t.linux' } } }) === 'docker-image.v2.gecko-t.linux',
    'Returns namespace from indexed-image object',
  );
  assert(
    extractImageName({ payload: {} }) === null,
    'Returns null when image missing',
  );
  assert(
    extractImageName({}) === null,
    'Returns null when payload has no image',
  );
  assert(
    extractImageName(null) === null,
    'Returns null for null taskDef',
  );
}

// --- Run all tests ---

try {
  await testTaskUpsertIdempotency();
  await testRunUpsertIdempotency();
  await testOutOfOrderEvents();
  await testOriginalPriorityImmutability();
  await testPriorityAtPendingImmutability();
  await testTaskRunSeparation();
  await testDurationCalculation();
  await testEnrichmentUpdate();
  await testEnrichedAtOnce();
  await testFkConstraint();
  await testBackfillQuery();
  await testQueuePendingCapture();
  await testTaskCoalesceMerge();
  await testCascadeDelete();
  await testNormalizeMetadataName();
  await testExtractImageName();

  console.log(`\n=== Results: ${passed} passed, ${failed} failed ===`);
  if (failed > 0) process.exit(1);
} finally {
  await pool.end();
}
