/**
 * Playground scorer: on-demand, store-nothing predictions for the dashboard
 * playground (src/playground.js in the api).
 *
 *   POST /score  {row, exclude?}       served wait + run, shadow wait (computePrediction)
 *   POST /group  {tPred, tasks}        edge-free group ETA (B0, B1c, B1c*)
 *   GET  /health
 *
 * Internal only: the compose service publishes no port and Caddy never proxies
 * here; the api is the sole client and does all input validation, rate
 * limiting and Taskcluster access. This process has no Taskcluster
 * credentials and never writes to the database.
 *
 * Kept apart from live-predictor so public traffic can never slow or crash the
 * service that writes served predictions. It holds its own BaselineStats,
 * refreshed every 6h (not hourly) to keep the extra DB load small.
 */
import fs from 'fs';
import http from 'http';
import path from 'path';
import pg from 'pg';
import { fileURLToPath } from 'url';
import { findLatestModelDir, loadBundle } from './live-predictor/model-loader.js';
import { BaselineStats } from './live-predictor/baseline-stats.js';
import { computePrediction, scoreWait } from './live-predictor/predict.js';
import { predictGroup, validateModel } from './scorer/group-eta.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = path.join(__dirname, '..');
const MODELS_ROOT = path.join(PROJECT_ROOT, 'trainer', 'data', 'models');
const GROUP_MODEL = process.env.GROUP_ETA_MODEL || path.join(PROJECT_ROOT, 'trainer', 'data', 'group_eta', 'live_model.json');

const PORT = parseInt(process.env.SCORER_PORT || '8081', 10);
const WAIT_STEM = 'wait_time_residual_throughput_filtered_baseline';
const DURATION_STEM = 'run_duration_residual';
const SHADOW_WAIT_STEM = process.env.SHADOW_WAIT_STEM || null;
const STATS_REFRESH_MS = parseInt(process.env.SCORER_STATS_REFRESH_MS || String(6 * 3600 * 1000), 10);
const STATS_RETRY_BASE_MS = 60 * 1000;      // first-refresh retry backoff: 1, 2, 4 ... min
const STATS_RETRY_MAX_MS = 30 * 60 * 1000;
const MAX_CONCURRENT = parseInt(process.env.SCORER_MAX_CONCURRENT || '2', 10);
const MAX_QUEUED = parseInt(process.env.SCORER_MAX_QUEUED || '4', 10);
const SCORE_BODY_MAX = 64 * 1024;
const GROUP_BODY_MAX = 4 * 1024 * 1024;
export const GROUP_TASKS_MAX = 10000;

// ─── Input validation ───────────────────────────────────────────────────────

const STR_MAX = 512;
const ROW_FIELDS = {
  task_queue_id: 'str', scheduler_id: 'str', metadata_name: 'str', normalized_name: 'str',
  repo_family: 'str', priority_at_pending: 'str',
  max_run_time_s: 'num', queue_pending: 'num',
  pending_at: 'time', started_at: 'time', resolved_at: 'time',
  tags: 'tags',
};

export class BadRequest extends Error {}

function check(cond, msg) { if (!cond) throw new BadRequest(msg); }

function isPlainObject(v) {
  return v != null && typeof v === 'object' && !Array.isArray(v);
}

/** Validate a /score row; returns a fresh object with only known fields. */
export function validateRow(row) {
  check(isPlainObject(row), 'row must be an object');
  const out = {};
  for (const [k, v] of Object.entries(row)) {
    const type = Object.hasOwn(ROW_FIELDS, k) ? ROW_FIELDS[k] : null;
    check(type, `unknown row field ${k}`);
    if (v == null) { out[k] = null; continue; }
    if (type === 'str') {
      check(typeof v === 'string' && v.length <= STR_MAX, `${k} must be a string`);
      out[k] = v;
    } else if (type === 'num') {
      check(typeof v === 'number' && Number.isFinite(v), `${k} must be a finite number`);
      out[k] = v;
    } else if (type === 'time') {
      check(typeof v === 'string' && v.length <= 40 && Number.isFinite(Date.parse(v)), `${k} must be an ISO timestamp`);
      out[k] = new Date(v);
    } else if (type === 'tags') {
      check(isPlainObject(v), 'tags must be an object');
      const entries = Object.entries(v);
      check(entries.length <= 64, 'too many tags');
      const tags = {};
      for (const [tk, tv] of entries) {
        check(tk.length <= 128 && typeof tv === 'string' && tv.length <= STR_MAX, 'tags must map strings to strings');
        tags[tk] = tv;
      }
      out.tags = tags;
    }
  }
  check(out.pending_at instanceof Date, 'row.pending_at is required');
  return out;
}

