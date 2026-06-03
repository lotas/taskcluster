import { test } from 'node:test';
import assert from 'node:assert';
import { applyWaitP90Guardrail } from '../../src/live-predictor/wait-p90-guardrail.js';

// The wait guardrail floors the served wait-time p90 with the historical
// queue+priority+bucket baseline p90, but ONLY when the baseline is that
// strongest, priority-aware level with enough samples. The priority-blind
// queue+bucket level and the weak fallback levels (queue, priority+bucket,
// global) do not apply: in a deep queue, flooring to an all-priority pooled
// p90 over-inflates high-priority tasks by many multiples.

const MIN_SAMPLE = 20;

test('queue+priority+bucket baseline above model p90: guard raises p90 to baseline', () => {
  const r = applyWaitP90Guardrail({
    waitP50: 120,
    rawModelP90: 300,
    baseline: { level: 'queue+priority+bucket', p50: 180, p90: 900, sample_size: 500 },
    minSampleSize: MIN_SAMPLE,
  });
  assert.strictEqual(r.applied, true);
  assert.strictEqual(r.finalP90, 900);
  assert.strictEqual(r.rawModelP90, 300);
  assert.strictEqual(r.baselineP90, 900);
  assert.strictEqual(r.baselineLevel, 'queue+priority+bucket');
  assert.strictEqual(r.baselineSampleSize, 500);
});

test('queue+priority+bucket baseline below model p90: does not lower p90', () => {
  const r = applyWaitP90Guardrail({
    waitP50: 120,
    rawModelP90: 800,
    baseline: { level: 'queue+priority+bucket', p50: 180, p90: 400, sample_size: 500 },
    minSampleSize: MIN_SAMPLE,
  });
  assert.strictEqual(r.applied, true);
  assert.strictEqual(r.finalP90, 800);
  assert.strictEqual(r.rawModelP90, 800);
  assert.strictEqual(r.baselineP90, 400);
});

test('priority-blind level (queue+bucket): guard not applied', () => {
  // This is the failure mode the priority-aware level fixes: a very-high task
  // in a deep queue must NOT be floored to the all-priority pooled p90.
  const r = applyWaitP90Guardrail({
    waitP50: 120,
    rawModelP90: 300,
    baseline: { level: 'queue+bucket', p50: 180, p90: 67000, sample_size: 6000 },
    minSampleSize: MIN_SAMPLE,
  });
  assert.strictEqual(r.applied, false);
  assert.strictEqual(r.finalP90, 300);
});

test('fallback baseline level (queue): guard not applied', () => {
  const r = applyWaitP90Guardrail({
    waitP50: 120,
    rawModelP90: 300,
    baseline: { level: 'queue', p50: 180, p90: 5000, sample_size: 500 },
    minSampleSize: MIN_SAMPLE,
  });
  assert.strictEqual(r.applied, false);
  assert.strictEqual(r.finalP90, 300);
});

test('fallback baseline level (priority+bucket): guard not applied', () => {
  const r = applyWaitP90Guardrail({
    waitP50: 120,
    rawModelP90: 300,
    baseline: { level: 'priority+bucket', p50: 180, p90: 5000, sample_size: 500 },
    minSampleSize: MIN_SAMPLE,
  });
  assert.strictEqual(r.applied, false);
  assert.strictEqual(r.finalP90, 300);
});

test('fallback baseline level (global): guard not applied', () => {
  const r = applyWaitP90Guardrail({
    waitP50: 120,
    rawModelP90: 300,
    baseline: { level: 'global', p50: 180, p90: 5000, sample_size: 500 },
    minSampleSize: MIN_SAMPLE,
  });
  assert.strictEqual(r.applied, false);
  assert.strictEqual(r.finalP90, 300);
});

test('non-finite baseline p90: guard not applied even on queue+priority+bucket', () => {
  const r = applyWaitP90Guardrail({
    waitP50: 120,
    rawModelP90: 300,
    baseline: { level: 'queue+priority+bucket', p50: 180, p90: NaN, sample_size: 500 },
    minSampleSize: MIN_SAMPLE,
  });
  assert.strictEqual(r.applied, false);
  assert.strictEqual(r.finalP90, 300);
});

test('null baseline: guard not applied', () => {
  const r = applyWaitP90Guardrail({
    waitP50: 120,
    rawModelP90: 300,
    baseline: null,
    minSampleSize: MIN_SAMPLE,
  });
  assert.strictEqual(r.applied, false);
  assert.strictEqual(r.finalP90, 300);
});

test('sample size below threshold: guard not applied', () => {
  const r = applyWaitP90Guardrail({
    waitP50: 120,
    rawModelP90: 300,
    baseline: { level: 'queue+priority+bucket', p50: 180, p90: 900, sample_size: 19 },
    minSampleSize: MIN_SAMPLE,
  });
  assert.strictEqual(r.applied, false);
  assert.strictEqual(r.finalP90, 300);
});

test('sample size at threshold: guard applies', () => {
  const r = applyWaitP90Guardrail({
    waitP50: 120,
    rawModelP90: 300,
    baseline: { level: 'queue+priority+bucket', p50: 180, p90: 900, sample_size: 20 },
    minSampleSize: MIN_SAMPLE,
  });
  assert.strictEqual(r.applied, true);
  assert.strictEqual(r.finalP90, 900);
});

test('final p90 is never below p50: floors via max with p50', () => {
  // Fallback level disables the guard, but p50 still floors the result.
  const r = applyWaitP90Guardrail({
    waitP50: 600,
    rawModelP90: 100,
    baseline: { level: 'queue', p50: 200, p90: 5000, sample_size: 500 },
    minSampleSize: MIN_SAMPLE,
  });
  assert.strictEqual(r.applied, false);
  assert.strictEqual(r.finalP90, 600);
});

test('final p90 floor with p50 also applies when guard applied', () => {
  // queue+priority+bucket baseline of 50, model p90 of 60, but p50=400 → final must be 400.
  const r = applyWaitP90Guardrail({
    waitP50: 400,
    rawModelP90: 60,
    baseline: { level: 'queue+priority+bucket', p50: 30, p90: 50, sample_size: 500 },
    minSampleSize: MIN_SAMPLE,
  });
  assert.strictEqual(r.applied, true);
  assert.strictEqual(r.finalP90, 400);
});
