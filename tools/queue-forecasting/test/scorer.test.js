import { test } from 'node:test';
import assert from 'node:assert';
import { predictGroup, sizeBucket, mode, validateModel, pickEstimator, isStaticEstimator } from '../src/scorer/group-eta.js';
import { validateRow, validateExclude, validateGroupBody, createGate, BadRequest } from '../src/playground-scorer.js';

const MODEL = validateModel({
  format: 'group-eta-tables-v1',
  version: 'test',
  trained_before: 0,
  estimators: ['B0', 'B1c', 'B1c*'],
  params: { size_buckets: [5, 10, 50, 200, 1000, null], min_key_samples: 20, min_group_tasks: 5, pred_delay_s: 120 },
  b0: { fs: { try: { 0: [100, 200] } }, fam: { try: [300, 400], other: [500, 600] }, glob: [700, 800] },
  b1_full: { q_key: { 'try\x1fbuild': [1000, 2000] }, q_fam: { try: [10, 20] } },
  b1_fit: { q_key: { 'try\x1fbuild': [1000, 1500] }, q_fam: { try: [10, 20] } },
  cal_c: { try: { n: 50, f50: 1.5, f90: 0.5 }, rare: { n: 3, f50: 9, f90: 9 }, __global__: { n: 99, f50: 2, f90: 2 } },
});

const tasks = (n, extra = {}) => Array.from({ length: n }, (_, i) => ({ created: 100 + i, repoFamily: 'try', normalizedName: 'test', ...extra }));

test('sizeBucket matches pd.cut(right=False)', () => {
  const b = MODEL.params.size_buckets;
  assert.strictEqual(sizeBucket(4, b), null);
  assert.strictEqual(sizeBucket(5, b), 0);
  assert.strictEqual(sizeBucket(9, b), 0);
  assert.strictEqual(sizeBucket(10, b), 1);
  assert.strictEqual(sizeBucket(5000, b), 4);
});

test('mode: most frequent, ties broken by smallest value', () => {
  assert.strictEqual(mode(['b', 'a', 'b']), 'b');
  assert.strictEqual(mode(['b', 'a']), 'a');
});

test('too few known tasks -> skipped', () => {
  const r = predictGroup(MODEL, 1e9, tasks(4));
  assert.ok(r.skipped);
  // tasks created after t_pred are not known
  assert.ok(predictGroup(MODEL, 102, tasks(10)).skipped);
});

test('B0 lookups: family+size, family, global', () => {
  const b0 = (fam, n) => predictGroup(MODEL, 1e9, tasks(n, { repoFamily: fam })).estimates.find(e => e.estimator === 'B0');
  assert.deepStrictEqual([b0('try', 6).p50_s, b0('try', 6).p90_s], [100, 200]);
  assert.deepStrictEqual([b0('try', 20).p50_s, b0('try', 20).p90_s], [300, 400]);
  assert.deepStrictEqual([b0('nope', 6).p50_s, b0('nope', 6).p90_s], [700, 800]);
});

test('B1c: max over tasks, key then family fallback; B1c* calibrated and ordered', () => {
  const ts = [...tasks(5), { created: 50, repoFamily: 'try', normalizedName: 'build' }, { created: 60, repoFamily: 'zzz', normalizedName: 'x' }];
  const r = predictGroup(MODEL, 1e9, ts);
  assert.strictEqual(r.t0, 50);
  assert.strictEqual(r.nKnown, 7);
  const by = Object.fromEntries(r.estimates.map(e => [e.estimator, [e.p50_s, e.p90_s]]));
  assert.deepStrictEqual(by.B1c, [1000, 2000]);
  // b1_fit [1000, 1500] * try factors (1.5, 0.5) = [1500, 750] -> p90 floored to p50
  assert.deepStrictEqual(by['B1c*'], [1500, 1500]);
});

test('B1c*: family with too few calibration groups uses __global__', () => {
  const r = predictGroup(MODEL, 1e9, tasks(6, { repoFamily: 'rare' }));
  assert.ok(!r.estimates.find(e => e.estimator === 'B1c'), 'unknown family tasks are dropped -> no B1c');
  const r2 = predictGroup({ ...MODEL, b1_fit: { q_key: {}, q_fam: { rare: [10, 20] } } }, 1e9, tasks(6, { repoFamily: 'rare' }));
  assert.deepStrictEqual(r2.estimates.find(e => e.estimator === 'B1c*'), { estimator: 'B1c*', p50_s: 20, p90_s: 40 });
});

