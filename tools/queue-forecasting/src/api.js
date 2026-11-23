/**
 * Minimal read-only prediction API (v0, experimental).
 *
 *   GET /api/v0/task/:taskId        served + shadow per-run predictions
 *   GET /api/v0/group/:taskGroupId  shadow group ETA + per-task predictions
 *   GET /api/v0/health              liveness + latest prediction time
 *   GET /api/v0/playground/task/:taskId        stored, else on-demand (src/playground.js)
 *   GET /api/v0/playground/group/:taskGroupId  stored, else on-demand
 *
 * Served publicly behind Caddy (docker/Caddyfile: /api/* -> api:8080). Every
 * lookup is an index probe (pkeys, idx_qf_tasks_task_group_id); a per-IP token
 * bucket and a statement_timeout bound the load one client can put on the
 * production database. No writes, no new dependencies. The playground routes
 * add Taskcluster reads (anonymous) and calls to the internal scorer, behind a
 * much stricter compute budget; they never write either.
 */
import http from 'http';
import pg from 'pg';
import taskcluster from '@taskcluster/client';
import { fileURLToPath } from 'url';
import { createPlayground, createScorerClient, PlaygroundError } from './playground.js';

const PORT = parseInt(process.env.API_PORT || '8080', 10);
const DATABASE_URL = process.env.DATABASE_URL || 'postgresql://postgres@localhost:5433/forecasting';
const STATEMENT_TIMEOUT_MS = parseInt(process.env.API_STATEMENT_TIMEOUT_MS || '5000', 10);
const RATE_PER_S = parseFloat(process.env.API_RATE_PER_S || '10');
const RATE_BURST = parseFloat(process.env.API_RATE_BURST || '30');
export const GROUP_TASK_LIMIT = parseInt(process.env.API_GROUP_TASK_LIMIT || '5000', 10);
// Fixed configuration, never derived from a request (SSRF).
const PLAYGROUND_TC_ROOT_URL = process.env.PLAYGROUND_TC_ROOT_URL || 'https://firefox-ci-tc.services.mozilla.com';
const SCORER_URL = process.env.SCORER_URL || 'http://playground-scorer:8081';
const PLAYGROUND_PER_MIN = parseFloat(process.env.PLAYGROUND_COMPUTE_PER_MIN || '6');
const PLAYGROUND_BURST = parseFloat(process.env.PLAYGROUND_COMPUTE_BURST || '3');
const PLAYGROUND_GLOBAL_PER_MIN = parseFloat(process.env.PLAYGROUND_GLOBAL_PER_MIN || '60');
const PLAYGROUND_MAX_CONCURRENT = parseInt(process.env.PLAYGROUND_MAX_CONCURRENT || '2', 10);

// Taskcluster slugids: 22 chars of URL-safe base64.
const SLUGID_RE = /^[A-Za-z0-9_-]{22}$/;
export function isSlugid(s) {
  return typeof s === 'string' && SLUGID_RE.test(s);
}

const SHADOW_NOTE = 'Experimental shadow estimate: not used by Taskcluster scheduling; may change or disappear.';

// ─── Helpers ────────────────────────────────────────────────────────────────

function iso(ts) {
  return ts ? new Date(ts).toISOString() : null;
}

function plusSeconds(ts, ...secs) {
  if (!ts || secs.some(s => s == null || !Number.isFinite(Number(s)))) return null;
  const total = secs.reduce((a, s) => a + Number(s), 0);
  return new Date(new Date(ts).getTime() + total * 1000).toISOString();
}

function num(v) {
  return v == null ? null : Number(v);
}

function runState(r) {
  if (!r || r.run_id == null) return 'unscheduled';
  if (r.resolved_at) return r.reason_resolved || 'resolved';
  if (r.started_at) return 'running';
  return 'pending';
}

