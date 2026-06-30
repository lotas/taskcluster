// Live queue-context features for ONE target row. Mirrors
// trainer/src/queue_context.py exactly (same definitions, same version).
//
// For a target row at time T = pending_at on a given task_queue_id, this builds
// a leakage-safe snapshot of the queue state visible at or before T via two SQL
// queries against the *current* DB state. The returned object carries EXACTLY
// the Python module's FEATURE_COLUMNS (21 keys).
export const QUEUE_CONTEXT_FEATURE_VERSION = 1;

// If the latest worker-count sample at/before T is older than this bound, there
// is effectively no usable capacity reading -> treated exactly like no_sample.
// Must match the trainer's capacity_staleness_s default in queue_context.py.
const QUEUE_CONTEXT_CAPACITY_STALENESS_S = 900;

const PRIORITY_RANK = {
  highest: 7,
  'very-high': 6,
  high: 5,
  medium: 4,
  low: 3,
  'very-low': 2,
  lowest: 1,
  normal: 1,
};
const rank = (p) => PRIORITY_RANK[p] ?? 0;

// The complete set of feature keys, matching Python FEATURE_COLUMNS order.
const FEATURE_COLUMNS = [
  'pending_higher_priority_same_queue',
  'pending_same_priority_same_queue',
  'pending_lower_priority_same_queue',
  'oldest_higher_or_equal_pending_age_same_queue',
  'arrivals_15m_same_queue',
  'arrivals_60m_same_queue',
  'arrivals_higher_or_equal_15m_same_queue',
  'arrivals_higher_or_equal_60m_same_queue',
  'starts_higher_or_equal_15m_same_queue',
  'pending_total_per_capacity',
  'pending_higher_or_equal_per_capacity',
  'running_per_capacity',
  'running_workers',
  'existing_capacity',
  'claimed_tasks',
  'capacity_sample_age_s',
  'capacity_null_reason',
  'backlog_coverage_ratio',
  'pending_try_higher_or_equal_same_queue',
  'pending_autoland_higher_or_equal_same_queue',
  'pending_release_beta_higher_or_equal_same_queue',
];

