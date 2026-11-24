import { test } from 'node:test';
import assert from 'node:assert';
import { createPlayground, createTtlCache, sanitizeTags, PlaygroundError, GROUP_PAGE_LIMIT } from '../src/playground.js';
import { createHandler, createRateLimiter, route, runPrediction } from '../src/api.js';

const TID = 'aiK7FhP9TNyOPVWf_vc2qQ';
const GID = 'Pfv0LG_9TcOML39k0q34IA';
const NOW = Date.parse('2026-11-20T12:00:00Z');
// A valid Taskcluster v4 slugid that varies with ch.
const vid = (ch) => `${ch.repeat(8)}Q${ch}C${ch.repeat(10)}A`;

function counter() {
  const calls = [];
  const rec = (name, ret) => async (...args) => {
    calls.push([name, ...args]);
    if (ret instanceof Error) throw ret;
    return typeof ret === 'function' ? ret(...args) : ret;
  };
  return { calls, rec, count: (n) => calls.filter(c => c[0] === n).length };
}

function notFound() {
  const e = new Error('ResourceNotFound');
  e.statusCode = 404;
  e.code = 'ResourceNotFound';
  return e;
}

const SCORE = {
  wait: { p50_s: 60, p90_s: 600, p90GuardrailApplied: true, modelVersion: 'w1' },
  run: { p50_s: 300, p90_s: 900, p90GuardrailApplied: false, modelVersion: 'd1' },
  shadowWait: { p50_s: 50, p90_s: 500, modelVersion: 's1' },
  baselines: { wait: null, duration: null },
  featureCoverage: { wait: { total: 23, missing: 0 }, duration: { total: 17, missing: 2 } },
};

const TASK_DEF = {
  taskQueueId: 'gecko-t/t-linux', schedulerId: 'gecko-level-1', taskGroupId: GID, projectId: 'gecko',
  created: '2026-01-01T00:00:00Z', priority: 'low', routes: [], tags: { kind: 'test', bad: { x: 1 } },
  metadata: { name: 'test-linux/opt-xpcshell-1' }, payload: { maxRunTime: 3600 },
};

function setup({ stored = null, storedGroup = null, dbRow = null, queue = {}, scorer = {}, ip, glob, maxConcurrent } = {}) {
  const c = counter();
  const pool = { query: c.rec('db', () => ({ rows: dbRow ? [dbRow] : [] })) };
  const q = {
    task: c.rec('task', queue.task ?? TASK_DEF),
    status: c.rec('status', queue.status ?? { status: { taskId: TID, runs: [] } }),
    taskQueueCounts: c.rec('counts', queue.counts ?? { pendingTasks: 42 }),
    listTaskGroup: c.rec('list', queue.list ?? { tasks: [] }),
  };
  const sc = { score: c.rec('score', scorer.score ?? SCORE), group: c.rec('group', scorer.group ?? {}) };
  const pg = createPlayground({
    pool, queue: q, scorer: sc,
    getTask: c.rec('getTask', stored), getGroup: c.rec('getGroup', storedGroup), runPrediction,
    ipLimiter: ip ?? createRateLimiter({ ratePerS: 0.1, burst: 3, now: () => NOW }),
    globalLimiter: glob ?? createRateLimiter({ ratePerS: 1, burst: 60, now: () => NOW }),
    maxConcurrent, cache: createTtlCache({ now: () => NOW }), now: () => NOW,
  });
  return { pg, c };
}

test('route: playground paths', () => {
  assert.deepStrictEqual(route(`/api/v0/playground/task/${TID}`), { kind: 'task', playground: true, id: TID });
  assert.deepStrictEqual(route(`/api/v0/playground/group/${GID}/`), { kind: 'group', playground: true, id: GID });
  assert.strictEqual(route('/api/v0/playground/other/x'), null);
});

