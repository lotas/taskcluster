/**
 * Playground endpoints for the public API (wired in by src/api.js):
 *
 *   GET /api/v0/playground/task/:taskId
 *   GET /api/v0/playground/group/:taskGroupId
 *
 * A stored prediction is returned as is (source "stored"). Otherwise the
 * missing inputs are fetched from Taskcluster and the internal
 * playground-scorer computes a prediction, which is cached in memory for a few
 * minutes and never written to the database (source "computed").
 *
 * Abuse controls, in order: the api's global per-IP bucket (every request),
 * then for compute only a per-IP compute bucket, a global compute bucket, and a
 * concurrency gate. Stored answers and cache hits are not charged.
 *
 * SSRF: ids are validated slugids before any I/O; the Taskcluster root URL and
 * the scorer URL are fixed configuration; only Queue.task/status/listTaskGroup/
 * taskQueueCounts are called, without credentials; a taskQueueId taken from a
 * Taskcluster response is shape-checked before it is used in a request.
 */
import QuickLRU from 'quick-lru';
import { taskDefToEnrichment } from './task-enrichment.js';

export const WINDOW_DAYS = 59;            // retention is 60d; keep a day of margin
export const GROUP_PAGE_LIMIT = 10;       // listTaskGroup pages (<= 1000 tasks each)
export const GROUP_RESPONSE_TASKS = 5000; // tasks echoed back in a group response
// A computed group result echoes up to GROUP_RESPONSE_TASKS task rows (~1.5 MB),
// so groups get their own, much smaller cache than tasks.
export const GROUP_CACHE_SIZE = 32;
const PRED_DELAY_S = 120;                 // group_eta_baselines.PRED_DELAY_S
const FALLBACK_DELAY_S = 300;             // group_eta_baselines.FALLBACK_DELAY_S
// Taskcluster's own (v4) slugid pattern: the Queue rejects anything else with
// InvalidRequestArguments, so such ids can never exist. Checked before any
// I/O so they cost neither a DB lookup nor compute budget.
export const TC_SLUGID_RE = /^[A-Za-z0-9_-]{8}[Q-T][A-Za-z0-9_-][CGKOSWaeimquy26-][A-Za-z0-9_-]{10}[AQgw]$/;
const TASK_QUEUE_ID_RE = /^[A-Za-z0-9_-]{1,38}\/[A-Za-z0-9_-]{1,38}$/;
const DAY_MS = 86400 * 1000;

const HYPOTHETICAL_NOTE = 'Hypothetical: predicted as if the task went pending now, from the current queue state. ' +
  'It is not what the live predictor would have said at the time.';
const AT_PENDING_NOTE = 'Computed on demand at the run\'s real pending time. Baseline statistics are current, ' +
  'so this approximates (not reproduces) what the live predictor would have served.';
const STORED_NOTE = 'Stored prediction, made by the live predictor when the run went pending.';
const GROUP_NOTE = 'Experimental edge-free estimate (same estimators as the group-ETA shadow); computed on demand.';

export class PlaygroundError extends Error {
  constructor(status, message, headers = {}) {
    super(message);
    this.status = status;
    this.headers = headers;
  }
}

// ─── Small utilities ────────────────────────────────────────────────────────

export function createTtlCache({ maxSize = 1000, now = () => Date.now() } = {}) {
  const lru = new QuickLRU({ maxSize });
  return {
    get(key) {
      const e = lru.get(key);
      if (!e) return undefined;
      if (e.expires <= now()) { lru.delete(key); return undefined; }
      return e.value;
    },
    set(key, value, ttlMs) { lru.set(key, { value, expires: now() + ttlMs }); },
    size: () => lru.size,
  };
}

function iso(ts) {
  return ts ? new Date(ts).toISOString() : null;
}

function epoch(ts) {
  return ts ? new Date(ts).getTime() / 1000 : null;
}

function isNotFound(err) {
  return err && (err.statusCode === 404 || err.code === 'ResourceNotFound');
}

function tcError(err, what) {
  if (isNotFound(err)) return new PlaygroundError(404, `${what} not found in Taskcluster`);
  if (err && err.statusCode === 400) return new PlaygroundError(400, `Taskcluster rejected the ${what} id`);
  return new PlaygroundError(502, 'Taskcluster request failed; try again later');
}