// $1=queue $2=T $3=rank $4=task_id $5=run_id
//
// Two CTEs:
//   `ahead`      - runs still pending at T (pending_at <= T AND (exit IS NULL
//                  OR exit > T), where exit = COALESCE(started_at, resolved_at)),
//                  excluding the target. A run that was resolved without ever
//                  starting (canceled / claim-expired / deadline-exceeded before
//                  claim) leaves the pending state at resolved_at, not forever.
//                  Used for the backlog/blocking/oldest-age/family counts.
//   `events`     - all runs on the queue regardless of started-state, excluding
//                  the target. A run that has already started is still an
//                  arrival, and starts are counted by started_at, so the flow
//                  features (arrivals/starts) must sweep over `events`, not the
//                  pending-only `ahead` CTE (mirrors Python's event sweep).
//   `allpending` - runs pending at T INCLUDING the target, for coverage.
export const BACKLOG_SQL = `-- queue_context_backlog
WITH ahead AS (
  SELECT r.priority_at_pending AS pr, r.pending_at, r.started_at, t.repo_family,
         r.task_id, r.run_id,
         CASE r.priority_at_pending
           WHEN 'highest' THEN 7 WHEN 'very-high' THEN 6 WHEN 'high' THEN 5
           WHEN 'medium' THEN 4 WHEN 'low' THEN 3 WHEN 'very-low' THEN 2
           WHEN 'lowest' THEN 1 WHEN 'normal' THEN 1 ELSE 0 END AS rnk
  FROM queue_forecast_task_runs r
  JOIN queue_forecast_tasks t ON r.task_id = t.task_id
  WHERE t.task_queue_id = $1
    AND r.pending_at <= $2::timestamptz
    AND (COALESCE(r.started_at, r.resolved_at) IS NULL
         OR COALESCE(r.started_at, r.resolved_at) > $2::timestamptz)
    AND NOT (r.task_id = $4 AND r.run_id = $5)
),
events AS (
  SELECT r.pending_at, r.started_at,
         CASE r.priority_at_pending
           WHEN 'highest' THEN 7 WHEN 'very-high' THEN 6 WHEN 'high' THEN 5
           WHEN 'medium' THEN 4 WHEN 'low' THEN 3 WHEN 'very-low' THEN 2
           WHEN 'lowest' THEN 1 WHEN 'normal' THEN 1 ELSE 0 END AS rnk
  FROM queue_forecast_task_runs r
  JOIN queue_forecast_tasks t ON r.task_id = t.task_id
  WHERE t.task_queue_id = $1
    -- Bound to the windows the outer query actually reads: arrivals (≤60m by
    -- pending_at) and starts (≤15m by started_at). A row can land in the starts
    -- window with an old pending_at (long wait, then a recent start), so the
    -- lower bound is an OR across both columns; the pending_at <= T upper bound
    -- is safe because anything started by T necessarily pended by T. Without
    -- this, the CTE materialized the queue's ENTIRE history on every prediction
    -- (minutes of runtime + GBs of pgsql_tmp spill). The 15/60m literals here
    -- must stay >= the largest window referenced in the SELECT below.
    AND r.pending_at <= $2::timestamptz
    AND (r.pending_at  > $2::timestamptz - INTERVAL '60 minutes'
         OR r.started_at > $2::timestamptz - INTERVAL '15 minutes')
    AND NOT (r.task_id = $4 AND r.run_id = $5)
),
allpending AS (
  SELECT 1 AS one
  FROM queue_forecast_task_runs r
  JOIN queue_forecast_tasks t ON r.task_id = t.task_id
  WHERE t.task_queue_id = $1
    AND r.pending_at <= $2::timestamptz
    AND (COALESCE(r.started_at, r.resolved_at) IS NULL
         OR COALESCE(r.started_at, r.resolved_at) > $2::timestamptz)
)
SELECT
  count(*) FILTER (WHERE rnk > $3)                                  AS pending_higher_priority_same_queue,
  -- Same-priority FIFO-before cohort. Matches the trainer's (pending_at,
  -- str(task_id), run_id) tie-break: strictly-earlier always counts, and
  -- same-instant peers count only when their (task_id, run_id) sorts before the
  -- target. The target itself is already excluded by the ahead CTE's NOT clause,
  -- and the prediction watermark ensures the same-instant cohort has landed.
  -- task_ids are ASCII (URL-safe base64), so COLLATE "C" (byte order) reproduces
  -- Python's str code-point comparison exactly; the DB default collation
  -- (en_US.UTF8) case-folds and would disagree on same-instant cohorts whose
  -- case-fold order differs from byte order. Equality (task_id = $4) is
  -- collation-independent, so the run_id tie-break needs no COLLATE.
  count(*) FILTER (WHERE rnk = $3 AND (pending_at < $2::timestamptz
                   OR (pending_at = $2::timestamptz AND (
                        (task_id COLLATE "C") < ($4 COLLATE "C")
                        OR (task_id = $4 AND run_id < $5)
                      )))) AS pending_same_priority_same_queue,
  count(*) FILTER (WHERE rnk < $3)                                  AS pending_lower_priority_same_queue,
  EXTRACT(EPOCH FROM ($2::timestamptz - min(pending_at) FILTER (WHERE rnk >= $3))) AS oldest_higher_or_equal_pending_age_same_queue,
  count(*) FILTER (WHERE rnk >= $3)                                 AS pending_higher_or_equal_excl_target,
  count(*) FILTER (WHERE rnk >= $3 AND repo_family = 'try')         AS pending_try_higher_or_equal_same_queue,
  count(*) FILTER (WHERE rnk >= $3 AND repo_family = 'autoland')    AS pending_autoland_higher_or_equal_same_queue,
  count(*) FILTER (WHERE rnk >= $3 AND repo_family = 'release_beta') AS pending_release_beta_higher_or_equal_same_queue,
  (SELECT count(*) FILTER (WHERE pending_at > $2::timestamptz - INTERVAL '15 minutes' AND pending_at <= $2::timestamptz) FROM events)
                                                                    AS arrivals_15m_same_queue,
  (SELECT count(*) FILTER (WHERE pending_at > $2::timestamptz - INTERVAL '60 minutes' AND pending_at <= $2::timestamptz) FROM events)
                                                                    AS arrivals_60m_same_queue,
  (SELECT count(*) FILTER (WHERE pending_at > $2::timestamptz - INTERVAL '15 minutes' AND pending_at <= $2::timestamptz AND rnk >= $3) FROM events)
                                                                    AS arrivals_higher_or_equal_15m_same_queue,
  (SELECT count(*) FILTER (WHERE pending_at > $2::timestamptz - INTERVAL '60 minutes' AND pending_at <= $2::timestamptz AND rnk >= $3) FROM events)
                                                                    AS arrivals_higher_or_equal_60m_same_queue,
  (SELECT count(*) FILTER (WHERE started_at > $2::timestamptz - INTERVAL '15 minutes' AND started_at <= $2::timestamptz AND rnk >= $3) FROM events)
                                                                    AS starts_higher_or_equal_15m_same_queue,
  (SELECT count(*) FROM allpending)                                AS pending_total_incl_target
FROM ahead;`;

const CAPACITY_SQL = `-- queue_context_capacity
SELECT running_workers, existing_capacity, claimed_tasks,
       EXTRACT(EPOCH FROM ($2::timestamptz - sampled_at)) AS capacity_sample_age_s
FROM queue_forecast_worker_counts
WHERE task_queue_id = $1 AND sampled_at <= $2::timestamptz
ORDER BY sampled_at DESC LIMIT 1;`;

// null/undefined -> NaN, else Number(v).
const num = (v) => (v === null || v === undefined ? NaN : Number(v));