test('stored prediction: no Taskcluster or scorer calls, not charged', async () => {
  const stored = { taskId: TID, runs: [{ runId: 0, served: { wait_p50_s: 1 } }] };
  const ip = { allow: () => { throw new Error('must not charge'); } };
  const { pg, c } = setup({ stored, ip });
  const r = await pg.task(TID, '1.2.3.4');
  assert.strictEqual(r.source, 'stored');
  assert.deepStrictEqual(c.calls.map(x => x[0]), ['getTask']);
});

test('unknown task -> hypothetical-now from Taskcluster, current queue count, sanitized tags', async () => {
  const { pg, c } = setup();
  const r = await pg.task(TID, 'k');
  assert.strictEqual(r.source, 'computed');
  assert.strictEqual(r.mode, 'hypothetical-now');
  const [, body] = c.calls.find(x => x[0] === 'score');
  assert.strictEqual(body.exclude, null);
  assert.strictEqual(body.row.pending_at, new Date(NOW).toISOString());
  assert.strictEqual(body.row.queue_pending, 42);
  assert.strictEqual(body.row.priority_at_pending, 'low');
  assert.strictEqual(body.row.repo_family, 'try');
  assert.deepStrictEqual(body.row.tags, { kind: 'test' });
  assert.strictEqual(r.prediction.served.wait_p50_s, 60);
  assert.strictEqual(r.prediction.expected.start_p50, new Date(NOW + 60e3).toISOString());
  assert.strictEqual(r.prediction.expected.finish_p90, new Date(NOW + 1500e3).toISOString());
  assert.strictEqual(r.prediction.shadow.wait_p90_s, 500);
});

test('in-window DB row without prediction -> at-pending, no Taskcluster calls', async () => {
  const pendingAt = new Date(NOW - 3600e3);
  const dbRow = {
    task_id: TID, task_group_id: GID, task_queue_id: 'a/b', scheduler_id: 's', metadata_name: 'n', normalized_name: 'n',
    max_run_time_s: 60, tags: { kind: 'build' }, enriched_at: new Date(), repo_family: 'autoland', original_priority: 'high',
    run_id: 1, pending_at: pendingAt, queue_pending: 7, priority_at_pending: 'very-high',
    started_at: new Date(NOW - 3000e3), resolved_at: null,
  };
  const stored = { taskId: TID, runs: [{ runId: 1, state: 'running', pending: pendingAt.toISOString(), served: null }] };
  const { pg, c } = setup({ stored, dbRow });
  const r = await pg.task(TID, 'k');
  assert.strictEqual(r.mode, 'at-pending');
  for (const n of ['task', 'status', 'counts']) assert.strictEqual(c.count(n), 0, n);
  const [, body] = c.calls.find(x => x[0] === 'score');
  assert.deepStrictEqual(body.exclude, { taskId: TID, runId: 1 });
  assert.strictEqual(body.row.queue_pending, 7);
  assert.strictEqual(body.row.priority_at_pending, 'very-high');
  // the actual start replaces the wait estimate
  assert.strictEqual(r.prediction.expected.start_p50, new Date(NOW - 3000e3).toISOString());
});

test('old DB row (outside window) -> hypothetical-now with actual runs from Taskcluster', async () => {
  const old = new Date(NOW - 80 * 86400e3);
  const dbRow = { task_id: TID, enriched_at: new Date(), task_queue_id: 'a/b', run_id: 0, pending_at: old, original_priority: 'low' };
  const status = { status: { taskId: TID, runs: [{ runId: 0, scheduled: old.toISOString(), started: new Date(old.getTime() + 10e3).toISOString(), resolved: new Date(old.getTime() + 70e3).toISOString(), reasonResolved: 'completed' }] } };
  const { pg } = setup({ dbRow, queue: { status } });
  const r = await pg.task(TID, 'k');
  assert.strictEqual(r.mode, 'hypothetical-now');
  assert.deepStrictEqual(r.runs[0].actual, { wait_s: 10, run_s: 60 });
  assert.strictEqual(r.runs[0].state, 'completed');
});

