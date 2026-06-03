import { test } from 'node:test';
import assert from 'node:assert';
import { BaselineStats } from '../../src/live-predictor/baseline-stats.js';

function fakePool(rowsByQuery) {
  // rowsByQuery is keyed by a substring that identifies each SQL.
  return {
    async query(sql) {
      for (const [needle, rows] of Object.entries(rowsByQuery)) {
        if (sql.includes(needle)) return { rows };
      }
      return { rows: [] };
    },
  };
}

// Bucket labels come from src/utils.js pendingBucket:
//   0 → 'empty', 1-5 → 'low', 6-20 → 'moderate', 21-50 → 'busy',
//   51-200 → 'heavy', 201-500 → 'very-heavy', 501-1500 → 'overloaded',
//   1501+ → 'saturated'. Tests below use queue_pending=5 → 'low'.

const fullPool = fakePool({
  'duration_by_metadata':
    [{ key: 'test-linux-mochitest-1', p50: '120', p90: '300', sample_size: '20' }],
  'duration_by_normalized':
    [{ key: 'test-linux-mochitest', p50: '110', p90: '280', sample_size: '50' }],
  'duration_by_kind_test_type':
    [{ key: 'test|mochitest', p50: '100', p90: '250', sample_size: '40' }],
  'duration_by_queue':
    [{ key: 'proj-a/linux', p50: '90', p90: '220', sample_size: '60' }],
  'duration_by_scheduler':
    [{ key: 'gecko', p50: '80', p90: '200', sample_size: '70' }],
  'duration_global':
    [{ p50: '70', p90: '180', sample_size: '500' }],
  // Most-specific wait level must be listed first: the fakePool matches by
  // substring and 'wait_by_queue' is a substring of every wait-by-queue query.
  'wait_by_queue_priority_and_bucket':
    [{ key: 'proj-a/linux|high|low', p50: '2', p90: '9', sample_size: '12' }],
  'wait_by_queue_and_bucket':
    [{ key: 'proj-a/linux|low', p50: '5', p90: '20', sample_size: '15' }],
  'wait_by_queue':
    [{ key: 'proj-a/linux', p50: '10', p90: '30', sample_size: '30' }],
  'wait_by_priority_and_bucket':
    [{ key: 'high|low', p50: '3', p90: '15', sample_size: '25' }],
  'wait_global':
    [{ p50: '8', p90: '25', sample_size: '600' }],
  'queue_forecast_daily_health':
    [{ sample_date: new Date('2026-04-23T00:00:00Z') },
     { sample_date: new Date('2026-04-24T00:00:00Z') }],
});

test('wait lookup: queue+priority+bucket wins when present (queue_pending=5→low, priority=high)', async () => {
  const stats = new BaselineStats(fullPool);
  await stats.refresh();
  const r = stats.predictWait({ task_queue_id: 'proj-a/linux', queue_pending: 5, priority_at_pending: 'high' });
  assert.equal(r.level, 'queue+priority+bucket');
  assert.equal(r.p50, 2);
  assert.equal(r.p90, 9);
});

test('wait lookup: queue+bucket when priority+bucket key missing (different priority)', async () => {
  const stats = new BaselineStats(fullPool);
  await stats.refresh();
  // priority='low' → key 'proj-a/linux|low|low' absent → falls back to
  // queue+bucket 'proj-a/linux|low' (present).
  const r = stats.predictWait({ task_queue_id: 'proj-a/linux', queue_pending: 5, priority_at_pending: 'low' });
  assert.equal(r.level, 'queue+bucket');
  assert.equal(r.p50, 5);
  assert.equal(r.p90, 20);
});

test('wait lookup: queue level when queue+bucket key missing (different bucket)', async () => {
  const stats = new BaselineStats(fullPool);
  await stats.refresh();
  // queue_pending=100 → bucket='heavy' → no queue+bucket key proj-a/linux|heavy
  // falls back to byQueue (proj-a/linux is present there).
  const r = stats.predictWait({ task_queue_id: 'proj-a/linux', queue_pending: 100, priority_at_pending: 'high' });
  assert.equal(r.level, 'queue');
});