// Expected times from per-run quantiles. The finish p90 adds the wait and run
// p90s, which is conservative (a sum of quantiles, not a quantile of the sum).
// Once a phase has happened, its actual time replaces the estimate.
export function runPrediction(r) {
  const served = r.wait_p50_s != null || r.run_p50_s != null ? {
    wait_p50_s: num(r.wait_p50_s), wait_p90_s: num(r.wait_p90_s),
    run_p50_s: num(r.run_p50_s), run_p90_s: num(r.run_p90_s),
    wait_model_version: r.wait_model_version ?? null,
    run_model_version: r.duration_model_version ?? null,
    predicted_at: iso(r.predicted_at),
  } : null;
  const shadow = r.sh_wait_p50_s != null ? {
    wait_p50_s: num(r.sh_wait_p50_s), wait_p90_s: num(r.sh_wait_p90_s),
    wait_model_version: r.sh_wait_model_version ?? null,
    note: SHADOW_NOTE,
  } : null;

  let expected = null;
  if (served && r.pending_at) {
    const start50 = r.started_at ? iso(r.started_at) : plusSeconds(r.pending_at, served.wait_p50_s);
    const start90 = r.started_at ? iso(r.started_at) : plusSeconds(r.pending_at, served.wait_p90_s);
    expected = {
      start_p50: start50,
      start_p90: start90,
      finish_p50: r.resolved_at ? iso(r.resolved_at) : plusSeconds(start50, served.run_p50_s),
      finish_p90: r.resolved_at ? iso(r.resolved_at) : plusSeconds(start90, served.run_p90_s),
    };
  }
  return { served, shadow, expected };
}

// ─── Queries ────────────────────────────────────────────────────────────────

const RUN_COLUMNS = `
  r.run_id, r.pending_at, r.started_at, r.resolved_at, r.reason_resolved,
  r.wait_duration_s, r.run_duration_s,
  p.predicted_at, p.wait_p50_s, p.wait_p90_s, p.run_p50_s, p.run_p90_s,
  p.wait_model_version, p.duration_model_version,
  s.wait_p50_s AS sh_wait_p50_s, s.wait_p90_s AS sh_wait_p90_s,
  s.wait_model_version AS sh_wait_model_version`;

export async function getTask(pool, taskId) {
  const t = await pool.query(`
    SELECT task_id, task_group_id, task_queue_id, scheduler_id, project_id,
           metadata_name, normalized_name, repo_family, task_created, deadline,
           dependency_count
    FROM queue_forecast_tasks WHERE task_id = $1`, [taskId]);
  if (!t.rows.length) return null;
  const task = t.rows[0];
  const runs = await pool.query(`
    SELECT ${RUN_COLUMNS}
    FROM queue_forecast_task_runs r
    LEFT JOIN queue_forecast_run_predictions p USING (task_id, run_id)
    LEFT JOIN queue_forecast_run_predictions_shadow s USING (task_id, run_id)
    WHERE r.task_id = $1
    ORDER BY r.run_id`, [taskId]);
  return {
    taskId: task.task_id,
    taskGroupId: task.task_group_id,
    taskQueueId: task.task_queue_id,
    schedulerId: task.scheduler_id,
    projectId: task.project_id,
    name: task.metadata_name,
    normalizedName: task.normalized_name,
    repoFamily: task.repo_family,
    created: iso(task.task_created),
    deadline: iso(task.deadline),
    dependencyCount: task.dependency_count,
    runs: runs.rows.map(r => ({
      runId: r.run_id,
      state: runState(r),
      pending: iso(r.pending_at),
      started: iso(r.started_at),
      resolved: iso(r.resolved_at),
      actual: { wait_s: num(r.wait_duration_s), run_s: num(r.run_duration_s) },
      ...runPrediction(r),
    })),
  };
}

