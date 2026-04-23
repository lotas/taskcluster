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
  } = enrichment;

  await pool.query(ENRICH_TASK_SQL, [
    taskId,
    metadata_name, normalized_name,
    tags ? JSON.stringify(tags) : null,
    task_created, original_priority,
    task_queue_id, task_group_id,
    scheduler_id, project_id,
    max_run_time_s,
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
