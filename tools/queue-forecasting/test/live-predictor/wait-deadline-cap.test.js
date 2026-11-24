import { test } from 'node:test';
import assert from 'node:assert';
import { applyWaitDeadlineCap, secondsToDeadline } from '../../src/live-predictor/wait-deadline-cap.js';

// A pending task resolves as deadline-exceeded at its deadline, so a completed
// run never waits longer than deadline - pending_at; quantiles above it are capped.

const PENDING = '2026-11-17T04:41:50Z';
const DEADLINE = '2026-11-18T04:00:00Z'; // 83,890 s after pending

test('secondsToDeadline: Date and ISO inputs', () => {
  assert.strictEqual(secondsToDeadline({ pending_at: PENDING, deadline: DEADLINE }), 83890);
  assert.strictEqual(secondsToDeadline({ pending_at: new Date(PENDING), deadline: new Date(DEADLINE) }), 83890);
});

test('secondsToDeadline: null when missing, unparsable, or not after pending', () => {
  assert.strictEqual(secondsToDeadline({ pending_at: PENDING, deadline: null }), null);
  assert.strictEqual(secondsToDeadline({ pending_at: PENDING }), null);
  assert.strictEqual(secondsToDeadline({ pending_at: null, deadline: DEADLINE }), null);
  assert.strictEqual(secondsToDeadline({ pending_at: PENDING, deadline: 'garbage' }), null);
  assert.strictEqual(secondsToDeadline({ pending_at: PENDING, deadline: PENDING }), null);
  assert.strictEqual(secondsToDeadline({ pending_at: DEADLINE, deadline: PENDING }), null);
  assert.strictEqual(secondsToDeadline(undefined), null);
});

test('caps p90 only when p50 is under the bound', () => {
  const r = applyWaitDeadlineCap({ p50: 165707, p90: 936080, row: { pending_at: PENDING, deadline: '2026-11-20T00:00:00Z' } });
  assert.strictEqual(r.applied, true);
  assert.strictEqual(r.p50, 165707);
  assert.strictEqual(r.p90, r.capS);
  assert.ok(r.p90 >= r.p50);
});

test('caps both quantiles when both exceed the bound', () => {
  const r = applyWaitDeadlineCap({ p50: 165707, p90: 936080, row: { pending_at: PENDING, deadline: DEADLINE } });
  assert.deepStrictEqual(r, { applied: true, capS: 83890, p50: 83890, p90: 83890 });
});

test('no-op under the bound or without a deadline', () => {
  const row = { pending_at: PENDING, deadline: DEADLINE };
  assert.deepStrictEqual(applyWaitDeadlineCap({ p50: 60, p90: 600, row }), { applied: false, capS: 83890, p50: 60, p90: 600 });
  assert.deepStrictEqual(applyWaitDeadlineCap({ p50: 9e5, p90: 9e6, row: { pending_at: PENDING } }),
    { applied: false, capS: null, p50: 9e5, p90: 9e6 });
});