test('wait lookup: priority+bucket level when queue missing entirely', async () => {
  const stats = new BaselineStats(fullPool);
  await stats.refresh();
  // queue 'proj-b/win' is not in byQueue → falls through to priority+bucket
  // (queue_pending=5 → bucket='low', priority='high' → key 'high|low' is present).
  const r = stats.predictWait({ task_queue_id: 'proj-b/win', queue_pending: 5, priority_at_pending: 'high' });
  assert.equal(r.level, 'priority+bucket');
});

test('wait lookup: global fallback when nothing matches', async () => {
  const onlyGlobal = fakePool({ 'wait_global': [{ p50: '8', p90: '25', sample_size: '600' }] });
  const stats = new BaselineStats(onlyGlobal);
  await stats.refresh();
  const r = stats.predictWait({ task_queue_id: 'nope', queue_pending: null, priority_at_pending: null });
  assert.equal(r.level, 'global');
});

test('per-target filter: wait queries get $2 (excluded dates), duration queries do not', async () => {
  const seen = [];
  const recordingPool = {
    async query(sql, params) {
      seen.push({ sql, paramsLen: (params || []).length });
      if (sql.includes('queue_forecast_daily_health')) {
        return { rows: [{ sample_date: new Date('2026-04-23T00:00:00Z') }] };
      }
      return { rows: [] };
    },
  };
  const stats = new BaselineStats(recordingPool, {
    waitFilterAnomalous: true,
    durationFilterAnomalous: false,
  });
  await stats.refresh();

  for (const { sql, paramsLen } of seen) {
    if (sql.includes('queue_forecast_daily_health')) continue;  // exclude-list lookup
    const isWait     = sql.includes('-- wait_');
    const isDuration = sql.includes('-- duration_');
    if (isDuration) {
      assert.equal(paramsLen, 1, `duration query should have 1 param, got ${paramsLen}: ${sql.split('\n')[1]}`);
      assert.ok(!sql.includes('<> ALL'), 'duration SQL must NOT include exclude clause');
    }
    if (isWait) {
      assert.equal(paramsLen, 2, `wait query should have 2 params, got ${paramsLen}: ${sql.split('\n')[1]}`);
      assert.ok(sql.includes('<> ALL'), 'wait SQL must include exclude clause');
    }
  }
});

test('duration filter: when durationFilterAnomalous=true, duration queries get $2 and <> ALL', async () => {
  const seen = [];
  const recordingPool = {
    async query(sql, params) {
      seen.push({ sql, paramsLen: (params || []).length });
      if (sql.includes('queue_forecast_daily_health')) {
        return { rows: [{ sample_date: new Date('2026-04-23T00:00:00Z') }] };
      }
      return { rows: [] };
    },
  };
  const stats = new BaselineStats(recordingPool, {
    waitFilterAnomalous: false,
    durationFilterAnomalous: true,
  });
  await stats.refresh();

  for (const { sql, paramsLen } of seen) {
    if (sql.includes('queue_forecast_daily_health')) continue;
    const isDuration = sql.includes('-- duration_');
    if (isDuration) {
      assert.equal(paramsLen, 2, `duration query should have 2 params, got ${paramsLen}: ${sql.split('\n')[1]}`);
      assert.ok(sql.includes('<> ALL'), 'duration SQL must include exclude clause when durationFilterAnomalous=true');
    }
  }
});

test('duration lookup: metadata_name wins, then normalized', async () => {
  const stats = new BaselineStats(fullPool);
  await stats.refresh();
  const a = stats.predictDuration({ metadata_name: 'test-linux-mochitest-1' });
  assert.equal(a.level, 'metadata_name');
  assert.equal(a.p50, 120);

  // metadata_name missing → normalized_name match
  const b = stats.predictDuration({ metadata_name: 'novel-name', normalized_name: 'test-linux-mochitest' });
  assert.equal(b.level, 'normalized_name');
});

test('duration lookup: tags kind+test-type level', async () => {
  const stats = new BaselineStats(fullPool);
  await stats.refresh();
  const r = stats.predictDuration({ tags: { kind: 'test', 'test-type': 'mochitest' } });
  assert.equal(r.level, 'kind+test-type');
  assert.equal(r.p50, 100);
});

test('predictWait returns null when no level matches (no global)', async () => {
  const stats = new BaselineStats(fakePool({}));
  await stats.refresh();
  assert.equal(stats.predictWait({ task_queue_id: 'x' }), null);
  assert.equal(stats.predictDuration({ metadata_name: 'x' }), null);
});