function emptyFeatures() {
  const f = {};
  for (const k of FEATURE_COLUMNS) f[k] = NaN;
  f.capacity_null_reason = 'no_sample';
  return f;
}

/**
 * Build the queue-context feature object for a single target row against the
 * current DB state. Returns an object carrying exactly FEATURE_COLUMNS keys.
 *
 * @param {{query: (sql: string, params?: any[]) => Promise<{rows: any[]}>}} pool
 * @param {object} row - { task_queue_id, pending_at, priority_at_pending,
 *                         queue_pending, repo_family, task_id?, run_id? }
 */
export async function getQueueContext(pool, row) {
  const f = emptyFeatures();

  if (!row || !row.task_queue_id || !row.pending_at) {
    return f;
  }

  const T = row.pending_at instanceof Date
    ? row.pending_at.toISOString()
    : row.pending_at;
  const targetRank = rank(row.priority_at_pending);
  const taskId = row.task_id ?? null;
  const runId = row.run_id ?? null;
  const qp = num(row.queue_pending);

  const [backlogRes, capRes] = await Promise.all([
    pool.query(BACKLOG_SQL, [row.task_queue_id, T, targetRank, taskId, runId]),
    pool.query(CAPACITY_SQL, [row.task_queue_id, T]),
  ]);

  const b = backlogRes.rows[0] ?? {};

  // Backlog / blocking counts (default NaN when no row).
  f.pending_higher_priority_same_queue = num(b.pending_higher_priority_same_queue);
  // pending_same_priority counts strictly-earlier peers plus the same-instant
  // FIFO-before cohort (tie-break on (pending_at, task_id, run_id)), matching
  // the Python trainer's offline definition. The prediction watermark ensures
  // the same-instant cohort has landed before this runs.
  f.pending_same_priority_same_queue = num(b.pending_same_priority_same_queue);
  f.pending_lower_priority_same_queue = num(b.pending_lower_priority_same_queue);
  f.oldest_higher_or_equal_pending_age_same_queue = num(
    b.oldest_higher_or_equal_pending_age_same_queue,
  );
  f.arrivals_15m_same_queue = num(b.arrivals_15m_same_queue);
  f.arrivals_60m_same_queue = num(b.arrivals_60m_same_queue);
  f.arrivals_higher_or_equal_15m_same_queue = num(b.arrivals_higher_or_equal_15m_same_queue);
  f.arrivals_higher_or_equal_60m_same_queue = num(b.arrivals_higher_or_equal_60m_same_queue);
  f.starts_higher_or_equal_15m_same_queue = num(b.starts_higher_or_equal_15m_same_queue);
  f.pending_try_higher_or_equal_same_queue = num(b.pending_try_higher_or_equal_same_queue);
  f.pending_autoland_higher_or_equal_same_queue = num(b.pending_autoland_higher_or_equal_same_queue);
  f.pending_release_beta_higher_or_equal_same_queue = num(b.pending_release_beta_higher_or_equal_same_queue);

  // higher-or-equal INCLUDING target (for per-capacity divisions): excl + 1.
  const heExcl = num(b.pending_higher_or_equal_excl_target);
  const heInclTarget = Number.isNaN(heExcl) ? NaN : heExcl + 1;

  // Coverage: (pending-at-T all-rank incl target) / queue_pending.
  const pendingTotalInclTarget = num(b.pending_total_incl_target);
  f.backlog_coverage_ratio = qp > 0 ? pendingTotalInclTarget / qp : NaN;

  // Capacity. Default already set to no_sample / NaN by emptyFeatures().
  // A sample older than the staleness bound is treated exactly like no_sample:
  // running/existing/claimed/age and all per-capacity ratios stay NaN.
  const c = capRes.rows[0];
  const capAge = c ? num(c.capacity_sample_age_s) : NaN;
  if (c && !Number.isNaN(capAge) && capAge <= QUEUE_CONTEXT_CAPACITY_STALENESS_S) {
    f.running_workers = num(c.running_workers);
    f.existing_capacity = num(c.existing_capacity);
    f.claimed_tasks = num(c.claimed_tasks);
    f.capacity_sample_age_s = num(c.capacity_sample_age_s);

    const cap = c.existing_capacity;
    if (cap === null || cap === undefined) {
      f.capacity_null_reason = 'static_pool_null';
      // per-capacity stays NaN (never imputed 0).
    } else {
      const capVal = Number(cap);
      if (capVal === 0) {
        f.capacity_null_reason = 'zero_capacity';
        // per-capacity stays NaN.
      } else {
        f.capacity_null_reason = 'ok';
        // pending_total_per_capacity NaN when queue_pending <= 0.
        f.pending_total_per_capacity = qp > 0 ? qp / capVal : NaN;
        f.pending_higher_or_equal_per_capacity = Number.isNaN(heInclTarget)
          ? NaN
          : heInclTarget / capVal;
        const running = num(c.running_workers);
        f.running_per_capacity = Number.isNaN(running) ? NaN : running / capVal;
      }
    }
  }

  return f;
}
