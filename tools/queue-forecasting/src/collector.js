import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import taskcluster from '@taskcluster/client';
import { Client, consume, pulseCredentials } from '@taskcluster/lib-pulse';
import { createNoOpMonitor } from './monitor.js';
import { createTaskCache } from './cache.js';
import { createPool, upsertTask, upsertTaskRun, enrichTask, getUnenrichedTaskIds } from './db.js';
import { normalizeMetadataName } from './utils.js';
import { deriveRepoFamily } from './repo-family.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ERROR_LOG_PATH = process.env.COLLECTOR_ERROR_LOG || path.join(__dirname, '..', 'collector-errors.log');
const errorLogStream = fs.createWriteStream(ERROR_LOG_PATH, { flags: 'a' });

function logError(category, message, details = {}) {
  const entry = JSON.stringify({ ts: new Date().toISOString(), category, message, ...details });
  errorLogStream.write(entry + '\n');
  console.error(`[${category}] ${message}`);
}

const MAX_CONCURRENT_FETCHES = 50;
let inFlightFetches = 0;
const inFlightTaskIds = new Set();
const BACKFILL_INTERVAL_MS = 60_000; // 1 minute
const MAX_BACKFILL_RETRIES = 3;
const backfillFailCounts = new Map(); // taskId -> attempt count

// --- Queue Pending Counter ---
const pendingCounts = new Map();       // taskQueueId -> number
const pendingCountsSeeded = new Set(); // queues that have been seeded from API
const SYNC_INTERVAL_MS = 60_000;

const pool = createPool(process.env.DATABASE_URL);
const taskCache = createTaskCache();
const monitor = createNoOpMonitor();

const pulseClient = new Client({
  namespace: 'taskcluster-queue-forecasting',
  credentials: pulseCredentials({
    hostname: process.env.PULSE_HOSTNAME,
    username: process.env.PULSE_USERNAME,
    password: process.env.PULSE_PASSWORD,
    vhost: process.env.PULSE_VHOST,
  }),
  monitor,
});

const queueClient = new taskcluster.Queue({
  rootUrl: process.env.TASKCLUSTER_ROOT_URL,
  credentials: {
    clientId: process.env.TASKCLUSTER_CLIENT_ID,
    accessToken: process.env.TASKCLUSTER_ACCESS_TOKEN,
  },
  timeout: 10000,
  retries: 1,
});

const queueEvents = new taskcluster.QueueEvents({
  rootUrl: process.env.TASKCLUSTER_ROOT_URL,
});

// Seed a queue's counter from the API
async function seedQueueCount(taskQueueId) {
  try {
    const counts = await queueClient.taskQueueCounts(taskQueueId);
    pendingCounts.set(taskQueueId, counts.pendingTasks);
    pendingCountsSeeded.add(taskQueueId);
  } catch (err) {
    logError('queue-counts', `Seed failed for ${taskQueueId}: ${err.message}`, {
      taskQueueId,
      statusCode: err.statusCode,
      code: err.code,
    });
  }
}

// Get current pending count (seeds from API on first encounter)
async function getQueuePending(taskQueueId) {
  if (!taskQueueId) return null;
  if (!pendingCountsSeeded.has(taskQueueId)) {
    await seedQueueCount(taskQueueId);
  }
  return pendingCounts.get(taskQueueId) ?? null;
}

function incrementQueuePending(taskQueueId) {
  if (!taskQueueId || !pendingCountsSeeded.has(taskQueueId)) return;
  pendingCounts.set(taskQueueId, (pendingCounts.get(taskQueueId) || 0) + 1);
}

function decrementQueuePending(taskQueueId) {
  if (!taskQueueId || !pendingCountsSeeded.has(taskQueueId)) return;
  const current = pendingCounts.get(taskQueueId) || 0;
  pendingCounts.set(taskQueueId, Math.max(0, current - 1));
}

