import pg from 'pg';

export function createPool(databaseUrl) {
  return new pg.Pool({ connectionString: databaseUrl, max: 20 });
}

// --- Task upsert (queue_forecast_tasks) ---

const UPSERT_TASK_SQL = `
INSERT INTO queue_forecast_tasks (
  task_id, task_queue_id, task_group_id, scheduler_id, project_id,
  metadata_name, normalized_name, original_priority,
  max_run_time_s, tags, task_created
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
ON CONFLICT (task_id) DO UPDATE SET
  task_queue_id     = COALESCE(EXCLUDED.task_queue_id,     queue_forecast_tasks.task_queue_id),
  task_group_id     = COALESCE(EXCLUDED.task_group_id,     queue_forecast_tasks.task_group_id),
  scheduler_id      = COALESCE(EXCLUDED.scheduler_id,      queue_forecast_tasks.scheduler_id),
  project_id        = COALESCE(EXCLUDED.project_id,        queue_forecast_tasks.project_id),
  metadata_name     = COALESCE(EXCLUDED.metadata_name,     queue_forecast_tasks.metadata_name),
  normalized_name   = COALESCE(EXCLUDED.normalized_name,   queue_forecast_tasks.normalized_name),
  original_priority = COALESCE(queue_forecast_tasks.original_priority, EXCLUDED.original_priority),
  max_run_time_s    = COALESCE(EXCLUDED.max_run_time_s,    queue_forecast_tasks.max_run_time_s),
  tags              = COALESCE(EXCLUDED.tags,               queue_forecast_tasks.tags),
  task_created      = COALESCE(EXCLUDED.task_created,      queue_forecast_tasks.task_created);
`;

export async function upsertTask(pool, fields) {
  const {
    task_id,
    task_queue_id = null, task_group_id = null,
    scheduler_id = null, project_id = null,
    metadata_name = null, normalized_name = null,
    original_priority = null,
    max_run_time_s = null, tags = null, task_created = null,
  } = fields;

  await pool.query(UPSERT_TASK_SQL, [
    task_id,
    task_queue_id, task_group_id, scheduler_id, project_id,
    metadata_name, normalized_name, original_priority,
    max_run_time_s,
    tags ? JSON.stringify(tags) : null,
    task_created,
  ]);
}

// --- Task run upsert (queue_forecast_task_runs) ---

const UPSERT_TASK_RUN_SQL = `
INSERT INTO queue_forecast_task_runs (
  task_id, run_id, priority_at_pending, reason_created, reason_resolved,
  pending_at, started_at, resolved_at, queue_pending,
  wait_duration_s, run_duration_s
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
ON CONFLICT (task_id, run_id) DO UPDATE SET
  priority_at_pending = COALESCE(queue_forecast_task_runs.priority_at_pending, EXCLUDED.priority_at_pending),
  reason_created      = COALESCE(EXCLUDED.reason_created,    queue_forecast_task_runs.reason_created),
  reason_resolved     = COALESCE(EXCLUDED.reason_resolved,   queue_forecast_task_runs.reason_resolved),
  pending_at          = COALESCE(EXCLUDED.pending_at,        queue_forecast_task_runs.pending_at),
  started_at          = COALESCE(EXCLUDED.started_at,        queue_forecast_task_runs.started_at),
  resolved_at         = COALESCE(EXCLUDED.resolved_at,       queue_forecast_task_runs.resolved_at),
  queue_pending       = COALESCE(EXCLUDED.queue_pending,     queue_forecast_task_runs.queue_pending),
  wait_duration_s     = COALESCE(EXCLUDED.wait_duration_s,   queue_forecast_task_runs.wait_duration_s),
  run_duration_s      = COALESCE(EXCLUDED.run_duration_s,    queue_forecast_task_runs.run_duration_s);
`;

export async function upsertTaskRun(pool, fields) {
  const {
    task_id, run_id,
    priority_at_pending = null, reason_created = null, reason_resolved = null,
    pending_at = null, started_at = null, resolved_at = null,
    queue_pending = null,
    wait_duration_s = null, run_duration_s = null,
  } = fields;

  await pool.query(UPSERT_TASK_RUN_SQL, [
    task_id, run_id,
    priority_at_pending, reason_created, reason_resolved,
    pending_at, started_at, resolved_at,
    queue_pending,
    wait_duration_s, run_duration_s,
  ]);
}

// --- Task enrichment (queue_forecast_tasks only) ---

const ENRICH_TASK_SQL = `
UPDATE queue_forecast_tasks SET
  metadata_name     = COALESCE($2, metadata_name),
  normalized_name   = COALESCE($3, normalized_name),
  tags              = COALESCE($4, tags),
  task_created      = COALESCE($5, task_created),
  original_priority = COALESCE(original_priority, $6),
  task_queue_id     = COALESCE($7, task_queue_id),
  task_group_id     = COALESCE($8, task_group_id),
  scheduler_id      = COALESCE($9, scheduler_id),
  project_id        = COALESCE($10, project_id),
  max_run_time_s    = COALESCE($11, max_run_time_s),
  repo_family                    = COALESCE($12, repo_family),
  repo_family_source             = COALESCE($13, repo_family_source),
  repo_family_evidence           = COALESCE($14, repo_family_evidence),
  repo_family_derivation_version = COALESCE($15, repo_family_derivation_version),
  enriched_at       = COALESCE(enriched_at, now())
WHERE task_id = $1;
`;

