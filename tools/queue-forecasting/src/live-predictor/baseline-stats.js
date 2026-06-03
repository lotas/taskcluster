/**
 * Hierarchical baseline stats for the live predictor.
 *
 * Mirrors the bulk-stats loading + lookup in src/predictor.js so that
 * bl_wait_p50 and bl_duration_p50 features match what the trainer computed.
 * Using a different hierarchy here than the trainer used would skew both the
 * baseline feature fed into the ONNX model AND the log_ratio inverse transform.
 *
 * Wait hierarchy:   queue+priority+bucket → queue+bucket → queue →
 *                    priority+bucket → global
 * Duration hierarchy: metadata_name → normalized_name → kind+test-type →
 *                     task_queue_id → scheduler_id → global
 *
 * Per-target anomaly filtering (matches deployed trainer configs):
 *   - wait:     waitFilterAnomalous=true  → exclude anomalous days from history
 *   - duration: durationFilterAnomalous=false → use full history
 */
import { pendingBucket, PENDING_BUCKET_SQL, normalizeMetadataName } from '../utils.js';

const MIN_SAMPLE_SIZE = 5;

// ---------------------------------------------------------------------------
// SQL — each query is tagged with a comment line used to identify it in tests.
// ---------------------------------------------------------------------------

const WAIT_BY_QUEUE_PRIORITY_AND_BUCKET = (withExclude) => `-- wait_by_queue_priority_and_bucket
SELECT t.task_queue_id || '|' || r.priority_at_pending || '|' || ${PENDING_BUCKET_SQL} AS key,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p90,
       count(*) AS sample_size
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.wait_duration_s IS NOT NULL
  AND t.task_queue_id IS NOT NULL
  AND r.priority_at_pending IS NOT NULL
  AND r.started_at IS NOT NULL
  AND r.resolved_at < $1::timestamptz
  AND r.resolved_at > $1::timestamptz - INTERVAL '7 days'${withExclude ? `
  AND r.resolved_at::date <> ALL($2::date[])` : ''}
GROUP BY t.task_queue_id, r.priority_at_pending, ${PENDING_BUCKET_SQL};`;

const WAIT_BY_QUEUE_AND_BUCKET = (withExclude) => `-- wait_by_queue_and_bucket
SELECT t.task_queue_id || '|' || ${PENDING_BUCKET_SQL} AS key,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p90,
       count(*) AS sample_size
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.wait_duration_s IS NOT NULL
  AND r.started_at IS NOT NULL
  AND r.resolved_at < $1::timestamptz
  AND r.resolved_at > $1::timestamptz - INTERVAL '7 days'${withExclude ? `
  AND r.resolved_at::date <> ALL($2::date[])` : ''}
GROUP BY t.task_queue_id, ${PENDING_BUCKET_SQL};`;

const WAIT_BY_QUEUE = (withExclude) => `-- wait_by_queue
SELECT t.task_queue_id AS key,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p90,
       count(*) AS sample_size
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.wait_duration_s IS NOT NULL
  AND t.task_queue_id IS NOT NULL
  AND r.started_at IS NOT NULL
  AND r.resolved_at < $1::timestamptz
  AND r.resolved_at > $1::timestamptz - INTERVAL '7 days'${withExclude ? `
  AND r.resolved_at::date <> ALL($2::date[])` : ''}
GROUP BY t.task_queue_id;`;

const WAIT_BY_PRIORITY_AND_BUCKET = (withExclude) => `-- wait_by_priority_and_bucket
SELECT r.priority_at_pending || '|' || ${PENDING_BUCKET_SQL} AS key,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p90,
       count(*) AS sample_size
FROM queue_forecast_task_runs r
WHERE r.wait_duration_s IS NOT NULL
  AND r.started_at IS NOT NULL
  AND r.resolved_at < $1::timestamptz
  AND r.resolved_at > $1::timestamptz - INTERVAL '7 days'${withExclude ? `
  AND r.resolved_at::date <> ALL($2::date[])` : ''}
GROUP BY r.priority_at_pending, ${PENDING_BUCKET_SQL};`;