const SLUGID_RE = /^[A-Za-z0-9_-]{22}$/;

export function validateExclude(ex) {
  if (ex == null) return { taskId: null, runId: null };
  check(isPlainObject(ex), 'exclude must be an object');
  check(ex.taskId == null || (typeof ex.taskId === 'string' && SLUGID_RE.test(ex.taskId)), 'exclude.taskId must be a slugid');
  check(ex.runId == null || (Number.isInteger(ex.runId) && ex.runId >= 0 && ex.runId < 1000), 'exclude.runId must be a small integer');
  return { taskId: ex.taskId ?? null, runId: ex.runId ?? null };
}

export function validateGroupBody(body) {
  check(isPlainObject(body), 'body must be an object');
  check(typeof body.tPred === 'number' && Number.isFinite(body.tPred), 'tPred must be epoch seconds');
  check(Array.isArray(body.tasks) && body.tasks.length <= GROUP_TASKS_MAX, `tasks must be an array of at most ${GROUP_TASKS_MAX}`);
  return {
    tPred: body.tPred,
    tasks: body.tasks.map((t) => {
      check(isPlainObject(t), 'task must be an object');
      check(typeof t.created === 'number' && Number.isFinite(t.created), 'task.created must be epoch seconds');
      for (const k of ['repoFamily', 'normalizedName']) {
        check(t[k] == null || (typeof t[k] === 'string' && t[k].length <= STR_MAX), `task.${k} must be a string`);
      }
      return { created: t.created, repoFamily: t.repoFamily ?? null, normalizedName: t.normalizedName ?? null };
    }),
  };
}

// ─── Scoring ────────────────────────────────────────────────────────────────

function finiteOrNull(x) {
  return Number.isFinite(x) ? x : null;
}

function baselineSummary(b) {
  return b ? { level: b.level, p50_s: finiteOrNull(Number(b.p50)), p90_s: finiteOrNull(Number(b.p90)), sampleSize: b.sample_size } : null;
}

export async function score({ pool, bundles, stats }, row, exclude) {
  const out = await computePrediction({
    pool, bundles, baselineStats: stats, row,
    excludeTaskId: exclude.taskId, excludeRunId: exclude.runId,
  });
  let shadowWait = null;
  if (bundles.shadowWait) {
    try {
      const sh = await scoreWait(bundles.shadowWait, row, out.liveFeatures, out.waitBaseline);
      let missing = 0;
      for (const x of sh.vec) if (!Number.isFinite(x)) missing++;
      shadowWait = {
        p50_s: finiteOrNull(sh.p50), p90_s: finiteOrNull(sh.p90),
        modelVersion: bundles.shadowWait.schema.model_version,
        featureCoverage: { total: sh.vec.length, missing },
      };
    } catch (err) {
      console.error('[playground-scorer] shadow wait failed:', err.message);
    }
  }
  return {
    wait: {
      p50_s: finiteOrNull(out.wait.p50), p90_s: finiteOrNull(out.wait.p90),
      p90GuardrailApplied: out.wait.guardrail.applied,
      modelVersion: bundles.wait.schema.model_version,
    },
    run: {
      p50_s: finiteOrNull(out.run.p50), p90_s: finiteOrNull(out.run.p90),
      p90GuardrailApplied: out.guardrail.applied,
      modelVersion: bundles.duration.schema.model_version,
    },
    shadowWait,
    baselines: { wait: baselineSummary(out.waitBaseline), duration: baselineSummary(out.durationBaseline) },
    featureCoverage: out.featureCoverage,
  };
}