// Periodic sync — re-fetch counts for all seeded queues to correct drift
async function syncAllQueueCounts() {
  const queues = [...pendingCountsSeeded];
  for (let i = 0; i < queues.length; i += MAX_CONCURRENT_FETCHES) {
    const batch = queues.slice(i, i + MAX_CONCURRENT_FETCHES);
    await Promise.all(batch.map(qid => seedQueueCount(qid)));
  }
}

function extractRunFields(status, runId) {
  const run = status.runs?.[runId];
  if (!run) return {};
  return {
    pending_at: run.scheduled || null,
    started_at: run.started || null,
    resolved_at: run.resolved || null,
    reason_created: run.reasonCreated || null,
    reason_resolved: run.reasonResolved || null,
  };
}

function computeDurations(pending_at, started_at, resolved_at) {
  let wait_duration_s = null;
  let run_duration_s = null;
  if (pending_at && started_at) {
    wait_duration_s = (new Date(started_at) - new Date(pending_at)) / 1000;
  }
  if (started_at && resolved_at) {
    run_duration_s = (new Date(resolved_at) - new Date(started_at)) / 1000;
  }
  return { wait_duration_s, run_duration_s };
}

function extractTaskFields(status) {
  return {
    task_queue_id: status.taskQueueId || null,
    task_group_id: status.taskGroupId || null,
    scheduler_id: status.schedulerId || null,
    project_id: status.projectId || null,
  };
}

function extractTags(payload) {
  const tags = payload.task?.tags;
  if (tags && Object.keys(tags).length > 0) return tags;
  return null;
}

async function handleTaskDefined(payload) {
  const { status } = payload;
  await upsertTask(pool, {
    task_id: status.taskId,
    ...extractTaskFields(status),
    tags: extractTags(payload),
  });
}

async function handleTaskPending(payload) {
  const { status, runId } = payload;
  const runFields = extractRunFields(status, runId);
  const taskQueueId = status.taskQueueId || null;

  // Increment BEFORE snapshotting: the seeded value from the API already
  // includes this task's pending event (Pulse delivers after state change),
  // but our local counter hasn't counted it yet. Incrementing first keeps
  // the snapshot aligned with reality.
  incrementQueuePending(taskQueueId);
  const queuePending = await getQueuePending(taskQueueId);

  await upsertTask(pool, {
    task_id: status.taskId,
    ...extractTaskFields(status),
    tags: extractTags(payload),
  });
  await upsertTaskRun(pool, {
    task_id: status.taskId,
    run_id: runId,
    ...runFields,
    priority_at_pending: status.priority || null,
    queue_pending: queuePending,
  });
}

async function handleTaskRunning(payload) {
  const { status, runId } = payload;
  const runFields = extractRunFields(status, runId);

  await upsertTask(pool, {
    task_id: status.taskId,
    ...extractTaskFields(status),
    tags: extractTags(payload),
  });
  await upsertTaskRun(pool, {
    task_id: status.taskId,
    run_id: runId,
    ...runFields,
  });
  decrementQueuePending(status.taskQueueId || null);
}

async function handleResolved(payload) {
  const { status, runId } = payload;
  const runFields = extractRunFields(status, runId);
  const durations = computeDurations(runFields.pending_at, runFields.started_at, runFields.resolved_at);

  await upsertTask(pool, {
    task_id: status.taskId,
    ...extractTaskFields(status),
    tags: extractTags(payload),
  });
  await upsertTaskRun(pool, {
    task_id: status.taskId,
    run_id: runId,
    ...runFields,
    ...durations,
  });

  // If the run never started (e.g. canceled before pickup), the pending
  // counter was incremented at task-pending but never decremented at
  // task-running. Correct the drift here.
  if (!runFields.started_at) {
    decrementQueuePending(status.taskQueueId || null);
  }
}

