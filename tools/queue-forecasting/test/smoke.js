import { createPool, upsertTaskEvent, enrichTaskRows, updatePriorityByTask, updatePriorityByGroup, getUnenrichedTaskIds } from '../src/db.js';
import { normalizeMetadataName, extractImageName } from '../src/utils.js';

const DATABASE_URL = process.env.DATABASE_URL || 'postgresql://postgres@localhost:5433/forecasting';
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

async function resetTable() {
  await pool.query('DELETE FROM task_events');
}

async function getRows(taskId) {
  const res = await pool.query(
    'SELECT * FROM task_events WHERE task_id = $1 ORDER BY run_id NULLS FIRST',
    [taskId],
  );
  return res.rows;
}

// Test 1: Upsert idempotency
async function testUpsertIdempotency() {
  console.log('\nTest 1: Upsert idempotency');
  await resetTable();

  await upsertTaskEvent(pool, {
    task_id: 'task-1', run_id: 0,
    task_queue_id: 'gecko-t/linux', priority: 'medium',
    scheduled: '2026-03-18T10:00:00Z',
  });
  await upsertTaskEvent(pool, {
    task_id: 'task-1', run_id: 0,
    task_queue_id: 'gecko-t/linux', priority: 'medium',
    scheduled: '2026-03-18T10:00:00Z',
  });

  const rows = await getRows('task-1');
  assert(rows.length === 1, 'Single row after duplicate upsert');
  assert(rows[0].task_queue_id === 'gecko-t/linux', 'task_queue_id preserved');
}

// Test 2: Out-of-order events (COALESCE merge)
async function testOutOfOrderEvents() {
  console.log('\nTest 2: Out-of-order events');
  await resetTable();

  // task-running arrives first
  await upsertTaskEvent(pool, {
    task_id: 'task-2', run_id: 0,
    started: '2026-03-18T10:05:00Z',
    worker_group: 'us-east-1',
  });
  // task-pending arrives second
  await upsertTaskEvent(pool, {
    task_id: 'task-2', run_id: 0,
    scheduled: '2026-03-18T10:00:00Z',
    reason_created: 'scheduled',
    task_queue_id: 'gecko-t/linux',
  });

  const rows = await getRows('task-2');
  assert(rows.length === 1, 'Single row after out-of-order events');
  assert(rows[0].started !== null, 'started preserved from first event');
  assert(rows[0].scheduled !== null, 'scheduled merged from second event');
  assert(rows[0].worker_group === 'us-east-1', 'worker_group preserved');
  assert(rows[0].task_queue_id === 'gecko-t/linux', 'task_queue_id merged');
}

// Test 3: original_priority immutability
async function testOriginalPriorityImmutability() {
  console.log('\nTest 3: original_priority immutability');
  await resetTable();

  await upsertTaskEvent(pool, {
    task_id: 'task-3', run_id: 0,
    original_priority: 'medium',
  });
  await upsertTaskEvent(pool, {
    task_id: 'task-3', run_id: 0,
    original_priority: 'high',
  });

  const rows = await getRows('task-3');
  assert(rows[0].original_priority === 'medium', 'original_priority stays medium (first write wins)');
}

// Test 4: NULL run_id uniqueness (UNIQUE NULLS NOT DISTINCT)
async function testNullRunIdUniqueness() {
  console.log('\nTest 4: NULL run_id uniqueness');
  await resetTable();

  await upsertTaskEvent(pool, {
    task_id: 'task-4', run_id: null,
    task_queue_id: 'gecko-t/linux',
  });
  await upsertTaskEvent(pool, {
    task_id: 'task-4', run_id: null,
    priority: 'high',
  });

  const rows = await getRows('task-4');
  assert(rows.length === 1, 'Single row for (task_id, NULL) — NULLS NOT DISTINCT works');
  assert(rows[0].task_queue_id === 'gecko-t/linux', 'task_queue_id preserved from first upsert');
  assert(rows[0].priority === 'high', 'priority merged from second upsert');
}