const WAIT_GLOBAL = (withExclude) => `-- wait_global
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY r.wait_duration_s) AS p90,
       count(*) AS sample_size
FROM queue_forecast_task_runs r
WHERE r.wait_duration_s IS NOT NULL
  AND r.started_at IS NOT NULL
  AND r.resolved_at < $1::timestamptz
  AND r.resolved_at > $1::timestamptz - INTERVAL '7 days'${withExclude ? `
  AND r.resolved_at::date <> ALL($2::date[])` : ''};`;

const DURATION_BY_METADATA_NAME = (withExclude) => `-- duration_by_metadata
SELECT t.metadata_name AS key,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY r.run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY r.run_duration_s) AS p90,
       count(*) AS sample_size
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.run_duration_s IS NOT NULL
  AND t.metadata_name IS NOT NULL
  AND r.reason_resolved = 'completed'
  AND r.resolved_at < $1::timestamptz
  AND r.resolved_at > $1::timestamptz - INTERVAL '7 days'${withExclude ? `
  AND r.resolved_at::date <> ALL($2::date[])` : ''}
GROUP BY t.metadata_name;`;

const DURATION_BY_NORMALIZED_NAME = (withExclude) => `-- duration_by_normalized
SELECT t.normalized_name AS key,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY r.run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY r.run_duration_s) AS p90,
       count(*) AS sample_size
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.run_duration_s IS NOT NULL
  AND t.normalized_name IS NOT NULL
  AND r.reason_resolved = 'completed'
  AND r.resolved_at < $1::timestamptz
  AND r.resolved_at > $1::timestamptz - INTERVAL '7 days'${withExclude ? `
  AND r.resolved_at::date <> ALL($2::date[])` : ''}
GROUP BY t.normalized_name;`;

const DURATION_BY_KIND_TEST_TYPE = (withExclude) => `-- duration_by_kind_test_type
SELECT (t.tags->>'kind') || '|' || (t.tags->>'test-type') AS key,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY r.run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY r.run_duration_s) AS p90,
       count(*) AS sample_size
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.run_duration_s IS NOT NULL
  AND t.tags IS NOT NULL
  AND t.tags->>'kind' IS NOT NULL
  AND t.tags->>'test-type' IS NOT NULL
  AND r.reason_resolved = 'completed'
  AND r.resolved_at < $1::timestamptz
  AND r.resolved_at > $1::timestamptz - INTERVAL '7 days'${withExclude ? `
  AND r.resolved_at::date <> ALL($2::date[])` : ''}
GROUP BY t.tags->>'kind', t.tags->>'test-type';`;

const DURATION_BY_QUEUE = (withExclude) => `-- duration_by_queue
SELECT t.task_queue_id AS key,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY r.run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY r.run_duration_s) AS p90,
       count(*) AS sample_size
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.run_duration_s IS NOT NULL
  AND t.task_queue_id IS NOT NULL
  AND r.reason_resolved = 'completed'
  AND r.resolved_at < $1::timestamptz
  AND r.resolved_at > $1::timestamptz - INTERVAL '7 days'${withExclude ? `
  AND r.resolved_at::date <> ALL($2::date[])` : ''}
GROUP BY t.task_queue_id;`;

const DURATION_BY_SCHEDULER = (withExclude) => `-- duration_by_scheduler
SELECT t.scheduler_id AS key,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY r.run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY r.run_duration_s) AS p90,
       count(*) AS sample_size
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.run_duration_s IS NOT NULL
  AND t.scheduler_id IS NOT NULL
  AND r.reason_resolved = 'completed'
  AND r.resolved_at < $1::timestamptz
  AND r.resolved_at > $1::timestamptz - INTERVAL '7 days'${withExclude ? `
  AND r.resolved_at::date <> ALL($2::date[])` : ''}
GROUP BY t.scheduler_id;`;

