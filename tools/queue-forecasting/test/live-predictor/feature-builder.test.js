import { test } from 'node:test';
import assert from 'node:assert';
import { buildFeatureVector } from '../../src/live-predictor/feature-builder.js';

const baseSchema = {
  feature_order: [
    'task_queue_id', 'priority_at_pending', 'tags.kind',
    'queue_pending', 'max_run_time_s',
    'hour_sin', 'hour_cos', 'day_sin', 'day_cos',
    'bl_wait_p50',
    'queue_tasks_started_15m', 'queue_tasks_completed_15m',
  ],
  categorical_features: ['task_queue_id', 'priority_at_pending', 'tags.kind'],
  numeric_features: [
    'queue_pending', 'max_run_time_s',
    'hour_sin', 'hour_cos', 'day_sin', 'day_cos',
    'bl_wait_p50',
    'queue_tasks_started_15m', 'queue_tasks_completed_15m',
  ],
  derived_features: { cyclical_time: { source: 'pending_at' } },
  cold_start_code: -1,
};

const categories = {
  task_queue_id: ['proj-a/linux', 'proj-b/win'],
  priority_at_pending: ['high', 'low'],
  'tags.kind': ['build', 'test'],
};

test('encodes known categoricals to their index codes', () => {
  const row = {
    task_id: 't1', run_id: 0,
    task_queue_id: 'proj-b/win',
    priority_at_pending: 'low',
    tags: { kind: 'test' },
    pending_at: new Date('2026-05-13T00:00:00Z'),  // Wed 00:00 UTC
    queue_pending: 10, max_run_time_s: 3600,
  };
  const liveFeatures = {
    bl_wait_p50: 60.0,
    queue_tasks_started_15m: 5, queue_tasks_completed_15m: 3,
  };
  const v = buildFeatureVector(row, liveFeatures, baseSchema, categories);
  // Indexes: task_queue_id=1 (proj-b/win), priority=1 (low), tags.kind=1 (test)
  assert.equal(v[0], 1.0);
  assert.equal(v[1], 1.0);
  assert.equal(v[2], 1.0);
  assert.equal(v[3], 10.0);    // queue_pending
  assert.equal(v[4], 3600.0);  // max_run_time_s
  // hour=0 → sin=0, cos=1 ; dow=Wed=2 → sin=sin(2*pi*2/7)≈0.9749, cos≈-0.2225
  assert.ok(Math.abs(v[5] - 0.0) < 1e-6);
  assert.ok(Math.abs(v[6] - 1.0) < 1e-6);
  assert.ok(Math.abs(v[7] - Math.sin(2 * Math.PI * 2 / 7)) < 1e-6);
  assert.ok(Math.abs(v[8] - Math.cos(2 * Math.PI * 2 / 7)) < 1e-6);
  assert.equal(v[9], 60.0);   // bl_wait_p50
  assert.equal(v[10], 5.0);   // started_15m
  assert.equal(v[11], 3.0);   // completed_15m
});

test('unseen categoricals encode to -1', () => {
  const row = {
    task_id: 't1', run_id: 0,
    task_queue_id: 'proj-c/unseen',
    priority_at_pending: 'medium',  // not in vocab
    tags: { kind: null },
    pending_at: new Date('2026-05-13T00:00:00Z'),
    queue_pending: 0, max_run_time_s: 0,
  };
  const liveFeatures = { bl_wait_p50: 0, queue_tasks_started_15m: 0, queue_tasks_completed_15m: 0 };
  const v = buildFeatureVector(row, liveFeatures, baseSchema, categories);
  assert.equal(v[0], -1.0);
  assert.equal(v[1], -1.0);
  assert.equal(v[2], -1.0);
});

test('NaN/null numerics stay NaN (LightGBM treats this as missing)', () => {
  const row = {
    task_id: 't1', run_id: 0,
    task_queue_id: 'proj-a/linux',
    priority_at_pending: 'high',
    tags: { kind: 'build' },
    pending_at: new Date('2026-05-13T00:00:00Z'),
    queue_pending: null,
    max_run_time_s: undefined,
  };
  const liveFeatures = { bl_wait_p50: null, queue_tasks_started_15m: NaN, queue_tasks_completed_15m: 0 };
  const v = buildFeatureVector(row, liveFeatures, baseSchema, categories);
  // null/undefined/NaN → NaN; a real 0 stays 0.
  assert.ok(Number.isNaN(v[3]),  `queue_pending NaN expected, got ${v[3]}`);
  assert.ok(Number.isNaN(v[4]),  `max_run_time_s NaN expected, got ${v[4]}`);
  assert.ok(Number.isNaN(v[9]),  `bl_wait_p50 NaN expected, got ${v[9]}`);
  assert.ok(Number.isNaN(v[10]), `queue_tasks_started_15m NaN expected, got ${v[10]}`);
  assert.equal(v[11], 0.0);  // queue_tasks_completed_15m: real zero passes through
});

test('build_type_regex derived feature extracts from metadata_name', () => {
  const schema = {
    feature_order: ['build_type'],
    categorical_features: ['build_type'],
    numeric_features: [],
    derived_features: {
      build_type_regex: { source: 'metadata_name', pattern: '/(debug|opt)[-/]' },
    },
    cold_start_code: -1,
  };
  const cats = { build_type: ['debug', 'opt'] };
  const v1 = buildFeatureVector(
    { metadata_name: 'test-linux2404-64/debug-mochitest-1' }, {}, schema, cats,
  );
  assert.equal(v1[0], 0.0);  // 'debug'
  const v2 = buildFeatureVector(
    { metadata_name: 'test-linux2404-64/opt-mochitest-2' }, {}, schema, cats,
  );
  assert.equal(v2[0], 1.0);  // 'opt'
  const v3 = buildFeatureVector({ metadata_name: 'build-something' }, {}, schema, cats);
  assert.equal(v3[0], -1.0);  // no match
});
