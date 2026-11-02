// Per-queue pending counters, seeded from the Taskcluster API and kept in step
// by Pulse events.
//
// 2026-10-29 incident: the collector kept every queue it had ever seen in the
// seeded set and re-fetched all of them every minute. Taskcluster's own
// integration tests create a few thousand single-use test-provisioner queues a
// day, so after three weeks of uptime the set held ~29.5k queues. A full sync
// then took longer than its 60s interval, the next sync started on top of it,
// requests began to time out, and the overlapping syncs ate the HTTP capacity
// that task enrichment needed (enrichment fell from ~100% to ~4%). Each failed
// fetch was also written to the error log, which grew by ~20GB a day.
//
// This module bounds that work:
//  - queues with no Pulse activity for idleEvictMs are dropped (the next
//    task-pending re-seeds them from the API, so nothing is lost);
//  - sync never overlaps itself, and it logs one summary per pass instead of
//    one line per failed queue;
//  - a queue whose first seed fails is not retried for failureBackoffMs;
//  - concurrent seeds of the same queue share a single request.

export const DEFAULTS = {
  maxConcurrent: 50,
  idleEvictMs: 60 * 60 * 1000,      // 1h without a pending/running/resolved event
  failureBackoffMs: 5 * 60 * 1000,  // 5 min between retries of a failed first seed
};

/**
 * @param {object} opts
 * @param {(taskQueueId: string) => Promise<{pendingTasks: number}>} opts.fetchCounts
 * @param {(category: string, message: string, details?: object) => void} opts.logError
 * @param {(err: any, context: string) => boolean} [opts.onFetchError]
 *   Called first on every fetch error. Return true if the error was handled (for
 *   example a fatal auth failure), in which case nothing is logged for it.
 * @param {() => number} [opts.now]
 */
export function createQueueCounter({
  fetchCounts,
  logError,
  onFetchError = () => false,
  now = Date.now,
  maxConcurrent = DEFAULTS.maxConcurrent,
  idleEvictMs = DEFAULTS.idleEvictMs,
  failureBackoffMs = DEFAULTS.failureBackoffMs,
} = {}) {
  const counts = new Map();      // taskQueueId -> pending count (seeded queues only)
  const lastSeen = new Map();    // taskQueueId -> ms of last Pulse activity
  const failedAt = new Map();    // taskQueueId -> ms of last failed first seed
  const inFlight = new Map();    // taskQueueId -> Promise of a seed in progress
  let syncRunning = false;

  function touch(taskQueueId) {
    lastSeen.set(taskQueueId, now());
  }

  async function fetchInto(taskQueueId) {
    const res = await fetchCounts(taskQueueId);
    counts.set(taskQueueId, res.pendingTasks);
    failedAt.delete(taskQueueId);
  }

  // First-encounter seed. A failure is logged and backs off; it never throws.
  function seed(taskQueueId) {
    const existing = inFlight.get(taskQueueId);
    if (existing) return existing;
    const p = fetchInto(taskQueueId)
      .catch(err => {
        if (onFetchError(err, `taskQueueCounts(${taskQueueId})`)) return;
        failedAt.set(taskQueueId, now());
        logError('queue-counts', `Seed failed for ${taskQueueId}: ${err?.message}`, {
          taskQueueId,
          statusCode: err?.statusCode,
          code: err?.code,
        });
      })
      .finally(() => inFlight.delete(taskQueueId));
    inFlight.set(taskQueueId, p);
    return p;
  }

  // Current pending count for a queue, seeding it from the API on first sight.
  // Returns null if the queue is not seeded (seed failed or is backing off).
  async function getPending(taskQueueId) {
    if (!taskQueueId) return null;
    touch(taskQueueId);
    if (!counts.has(taskQueueId)) {
      const failed = failedAt.get(taskQueueId);
      if (failed === undefined || now() - failed >= failureBackoffMs) {
        await seed(taskQueueId);
      }
    }
    return counts.get(taskQueueId) ?? null;
  }

  function increment(taskQueueId) {
    if (!taskQueueId) return;
    touch(taskQueueId);
    if (!counts.has(taskQueueId)) return;
    counts.set(taskQueueId, counts.get(taskQueueId) + 1);
  }

  function decrement(taskQueueId) {
    if (!taskQueueId) return;
    touch(taskQueueId);
    if (!counts.has(taskQueueId)) return;
    counts.set(taskQueueId, Math.max(0, counts.get(taskQueueId) - 1));
  }

  // Forget queues with no recent Pulse activity. Returns the number evicted.
  function evictIdle() {
    const cutoff = now() - idleEvictMs;
    let evicted = 0;
    for (const [qid, seen] of lastSeen) {
      if (seen < cutoff && !inFlight.has(qid)) {
        lastSeen.delete(qid);
        counts.delete(qid);
        failedAt.delete(qid);
        evicted++;
      }
    }
    return evicted;
  }

  // Re-fetch counts for active seeded queues to correct drift. Skips if the
  // previous pass is still running. A failed re-fetch keeps the existing count.
  async function sync() {
    if (syncRunning) return { skipped: true };
    syncRunning = true;
    try {
      const evicted = evictIdle();
      const queues = [...counts.keys()];
      let failed = 0;
      let firstError = null;
      for (let i = 0; i < queues.length; i += maxConcurrent) {
        const batch = queues.slice(i, i + maxConcurrent);
        await Promise.all(batch.map(async qid => {
          try {
            const res = await fetchCounts(qid);
            if (counts.has(qid)) counts.set(qid, res.pendingTasks);
          } catch (err) {
            if (onFetchError(err, `taskQueueCounts(${qid})`)) return;
            failed++;
            firstError ??= { taskQueueId: qid, message: err?.message, statusCode: err?.statusCode, code: err?.code };
          }
        }));
      }
      if (failed > 0) {
        logError('queue-counts', `Sync: ${failed}/${queues.length} queue count fetches failed; ` +
          `first: ${firstError.taskQueueId}: ${firstError.message}`, { failed, total: queues.length, first: firstError });
      }
      return { skipped: false, synced: queues.length, failed, evicted };
    } finally {
      syncRunning = false;
    }
  }

  return {
    getPending,
    increment,
    decrement,
    sync,
    evictIdle,
    isSeeded: qid => counts.has(qid),
    size: () => counts.size,
    tracked: () => lastSeen.size,
  };
}