/** Reloads the group JSON when its mtime changes (checked at most once a minute). */
export function createGroupModelLoader(file, { now = () => Date.now(), checkEveryMs = 60 * 1000 } = {}) {
  let model = null;
  let mtime = 0;
  let checkedAt = -Infinity;
  return function get() {
    const t = now();
    if (t - checkedAt >= checkEveryMs) {
      checkedAt = t;
      try {
        const st = fs.statSync(file);
        if (st.mtimeMs !== mtime) {
          model = validateModel(JSON.parse(fs.readFileSync(file, 'utf8')));
          mtime = st.mtimeMs;
          console.log(`[playground-scorer] group model ${model.version} loaded`);
        }
      } catch (err) {
        console.error(`[playground-scorer] group model load failed (${file}):`, err.message);
      }
    }
    return model;
  };
}

// ─── HTTP ───────────────────────────────────────────────────────────────────

function send(res, status, body) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(body));
}

function readJson(req, max) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on('data', (c) => {
      size += c.length;
      if (size > max) { reject(new BadRequest('body too large')); req.destroy(); return; }
      chunks.push(c);
    });
    req.on('end', () => {
      try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); } catch { reject(new BadRequest('invalid JSON')); }
    });
    req.on('error', reject);
  });
}

export function createGate(maxConcurrent = MAX_CONCURRENT, maxQueued = MAX_QUEUED) {
  let active = 0;
  const waiting = [];
  return {
    /** Resolves to a release fn, or null when the queue is full. */
    async enter() {
      if (active < maxConcurrent) { active++; return release; }
      if (waiting.length >= maxQueued) return null;
      // release() hands its slot straight to us (active is not decremented),
      // so no enter() can slip in between the wake-up and this continuation.
      await new Promise((r) => waiting.push(r));
      return release;
    },
    stats: () => ({ active, queued: waiting.length }),
  };
  function release() {
    const next = waiting.shift();
    if (next) next();
    else active--;
  }
}

export function createScorerHandler(ctx, { gate = createGate(), groupModel } = {}) {
  return async function handler(req, res) {
    try {
      if (req.method === 'GET' && req.url === '/health') {
        const gm = groupModel();
        return send(res, 200, {
          ok: true, statsReady: ctx.statsReady(), gate: gate.stats(),
          models: {
            wait: ctx.bundles.wait.schema.model_version,
            duration: ctx.bundles.duration.schema.model_version,
            shadowWait: ctx.bundles.shadowWait?.schema.model_version ?? null,
            group: gm?.version ?? null,
          },
        });
      }
      if (req.method !== 'POST' || (req.url !== '/score' && req.url !== '/group')) {
        return send(res, 404, { error: 'not found' });
      }
      const body = await readJson(req, req.url === '/score' ? SCORE_BODY_MAX : GROUP_BODY_MAX);
      if (req.url === '/group') {
        const { tPred, tasks } = validateGroupBody(body);
        const gm = groupModel();
        if (!gm) return send(res, 503, { error: 'group model unavailable' });
        return send(res, 200, { model: { version: gm.version, trainedBefore: gm.trained_before }, ...predictGroup(gm, tPred, tasks) });
      }
      check(isPlainObject(body), 'body must be an object');
      const row = validateRow(body.row);
      const exclude = validateExclude(body.exclude);
      if (!ctx.statsReady()) return send(res, 503, { error: 'baseline stats not loaded yet' });
      const release = await gate.enter();
      if (!release) return send(res, 503, { error: 'scorer busy' });
      try {
        return send(res, 200, await score(ctx, row, exclude));
      } finally {
        release();
      }
    } catch (err) {
      if (err instanceof BadRequest) return send(res, 400, { error: err.message });
      console.error(`[playground-scorer] ${req.method} ${req.url} failed:`, err.message);
      const timeout = /statement timeout/i.test(err.message);
      if (!res.headersSent) send(res, timeout ? 503 : 500, { error: timeout ? 'query timed out' : 'internal error' });
    }
  };
}