async function handleTaskException(payload) {
  const { status } = payload;
  const runId = payload.runId;

  await upsertTask(pool, {
    task_id: status.taskId,
    ...extractTaskFields(status),
    tags: extractTags(payload),
  });

  if (runId != null) {
    const runFields = extractRunFields(status, runId);
    const durations = computeDurations(runFields.pending_at, runFields.started_at, runFields.resolved_at);
    await upsertTaskRun(pool, {
      task_id: status.taskId,
      run_id: runId,
      ...runFields,
      ...durations,
    });

    // Exception without starting — pending counter was never decremented
    if (!runFields.started_at) {
      decrementQueuePending(status.taskQueueId || null);
    }
  }
}

async function backgroundApiFetch(taskId, status) {
  if (taskCache.has(taskId)) return;
  if (inFlightTaskIds.has(taskId)) return;
  if (inFlightFetches >= MAX_CONCURRENT_FETCHES) return;

  inFlightFetches++;
  inFlightTaskIds.add(taskId);
  try {
    const taskDef = await queueClient.task(taskId);
    const rf = deriveRepoFamily({
      routes: taskDef.routes || [],
      metadataSource: taskDef.metadata?.source ?? null,
      schedulerId: taskDef.schedulerId || status.schedulerId || null,
    });
    const metadataName = taskDef.metadata?.name || null;
    const enrichment = {
      metadata_name: metadataName,
      normalized_name: normalizeMetadataName(metadataName),
      tags: taskDef.tags && Object.keys(taskDef.tags).length > 0 ? taskDef.tags : null,
      task_created: taskDef.created || null,
      original_priority: taskDef.priority || null,
      task_queue_id: taskDef.taskQueueId || status.taskQueueId,
      task_group_id: taskDef.taskGroupId || status.taskGroupId,
      scheduler_id: taskDef.schedulerId || status.schedulerId,
      project_id: taskDef.projectId || status.projectId,
      max_run_time_s: taskDef.payload?.maxRunTime ?? null,
      repo_family: rf.family,
      repo_family_source: rf.source,
      repo_family_evidence: rf.evidence,
      repo_family_derivation_version: rf.version,
    };
    await enrichTask(pool, taskId, enrichment);
    taskCache.set(taskId, enrichment);
  } catch (err) {
    logError('api-fetch', `Failed for ${taskId}: ${err.message}`, {
      taskId,
      statusCode: err.statusCode,
      code: err.code,
    });
    const count = (backfillFailCounts.get(taskId) || 0) + 1;
    backfillFailCounts.set(taskId, count);
  } finally {
    inFlightTaskIds.delete(taskId);
    inFlightFetches--;
  }
}

async function handleMessage(message) {
  const { payload, exchange } = message;
  const eventType = exchange.split('/').pop();

  switch (eventType) {
    case 'task-defined':
      await handleTaskDefined(payload);
      break;
    case 'task-pending':
      await handleTaskPending(payload);
      break;
    case 'task-running':
      await handleTaskRunning(payload);
      break;
    case 'task-completed':
    case 'task-failed':
      await handleResolved(payload);
      break;
    case 'task-exception':
      await handleTaskException(payload);
      break;
    // task-priority-changed and task-group-priority-changed are no-ops:
    // priority_at_pending is an immutable snapshot captured at pending time
    default:
      break;
  }

  // Fire-and-forget API enrichment (does not block ack)
  if (payload.status) {
    const taskId = payload.status.taskId;
    if (!taskCache.has(taskId)) {
      backgroundApiFetch(taskId, payload.status).catch(() => {});
    }
  }
}

// --- Bindings ---

const bindings = [
  queueEvents.taskDefined(),
  queueEvents.taskPending(),
  queueEvents.taskRunning(),
  queueEvents.taskCompleted(),
  queueEvents.taskFailed(),
  queueEvents.taskException(),
].map(binding => ({
  exchange: binding.exchange,
  routingKeyPattern: '#',
}));