test('taskQueueId from Taskcluster is shape-checked before taskQueueCounts', async () => {
  const { pg, c } = setup({ queue: { task: { ...TASK_DEF, taskQueueId: '../../evil?x=1' } } });
  const r = await pg.task(TID, 'k');
  assert.strictEqual(c.count('counts'), 0);
  assert.strictEqual(r.anchor.queuePending, null);
});

test('Taskcluster 404 -> 404, negatively cached without re-charging', async () => {
  const { pg, c } = setup({ queue: { task: notFound() } });
  await assert.rejects(pg.task(TID, 'k'), (e) => e instanceof PlaygroundError && e.status === 404);
  await assert.rejects(pg.task(TID, 'k'), (e) => e.status === 404);
  assert.strictEqual(c.count('task'), 1);
});

test('Taskcluster outage -> 502 and not cached', async () => {
  const { pg, c } = setup({ queue: { task: new Error('ECONNRESET') } });
  await assert.rejects(pg.task(TID, 'k'), (e) => e.status === 502);
  await assert.rejects(pg.task(TID, 'k'), (e) => e.status === 502);
  assert.strictEqual(c.count('task'), 2);
});

test('scorer failure -> 503', async () => {
  const { pg } = setup({ scorer: { score: new Error('ECONNREFUSED') } });
  await assert.rejects(pg.task(TID, 'k'), (e) => e.status === 503 && Boolean(e.headers['Retry-After']));
});

test('computed results are cached: second call is free', async () => {
  const { pg, c } = setup();
  await pg.task(TID, 'k');
  await pg.task(TID, 'k');
  assert.strictEqual(c.count('score'), 1);
});

test('per-IP compute bucket -> 429 with Retry-After; other IPs unaffected', async () => {
  const { pg } = setup();
  const ids = ['A', 'B', 'C', 'D'].map(vid);
  for (const id of ids.slice(0, 3)) await pg.task(id, 'ip1');
  await assert.rejects(pg.task(ids[3], 'ip1'), (e) => e.status === 429 && e.headers['Retry-After'] === '10');
  await pg.task(ids[3], 'ip2');
});

test('global compute bucket -> 429, and the per-IP token is refunded', async () => {
  const glob = createRateLimiter({ ratePerS: 0, burst: 1, now: () => NOW });
  const ip = createRateLimiter({ ratePerS: 0, burst: 2, now: () => NOW });
  const { pg } = setup({ glob, ip });
  await pg.task(vid('A'), 'ip1');
  for (let i = 0; i < 3; i++) await assert.rejects(pg.task(vid('B'), 'ip2'), (e) => e.status === 429 && /busy/.test(e.message));
  assert.strictEqual(ip.allow('ip2') && ip.allow('ip2'), true, 'ip2 still has its full burst');
});

test('busy (concurrency) refusals do not charge the per-IP bucket', async () => {
  let release;
  const gate = new Promise(r => { release = r; });
  const ip = createRateLimiter({ ratePerS: 0, burst: 1, now: () => NOW });
  const { pg } = setup({ maxConcurrent: 1, ip: { allow: (k) => k === 'ip1' || ip.allow(k) }, scorer: { score: () => gate.then(() => SCORE) } });
  const a = pg.task(TID, 'ip1');
  await new Promise(r => setTimeout(r, 0));
  for (let i = 0; i < 3; i++) await assert.rejects(pg.task(vid('Z'), 'ip2'), (e) => e.status === 503);
  release();
  await a;
  await pg.task(vid('Z'), 'ip2');   // the single token is still there
});

test('createRateLimiter.refund restores at most up to burst', () => {
  const l = createRateLimiter({ ratePerS: 0, burst: 1, now: () => NOW });
  l.refund('k');                    // unknown key: no-op
  assert.strictEqual(l.allow('k'), true);
  assert.strictEqual(l.allow('k'), false);
  l.refund('k'); l.refund('k');
  assert.strictEqual(l.allow('k'), true);
  assert.strictEqual(l.allow('k'), false);
});

