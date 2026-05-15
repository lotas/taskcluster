/**
 * Live throughput features for a single task_queue_id.
 *
 * Matches trainer/src/queue_throughput.py semantics:
 *   queue_tasks_started_W    — count of resolved rows with started_at ∈ [pending_at-W, pending_at)
 *   queue_tasks_completed_W  — count of rows with resolved_at ∈ [pending_at-W, pending_at)
 *   queue_avg_wait_W         — avg(wait_duration_s) over the COMPLETED window
 *   queue_avg_run_time_W     — avg(run_duration_s) over the COMPLETED window
 *
 * Windows are anchored at pending_at (not now()) to match the trainer's
 * feature-generation time and prevent the current row's own outcome from
 * leaking in when predict is called on an already-resolved catch-up row.
 * The current (task_id, run_id) is also explicitly excluded.
 *
 * Queues with no resolved-row history return NaN for all columns (not 0).
 * Empty windows return NaN for averages but 0 for counts.
 */

// $1 = task_queue_id, $2 = pending_at, $3 = task_id (exclude), $4 = run_id (exclude)
const THROUGHPUT_SQL = `
WITH recent AS (
  SELECT r.task_id, r.run_id, r.started_at, r.resolved_at, r.wait_duration_s, r.run_duration_s
  FROM queue_forecast_task_runs r
  JOIN queue_forecast_tasks t ON r.task_id = t.task_id
  WHERE t.task_queue_id = $1
    AND r.resolved_at IS NOT NULL
    AND r.resolved_at < $2::timestamptz
    AND r.resolved_at >= $2::timestamptz - INTERVAL '60 minutes'
    AND NOT (r.task_id = $3 AND r.run_id = $4)
),
any_row AS (
  SELECT EXISTS (
    SELECT 1
    FROM queue_forecast_task_runs r
    JOIN queue_forecast_tasks t ON r.task_id = t.task_id
    WHERE t.task_queue_id = $1
      AND r.resolved_at IS NOT NULL
      AND r.resolved_at < $2::timestamptz
      AND NOT (r.task_id = $3 AND r.run_id = $4)
    LIMIT 1
  ) AS has_history
)
SELECT
  (SELECT has_history FROM any_row) AS has_history,
  count(*) FILTER (WHERE started_at  >= $2::timestamptz - INTERVAL '15 minutes') AS queue_tasks_started_15m,
  count(*) FILTER (WHERE resolved_at >= $2::timestamptz - INTERVAL '15 minutes') AS queue_tasks_completed_15m,
  avg(wait_duration_s) FILTER (WHERE resolved_at >= $2::timestamptz - INTERVAL '15 minutes') AS queue_avg_wait_15m,
  avg(run_duration_s)  FILTER (WHERE resolved_at >= $2::timestamptz - INTERVAL '15 minutes') AS queue_avg_run_time_15m,
  count(*) FILTER (WHERE started_at  >= $2::timestamptz - INTERVAL '60 minutes') AS queue_tasks_started_60m,
  count(*) FILTER (WHERE resolved_at >= $2::timestamptz - INTERVAL '60 minutes') AS queue_tasks_completed_60m,
  avg(wait_duration_s) FILTER (WHERE resolved_at >= $2::timestamptz - INTERVAL '60 minutes') AS queue_avg_wait_60m,
  avg(run_duration_s)  FILTER (WHERE resolved_at >= $2::timestamptz - INTERVAL '60 minutes') AS queue_avg_run_time_60m
FROM recent;`;

const ALL_NAN = Object.freeze({
  queue_tasks_started_15m: NaN, queue_tasks_completed_15m: NaN,
  queue_avg_wait_15m: NaN,      queue_avg_run_time_15m: NaN,
  queue_tasks_started_60m: NaN, queue_tasks_completed_60m: NaN,
  queue_avg_wait_60m: NaN,      queue_avg_run_time_60m: NaN,
});

function nullableNum(v) {
  if (v === null || v === undefined) return NaN;
  return Number(v);
}

/**
 * @param {object} pool  Postgres pool with `.query(sql, params)`.
 * @param {string|null} taskQueueId
 * @param {Date|string|null} pendingAt  Anchor timestamp for window boundaries.
 * @param {string|null} taskId   Current task to exclude from the window.
 * @param {number|null} runId    Current run to exclude from the window.
 * @returns {Promise<object>}  Eight throughput features; NaN where unavailable.
 */
export async function getThroughput(pool, taskQueueId, pendingAt, taskId, runId) {
  if (!taskQueueId || !pendingAt) return { ...ALL_NAN };
  const res = await pool.query(THROUGHPUT_SQL, [taskQueueId, pendingAt, taskId ?? '', runId ?? -1]);
  const row = res.rows[0];
  if (!row || row.has_history !== true) return { ...ALL_NAN };
  return {
    queue_tasks_started_15m:   nullableNum(row.queue_tasks_started_15m),
    queue_tasks_completed_15m: nullableNum(row.queue_tasks_completed_15m),
    queue_avg_wait_15m:        nullableNum(row.queue_avg_wait_15m),
    queue_avg_run_time_15m:    nullableNum(row.queue_avg_run_time_15m),
    queue_tasks_started_60m:   nullableNum(row.queue_tasks_started_60m),
    queue_tasks_completed_60m: nullableNum(row.queue_tasks_completed_60m),
    queue_avg_wait_60m:        nullableNum(row.queue_avg_wait_60m),
    queue_avg_run_time_60m:    nullableNum(row.queue_avg_run_time_60m),
  };
}