// --- Start ---

console.log('[collector] Starting...');
const pq = await consume({
  client: pulseClient,
  bindings,
  queueName: 'queue-forecasting-collector',
  prefetch: 100,
}, handleMessage);
console.log('[collector] Listening for Pulse messages');

// --- Backfill Sweep ---

let backfillRunning = false;

async function backfillSweep() {
  if (backfillRunning) return;
  backfillRunning = true;
  try {
    const exhaustedIds = [...backfillFailCounts.entries()]
      .filter(([, count]) => count >= MAX_BACKFILL_RETRIES)
      .map(([id]) => id);
    const taskIds = await getUnenrichedTaskIds(pool, 200, exhaustedIds);
    if (taskIds.length === 0) return;
    console.log(`[backfill] Found ${taskIds.length} unenriched tasks, fetching...`);

    const fetchTasks = [];
    for (const taskId of taskIds) {
      if (taskCache.has(taskId)) {
        await enrichTask(pool, taskId, taskCache.get(taskId));
      } else if ((backfillFailCounts.get(taskId) || 0) >= MAX_BACKFILL_RETRIES) {
        continue;
      } else {
        fetchTasks.push(taskId);
      }
    }

    for (let i = 0; i < fetchTasks.length; i += MAX_CONCURRENT_FETCHES) {
      const batch = fetchTasks.slice(i, i + MAX_CONCURRENT_FETCHES);
      await Promise.all(batch.map(taskId => backgroundApiFetch(taskId, {})));
    }
  } catch (err) {
    logError('backfill', `Sweep error: ${err.message}`);
  } finally {
    backfillRunning = false;
  }
}

const backfillTimer = setInterval(backfillSweep, BACKFILL_INTERVAL_MS);
const syncTimer = setInterval(syncAllQueueCounts, SYNC_INTERVAL_MS);

// --- Missing queue_pending Monitor ---

const PENDING_GAP_CHECK_INTERVAL_MS = 300_000; // 5 minutes
let lastPendingGapCount = 0;

async function checkPendingGaps() {
  try {
    const res = await pool.query(`
      SELECT count(*) AS n FROM queue_forecast_task_runs
      WHERE queue_pending IS NULL
        AND reason_resolved = 'completed'
        AND started_at IS NOT NULL
    `);
    const count = parseInt(res.rows[0].n, 10);
    if (count > lastPendingGapCount) {
      const delta = count - lastPendingGapCount;
      logError('pending-gap', `${delta} new completed tasks missing queue_pending (total: ${count})`, { total: count, delta });
      lastPendingGapCount = count;
    }
  } catch (err) {
    // non-critical, don't crash
  }
}

const pendingGapTimer = setInterval(checkPendingGaps, PENDING_GAP_CHECK_INTERVAL_MS);
checkPendingGaps(); // initial check on startup

// --- Graceful Shutdown ---

let shuttingDown = false;
async function shutdown(signal) {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log(`[collector] Received ${signal}, shutting down...`);
  clearInterval(backfillTimer);
  clearInterval(syncTimer);
  clearInterval(pendingGapTimer);

  try {
    await pq.stop();
  } catch (err) {
    console.error('[collector] Error stopping consumer:', err.message);
  }

  // Wait for in-flight API fetches (max 15s)
  const deadline = Date.now() + 15000;
  while (inFlightFetches > 0 && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  if (inFlightFetches > 0) {
    console.warn(`[collector] ${inFlightFetches} API fetches still in-flight, proceeding with shutdown`);
  }

  try {
    await pulseClient.stop();
  } catch (err) {
    console.error('[collector] Error stopping Pulse client:', err.message);
  }

  try {
    await pool.end();
  } catch (err) {
    console.error('[collector] Error closing DB pool:', err.message);
  }

  errorLogStream.end();
  console.log('[collector] Shutdown complete');
  process.exit(0);
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));