// The scorer accepts at most 64 string->string tags of bounded length.
export function sanitizeTags(tags) {
  if (!tags || typeof tags !== 'object' || Array.isArray(tags)) return null;
  const out = {};
  let n = 0;
  for (const [k, v] of Object.entries(tags)) {
    if (n >= 64) break;
    if (typeof v !== 'string' || k.length > 128 || v.length > 512) continue;
    out[k] = v;
    n++;
  }
  return n ? out : null;
}

const clip = (s) => (typeof s === 'string' ? s.slice(0, 512) : null);

function runState(run) {
  if (!run) return 'unscheduled';
  if (run.resolved) return run.reasonResolved || run.state || 'resolved';
  if (run.started) return 'running';
  if (run.scheduled) return 'pending';
  return run.state || 'unscheduled';
}

function runsFromStatus(status) {
  return (status?.runs || []).map(r => ({
    runId: r.runId,
    state: runState(r),
    pending: iso(r.scheduled),
    started: iso(r.started),
    resolved: iso(r.resolved),
    actual: {
      wait_s: r.scheduled && r.started ? (Date.parse(r.started) - Date.parse(r.scheduled)) / 1000 : null,
      run_s: r.started && r.resolved ? (Date.parse(r.resolved) - Date.parse(r.started)) / 1000 : null,
    },
  }));
}

// Latest run + every column computePrediction needs (FETCH_ROW_SQL shape).
const TASK_ROW_SQL = `
SELECT t.task_id, t.task_group_id, t.task_queue_id, t.scheduler_id, t.metadata_name,
       t.normalized_name, t.max_run_time_s, t.tags, t.enriched_at, t.repo_family,
       t.original_priority,
       r.run_id, r.pending_at, r.queue_pending, r.priority_at_pending, r.started_at, r.resolved_at
FROM queue_forecast_tasks t
LEFT JOIN LATERAL (
  SELECT * FROM queue_forecast_task_runs r WHERE r.task_id = t.task_id ORDER BY run_id DESC LIMIT 1
) r ON true
WHERE t.task_id = $1`;

// ─── Playground ─────────────────────────────────────────────────────────────

/**
 * @param {object} deps
 * @param {object} deps.pool            pg pool (read-only use)
 * @param {object} deps.queue           Taskcluster Queue client (no credentials)
 * @param {object} deps.scorer          {score(body), group(body)} -> JSON
 * @param {Function} deps.getTask       api.js getTask
 * @param {Function} deps.getGroup      api.js getGroup
 * @param {Function} deps.runPrediction api.js runPrediction
 * @param {object} deps.ipLimiter       per-IP compute bucket ({allow(key)})
 * @param {object} deps.globalLimiter   global compute bucket
 * @param {number} [deps.maxConcurrent] computes in flight across all clients
 */
