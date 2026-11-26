import { test } from 'node:test';
import assert from 'node:assert';
import {
  isSlugid, route, runPrediction, createRateLimiter, createHandler, GROUP_TASK_LIMIT,
} from '../src/api.js';

const TID = 'abcdefghijklmnopqrstuv'; // 22 chars

function fakeRes() {
  return {
    status: null, headers: null, body: '', headersSent: false,
    writeHead(s, h) { this.status = s; this.headers = h; this.headersSent = true; },
    end(b) { if (b) this.body += b; },
    json() { return JSON.parse(this.body); },
  };
}

function fakeReq(url, method = 'GET', ip = '1.2.3.4') {
  return { url, method, headers: {}, socket: { remoteAddress: ip } };
}

// Pool stub: picks a canned result by matching a substring of the SQL.
function fakePool(handlers) {
  const calls = [];
  return {
    calls,
    async query(sql, params) {
      calls.push({ sql, params });
      for (const [needle, fn] of handlers) {
        if (sql.includes(needle)) return { rows: typeof fn === 'function' ? fn(params) : fn };
      }
      throw new Error(`unexpected query: ${sql.slice(0, 80)}`);
    },
  };
}

const quietLog = { log() {}, error() {} };

test('isSlugid accepts 22-char url-safe ids only', () => {
  assert.ok(isSlugid(TID));
  assert.ok(isSlugid('A-_0123456789abcdefghi'));
  assert.ok(!isSlugid('short'));
  assert.ok(!isSlugid(TID + 'x'));
  assert.ok(!isSlugid("abcdefghijklmnopqrst';"));
  assert.ok(!isSlugid(undefined));
});

test('route maps paths', () => {
  assert.deepStrictEqual(route(`/api/v0/task/${TID}`), { kind: 'task', id: TID });
  assert.deepStrictEqual(route(`/api/v0/group/${TID}/`), { kind: 'group', id: TID });
  assert.deepStrictEqual(route('/api/v0/health'), { kind: 'health' });
  assert.deepStrictEqual(route('/api/v0'), { kind: 'index' });
  assert.strictEqual(route('/api/v1/task/x'), null);
  assert.strictEqual(route(`/api/v0/task/${TID}/extra`), null);
});

test('runPrediction: pending run gets expected times from served quantiles', () => {
  const p = runPrediction({
    pending_at: '2026-11-20T10:00:00Z',
    wait_p50_s: 60, wait_p90_s: 600, run_p50_s: 120, run_p90_s: 1200,
  });
  assert.strictEqual(p.expected.start_p50, '2026-11-20T10:01:00.000Z');
  assert.strictEqual(p.expected.start_p90, '2026-11-20T10:10:00.000Z');
  assert.strictEqual(p.expected.finish_p50, '2026-11-20T10:03:00.000Z');
  assert.strictEqual(p.expected.finish_p90, '2026-11-20T10:30:00.000Z');
  assert.strictEqual(p.shadow, null);
});

test('runPrediction: actual times replace estimates once known', () => {
  const p = runPrediction({
    pending_at: '2026-11-20T10:00:00Z', started_at: '2026-11-20T10:05:00Z',
    wait_p50_s: 60, wait_p90_s: 600, run_p50_s: 120, run_p90_s: 1200,
    sh_wait_p50_s: 30, sh_wait_p90_s: 300,
  });
  assert.strictEqual(p.expected.start_p50, '2026-11-20T10:05:00.000Z');
  assert.strictEqual(p.expected.finish_p50, '2026-11-20T10:07:00.000Z');
  assert.strictEqual(p.shadow.wait_p50_s, 30);
  assert.match(p.shadow.note, /shadow/i);
});

test('runPrediction: no served prediction -> null expected', () => {
  const p = runPrediction({ pending_at: '2026-11-20T10:00:00Z' });
  assert.strictEqual(p.served, null);
  assert.strictEqual(p.expected, null);
});

test('rate limiter refills over time', () => {
  let t = 0;
  const rl = createRateLimiter({ ratePerS: 1, burst: 2, now: () => t });
  assert.ok(rl.allow('a'));
  assert.ok(rl.allow('a'));
  assert.ok(!rl.allow('a'));
  assert.ok(rl.allow('b'));
  t = 1000;
  assert.ok(rl.allow('a'));
  assert.ok(!rl.allow('a'));
  t = 20 * 60 * 1000;
  rl.sweep();
  assert.strictEqual(rl.size(), 0);
});

test('handler: bad id -> 400 without touching the db', async () => {
  const pool = fakePool([]);
  const res = fakeRes();
  await createHandler(pool, { log: quietLog })(fakeReq('/api/v0/task/nope'), res);
  assert.strictEqual(res.status, 400);
  assert.strictEqual(pool.calls.length, 0);
  assert.strictEqual(res.headers['Access-Control-Allow-Origin'], '*');
});