test('concurrency gate -> 503; identical in-flight requests share one compute', async () => {
  let release;
  const gate = new Promise(r => { release = r; });
  const { pg, c } = setup({ maxConcurrent: 1, scorer: { score: () => gate.then(() => SCORE) } });
  const a = pg.task(TID, 'ip1');
  const b = pg.task(TID, 'ip2');                       // dedupe: same id
  await new Promise(r => setTimeout(r, 0));
  await assert.rejects(pg.task(vid('Z'), 'ip3'), (e) => e.status === 503);
  release();
  const [ra, rb] = await Promise.all([a, b]);
  assert.strictEqual(ra, rb);
  assert.strictEqual(c.count('score'), 1);
});

test('ids that are not Taskcluster v4 slugids -> 400 before any I/O or charge', async () => {
  const ip = { allow: () => { throw new Error('must not charge'); } };
  const { pg, c } = setup({ ip });
  await assert.rejects(pg.task('A'.repeat(22), 'k'), (e) => e.status === 400);
  await assert.rejects(pg.group('A'.repeat(22), 'k'), (e) => e.status === 400);
  assert.strictEqual(c.calls.length, 0);
});

test('Taskcluster 400 -> 400', async () => {
  const e = new Error('InvalidRequestArguments');
  e.statusCode = 400;
  const { pg } = setup({ queue: { task: e } });
  await assert.rejects(pg.task(TID, 'k'), (err) => err.status === 400);
});

test('sanitizeTags keeps only bounded string values', () => {
  assert.deepStrictEqual(sanitizeTags({ a: 'x', b: 1, c: { d: 1 }, e: 'y'.repeat(600) }), { a: 'x' });
  assert.strictEqual(sanitizeTags(['a']), null);
  assert.strictEqual(Object.keys(sanitizeTags(Object.fromEntries(Array.from({ length: 100 }, (_, i) => [`k${i}`, 'v'])))).length, 64);
});

// ── group ──

function entry(taskId, created, { resolved = null, name = 'n', fam } = {}) {
  return {
    task: { created, taskQueueId: 'a/b', schedulerId: fam === 'other' ? 'gecko-level-3' : 'gecko-level-1', routes: [], metadata: { name } },
    status: { taskId, runs: resolved ? [{ runId: 0, scheduled: created, started: created, resolved, reasonResolved: 'completed' }] : [] },
  };
}

test('stored group ETA is returned without Taskcluster', async () => {
  const { pg, c } = setup({ storedGroup: { taskGroupId: GID, eta: { estimates: [{ estimator: 'B0' }] } } });
  const r = await pg.group(GID, 'k');
  assert.strictEqual(r.source, 'stored');
  assert.deepStrictEqual(c.calls.map(x => x[0]), ['getGroup']);
});

test('group: t_pred = decision resolved + 120s; tasks created later are not in the original graph', async () => {
  const t0 = '2026-11-20T10:00:00.000Z';
  const decResolved = '2026-11-20T10:05:00.000Z';
  const tasks = [entry(GID, t0, { resolved: decResolved })];
  for (let i = 0; i < 6; i++) tasks.push(entry(`T${String(i).padStart(21, '0')}`, '2026-11-20T10:06:00.000Z', { resolved: '2026-11-20T11:00:00.000Z' }));
  tasks.push(entry('L'.repeat(22), '2026-11-20T10:30:00.000Z'));
  const groupOut = { t0: Date.parse(t0) / 1000, nKnown: 7, repoFamily: 'try', model: { version: 'm' }, estimates: [{ estimator: 'B0', p50_s: 600, p90_s: 1200 }] };
  const { pg, c } = setup({ queue: { list: { tasks } }, scorer: { group: groupOut } });
  const r = await pg.group(GID, 'k');
  const [, body] = c.calls.find(x => x[0] === 'group');
  assert.strictEqual(body.tPred, Date.parse(decResolved) / 1000 + 120);
  assert.strictEqual(body.tasks.length, 8);
  assert.strictEqual(r.mode, 'decision-resolved');
  assert.strictEqual(r.eta.originalGraph.tasks, 7);
  assert.strictEqual(r.eta.originalGraph.state, 'done');
  assert.strictEqual(r.eta.estimates[0].finish_p50, '2026-11-20T10:10:00.000Z');
  assert.strictEqual(r.eta.recommended, 'B0');
  assert.strictEqual(r.status.tasks, 8);
});