test('B1c*: a NaN (JSON null) calibration factor gives no estimate, as in Python', () => {
  const m = { ...MODEL, cal_c: { ...MODEL.cal_c, try: { n: 50, f50: null, f90: 0.5 } } };
  assert.ok(!predictGroup(m, 1e9, tasks(6)).estimates.find(e => e.estimator === 'B1c*'));
  // only f90 missing: np.fmax(NaN, c50) = c50
  const m2 = { ...MODEL, cal_c: { ...MODEL.cal_c, try: { n: 50, f50: 2, f90: null } } };
  assert.deepStrictEqual(predictGroup(m2, 1e9, tasks(6)).estimates.find(e => e.estimator === 'B1c*'), { estimator: 'B1c*', p50_s: 20, p90_s: 20 });
});

test('null family/name are coalesced like LIVE_TASKS_SQL', () => {
  const m = { ...MODEL, b1_full: { q_key: { 'unknown\x1f?': [5, 6] }, q_fam: {} } };
  const r = predictGroup(m, 1e9, tasks(5, { repoFamily: null, normalizedName: null }));
  assert.strictEqual(r.repoFamily, 'unknown');
  assert.deepStrictEqual(r.estimates.find(e => e.estimator === 'B1c'), { estimator: 'B1c', p50_s: 5, p90_s: 6 });
});

test('validateModel rejects other formats', () => {
  assert.throws(() => validateModel({ format: 'x' }));
});

test('validateRow: whitelist, types, dates', () => {
  const r = validateRow({ pending_at: '2026-11-20T00:00:00Z', task_queue_id: 'a/b', tags: { kind: 'x' }, queue_pending: 3, started_at: null });
  assert.ok(r.pending_at instanceof Date);
  assert.strictEqual(r.started_at, null);
  assert.throws(() => validateRow({ pending_at: '2026-11-20T00:00:00Z', __proto__x: 1 }), BadRequest);
  assert.throws(() => validateRow({ pending_at: 'yesterday' }), BadRequest);
  assert.throws(() => validateRow({ task_queue_id: 'a/b' }), /pending_at/);
  assert.throws(() => validateRow({ pending_at: '2026-11-20T00:00:00Z', queue_pending: '3' }), BadRequest);
  assert.throws(() => validateRow({ pending_at: '2026-11-20T00:00:00Z', tags: { a: { b: 1 } } }), BadRequest);
  assert.throws(() => validateRow({ pending_at: '2026-11-20T00:00:00Z', metadata_name: 'x'.repeat(600) }), BadRequest);
  assert.throws(() => validateRow(JSON.parse('{"pending_at":"2026-11-20T00:00:00Z","__proto__":{"x":1}}')), BadRequest);
});

test('validateExclude / validateGroupBody', () => {
  assert.deepStrictEqual(validateExclude(null), { taskId: null, runId: null });
  assert.throws(() => validateExclude({ taskId: "x' OR 1=1" }), BadRequest);
  assert.throws(() => validateGroupBody({ tPred: 1, tasks: 'no' }), BadRequest);
  assert.throws(() => validateGroupBody({ tPred: 1, tasks: [{ created: 'x' }] }), BadRequest);
  assert.deepStrictEqual(validateGroupBody({ tPred: 1, tasks: [{ created: 2, extra: 1 }] }).tasks, [{ created: 2, repoFamily: null, normalizedName: null }]);
});

test('createGate: bounded concurrency and queue', async () => {
  const g = createGate(1, 1);
  const r1 = await g.enter();
  const p2 = g.enter();
  assert.strictEqual(await g.enter(), null);
  r1();
  const r2 = await p2;
  assert.deepStrictEqual(g.stats(), { active: 1, queued: 0 });
  r2();
});

test('createGate: a released slot goes to the waiter, not to a later enter()', async () => {
  const g = createGate(1, 2);
  const r1 = await g.enter();
  const p2 = g.enter();
  r1();
  const p3 = g.enter();              // same tick as the release
  const r2 = await p2;
  assert.deepStrictEqual(g.stats(), { active: 1, queued: 1 });
  r2();
  const r3 = await p3;
  assert.deepStrictEqual(g.stats(), { active: 1, queued: 0 });
  r3();
  assert.deepStrictEqual(g.stats(), { active: 0, queued: 0 });
});

test('pickEstimator: B1c is the default, then B1c*, then B0, then whatever is live', () => {
  assert.strictEqual(pickEstimator(['B0', 'B1c', 'B1c*']), 'B1c');
  assert.strictEqual(pickEstimator(['B0', 'B1c*']), 'B1c*');
  assert.strictEqual(pickEstimator(['B0']), 'B0');
  assert.strictEqual(pickEstimator(['X9']), 'X9');
  assert.strictEqual(pickEstimator([]), null);
});

test('isStaticEstimator: B2* in-flight re-estimates are not static', () => {
  for (const e of ['B0', 'B1c', 'B1c*', 'B1c**']) assert.ok(isStaticEstimator(e), e);
  for (const e of ['B2*+15m', 'B2*+120m']) assert.ok(!isStaticEstimator(e), e);
});
