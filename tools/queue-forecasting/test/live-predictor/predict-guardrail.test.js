import { test } from 'node:test';
import assert from 'node:assert';
import { applyDurationP90Guardrail } from '../../src/live-predictor/duration-p90-guardrail.js';

// The guardrail floors the served run-duration p90 with the historical
// per-metadata_name baseline p90, but ONLY when the baseline is a strong
// exact-name cohort (level === 'metadata_name') with enough samples. Weak
// fallback levels (task_queue_id, kind+test-type, scheduler_id, global) are
// known to be poorly calibrated and do not apply.

const MIN_SAMPLE = 20;

test('metadata_name baseline above model p90: guard raises p90 to baseline', () => {
  const r = applyDurationP90Guardrail({
    runP50: 200,
    rawModelP90: 360,
    baseline: { level: 'metadata_name', p50: 300, p90: 610, sample_size: 80 },
    minSampleSize: MIN_SAMPLE,
  });
  assert.strictEqual(r.applied, true);
  assert.strictEqual(r.finalP90, 610);
  assert.strictEqual(r.rawModelP90, 360);
  assert.strictEqual(r.baselineP90, 610);
  assert.strictEqual(r.baselineLevel, 'metadata_name');
  assert.strictEqual(r.baselineSampleSize, 80);
});

test('metadata_name baseline below model p90: does not lower p90', () => {
  const r = applyDurationP90Guardrail({
    runP50: 200,
    rawModelP90: 500,
    baseline: { level: 'metadata_name', p50: 300, p90: 400, sample_size: 80 },
    minSampleSize: MIN_SAMPLE,
  });
  // Guard was eligible — it just didn't raise the value (max picked the model).
  assert.strictEqual(r.applied, true);
  assert.strictEqual(r.finalP90, 500);
  assert.strictEqual(r.rawModelP90, 500);
  assert.strictEqual(r.baselineP90, 400);
});

test('fallback baseline level (task_queue_id): guard not applied', () => {
  const r = applyDurationP90Guardrail({
    runP50: 200,
    rawModelP90: 360,
    baseline: { level: 'task_queue_id', p50: 300, p90: 800, sample_size: 80 },
    minSampleSize: MIN_SAMPLE,
  });
  assert.strictEqual(r.applied, false);
  assert.strictEqual(r.finalP90, 360);
});

test('fallback baseline level (kind+test-type): guard not applied', () => {
  const r = applyDurationP90Guardrail({
    runP50: 50,
    rawModelP90: 360,
    baseline: { level: 'kind+test-type', p50: 300, p90: 999, sample_size: 80 },
    minSampleSize: MIN_SAMPLE,
  });
  assert.strictEqual(r.applied, false);
  assert.strictEqual(r.finalP90, 360);
});

test('fallback baseline level (scheduler_id): guard not applied', () => {
  const r = applyDurationP90Guardrail({
    runP50: 50,
    rawModelP90: 360,
    baseline: { level: 'scheduler_id', p50: 300, p90: 999, sample_size: 80 },
    minSampleSize: MIN_SAMPLE,
  });
  assert.strictEqual(r.applied, false);
  assert.strictEqual(r.finalP90, 360);
});

test('fallback baseline level (global): guard not applied', () => {
  const r = applyDurationP90Guardrail({
    runP50: 50,
    rawModelP90: 360,
    baseline: { level: 'global', p50: 300, p90: 999, sample_size: 80 },
    minSampleSize: MIN_SAMPLE,
  });
  assert.strictEqual(r.applied, false);
  assert.strictEqual(r.finalP90, 360);
});

test('non-finite baseline p90: guard not applied even on metadata_name', () => {
  const r = applyDurationP90Guardrail({
    runP50: 200,
    rawModelP90: 360,
    baseline: { level: 'metadata_name', p50: 300, p90: NaN, sample_size: 80 },
    minSampleSize: MIN_SAMPLE,
  });
  assert.strictEqual(r.applied, false);
  assert.strictEqual(r.finalP90, 360);
});

test('null baseline: guard not applied', () => {
  const r = applyDurationP90Guardrail({
    runP50: 200,
    rawModelP90: 360,
    baseline: null,
    minSampleSize: MIN_SAMPLE,
  });
  assert.strictEqual(r.applied, false);
  assert.strictEqual(r.finalP90, 360);
});

test('sample size below threshold: guard not applied', () => {
  const r = applyDurationP90Guardrail({
    runP50: 200,
    rawModelP90: 360,
    baseline: { level: 'metadata_name', p50: 300, p90: 610, sample_size: 19 },
    minSampleSize: MIN_SAMPLE,
  });
  assert.strictEqual(r.applied, false);
  assert.strictEqual(r.finalP90, 360);
});

test('sample size at threshold: guard applies', () => {
  const r = applyDurationP90Guardrail({
    runP50: 200,
    rawModelP90: 360,
    baseline: { level: 'metadata_name', p50: 300, p90: 610, sample_size: 20 },
    minSampleSize: MIN_SAMPLE,
  });
  assert.strictEqual(r.applied, true);
  assert.strictEqual(r.finalP90, 610);
});

test('final p90 is never below p50: floors via max with p50', () => {
  // Model says p90=100, baseline disabled level, but p50=500 → final must be ≥ 500.
  const r = applyDurationP90Guardrail({
    runP50: 500,
    rawModelP90: 100,
    baseline: { level: 'task_queue_id', p50: 200, p90: 800, sample_size: 80 },
    minSampleSize: MIN_SAMPLE,
  });
  assert.strictEqual(r.applied, false);
  assert.strictEqual(r.finalP90, 500);
});

test('final p90 floor with p50 also applies when guard applied', () => {
  // metadata_name baseline of 50, model p90 of 60, but p50=400 → final must be 400.
  const r = applyDurationP90Guardrail({
    runP50: 400,
    rawModelP90: 60,
    baseline: { level: 'metadata_name', p50: 30, p90: 50, sample_size: 80 },
    minSampleSize: MIN_SAMPLE,
  });
  assert.strictEqual(r.applied, true);
  assert.strictEqual(r.finalP90, 400);
});