test('handler: unknown task -> 404', async () => {
  const pool = fakePool([['FROM queue_forecast_tasks WHERE task_id', []]]);
  const res = fakeRes();
  await createHandler(pool, { log: quietLog })(fakeReq(`/api/v0/task/${TID}`), res);
  assert.strictEqual(res.status, 404);
});

test('handler: task with runs -> 200 with predictions', async () => {
  const pool = fakePool([
    ['FROM queue_forecast_tasks WHERE task_id', [{
      task_id: TID, task_group_id: TID, task_queue_id: 'gecko-t/t-linux', metadata_name: 'test-x',
      task_created: '2026-11-20T09:59:00Z', repo_family: 'try',
    }]],
    ['FROM queue_forecast_task_runs r', [{
      run_id: 0, pending_at: '2026-11-20T10:00:00Z', wait_p50_s: 60, wait_p90_s: 600,
      run_p50_s: 120, run_p90_s: 1200, sh_wait_p50_s: 45, sh_wait_p90_s: 500,
    }]],
  ]);
  const res = fakeRes();
  await createHandler(pool, { log: quietLog })(fakeReq(`/api/v0/task/${TID}`), res);
  assert.strictEqual(res.status, 200);
  const b = res.json();
  assert.strictEqual(b.taskGroupId, TID);
  assert.strictEqual(b.runs[0].state, 'pending');
  assert.strictEqual(b.runs[0].served.wait_p90_s, 600);
  assert.strictEqual(b.runs[0].shadow.wait_p50_s, 45);
  assert.deepStrictEqual(pool.calls[1].params, [TID]);
});

test('handler: group -> eta, original-graph status, truncation', async () => {
  const many = Array.from({ length: GROUP_TASK_LIMIT + 1 }, (_, i) => ({
    task_id: `t${i}`, task_created: '2026-11-20T10:00:00Z', run_id: null,
  }));
  const pool = fakePool([
    ['FROM queue_forecast_group_predictions_shadow', [{
      estimator: 'B1c*', t0: '2026-11-20T10:00:00Z', t_pred: '2026-11-20T10:10:00Z',
      p50_s: 3600, p90_s: 7200, n_known: 50, repo_family: 'try', model_version: 'v1',
    }]],
    ['count(*)::int AS n_tasks', [{
      n_tasks: 60, n_resolved: 60, orig_n: 50, orig_unresolved: 0,
      orig_last_resolved: '2026-11-20T11:30:00Z', first_created: '2026-11-20T10:00:00Z',
    }]],
    ['ORDER BY t.task_created', many],
  ]);
  const res = fakeRes();
  await createHandler(pool, { log: quietLog })(fakeReq(`/api/v0/group/${TID}`), res);
  assert.strictEqual(res.status, 200);
  const b = res.json();
  assert.strictEqual(b.eta.estimates[0].finish_p50, '2026-11-20T11:00:00.000Z');
  assert.strictEqual(b.eta.recommended, 'B1c*'); // B1c not live here -> next preference
  assert.strictEqual(b.eta.originalGraph.state, 'done');
  assert.strictEqual(b.eta.originalGraph.actualFinish, '2026-11-20T11:30:00.000Z');
  assert.strictEqual(b.truncated, true);
  assert.strictEqual(b.tasks.length, GROUP_TASK_LIMIT);
  assert.strictEqual(b.tasks[0].latestRun, null);
  // B2* in-flight shadow rows are excluded from the API
  const shadowQ = pool.calls.find(c => c.sql.includes('FROM queue_forecast_group_predictions_shadow'));
  assert.match(shadowQ.sql, /left\(estimator, length\(\$2\)\) <> \$2/);
  assert.deepStrictEqual(shadowQ.params, [TID, 'B2*+']);
});

test('handler: unknown group -> 404', async () => {
  const pool = fakePool([
    ['FROM queue_forecast_group_predictions_shadow', []],
    ['count(*)::int AS n_tasks', [{ n_tasks: 0 }]],
  ]);
  const res = fakeRes();
  await createHandler(pool, { log: quietLog })(fakeReq(`/api/v0/group/${TID}`), res);
  assert.strictEqual(res.status, 404);
});

test('handler: rate limit -> 429, POST -> 405, db error -> 500', async () => {
  const limiter = createRateLimiter({ ratePerS: 0, burst: 1 });
  const pool = fakePool([['max(predicted_at)', [{}]]]);
  const h = createHandler(pool, { limiter, log: quietLog });
  let res = fakeRes(); await h(fakeReq('/api/v0/health'), res);
  assert.strictEqual(res.status, 200);
  res = fakeRes(); await h(fakeReq('/api/v0/health'), res);
  assert.strictEqual(res.status, 429);
  res = fakeRes(); await h(fakeReq('/api/v0/health', 'POST'), res);
  assert.strictEqual(res.status, 405);

  const bad = createHandler({ query: async () => { throw new Error('canceling statement due to statement timeout'); } }, { log: quietLog });
  res = fakeRes(); await bad(fakeReq(`/api/v0/task/${TID}`), res);
  assert.strictEqual(res.status, 503);
});