const DURATION_GLOBAL = (withExclude) => `-- duration_global
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY r.run_duration_s) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY r.run_duration_s) AS p90,
       count(*) AS sample_size
FROM queue_forecast_task_runs r
WHERE r.run_duration_s IS NOT NULL
  AND r.reason_resolved = 'completed'
  AND r.resolved_at < $1::timestamptz
  AND r.resolved_at > $1::timestamptz - INTERVAL '7 days'${withExclude ? `
  AND r.resolved_at::date <> ALL($2::date[])` : ''};`;

const ANOMALOUS_DATES_SQL = `
SELECT sample_date FROM queue_forecast_daily_health WHERE is_anomalous = true;`;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function toMap(rows) {
  const m = new Map();
  for (const r of rows) {
    if (parseInt(r.sample_size, 10) >= MIN_SAMPLE_SIZE) {
      m.set(r.key, { p50: parseFloat(r.p50), p90: parseFloat(r.p90), sample_size: parseInt(r.sample_size, 10) });
    }
  }
  return m;
}

function toGlobal(rows) {
  const r = rows[0];
  if (!r || parseInt(r.sample_size, 10) < MIN_SAMPLE_SIZE) return null;
  return { p50: parseFloat(r.p50), p90: parseFloat(r.p90), sample_size: parseInt(r.sample_size, 10) };
}

function parseTags(tags) {
  if (!tags) return null;
  if (typeof tags === 'object') return tags;
  try { return JSON.parse(tags); } catch { return null; }
}

// ---------------------------------------------------------------------------
// BaselineStats
// ---------------------------------------------------------------------------

const REFRESH_INTERVAL_MS = 60 * 60 * 1000; // 1 hour

export class BaselineStats {
  constructor(pool, opts = {}) {
    this._pool = pool;
    this._opts = {
      waitFilterAnomalous:     opts.waitFilterAnomalous     ?? true,
      durationFilterAnomalous: opts.durationFilterAnomalous ?? false,
    };
    this._waitFilter     = this._opts.waitFilterAnomalous;
    this._durationFilter = this._opts.durationFilterAnomalous;
    this._refreshTimer   = null;

    this._wait = {
      byQueuePriorityAndBucket: new Map(),
      byQueueAndBucket: new Map(),
      byQueue: new Map(),
      byPriorityAndBucket: new Map(),
      global: null,
    };
    this._duration = {
      byMetadataName: new Map(),
      byNormalizedName: new Map(),
      byKindTestType: new Map(),
      byTaskQueueId: new Map(),
      bySchedulerId: new Map(),
      global: null,
    };
  }

  startPeriodicRefresh() {
    if (this._refreshTimer) return;
    this._refreshTimer = setInterval(() => {
      this.refresh().catch(() => {});
    }, REFRESH_INTERVAL_MS);
    this._refreshTimer.unref?.();
  }

  stopPeriodicRefresh() {
    if (this._refreshTimer) {
      clearInterval(this._refreshTimer);
      this._refreshTimer = null;
    }
  }

  async refresh() {
    const cutoff = new Date();

    // Fetch anomalous dates for whichever targets need filtering.
    let anomalousDates = [];
    if (this._waitFilter || this._durationFilter) {
      const res = await this._pool.query(ANOMALOUS_DATES_SQL);
      anomalousDates = res.rows.map(r => r.sample_date);
    }

    const waitParams    = this._waitFilter     ? [cutoff, anomalousDates] : [cutoff];
    const durationParams = this._durationFilter ? [cutoff, anomalousDates] : [cutoff];
    const we = this._waitFilter;
    const de = this._durationFilter;

    const [wpb, wb, wq, wp, wg, dm, dn, dk, dq, ds, dg] = await Promise.all([
      this._pool.query(WAIT_BY_QUEUE_PRIORITY_AND_BUCKET(we), waitParams),
      this._pool.query(WAIT_BY_QUEUE_AND_BUCKET(we), waitParams),
      this._pool.query(WAIT_BY_QUEUE(we), waitParams),
      this._pool.query(WAIT_BY_PRIORITY_AND_BUCKET(we), waitParams),
      this._pool.query(WAIT_GLOBAL(we), waitParams),
      this._pool.query(DURATION_BY_METADATA_NAME(de), durationParams),
      this._pool.query(DURATION_BY_NORMALIZED_NAME(de), durationParams),
      this._pool.query(DURATION_BY_KIND_TEST_TYPE(de), durationParams),
      this._pool.query(DURATION_BY_QUEUE(de), durationParams),
      this._pool.query(DURATION_BY_SCHEDULER(de), durationParams),
      this._pool.query(DURATION_GLOBAL(de), durationParams),
    ]);

    this._wait = {
      byQueuePriorityAndBucket: toMap(wpb.rows),
      byQueueAndBucket:   toMap(wb.rows),
      byQueue:            toMap(wq.rows),
      byPriorityAndBucket: toMap(wp.rows),
      global:             toGlobal(wg.rows),
    };
    this._duration = {
      byMetadataName:  toMap(dm.rows),
      byNormalizedName: toMap(dn.rows),
      byKindTestType:  toMap(dk.rows),
      byTaskQueueId:   toMap(dq.rows),
      bySchedulerId:   toMap(ds.rows),
      global:          toGlobal(dg.rows),
    };
  }

