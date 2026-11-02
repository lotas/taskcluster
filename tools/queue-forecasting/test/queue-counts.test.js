import { test } from 'node:test';
import assert from 'node:assert';
import { createQueueCounter } from '../src/queue-counts.js';

function harness({ counts = {}, fail = new Set(), ...opts } = {}) {
  let t = 1_000_000;
  const calls = [];
  const errors = [];
  let inFlight = 0;
  let maxInFlight = 0;
  const qc = createQueueCounter({
    fetchCounts: async qid => {
      calls.push(qid);
      inFlight++;
      maxInFlight = Math.max(maxInFlight, inFlight);
      await new Promise(r => setImmediate(r));
      inFlight--;
      if (fail.has(qid)) throw Object.assign(new Error('Timeout awaiting request'), { code: 'ETIMEDOUT' });
      return { pendingTasks: counts[qid] ?? 0 };
    },
    logError: (category, message, details) => errors.push({ category, message, details }),
    now: () => t,
    ...opts,
  });
  return {
    qc, calls, errors, counts, fail,
    advance: ms => { t += ms; },
    maxInFlight: () => maxInFlight,
  };
}

test('first encounter seeds from the API, then counts locally', async () => {
  const h = harness({ counts: { 'p/a': 4 } });
  assert.equal(await h.qc.getPending('p/a'), 4);
  h.qc.increment('p/a');
  assert.equal(await h.qc.getPending('p/a'), 5);
  h.qc.decrement('p/a');
  h.qc.decrement('p/a');
  assert.equal(await h.qc.getPending('p/a'), 3);
  assert.deepEqual(h.calls, ['p/a']);
});

test('increment/decrement on an unseeded queue do nothing and never go negative', async () => {
  const h = harness({ counts: { 'p/a': 0 } });
  h.qc.increment('p/a');
  assert.equal(h.qc.isSeeded('p/a'), false);
  assert.equal(await h.qc.getPending('p/a'), 0);
  h.qc.decrement('p/a');
  assert.equal(await h.qc.getPending('p/a'), 0);
});

test('null queue id returns null without fetching', async () => {
  const h = harness();
  assert.equal(await h.qc.getPending(null), null);
  assert.equal(h.calls.length, 0);
});

test('concurrent first encounters share one request', async () => {
  const h = harness({ counts: { 'p/a': 2 } });
  const results = await Promise.all([h.qc.getPending('p/a'), h.qc.getPending('p/a'), h.qc.getPending('p/a')]);
  assert.deepEqual(results, [2, 2, 2]);
  assert.equal(h.calls.length, 1);
});

test('a failed first seed backs off instead of retrying on every message', async () => {
  const h = harness({ fail: new Set(['p/bad']), failureBackoffMs: 60_000 });
  for (let i = 0; i < 10; i++) assert.equal(await h.qc.getPending('p/bad'), null);
  assert.equal(h.calls.length, 1);
  assert.equal(h.errors.length, 1);
  h.advance(60_000);
  h.fail.delete('p/bad');
  h.counts['p/bad'] = 7;
  assert.equal(await h.qc.getPending('p/bad'), 7);
  assert.equal(h.calls.length, 2);
});

test('sync refreshes active queues and evicts idle ones', async () => {
  const h = harness({ counts: { 'p/a': 1, 'p/b': 1 }, idleEvictMs: 3_600_000 });
  await h.qc.getPending('p/a');
  await h.qc.getPending('p/b');
  h.advance(3_000_000);
  h.qc.increment('p/a'); // p/a stays active
  h.advance(700_000);    // p/b now idle for > 1h
  h.counts['p/a'] = 9;
  h.calls.length = 0;
  const res = await h.qc.sync();
  assert.equal(res.evicted, 1);
  assert.deepEqual(h.calls, ['p/a']);
  assert.equal(h.qc.isSeeded('p/b'), false);
  assert.equal(h.qc.tracked(), 1);
  assert.equal(await h.qc.getPending('p/a'), 9);
  // An evicted queue is re-seeded on its next event.
  h.counts['p/b'] = 3;
  assert.equal(await h.qc.getPending('p/b'), 3);
});

test('seeded set stays bounded under a stream of single-use queues', async () => {
  const h = harness({ idleEvictMs: 3_600_000 });
  for (let day = 0; day < 21; day++) {
    for (let i = 0; i < 4000; i++) {
      await h.qc.getPending(`test-provisioner/d${day}-q${i}`);
      if (i % 100 === 0) h.advance(2_160_000); // 40 steps of 36 min = one day per 4000 queues
    }
    await h.qc.sync();
  }
  assert.ok(h.qc.size() < 1000, `seeded set grew to ${h.qc.size()}`);
});

test('sync does not overlap itself', async () => {
  const h = harness({ counts: { 'p/a': 1 } });
  await h.qc.getPending('p/a');
  const [a, b] = await Promise.all([h.qc.sync(), h.qc.sync()]);
  assert.equal(a.skipped, false);
  assert.equal(b.skipped, true);
});

test('sync respects maxConcurrent and logs one summary for many failures', async () => {
  const counts = {};
  for (let i = 0; i < 200; i++) counts[`p/q${i}`] = 0;
  const h = harness({ counts, maxConcurrent: 10 });
  for (const qid of Object.keys(counts)) await h.qc.getPending(qid);
  for (let i = 0; i < 150; i++) h.fail.add(`p/q${i}`);
  h.errors.length = 0;
  const res = await h.qc.sync();
  assert.ok(h.maxInFlight() <= 10, `max in flight ${h.maxInFlight()}`);
  assert.equal(res.failed, 150);
  assert.equal(h.errors.length, 1);
  assert.match(h.errors[0].message, /150\/200/);
  // A failed refresh keeps the last known count.
  assert.equal(h.qc.isSeeded('p/q0'), true);
});

test('errors claimed by onFetchError are not logged', async () => {
  const h = harness({ fail: new Set(['p/a']), onFetchError: () => true });
  assert.equal(await h.qc.getPending('p/a'), null);
  assert.equal(h.errors.length, 0);
});
