import { test } from 'node:test';
import assert from 'node:assert';
import { getThroughput } from '../../src/live-predictor/throughput.js';

const NOW = new Date('2026-05-15T12:00:00Z');
const QUEUE = 'proj-a/linux';
const TASK_ID = 'task-1';
const RUN_ID = 0;

function fakePool(rows) { return { query: async () => ({ rows }) }; }

test('queue with no history at all (has_history=false) → all NaN', async () => {
  const r = await getThroughput(fakePool([{
    has_history: false,
    queue_tasks_started_15m: '0', queue_tasks_completed_15m: '0',
    queue_avg_wait_15m: null, queue_avg_run_time_15m: null,
    queue_tasks_started_60m: '0', queue_tasks_completed_60m: '0',
    queue_avg_wait_60m: null, queue_avg_run_time_60m: null,
  }]), 'unknown-queue', NOW, TASK_ID, RUN_ID);
  for (const v of Object.values(r)) {
    assert.ok(Number.isNaN(v), `expected NaN, got ${v}`);
  }
});

test('queue with history but empty 15m window → counts=0, averages=NaN', async () => {
  const r = await getThroughput(fakePool([{
    has_history: true,
    queue_tasks_started_15m: '0',
    queue_tasks_completed_15m: '0',
    queue_avg_wait_15m: null,
    queue_avg_run_time_15m: null,
    queue_tasks_started_60m: '3',
    queue_tasks_completed_60m: '2',
    queue_avg_wait_60m: '120.5',
    queue_avg_run_time_60m: '500.0',
  }]), QUEUE, NOW, TASK_ID, RUN_ID);
  assert.equal(r.queue_tasks_started_15m, 0);
  assert.equal(r.queue_tasks_completed_15m, 0);
  assert.ok(Number.isNaN(r.queue_avg_wait_15m));
  assert.ok(Number.isNaN(r.queue_avg_run_time_15m));
  assert.equal(r.queue_tasks_started_60m, 3);
  assert.equal(r.queue_avg_wait_60m, 120.5);
});

test('no result row at all → all NaN', async () => {
  const r = await getThroughput(fakePool([]), QUEUE, NOW, TASK_ID, RUN_ID);
  for (const v of Object.values(r)) assert.ok(Number.isNaN(v));
});

test('no task_queue_id returns all NaN', async () => {
  const r = await getThroughput(fakePool([]), null, NOW, TASK_ID, RUN_ID);
  for (const v of Object.values(r)) {
    assert.ok(Number.isNaN(v));
  }
});

test('no pendingAt returns all NaN', async () => {
  const r = await getThroughput(fakePool([{ has_history: true }]), QUEUE, null, TASK_ID, RUN_ID);
  for (const v of Object.values(r)) {
    assert.ok(Number.isNaN(v));
  }
});

test('SQL uses pending_at and excludes current task_id/run_id', async () => {
  let capturedSql, capturedParams;
  const recordingPool = {
    async query(sql, params) {
      capturedSql = sql;
      capturedParams = params;
      return { rows: [] };
    },
  };
  const pendingAt = new Date('2026-05-15T10:00:00Z');
  await getThroughput(recordingPool, QUEUE, pendingAt, 'my-task', 7);
  assert.ok(capturedSql.includes('$2::timestamptz'), 'SQL should anchor on $2 (pendingAt)');
  assert.ok(!capturedSql.includes('now()'), 'SQL must not use now()');
  assert.ok(capturedSql.includes('r.task_id = $3'), 'SQL should exclude current task');
  assert.ok(capturedSql.includes('r.run_id = $4'), 'SQL should exclude current run');
  assert.equal(capturedParams[1], pendingAt);
  assert.equal(capturedParams[2], 'my-task');
  assert.equal(capturedParams[3], 7);
});