export async function getGroup(pool, taskGroupId) {
  // Only estimators still produced by the pipeline (predicted within 1h of the
  // newest prediction); retired ones (B1i, B1i*) keep old rows until retention.
  const est = await pool.query(`
    WITH live AS (
      SELECT estimator FROM queue_forecast_group_predictions_shadow
      GROUP BY estimator
      HAVING max(predicted_at) >= (SELECT max(predicted_at) FROM queue_forecast_group_predictions_shadow) - interval '1 hour'
    )
    SELECT estimator, t0, t_pred, p50_s, p90_s, n_known, repo_family, model_version, predicted_at
    FROM queue_forecast_group_predictions_shadow
    WHERE task_group_id = $1 AND estimator IN (SELECT estimator FROM live)
    ORDER BY estimator`, [taskGroupId]);
  const tPred = est.rows.length ? est.rows[0].t_pred : null;

  const summary = await pool.query(`
    SELECT count(*)::int AS n_tasks,
           min(t.task_created) AS first_created,
           max(t.task_created) AS last_created,
           count(*) FILTER (WHERE r.resolved_at IS NOT NULL)::int AS n_resolved,
           max(r.resolved_at) AS last_resolved,
           count(*) FILTER (WHERE t.task_created <= $2)::int AS orig_n,
           count(*) FILTER (WHERE t.task_created <= $2 AND r.resolved_at IS NULL)::int AS orig_unresolved,
           max(r.resolved_at) FILTER (WHERE t.task_created <= $2) AS orig_last_resolved
    FROM queue_forecast_tasks t
    LEFT JOIN LATERAL (
      SELECT resolved_at FROM queue_forecast_task_runs r
      WHERE r.task_id = t.task_id ORDER BY run_id DESC LIMIT 1
    ) r ON true
    WHERE t.task_group_id = $1`, [taskGroupId, tPred]);
  const sm = summary.rows[0];
  if (!sm.n_tasks && !est.rows.length) return null;

  const tasks = await pool.query(`
    SELECT t.task_id, t.metadata_name, t.task_queue_id, t.task_created, t.dependency_count,
           ${RUN_COLUMNS}
    FROM queue_forecast_tasks t
    LEFT JOIN LATERAL (
      SELECT * FROM queue_forecast_task_runs r
      WHERE r.task_id = t.task_id ORDER BY run_id DESC LIMIT 1
    ) r ON true
    LEFT JOIN queue_forecast_run_predictions p ON p.task_id = r.task_id AND p.run_id = r.run_id
    LEFT JOIN queue_forecast_run_predictions_shadow s ON s.task_id = r.task_id AND s.run_id = r.run_id
    WHERE t.task_group_id = $1
    ORDER BY t.task_created, t.task_id
    LIMIT $2`, [taskGroupId, GROUP_TASK_LIMIT + 1]);
  const truncated = tasks.rows.length > GROUP_TASK_LIMIT;
  const taskRows = truncated ? tasks.rows.slice(0, GROUP_TASK_LIMIT) : tasks.rows;

  const origDone = tPred != null && sm.orig_n > 0 && sm.orig_unresolved === 0;
  return {
    taskGroupId,
    repoFamily: est.rows[0]?.repo_family ?? null,
    status: {
      tasks: sm.n_tasks,
      resolved: sm.n_resolved,
      firstCreated: iso(sm.first_created),
      lastCreated: iso(sm.last_created),
      lastResolved: iso(sm.last_resolved),
    },
    eta: est.rows.length ? {
      note: SHADOW_NOTE,
      definition: 'Completion of the tasks that existed at predictionTime (the original graph), measured from t0 = earliest task_created.',
      t0: iso(est.rows[0].t0),
      predictionTime: iso(tPred),
      originalGraph: {
        tasks: sm.orig_n,
        state: origDone ? 'done' : 'running',
        actualFinish: origDone ? iso(sm.orig_last_resolved) : null,
      },
      estimates: est.rows.map(e => ({
        estimator: e.estimator,
        p50_s: num(e.p50_s),
        p90_s: num(e.p90_s),
        finish_p50: plusSeconds(e.t0, e.p50_s),
        finish_p90: plusSeconds(e.t0, e.p90_s),
        tasksKnown: e.n_known,
        modelVersion: e.model_version,
        predictedAt: iso(e.predicted_at),
      })),
    } : null,
    truncated,
    tasks: taskRows.map(r => ({
      taskId: r.task_id,
      name: r.metadata_name,
      taskQueueId: r.task_queue_id,
      created: iso(r.task_created),
      dependencyCount: r.dependency_count,
      latestRun: r.run_id == null ? null : {
        runId: r.run_id,
        state: runState(r),
        pending: iso(r.pending_at),
        started: iso(r.started_at),
        resolved: iso(r.resolved_at),
        ...runPrediction(r),
      },
    })),
  };
}

export async function getHealth(pool) {
  const { rows } = await pool.query(`
    SELECT (SELECT max(predicted_at) FROM queue_forecast_run_predictions) AS last_task_prediction,
           (SELECT max(predicted_at) FROM queue_forecast_group_predictions_shadow) AS last_group_prediction`);
  return {
    ok: true,
    lastTaskPrediction: iso(rows[0].last_task_prediction),
    lastGroupPrediction: iso(rows[0].last_group_prediction),
  };
}

// ─── Rate limiting ──────────────────────────────────────────────────────────

