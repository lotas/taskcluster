// predictAndStore end-to-end with fake ONNX sessions and a fake pool: pins the
// exact INSERT params so the computePrediction() refactor stays behaviour-neutral.
import { test } from 'node:test';
import assert from 'node:assert';
import * as predict from '../../src/live-predictor/predict.js';
const { predictAndStore, computePrediction } = predict;

function session(value) {
  return {
    inputNames: ['x'], outputNames: ['y'],
    calls: [],
    async run(feeds) { this.calls.push(Array.from(feeds.x.data)); return { y: { data: [value] } }; },
  };
}

function bundle(features, baselineFeature, p50, p90, version) {
  return {
    schema: {
      model_version: version,
      feature_order: features,
      categorical_features: ['task_queue_id'],
      numeric_features: features.filter(f => f !== 'task_queue_id'),
      residual: { baseline_feature: baselineFeature, transform: 'log_ratio' },
      derived_features: { cyclical_time: { source: 'pending_at' } },
      cold_start_code: -1,
    },
    categories: { task_queue_id: ['a/b', 'c/d'] },
    sessionP50: session(p50),
    sessionP90: session(p90),
    artifact_hash: `hash-${version}`,
  };
}

const ROW = {
  task_id: 'T', run_id: 0, pending_at: new Date('2026-11-20T10:00:00Z'), queue_pending: 12,
  priority_at_pending: 'low', started_at: null, resolved_at: null,
  task_queue_id: 'c/d', scheduler_id: 's', metadata_name: 'test-linux/opt-x', normalized_name: 'test-linux/opt-x',
  max_run_time_s: 3600, tags: { kind: 'test' }, enriched_at: new Date(), repo_family: 'try',
};

function fakePool(inserts) {
  return {
    async query(sql, params) {
      if (sql.includes('SELECT enriched_at')) return { rows: [{ enriched_at: new Date() }], rowCount: 1 };
      if (sql.includes('FROM queue_forecast_task_runs r\nJOIN queue_forecast_tasks t ON r.task_id = t.task_id\nWHERE r.task_id = $1 AND r.run_id = $2')) {
        return { rows: [{ ...ROW }], rowCount: 1 };
      }
      if (sql.includes('INSERT INTO')) { inserts.push({ sql: sql.split('(')[0].trim(), params }); return { rowCount: 1, rows: [] }; }
      // throughput / queue-context: deterministic numbers
      return { rows: [{ has_history: true, queue_tasks_started_15m: 3, queue_avg_wait_15m: 40,
        pending_higher_priority_same_queue: 2, arrivals_15m_same_queue: 5 }], rowCount: 1 };
    },
  };
}

const stats = {
  predictWait: () => ({ level: 'queue+bucket', p50: 60, p90: 600, sample_size: 50 }),
  predictDuration: () => ({ level: 'metadata_name', p50: 300, p90: 900, sample_size: 50 }),
};

function mkBundles() {
  return {
    wait: bundle(['task_queue_id', 'queue_pending', 'hour_sin', 'bl_wait_p50', 'queue_avg_wait_15m'], 'bl_wait_p50', 0.1, 0.9, 'w1'),
    duration: bundle(['task_queue_id', 'max_run_time_s', 'bl_duration_p50'], 'bl_duration_p50', -0.2, 0.3, 'd1'),
    shadowWait: bundle(['task_queue_id', 'pending_higher_priority_same_queue', 'bl_wait_p50'], 'bl_wait_p50', 0.2, 1.1, 's1'),
  };
}

test('predictAndStore INSERT params are pinned', async () => {
  const inserts = [];
  const r = await predictAndStore({ pool: fakePool(inserts), bundles: mkBundles(), baselineStats: stats, taskId: 'T', runId: 0 });
  assert.strictEqual(r.inserted, true);
  assert.strictEqual(inserts.length, 2);
  const [served, shadow] = inserts;
  const p = served.params;
  const close = (a, b) => assert.ok(Math.abs(a - b) < 1e-9, `${a} != ${b}`);
  close(p[2], Math.exp(0.1) * 61 - 1);                      // wait p50
  close(p[3], Math.max(Math.exp(0.9) * 61 - 1, 0));           // wait p90 (guard: queue+bucket floors to 600)
  close(p[4], Math.exp(-0.2) * 301 - 1);                      // run p50
  close(p[5], 900);                                           // run p90 floored by metadata_name baseline
  assert.deepStrictEqual(p.slice(6, 10), ['w1', 'hash-w1', 'd1', 'hash-d1']);
  const audit = JSON.parse(p[10]);
  assert.deepStrictEqual(audit.wait_vector.slice(0, 2), [1, 12]);
  assert.strictEqual(audit.wait_vector[4], 40);
  assert.deepStrictEqual(audit.duration_vector, [1, 3600, 300]);
  assert.strictEqual(audit.wait_p90_guardrail.baseline_p90_s, 600);
  assert.strictEqual(shadow.params[4], 's1');
  close(shadow.params[2], Math.exp(0.2) * 61 - 1);
  // Emit the full params so the pre/post-refactor runs can be diffed.
  if (process.env.PARITY_OUT) {
    const fs = await import('node:fs');
    fs.writeFileSync(process.env.PARITY_OUT, JSON.stringify(inserts.map(i => i.params)));
  }
});

test('computePrediction writes nothing and reports feature coverage', async () => {
  if (typeof computePrediction !== 'function') return; // pre-refactor run
  const inserts = [];
  const out = await computePrediction({ pool: fakePool(inserts), bundles: mkBundles(), baselineStats: stats, row: { ...ROW, task_queue_id: 'zz/unknown' } });
  assert.strictEqual(inserts.length, 0);
  assert.strictEqual(out.featureCoverage.wait.total, 5);
  assert.strictEqual(out.featureCoverage.duration.missing, 0);
  assert.ok(out.run.p90 >= out.run.p50);
});