// ─── Main ───────────────────────────────────────────────────────────────────

async function main() {
  const databaseUrl = process.env.DATABASE_URL;
  if (!databaseUrl) throw new Error('DATABASE_URL is required');

  const modelDir = findLatestModelDir(MODELS_ROOT, [WAIT_STEM, DURATION_STEM]);
  if (!modelDir) throw new Error(`No complete model directories under ${MODELS_ROOT}`);
  const [wait, duration, shadowWait] = await Promise.all([
    loadBundle(modelDir, WAIT_STEM),
    loadBundle(modelDir, DURATION_STEM),
    SHADOW_WAIT_STEM ? loadBundle(modelDir, SHADOW_WAIT_STEM).catch((err) => {
      console.error(`[playground-scorer] shadow ${SHADOW_WAIT_STEM} disabled:`, err.message);
      return null;
    }) : null,
  ]);
  const bundles = { wait, duration, shadowWait };
  console.log(`[playground-scorer] models from ${modelDir}: wait ${wait.schema.model_version}, ` +
    `duration ${duration.schema.model_version}, shadow ${shadowWait?.schema.model_version ?? 'off'}`);

  // Request pool: short statement_timeout bounds what one request can cost.
  // Stats pool: the hierarchical percentile refresh is legitimately slow.
  const pool = new pg.Pool({ connectionString: databaseUrl, max: 2, statement_timeout: 5000, application_name: 'qf-playground-scorer' });
  const statsPool = new pg.Pool({ connectionString: databaseUrl, max: 2, statement_timeout: 15 * 60 * 1000, application_name: 'qf-playground-scorer-stats' });
  for (const p of [pool, statsPool]) p.on('error', (err) => console.error('[playground-scorer] pg pool error:', err.message));

  const filterFromSchema = (schema) => {
    const af = schema?.anomaly_filter;
    return Boolean(af && af.enabled && ['baseline', 'both'].includes(af.mode));
  };
  const stats = new BaselineStats(statsPool, {
    waitFilterAnomalous: filterFromSchema(wait.schema),
    durationFilterAnomalous: filterFromSchema(duration.schema),
    refreshIntervalMs: STATS_REFRESH_MS,
  });
  let statsReady = false;
  const ctx = { pool, bundles, stats, statsReady: () => statsReady };

  const groupModel = createGroupModelLoader(GROUP_MODEL);
  groupModel();
  const server = http.createServer(createScorerHandler(ctx, { groupModel }));
  server.requestTimeout = 20000;
  server.headersTimeout = 10000;
  server.listen(PORT, () => console.log(`[playground-scorer] listening on :${PORT}`));

  let stopping = false;
  const stop = () => {
    stopping = true;
    stats.stopPeriodicRefresh();
    server.close(() => Promise.all([pool.end(), statsPool.end()]).then(() => process.exit(0)));
  };
  process.on('SIGTERM', stop);
  process.on('SIGINT', stop);

  // Serve /group and /health while the (slow) first stats refresh runs. A
  // failed refresh is retried in-process with backoff (/score stays 503):
  // exiting would make `restart: unless-stopped` re-run the heavy percentile
  // queries against production back to back.
  for (let attempt = 0; !stopping; attempt++) {
    const t0 = Date.now();
    try {
      await stats.refresh();
      statsReady = true;
      console.log(`[playground-scorer] baseline stats ready in ${((Date.now() - t0) / 1000).toFixed(0)}s; refresh every ${STATS_REFRESH_MS / 3600000}h`);
      stats.startPeriodicRefresh();
      return;
    } catch (err) {
      const delayMs = Math.min(STATS_RETRY_BASE_MS * 2 ** attempt, STATS_RETRY_MAX_MS);
      console.error(`[playground-scorer] baseline stats refresh failed (attempt ${attempt + 1}); retrying in ${delayMs / 1000}s:`, err.message);
      await new Promise((r) => setTimeout(r, delayMs).unref());
    }
  }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  main().catch((err) => {
    console.error('[playground-scorer] fatal:', err);
    process.exit(1);
  });
}