// Test 5: Priority update scoping (only unresolved rows)
async function testPriorityUpdateScoping() {
  console.log('\nTest 5: Priority update scoping');
  await resetTable();

  // Unresolved placeholder row
  await upsertTaskEvent(pool, {
    task_id: 'task-5', run_id: null,
    priority: 'low',
  });
  // Unresolved run row
  await upsertTaskEvent(pool, {
    task_id: 'task-5', run_id: 0,
    priority: 'low',
    scheduled: '2026-03-18T10:00:00Z',
  });
  // Resolved run row
  await upsertTaskEvent(pool, {
    task_id: 'task-5', run_id: 1,
    priority: 'low',
    resolved: '2026-03-18T10:10:00Z',
  });

  await updatePriorityByTask(pool, 'task-5', 'high');

  const rows = await getRows('task-5');
  const placeholder = rows.find(r => r.run_id === null);
  const run0 = rows.find(r => r.run_id === 0);
  const run1 = rows.find(r => r.run_id === 1);

  assert(placeholder.priority === 'high', 'Placeholder updated');
  assert(run0.priority === 'high', 'Unresolved run row updated');
  assert(run1.priority === 'high', 'Resolved run row also updated (priority is task-level)');
}

// Test 6: Group priority update scoping
async function testGroupPriorityUpdateScoping() {
  console.log('\nTest 6: Group priority update scoping');
  await resetTable();

  await upsertTaskEvent(pool, {
    task_id: 'task-6a', run_id: 0,
    task_group_id: 'group-6', priority: 'low',
  });
  await upsertTaskEvent(pool, {
    task_id: 'task-6b', run_id: 0,
    task_group_id: 'group-6', priority: 'low',
    resolved: '2026-03-18T10:10:00Z',
  });

  await updatePriorityByGroup(pool, 'group-6', 'high');

  const rowsA = await getRows('task-6a');
  const rowsB = await getRows('task-6b');

  assert(rowsA[0].priority === 'high', 'Unresolved group member updated');
  assert(rowsB[0].priority === 'high', 'Resolved group member also updated (priority is task-level)');
}

// Test 7: Wait/run duration calculation
async function testDurationCalculation() {
  console.log('\nTest 7: Wait/run duration calculation');
  await resetTable();

  await upsertTaskEvent(pool, {
    task_id: 'task-7', run_id: 0,
    scheduled: '2026-03-18T10:00:00Z',
    started: '2026-03-18T10:02:00Z',
    resolved: '2026-03-18T10:07:00Z',
    wait_duration_s: 120,
    run_duration_s: 300,
  });

  const rows = await getRows('task-7');
  assert(parseFloat(rows[0].wait_duration_s) === 120, 'wait_duration_s = 120s');
  assert(parseFloat(rows[0].run_duration_s) === 300, 'run_duration_s = 300s');
}

// Test 8: Enrichment update
async function testEnrichmentUpdate() {
  console.log('\nTest 8: Enrichment update');
  await resetTable();

  // Placeholder row
  await upsertTaskEvent(pool, {
    task_id: 'task-8', run_id: null,
    task_queue_id: 'gecko-t/linux',
  });
  // Run row
  await upsertTaskEvent(pool, {
    task_id: 'task-8', run_id: 0,
    scheduled: '2026-03-18T10:00:00Z',
  });

  await enrichTaskRows(pool, 'task-8', {
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
    image_name: 'ubuntu:22.04',
  });

  const rows = await getRows('task-8');
  assert(rows.length === 2, 'Both placeholder and run rows exist');
  for (const row of rows) {
    assert(row.metadata_name === 'test-linux/debug-mochitest-1', `metadata_name enriched (run_id=${row.run_id})`);
    assert(row.normalized_name === 'test-linux/debug-mochitest-1', `normalized_name enriched (run_id=${row.run_id})`);
    assert(row.scheduler_id === 'gecko-level-3', `scheduler_id enriched (run_id=${row.run_id})`);
    assert(row.project_id === 'mozilla-central', `project_id enriched (run_id=${row.run_id})`);
    assert(row.max_run_time_s === 3600, `max_run_time_s enriched (run_id=${row.run_id})`);
    assert(row.image_name === 'ubuntu:22.04', `image_name enriched (run_id=${row.run_id})`);
  }
}

