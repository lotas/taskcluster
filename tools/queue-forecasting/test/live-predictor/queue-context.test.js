import { test } from 'node:test';
import assert from 'node:assert';
import { getQueueContext, QUEUE_CONTEXT_FEATURE_VERSION } from '../../src/live-predictor/queue-context.js';

function fakePool(byNeedle) {
  return {
    async query(sql) {
      for (const [n, rows] of Object.entries(byNeedle)) {
        if (sql.includes(n)) return { rows };
      }
      return { rows: [] };
    },
  };
}

// Must EXACTLY equal trainer/src/queue_context.py FEATURE_COLUMNS (21 keys).
const EXPECTED_KEYS = [
  'pending_higher_priority_same_queue',
  'pending_same_priority_same_queue',
  'pending_lower_priority_same_queue',
  'oldest_higher_or_equal_pending_age_same_queue',
  'arrivals_15m_same_queue',
  'arrivals_60m_same_queue',
  'arrivals_higher_or_equal_15m_same_queue',
  'arrivals_higher_or_equal_60m_same_queue',
  'starts_higher_or_equal_15m_same_queue',
  'pending_total_per_capacity',
  'pending_higher_or_equal_per_capacity',
  'running_per_capacity',
  'running_workers',
  'existing_capacity',
  'claimed_tasks',
  'capacity_sample_age_s',
  'capacity_null_reason',
  'backlog_coverage_ratio',
  'pending_try_higher_or_equal_same_queue',
  'pending_autoland_higher_or_equal_same_queue',
  'pending_release_beta_higher_or_equal_same_queue',
];

test('version is a number', () => {
  assert.equal(typeof QUEUE_CONTEXT_FEATURE_VERSION, 'number');
});

test('returns exactly the 21 feature keys (parity with Python FEATURE_COLUMNS)', async () => {
  const pool = fakePool({});
  const f = await getQueueContext(pool, {
    task_queue_id: 'q/a',
    pending_at: new Date(),
    priority_at_pending: 'low',
    queue_pending: 5,
    repo_family: 'try',
  });
  assert.deepEqual(Object.keys(f).sort(), [...EXPECTED_KEYS].sort());
});

test('maps backlog + capacity rows; per-capacity includes target', async () => {
  const pool = fakePool({
    queue_context_backlog: [{
      pending_higher_priority_same_queue: 3,
      pending_same_priority_same_queue: 2,
      pending_lower_priority_same_queue: 5,
      oldest_higher_or_equal_pending_age_same_queue: 600,
      pending_higher_or_equal_excl_target: 5,
      pending_total_incl_target: 11,
      arrivals_15m_same_queue: 4,
      arrivals_60m_same_queue: 9,
      arrivals_higher_or_equal_15m_same_queue: 1,
      arrivals_higher_or_equal_60m_same_queue: 2,
      starts_higher_or_equal_15m_same_queue: 1,
      pending_try_higher_or_equal_same_queue: 1,
      pending_autoland_higher_or_equal_same_queue: 2,
      pending_release_beta_higher_or_equal_same_queue: 0,
    }],
    queue_context_capacity: [{
      running_workers: 8,
      existing_capacity: 10,
      claimed_tasks: 7,
      capacity_sample_age_s: 42,
    }],
  });
  const f = await getQueueContext(pool, {
    task_queue_id: 'q/a',
    pending_at: new Date('2026-06-01T00:10:00Z'),
    priority_at_pending: 'low',
    queue_pending: 12,
    repo_family: 'try',
  });
  assert.equal(f.pending_higher_priority_same_queue, 3);
  assert.equal(f.running_per_capacity, 0.8);
  assert.equal(f.pending_total_per_capacity, 12 / 10);
  assert.equal(f.pending_higher_or_equal_per_capacity, (5 + 1) / 10); // includes target
  assert.equal(f.backlog_coverage_ratio, 11 / 12);
  assert.equal(f.capacity_null_reason, 'ok');
  assert.equal(f.arrivals_higher_or_equal_15m_same_queue, 1);
  assert.equal(f.starts_higher_or_equal_15m_same_queue, 1);
});