export async function enrichTask(pool, taskId, enrichment) {
  const {
    metadata_name = null, normalized_name = null,
    tags = null, task_created = null, original_priority = null,
    task_queue_id = null, task_group_id = null,
    scheduler_id = null, project_id = null,
    max_run_time_s = null,
    repo_family = null, repo_family_source = null,
    repo_family_evidence = null, repo_family_derivation_version = null,
  } = enrichment;

  await pool.query(ENRICH_TASK_SQL, [
    taskId,
    metadata_name, normalized_name,
    tags ? JSON.stringify(tags) : null,
    task_created, original_priority,
    task_queue_id, task_group_id,
    scheduler_id, project_id,
    max_run_time_s,
    repo_family, repo_family_source, repo_family_evidence, repo_family_derivation_version,
  ]);
}

// --- Unenriched task query ---

const UNENRICHED_TASKS_SQL = `
SELECT task_id
FROM queue_forecast_tasks
WHERE metadata_name IS NULL
  AND ($2::text[] IS NULL OR task_id != ALL($2::text[]))
ORDER BY task_id
LIMIT $1;
`;

export async function getUnenrichedTaskIds(pool, limit = 200, excludeTaskIds = []) {
  const excludeParam = excludeTaskIds.length > 0 ? excludeTaskIds : null;
  const res = await pool.query(UNENRICHED_TASKS_SQL, [limit, excludeParam]);
  return res.rows.map(r => r.task_id);
}

// --- Repo-family backfill ---

const UPDATE_REPO_FAMILY_SQL = `
UPDATE queue_forecast_tasks SET
  repo_family = $2, repo_family_source = $3,
  repo_family_evidence = $4, repo_family_derivation_version = $5
WHERE task_id = $1;
`;

export async function updateRepoFamily(pool, taskId, rf) {
  await pool.query(UPDATE_REPO_FAMILY_SQL, [taskId, rf.family, rf.source, rf.evidence, rf.version]);
}

// "Needs work" is expressed canonically as repo_family_derivation_version IS NULL.
// That predicate is matched by the partial index idx_qf_tasks_needs_repo_family, so
// selection is O(remaining) instead of re-scanning the (mostly-done) table each batch.
//
// Why not `IS DISTINCT FROM $version`: a partial index `WHERE ... IS NULL` is only
// usable when the query predicate implies it, and IS DISTINCT FROM does not — so it
// forced a full nested-loop probe of every in-window task (see resetStaleRepoFamily
// for how a derivation-version bump re-establishes the NULL invariant).
//
// Window membership is a correlated EXISTS driven by queue_forecast_task_runs' PK
// (task_id, run_id), replacing an `IN (SELECT DISTINCT ...)` that rebuilt a full-window
// hash-aggregate on every batch. ORDER BY task_id keeps the scan forward-only: a row
// leaves the partial index the moment its version is set, so batches never rescan it.
const SELECT_REPO_FAMILY_BACKFILL_SQL = `
SELECT t.task_id
FROM queue_forecast_tasks t
WHERE t.repo_family_derivation_version IS NULL
  AND EXISTS (
    SELECT 1 FROM queue_forecast_task_runs r
    WHERE r.task_id = t.task_id
      AND r.pending_at >= $1::timestamptz AND r.pending_at < $2::timestamptz
  )
ORDER BY t.task_id
LIMIT $3;
`;

export async function selectTasksNeedingRepoFamily(pool, fromTs, toTs, limit) {
  const { rows } = await pool.query(SELECT_REPO_FAMILY_BACKFILL_SQL, [fromTs, toTs, limit]);
  return rows.map(r => r.task_id);
}

// On a derivation-LOGIC change the version constant is bumped; rows carrying an older
// (non-NULL, non-current) version within the window must be reprocessed. Re-NULLing
// their derivation version restores the "needs work <=> IS NULL" invariant so the fast
// path above re-selects them. Already-NULL and current-version rows are untouched. Only
// the version is cleared (not the family value), matching prior behaviour where a row
// keeps its last value until reprocessing overwrites it. Scoped to the same window as
// the backfill via EXISTS. Run once at backfill startup.
const RESET_STALE_REPO_FAMILY_SQL = `
UPDATE queue_forecast_tasks t
SET repo_family_derivation_version = NULL
WHERE t.repo_family_derivation_version IS NOT NULL
  AND t.repo_family_derivation_version <> $1
  AND EXISTS (
    SELECT 1 FROM queue_forecast_task_runs r
    WHERE r.task_id = t.task_id
      AND r.pending_at >= $2::timestamptz AND r.pending_at < $3::timestamptz
  );
`;

export async function resetStaleRepoFamily(pool, version, fromTs, toTs) {
  const { rowCount } = await pool.query(RESET_STALE_REPO_FAMILY_SQL, [version, fromTs, toTs]);
  return rowCount;
}