// Test 9: Placeholder vs run row distinction
async function testPlaceholderVsRunRow() {
  console.log('\nTest 9: Placeholder vs run row distinction');
  await resetTable();

  await upsertTaskEvent(pool, {
    task_id: 'task-9', run_id: null,
    task_queue_id: 'gecko-t/linux',
  });
  await upsertTaskEvent(pool, {
    task_id: 'task-9', run_id: 0,
    task_queue_id: 'gecko-t/linux',
    scheduled: '2026-03-18T10:00:00Z',
  });

  const allRows = await getRows('task-9');
  assert(allRows.length === 2, 'Both placeholder and run row exist');

  const runOnlyRes = await pool.query(
    'SELECT * FROM task_events WHERE task_id = $1 AND run_id IS NOT NULL',
    ['task-9'],
  );
  assert(runOnlyRes.rows.length === 1, 'Predictor query (run_id IS NOT NULL) excludes placeholder');
  assert(runOnlyRes.rows[0].run_id === 0, 'Only run row returned');
}

// Test 10: Priority updates via upsert (new value wins)
async function testPriorityUpsertNewValueWins() {
  console.log('\nTest 10: Priority updates via upsert (new value wins)');
  await resetTable();

  await upsertTaskEvent(pool, {
    task_id: 'task-10', run_id: 0,
    priority: 'medium',
    scheduled: '2026-03-18T10:00:00Z',
  });
  // A later lifecycle event carries an updated priority
  await upsertTaskEvent(pool, {
    task_id: 'task-10', run_id: 0,
    priority: 'high',
    started: '2026-03-18T10:05:00Z',
  });

  const rows = await getRows('task-10');
  assert(rows.length === 1, 'Single row after upsert');
  assert(rows[0].priority === 'high', 'priority updated to high (new value wins)');
  assert(rows[0].scheduled !== null, 'scheduled preserved from first event');
  assert(rows[0].started !== null, 'started merged from second event');
}

// Test 11: Backfill query includes placeholder rows (run_id IS NULL)
async function testBackfillIncludesPlaceholders() {
  console.log('\nTest 11: Backfill query includes placeholder rows');
  await resetTable();

  // Placeholder row without metadata
  await upsertTaskEvent(pool, {
    task_id: 'task-11', run_id: null,
    task_queue_id: 'gecko-t/linux',
  });

  const taskIds = await getUnenrichedTaskIds(pool);
  assert(taskIds.includes('task-11'), 'Placeholder row (run_id IS NULL) included in backfill query');
}

// Test 12: normalizeMetadataName
async function testNormalizeMetadataName() {
  console.log('\nTest 12: normalizeMetadataName');

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

// Test 13: extractImageName
async function testExtractImageName() {
  console.log('\nTest 13: extractImageName');

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

// Test 14: queue_pending capture and COALESCE preservation
async function testQueuePendingCapture() {
  console.log('\nTest 14: queue_pending capture');
  await resetTable();

  await upsertTaskEvent(pool, {
    task_id: 'task-qp', run_id: 0,
    task_queue_id: 'gecko-t/linux',
    queue_pending: 42,
    scheduled: '2026-03-20T10:00:00Z',
  });

  const rows = await getRows('task-qp');
  assert(rows[0].queue_pending === 42, 'queue_pending persisted');

  // Second upsert without queue_pending should not overwrite
  await upsertTaskEvent(pool, {
    task_id: 'task-qp', run_id: 0,
    started: '2026-03-20T10:01:00Z',
  });

  const rows2 = await getRows('task-qp');
  assert(rows2[0].queue_pending === 42, 'queue_pending preserved by COALESCE');
}

// --- Run all tests ---

try {
  await testUpsertIdempotency();
  await testOutOfOrderEvents();
  await testOriginalPriorityImmutability();
  await testNullRunIdUniqueness();
  await testPriorityUpdateScoping();
  await testGroupPriorityUpdateScoping();
  await testDurationCalculation();
  await testEnrichmentUpdate();
  await testPlaceholderVsRunRow();
  await testPriorityUpsertNewValueWins();
  await testBackfillIncludesPlaceholders();
  await testNormalizeMetadataName();
  await testExtractImageName();
  await testQueuePendingCapture();

  console.log(`\n=== Results: ${passed} passed, ${failed} failed ===`);
  if (failed > 0) process.exit(1);
} finally {
  await pool.end();
}