test('static-pool null capacity -> per_capacity NaN + reason', async () => {
  const pool = fakePool({
    queue_context_backlog: [{ pending_higher_or_equal_excl_target: 1, pending_total_incl_target: 3 }],
    queue_context_capacity: [{ running_workers: null, existing_capacity: null, claimed_tasks: 4, capacity_sample_age_s: 30 }],
  });
  const f = await getQueueContext(pool, {
    task_queue_id: 'q/a',
    pending_at: new Date(),
    priority_at_pending: 'low',
    queue_pending: 3,
    repo_family: 'try',
  });
  assert.ok(Number.isNaN(f.pending_total_per_capacity));
  assert.equal(f.capacity_null_reason, 'static_pool_null');
});

test('zero capacity -> per_capacity NaN + reason', async () => {
  const pool = fakePool({
    queue_context_backlog: [{ pending_higher_or_equal_excl_target: 1, pending_total_incl_target: 3 }],
    queue_context_capacity: [{ running_workers: 0, existing_capacity: 0, claimed_tasks: 0, capacity_sample_age_s: 30 }],
  });
  const f = await getQueueContext(pool, {
    task_queue_id: 'q/a',
    pending_at: new Date(),
    priority_at_pending: 'low',
    queue_pending: 3,
    repo_family: 'try',
  });
  assert.ok(Number.isNaN(f.pending_total_per_capacity));
  assert.ok(Number.isNaN(f.pending_higher_or_equal_per_capacity));
  assert.ok(Number.isNaN(f.running_per_capacity));
  assert.equal(f.capacity_null_reason, 'zero_capacity');
});

test('stale capacity sample (age > 900s) -> no_sample + per_capacity NaN', async () => {
  const pool = fakePool({
    queue_context_backlog: [{ pending_higher_or_equal_excl_target: 1, pending_total_incl_target: 3 }],
    queue_context_capacity: [{ running_workers: 8, existing_capacity: 10, claimed_tasks: 7, capacity_sample_age_s: 1800 }],
  });
  const f = await getQueueContext(pool, {
    task_queue_id: 'q/a',
    pending_at: new Date(),
    priority_at_pending: 'low',
    queue_pending: 12,
    repo_family: 'try',
  });
  assert.equal(f.capacity_null_reason, 'no_sample');
  assert.ok(Number.isNaN(f.running_workers));
  assert.ok(Number.isNaN(f.existing_capacity));
  assert.ok(Number.isNaN(f.claimed_tasks));
  assert.ok(Number.isNaN(f.capacity_sample_age_s));
  assert.ok(Number.isNaN(f.pending_total_per_capacity));
  assert.ok(Number.isNaN(f.pending_higher_or_equal_per_capacity));
  assert.ok(Number.isNaN(f.running_per_capacity));
});

test('no queue/pending -> all-NaN shape', async () => {
  const f = await getQueueContext(fakePool({}), { task_queue_id: null, pending_at: null });
  assert.ok(Number.isNaN(f.pending_higher_priority_same_queue));
  assert.equal(f.capacity_null_reason, 'no_sample');
  assert.deepEqual(Object.keys(f).sort(), [...EXPECTED_KEYS].sort());
});

test('coverage NaN when queue_pending <= 0', async () => {
  const pool = fakePool({
    queue_context_backlog: [{ pending_higher_or_equal_excl_target: 1, pending_total_incl_target: 3 }],
    queue_context_capacity: [{ running_workers: 1, existing_capacity: 5, claimed_tasks: 1, capacity_sample_age_s: 10 }],
  });
  const f = await getQueueContext(pool, {
    task_queue_id: 'q/a',
    pending_at: new Date(),
    priority_at_pending: 'low',
    queue_pending: 0,
    repo_family: 'try',
  });
  assert.ok(Number.isNaN(f.backlog_coverage_ratio));
  // pending_total_per_capacity is NaN when qp <= 0, but he-per-capacity still computed.
  assert.ok(Number.isNaN(f.pending_total_per_capacity));
  assert.equal(f.pending_higher_or_equal_per_capacity, (1 + 1) / 5);
});