export function createPlayground({
  pool, queue, scorer, getTask, getGroup, runPrediction,
  listQueue = queue,
  ipLimiter, globalLimiter, ipRetryAfterS = 10, globalRetryAfterS = 5,
  maxConcurrent = 2, cache = createTtlCache(), groupCache = createTtlCache({ maxSize: GROUP_CACHE_SIZE }),
  computedTtlMs = 5 * 60 * 1000, notFoundTtlMs = 60 * 1000,
  now = () => Date.now(),
}) {
  let active = 0;
  const inFlight = new Map();

  /**
   * Charge the compute budget, run fn once per key, cache the outcome.
   * Admission order: the free concurrency check first, then the per-IP
   * bucket, then the global one (refunding the per-IP token if the global
   * bucket refuses), so a refused request never costs the client budget.
   */
  async function compute(cache, cacheKey, clientKey, fn) {
    const hit = cache.get(cacheKey);
    if (hit) return replay(hit);
    const pending = inFlight.get(cacheKey);
    if (pending) return replay(await pending);

    if (active >= maxConcurrent) {
      throw new PlaygroundError(503, 'playground is busy; retry shortly', { 'Retry-After': '2' });
    }
    if (!ipLimiter.allow(clientKey)) {
      throw new PlaygroundError(429, `compute rate limit: at most a few on-demand predictions per minute; retry in ${ipRetryAfterS}s`,
        { 'Retry-After': String(ipRetryAfterS) });
    }
    if (!globalLimiter.allow('global')) {
      ipLimiter.refund?.(clientKey);
      throw new PlaygroundError(429, `playground is busy; retry in ${globalRetryAfterS}s`, { 'Retry-After': String(globalRetryAfterS) });
    }
    active++;
    const p = (async () => {
      try {
        const body = await fn();
        const entry = { ok: true, body };
        cache.set(cacheKey, entry, computedTtlMs);
        return entry;
      } catch (err) {
        if (err instanceof PlaygroundError && (err.status === 404 || err.status === 400)) {
          const entry = { ok: false, status: err.status, message: err.message };
          cache.set(cacheKey, entry, notFoundTtlMs);
          return entry;
        }
        return { ok: false, error: err };
      } finally {
        active--;
        inFlight.delete(cacheKey);
      }
    })();
    inFlight.set(cacheKey, p);
    return replay(await p);
  }

  function replay(entry) {
    if (entry.ok) return entry.body;
    if (entry.error) throw entry.error;
    throw new PlaygroundError(entry.status, entry.message);
  }

  async function scorerCall(method, body) {
    try {
      return await scorer[method](body);
    } catch (err) {
      if (err.status === 503) throw new PlaygroundError(503, `scorer unavailable: ${err.message}`, { 'Retry-After': '5' });
      throw new PlaygroundError(503, 'scorer unavailable; try again later', { 'Retry-After': '5' });
    }
  }

  // ── Task ──────────────────────────────────────────────────────────────────

  function checkId(id, what) {
    if (!TC_SLUGID_RE.test(id)) throw new PlaygroundError(400, `invalid ${what}: not a Taskcluster slugid`);
  }

  async function task(taskId, clientKey) {
    checkId(taskId, 'taskId');
    const stored = await getTask(pool, taskId);
    const latest = stored?.runs?.at(-1);
    if (latest?.served) {
      return { source: 'stored', note: STORED_NOTE, ...stored };
    }
    return compute(cache, `task:${taskId}`, clientKey, () => computeTask(taskId, stored));
  }

  async function computeTask(taskId, stored) {
    const db = (await pool.query(TASK_ROW_SQL, [taskId])).rows[0] || null;
    const t = now();
    const inWindow = db?.pending_at && new Date(db.pending_at).getTime() >= t - WINDOW_DAYS * DAY_MS;

    // Task definition: the DB copy is enough when the collector enriched it.
    let enr = null;
    if (db?.enriched_at) {
      enr = {
        task_group_id: db.task_group_id, task_queue_id: db.task_queue_id, scheduler_id: db.scheduler_id,
        metadata_name: db.metadata_name, normalized_name: db.normalized_name, max_run_time_s: db.max_run_time_s,
        tags: db.tags, repo_family: db.repo_family, original_priority: db.original_priority,
      };
    } else {
      let taskDef;
      try { taskDef = await queue.task(taskId); } catch (err) { throw tcError(err, 'task'); }
      enr = taskDefToEnrichment(taskDef);
    }

    // Runs: the DB has them inside the window; otherwise ask Taskcluster.
    let runs = stored?.runs?.length ? stored.runs.map(r => ({
      runId: r.runId, state: r.state, pending: r.pending, started: r.started, resolved: r.resolved, actual: r.actual,
    })) : null;
    if (!inWindow) {
      try {
        runs = runsFromStatus((await queue.status(taskId)).status);
      } catch (err) {
        if (!runs) throw tcError(err, 'task');
      }
    }
    runs = runs || [];

    let mode, row, exclude, anchor;
    if (inWindow) {
      mode = 'at-pending';
      row = {
        pending_at: iso(db.pending_at), queue_pending: db.queue_pending == null ? null : Number(db.queue_pending),
        priority_at_pending: db.priority_at_pending, started_at: iso(db.started_at), resolved_at: iso(db.resolved_at),
      };
      exclude = { taskId, runId: db.run_id };
    } else {
      mode = 'hypothetical-now';
      let queuePending = null;
      if (typeof enr.task_queue_id === 'string' && TASK_QUEUE_ID_RE.test(enr.task_queue_id)) {
        try {
          queuePending = Number((await queue.taskQueueCounts(enr.task_queue_id)).pendingTasks);
          if (!Number.isFinite(queuePending)) queuePending = null;
        } catch { /* scored with the feature missing */ }
      }
      row = { pending_at: new Date(t).toISOString(), queue_pending: queuePending, priority_at_pending: clip(enr.original_priority) };
      exclude = null;
    }
    Object.assign(row, {
      task_queue_id: clip(enr.task_queue_id), scheduler_id: clip(enr.scheduler_id),
      metadata_name: clip(enr.metadata_name), normalized_name: clip(enr.normalized_name),
      max_run_time_s: enr.max_run_time_s == null ? null : Number(enr.max_run_time_s),
      tags: sanitizeTags(enr.tags), repo_family: enr.repo_family ?? null,
    });
    anchor = { pendingAt: row.pending_at, queuePending: row.queue_pending, priority: row.priority_at_pending };

    const s = await scorerCall('score', { row, exclude });
    const { served, shadow, expected } = runPrediction({
      wait_p50_s: s.wait.p50_s, wait_p90_s: s.wait.p90_s, run_p50_s: s.run.p50_s, run_p90_s: s.run.p90_s,
      wait_model_version: s.wait.modelVersion, duration_model_version: s.run.modelVersion,
      predicted_at: t,
      sh_wait_p50_s: s.shadowWait?.p50_s ?? null, sh_wait_p90_s: s.shadowWait?.p90_s ?? null,
      sh_wait_model_version: s.shadowWait?.modelVersion ?? null,
      pending_at: row.pending_at,
      started_at: mode === 'at-pending' ? row.started_at : null,
      resolved_at: mode === 'at-pending' ? row.resolved_at : null,
    });
    return {
      source: 'computed',
      mode,
      note: mode === 'at-pending' ? AT_PENDING_NOTE : HYPOTHETICAL_NOTE,
      taskId,
      taskGroupId: enr.task_group_id ?? null,
      taskQueueId: enr.task_queue_id ?? null,
      name: enr.metadata_name ?? null,
      repoFamily: enr.repo_family ?? null,
      anchor,
      prediction: { served, shadow, expected },
      runs,
      featureCoverage: s.featureCoverage,
      baselines: s.baselines,
      computedAt: new Date(t).toISOString(),
    };
  }

  // ── Group ─────────────────────────────────────────────────────────────────

  async function group(taskGroupId, clientKey) {
    checkId(taskGroupId, 'taskGroupId');
    const stored = await getGroup(pool, taskGroupId);
    if (stored?.eta?.estimates?.length) {
      return { source: 'stored', ...stored };
    }
    return compute(groupCache, `group:${taskGroupId}`, clientKey, () => computeGroup(taskGroupId));
  }

  // Reduce one listTaskGroup entry to what computeGroup uses, as each page
  // arrives: a page is ~5 MB of full task definitions, and keeping ten of them
  // alive per request (two requests at a time) would not fit the api's memory.
  function slimEntry({ task: def = {}, status = {} }) {
    const enr = taskDefToEnrichment(def, status);
    const runs = status.runs || [];
    const last = runs.at(-1);
    return {
      taskId: status.taskId,
      def: { metadata_name: enr.metadata_name, task_queue_id: enr.task_queue_id, repo_family: enr.repo_family, normalized_name: enr.normalized_name },
      created: epoch(def.created),
      lastResolved: runs.reduce((m, r) => (r.resolved && epoch(r.resolved) > m ? epoch(r.resolved) : m), null),
      resolved: Boolean(last?.resolved),
      latestRun: last ? { runId: last.runId, state: runState(last), pending: iso(last.scheduled), started: iso(last.started), resolved: iso(last.resolved) } : null,
    };
  }

  async function listGroup(taskGroupId) {
    const tasks = [];
    let token;
    let pages = 0;
    do {
      let res;
      try {
        res = await listQueue.listTaskGroup(taskGroupId, token ? { continuationToken: token, limit: 1000 } : { limit: 1000 });
      } catch (err) {
        throw tcError(err, 'task group');
      }
      for (const e of res.tasks || []) tasks.push(slimEntry(e));
      token = res.continuationToken;
      pages++;
    } while (token && pages < GROUP_PAGE_LIMIT);
    return { tasks, truncated: Boolean(token) };
  }

  async function computeGroup(taskGroupId) {
    const { tasks, truncated } = await listGroup(taskGroupId);
    if (!tasks.length) throw new PlaygroundError(404, 'task group has no tasks in Taskcluster');
    const t = now() / 1000;

    // t_pred as in group_eta_baselines.build_groups: decision task resolved
    // + 120s; no decision task -> t0 + 300s; decision task still running ->
    // now (hypothetical: more tasks may still be added).
    const t0 = Math.min(...tasks.map(x => x.created).filter(Number.isFinite));
    const dec = tasks.find(x => x.taskId === taskGroupId);
    let mode, tPred;
    if (truncated) { mode = 'truncated'; tPred = null; }
    else if (!dec) { mode = 'no-decision-task'; tPred = t0 + FALLBACK_DELAY_S; }
    else if (dec.lastResolved != null) { mode = 'decision-resolved'; tPred = dec.lastResolved + PRED_DELAY_S; }
    else { mode = 'hypothetical-now'; tPred = t; }

    // Past the page cap neither the decision task nor the original graph is
    // known (tasks after the cap may predate t_pred), so any estimate would be
    // anchored wrongly and undercount nKnown. Refuse rather than guess.
    const g = truncated ? {
      t0: null, repoFamily: null,
      skipped: `the group has more than ${tasks.length} tasks, past the on-demand limit, so its decision task and ` +
        'original graph are unknown and no estimate is made',
    } : await scorerCall('group', {
      tPred,
      tasks: tasks.filter(x => Number.isFinite(x.created))
        .map(x => ({ created: x.created, repoFamily: clip(x.def.repo_family), normalizedName: clip(x.def.normalized_name) })),
    });

    const orig = truncated ? [] : tasks.filter(x => Number.isFinite(x.created) && x.created <= tPred);
    const origDone = orig.length > 0 && orig.every(x => x.resolved);
    const plus = (s) => (g.t0 != null && Number.isFinite(s) ? new Date((g.t0 + s) * 1000).toISOString() : null);
    const sorted = [...tasks].sort((a, b) => (a.created ?? 0) - (b.created ?? 0) || String(a.taskId).localeCompare(String(b.taskId)));
    return {
      source: 'computed',
      mode,
      taskGroupId,
      repoFamily: g.repoFamily ?? null,
      status: {
        tasks: tasks.length,
        resolved: tasks.filter(x => x.resolved).length,
        firstCreated: iso(t0 * 1000),
        lastResolved: (() => {
          const m = Math.max(...tasks.map(x => x.lastResolved ?? -Infinity));
          return Number.isFinite(m) ? iso(m * 1000) : null;
        })(),
      },
      eta: g.skipped ? { note: GROUP_NOTE, skipped: g.skipped, estimates: [] } : {
        note: GROUP_NOTE + (mode === 'hypothetical-now' ? ' The decision task has not resolved, so the graph may still grow.' : ''),
        definition: 'Completion of the tasks that existed at predictionTime (the original graph), measured from t0 = earliest task created.',
        t0: iso(g.t0 * 1000),
        predictionTime: iso(tPred * 1000),
        originalGraph: {
          tasks: orig.length,
          state: origDone ? 'done' : 'running',
          actualFinish: origDone ? iso(Math.max(...orig.map(x => x.lastResolved)) * 1000) : null,
        },
        estimates: g.estimates.map(e => ({
          estimator: e.estimator, p50_s: e.p50_s, p90_s: e.p90_s,
          finish_p50: plus(e.p50_s), finish_p90: plus(e.p90_s),
          tasksKnown: g.nKnown, modelVersion: g.model?.version ?? null,
        })),
      },
      truncated: truncated || sorted.length > GROUP_RESPONSE_TASKS,
      tasks: sorted.slice(0, GROUP_RESPONSE_TASKS).map(x => ({
        taskId: x.taskId, name: x.def.metadata_name ?? null, taskQueueId: x.def.task_queue_id ?? null,
        created: iso(x.created * 1000), latestRun: x.latestRun,
      })),
      computedAt: new Date(t * 1000).toISOString(),
    };
  }

  return { task, group, stats: () => ({ active, inFlight: inFlight.size, cached: cache.size() }) };
}

/** HTTP client for the internal scorer; errors carry .status. */
export function createScorerClient(baseUrl, { timeoutMs = 15000, fetchImpl = fetch } = {}) {
  async function post(path, body) {
    const res = await fetchImpl(new URL(path, baseUrl), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
      redirect: 'error',
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(json.error || `scorer HTTP ${res.status}`);
      err.status = res.status;
      throw err;
    }
    return json;
  }
  return { score: (b) => post('/score', b), group: (b) => post('/group', b) };
}