test('group: pagination stops at the page cap; a truncated group is not estimated', async () => {
  const list = () => ({ tasks: [entry('P'.repeat(22), '2026-11-20T10:00:00Z')], continuationToken: 'more' });
  const { pg, c } = setup({ queue: { list } });
  const r = await pg.group(GID, 'k');
  assert.strictEqual(c.count('list'), GROUP_PAGE_LIMIT);
  assert.strictEqual(c.count('group'), 0, 'no scorer call: t_pred and the original graph are unknown');
  assert.strictEqual(r.truncated, true);
  assert.strictEqual(r.mode, 'truncated');
  assert.match(r.eta.skipped, /more than 10 tasks/);
  assert.deepStrictEqual(r.eta.estimates, []);
  assert.strictEqual(r.status.tasks, GROUP_PAGE_LIMIT);
});

test('group: entries are reduced to slim rows (no task definitions kept)', async () => {
  const big = entry(GID, '2026-11-20T10:00:00Z', { resolved: '2026-11-20T10:05:00Z' });
  big.task.payload = { env: { BLOB: 'x'.repeat(10000) } };
  const tasks = [big];
  for (let i = 0; i < 5; i++) tasks.push(entry(`S${String(i).padStart(21, '0')}`, '2026-11-20T10:01:00Z'));
  const { pg } = setup({ queue: { list: { tasks } }, scorer: { group: { skipped: 'x', estimates: [] } } });
  const r = await pg.group(GID, 'k');
  assert.ok(JSON.stringify(r).length < 5000);
  assert.strictEqual(r.mode, 'decision-resolved');
});

test('group: unknown in Taskcluster -> 404', async () => {
  const { pg } = setup({ queue: { list: notFound() } });
  await assert.rejects(pg.group(GID, 'k'), (e) => e.status === 404);
});

// ── HTTP wiring ──

function fakeRes() {
  return {
    status: null, headers: null, body: '',
    writeHead(s, h) { this.status = s; this.headers = h; },
    end(b) { this.body = b || ''; },
  };
}

test('handler: bad id -> 400 before playground; playground errors map to status + headers', async () => {
  let called = 0;
  const playground = {
    task: async () => { called++; throw new PlaygroundError(429, 'slow down', { 'Retry-After': '10' }); },
    group: async () => ({ source: 'computed' }),
  };
  const h = createHandler({ query: async () => { throw new Error('no db'); } }, { playground, log: { error() {}, log() {} } });
  const mk = (url) => ({ method: 'GET', url, headers: {}, socket: { remoteAddress: '1.1.1.1' } });

  let res = fakeRes();
  await h(mk('/api/v0/playground/task/%3Cscript%3E'), res);
  assert.strictEqual(res.status, 400);
  assert.strictEqual(called, 0);

  res = fakeRes();
  await h(mk(`/api/v0/playground/task/${TID}`), res);
  assert.strictEqual(res.status, 429);
  assert.strictEqual(res.headers['Retry-After'], '10');

  res = fakeRes();
  await h(mk(`/api/v0/playground/group/${GID}`), res);
  assert.strictEqual(res.status, 200);
  assert.strictEqual(res.headers['Cache-Control'], 'private, max-age=60');
  assert.strictEqual(res.headers['Content-Type'], 'application/json; charset=utf-8');
});

test('handler: playground disabled -> 503', async () => {
  const h = createHandler({ query: async () => ({ rows: [] }) }, { log: { error() {}, log() {} } });
  const res = fakeRes();
  await h({ method: 'GET', url: `/api/v0/playground/task/${TID}`, headers: {}, socket: {} }, res);
  assert.strictEqual(res.status, 503);
});