export function createRateLimiter({ ratePerS = RATE_PER_S, burst = RATE_BURST, now = () => Date.now() } = {}) {
  const buckets = new Map();
  function allow(key) {
    const t = now();
    let b = buckets.get(key);
    if (!b) { b = { tokens: burst, t }; buckets.set(key, b); }
    b.tokens = Math.min(burst, b.tokens + ((t - b.t) / 1000) * ratePerS);
    b.t = t;
    if (b.tokens < 1) return false;
    b.tokens -= 1;
    return true;
  }
  /** Give back a token taken by allow() for a request that was then refused. */
  function refund(key) {
    const b = buckets.get(key);
    if (b) b.tokens = Math.min(burst, b.tokens + 1);
  }
  function sweep() {
    const t = now();
    for (const [k, b] of buckets) if (t - b.t > 10 * 60 * 1000) buckets.delete(k);
  }
  return { allow, refund, sweep, size: () => buckets.size };
}

// Caddy overwrites X-Forwarded-For with the real peer unless trusted_proxies is
// configured, so the last entry is the client as Caddy saw it.
function clientKey(req) {
  const xff = req.headers['x-forwarded-for'];
  if (xff) {
    const parts = String(xff).split(',').map(s => s.trim()).filter(Boolean);
    if (parts.length) return parts[parts.length - 1];
  }
  return req.socket.remoteAddress || 'unknown';
}

// ─── HTTP ───────────────────────────────────────────────────────────────────

function send(res, status, body, extraHeaders = {}) {
  const payload = JSON.stringify(body, null, 2) + '\n';
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Access-Control-Allow-Origin': '*',
    'Cache-Control': status === 200 ? 'public, max-age=30' : 'no-store',
    'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'",
    'X-Content-Type-Options': 'nosniff',
    ...extraHeaders,
  });
  res.end(payload);
}

export function route(pathname) {
  const m = /^\/api\/v0\/(task|group)\/([^/]+)\/?$/.exec(pathname);
  if (m) return { kind: m[1], id: decodeURIComponentSafe(m[2]) };
  const pm = /^\/api\/v0\/playground\/(task|group)\/([^/]+)\/?$/.exec(pathname);
  if (pm) return { kind: pm[1], playground: true, id: decodeURIComponentSafe(pm[2]) };
  if (pathname === '/api/v0/health' || pathname === '/api/v0/health/') return { kind: 'health' };
  if (pathname === '/api/v0' || pathname === '/api/v0/' || pathname === '/api' || pathname === '/api/') return { kind: 'index' };
  return null;
}

function decodeURIComponentSafe(s) {
  try { return decodeURIComponent(s); } catch { return s; }
}

const INDEX = {
  name: 'queue-forecasting prediction API',
  version: 'v0',
  status: 'experimental',
  endpoints: {
    'GET /api/v0/task/{taskId}': 'served + shadow per-run wait/run predictions',
    'GET /api/v0/group/{taskGroupId}': `shadow group ETA + per-task predictions (latest run, up to ${GROUP_TASK_LIMIT} tasks)`,
    'GET /api/v0/health': 'liveness',
    'GET /api/v0/playground/task/{taskId}': 'stored prediction if any, else computed on demand (old or not-yet-pending tasks: hypothetical "if pending now"); strict per-IP compute limit',
    'GET /api/v0/playground/group/{taskGroupId}': 'stored group ETA if any, else computed on demand from Taskcluster; strict per-IP compute limit',
  },
  docs: '/status.html#api',
};