  /**
   * Hierarchical wait-time baseline lookup (mirrors predictor.js predictWaitFromStats).
   * @param {object} task  Must have task_queue_id, queue_pending, priority_at_pending.
   * @returns {{ level, p50, p90, sample_size } | null}
   */
  predictWait(task) {
    const bucket = pendingBucket(task.queue_pending);

    // Most specific: queue + priority + depth bucket. Priority dominates wait
    // in deep queues, so a priority-blind baseline mis-anchors the model.
    if (task.task_queue_id && task.priority_at_pending != null && bucket != null) {
      const s = this._wait.byQueuePriorityAndBucket.get(`${task.task_queue_id}|${task.priority_at_pending}|${bucket}`);
      if (s) return { level: 'queue+priority+bucket', ...s };
    }

    if (task.task_queue_id && bucket != null) {
      const s = this._wait.byQueueAndBucket.get(`${task.task_queue_id}|${bucket}`);
      if (s) return { level: 'queue+bucket', ...s };
    }

    if (task.task_queue_id) {
      const s = this._wait.byQueue.get(task.task_queue_id);
      if (s) return { level: 'queue', ...s };
    }

    if (task.priority_at_pending != null && bucket != null) {
      const s = this._wait.byPriorityAndBucket.get(`${task.priority_at_pending}|${bucket}`);
      if (s) return { level: 'priority+bucket', ...s };
    }

    if (this._wait.global) return { level: 'global', ...this._wait.global };
    return null;
  }

  /**
   * Hierarchical duration baseline lookup (mirrors predictor.js predictDurationFromStats).
   * @param {object} task  Must have metadata_name, normalized_name, tags, task_queue_id, scheduler_id.
   * @returns {{ level, p50, p90, sample_size } | null}
   */
  predictDuration(task) {
    if (task.metadata_name) {
      const s = this._duration.byMetadataName.get(task.metadata_name);
      if (s) return { level: 'metadata_name', ...s };
    }

    const normName = task.normalized_name || normalizeMetadataName(task.metadata_name);
    if (normName && normName !== task.metadata_name) {
      const s = this._duration.byNormalizedName.get(normName);
      if (s) return { level: 'normalized_name', ...s };
    }

    const tags = parseTags(task.tags);
    const kind = tags?.kind;
    const testType = tags?.['test-type'];
    if (kind && testType) {
      const s = this._duration.byKindTestType.get(`${kind}|${testType}`);
      if (s) return { level: 'kind+test-type', ...s };
    }

    if (task.task_queue_id) {
      const s = this._duration.byTaskQueueId.get(task.task_queue_id);
      if (s) return { level: 'task_queue_id', ...s };
    }

    if (task.scheduler_id) {
      const s = this._duration.bySchedulerId.get(task.scheduler_id);
      if (s) return { level: 'scheduler_id', ...s };
    }

    if (this._duration.global) return { level: 'global', ...this._duration.global };
    return null;
  }
}