export function createHandler(pool, { limiter = createRateLimiter(), playground = null, log = console } = {}) {
  return async function handler(req, res) {
    const t0 = Date.now();
    let status = 500;
    try {
      if (req.method === 'OPTIONS') {
        res.writeHead(204, {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'GET, OPTIONS',
          'Access-Control-Max-Age': '86400',
        });
        status = 204;
        return res.end();
      }
      if (req.method !== 'GET' && req.method !== 'HEAD') {
        status = 405;
        return send(res, 405, { error: 'method not allowed' }, { Allow: 'GET, OPTIONS' });
      }
      if (!limiter.allow(clientKey(req))) {
        status = 429;
        return send(res, 429, { error: 'rate limited' }, { 'Retry-After': '1' });
      }
      const url = new URL(req.url, 'http://localhost');
      const r = route(url.pathname);
      if (!r) { status = 404; return send(res, 404, { error: 'not found', see: '/api/v0' }); }
      if (r.kind === 'index') { status = 200; return send(res, 200, INDEX); }
      if (r.kind === 'health') { status = 200; return send(res, 200, await getHealth(pool)); }
      if (!isSlugid(r.id)) {
        status = 400;
        return send(res, 400, { error: `invalid ${r.kind === 'task' ? 'taskId' : 'taskGroupId'}: expected a 22-character slugid` });
      }
      if (r.playground) {
        if (!playground) { status = 503; return send(res, 503, { error: 'playground disabled' }); }
        try {
          const body = r.kind === 'task'
            ? await playground.task(r.id, clientKey(req))
            : await playground.group(r.id, clientKey(req));
          status = 200;
          return send(res, 200, body, { 'Cache-Control': 'private, max-age=60' });
        } catch (err) {
          if (!(err instanceof PlaygroundError)) throw err;
          status = err.status;
          return send(res, err.status, { error: err.message }, err.headers);
        }
      }
      const body = r.kind === 'task' ? await getTask(pool, r.id) : await getGroup(pool, r.id);
      if (!body) {
        status = 404;
        return send(res, 404, { error: `${r.kind} not found`, note: 'Only tasks seen by the collector in the last 60 days are known.' });
      }
      status = 200;
      return send(res, 200, body);
    } catch (err) {
      log.error(`[api] ${req.method} ${req.url} failed: ${err.message}`);
      const timeout = /statement timeout/i.test(err.message);
      status = timeout ? 503 : 500;
      if (!res.headersSent) send(res, status, { error: timeout ? 'query timed out' : 'internal error' });
    } finally {
      if (status >= 500 || Date.now() - t0 > 1000) {
        log.log(`[api] ${req.method} ${req.url} ${status} ${Date.now() - t0}ms`);
      }
    }
  };
}

// ─── Main ───────────────────────────────────────────────────────────────────

function main() {
  const pool = new pg.Pool({
    connectionString: DATABASE_URL,
    max: 2,
    statement_timeout: STATEMENT_TIMEOUT_MS,
    application_name: 'qf-api',
  });
  pool.on('error', err => console.error('[api] pg pool error:', err.message));
  const limiter = createRateLimiter();
  const ipLimiter = createRateLimiter({ ratePerS: PLAYGROUND_PER_MIN / 60, burst: PLAYGROUND_BURST });
  const globalLimiter = createRateLimiter({ ratePerS: PLAYGROUND_GLOBAL_PER_MIN / 60, burst: PLAYGROUND_GLOBAL_PER_MIN / 4 });
  setInterval(() => { limiter.sweep(); ipLimiter.sweep(); }, 60 * 1000).unref();
  // Anonymous client: task/status/listTaskGroup/taskQueueCounts are public on
  // firefox-ci, and the api must not hold Taskcluster credentials.
  const queue = new taskcluster.Queue({ rootUrl: PLAYGROUND_TC_ROOT_URL, timeout: 5000, retries: 0 });
  // listTaskGroup pages are ~5 MB each (1000 full task definitions); measured
  // 0.8-3.8 s per page on firefox-ci (11-23), so they get a longer timeout
  // and one retry: a failed page wastes every page before it.
  const listQueue = new taskcluster.Queue({ rootUrl: PLAYGROUND_TC_ROOT_URL, timeout: 20000, retries: 1 });
  const playground = createPlayground({
    pool, queue, listQueue, scorer: createScorerClient(SCORER_URL),
    getTask, getGroup, runPrediction,
    ipLimiter, globalLimiter,
    ipRetryAfterS: Math.ceil(60 / PLAYGROUND_PER_MIN),
    globalRetryAfterS: Math.ceil(60 / PLAYGROUND_GLOBAL_PER_MIN),
    maxConcurrent: PLAYGROUND_MAX_CONCURRENT,
  });
  const server = http.createServer(createHandler(pool, { limiter, playground }));
  server.requestTimeout = 15000;
  server.headersTimeout = 10000;
  server.listen(PORT, () => console.log(`[api] listening on :${PORT}`));
  const stop = () => server.close(() => pool.end().then(() => process.exit(0)));
  process.on('SIGTERM', stop);
  process.on('SIGINT', stop);
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  main();
}
