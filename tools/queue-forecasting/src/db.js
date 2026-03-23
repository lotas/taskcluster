import pg from 'pg';

export function createPool(databaseUrl) {
  return new pg.Pool({ connectionString: databaseUrl, max: 20 });
}

// Promote a run_id=NULL placeholder to a real run row, merging fields.
// Only promotes if no row with the target run_id already exists.
const PROMOTE_PLACEHOLDER_SQL = `
UPDATE task_events SET
  run_id            = $2,
  task_queue_id     = COALESCE($3,  task_events.task_queue_id),
  task_group_id     = COALESCE($4,  task_events.task_group_id),
  priority          = COALESCE($5,  task_events.priority),
  original_priority = COALESCE(task_events.original_priority, $6),
  metadata_name     = COALESCE($7,  task_events.metadata_name),
  scheduler_id      = COALESCE($8,  task_events.scheduler_id),
  project_id        = COALESCE($9,  task_events.project_id),
  tags              = COALESCE($10, task_events.tags),
  worker_group      = COALESCE($11, task_events.worker_group),
  task_created      = COALESCE($12, task_events.task_created),
  scheduled         = COALESCE($13, task_events.scheduled),
  started           = COALESCE($14, task_events.started),
  resolved          = COALESCE($15, task_events.resolved),
  reason_created    = COALESCE($16, task_events.reason_created),
  reason_resolved   = COALESCE($17, task_events.reason_resolved),
  queue_pending     = COALESCE($18, task_events.queue_pending),
  wait_duration_s   = COALESCE($19, task_events.wait_duration_s),
  run_duration_s    = COALESCE($20, task_events.run_duration_s),
  normalized_name   = COALESCE($21, task_events.normalized_name),
  max_run_time_s    = COALESCE($22, task_events.max_run_time_s),
  image_name        = COALESCE($23, task_events.image_name)
WHERE task_id = $1 AND run_id IS NULL
  AND NOT EXISTS (SELECT 1 FROM task_events WHERE task_id = $1 AND run_id = $2);
`;

const UPSERT_SQL = `
INSERT INTO task_events (
  task_id, run_id,
  task_queue_id, task_group_id, priority, original_priority,
  metadata_name, scheduler_id, project_id, tags, worker_group,
  task_created, scheduled, started, resolved,
  reason_created, reason_resolved,
  queue_pending,
  wait_duration_s, run_duration_s,
  normalized_name, max_run_time_s, image_name
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23)
ON CONFLICT (task_id, run_id) DO UPDATE SET
  task_queue_id     = COALESCE(EXCLUDED.task_queue_id,     task_events.task_queue_id),
  task_group_id     = COALESCE(EXCLUDED.task_group_id,     task_events.task_group_id),
  priority          = COALESCE(EXCLUDED.priority,              task_events.priority),
  original_priority = COALESCE(task_events.original_priority, EXCLUDED.original_priority),
  metadata_name     = COALESCE(EXCLUDED.metadata_name,     task_events.metadata_name),
  normalized_name   = COALESCE(EXCLUDED.normalized_name,   task_events.normalized_name),
  scheduler_id      = COALESCE(EXCLUDED.scheduler_id,      task_events.scheduler_id),
  project_id        = COALESCE(EXCLUDED.project_id,        task_events.project_id),
  tags              = COALESCE(EXCLUDED.tags,               task_events.tags),
  worker_group      = COALESCE(EXCLUDED.worker_group,      task_events.worker_group),
  max_run_time_s    = COALESCE(EXCLUDED.max_run_time_s,    task_events.max_run_time_s),
  image_name        = COALESCE(EXCLUDED.image_name,        task_events.image_name),
  task_created      = COALESCE(EXCLUDED.task_created,      task_events.task_created),
  scheduled         = COALESCE(EXCLUDED.scheduled,         task_events.scheduled),
  started           = COALESCE(EXCLUDED.started,           task_events.started),
  resolved          = COALESCE(EXCLUDED.resolved,          task_events.resolved),
  reason_created    = COALESCE(EXCLUDED.reason_created,    task_events.reason_created),
  reason_resolved   = COALESCE(EXCLUDED.reason_resolved,   task_events.reason_resolved),
  queue_pending     = COALESCE(EXCLUDED.queue_pending,     task_events.queue_pending),
  wait_duration_s   = COALESCE(EXCLUDED.wait_duration_s,   task_events.wait_duration_s),
  run_duration_s    = COALESCE(EXCLUDED.run_duration_s,    task_events.run_duration_s);
`;

export async function upsertTaskEvent(pool, fields) {
  const {
    task_id, run_id = null,
    task_queue_id = null, task_group_id = null,
    priority = null, original_priority = null,
    metadata_name = null, scheduler_id = null,
    project_id = null, tags = null, worker_group = null,
    task_created = null,
    scheduled = null, started = null, resolved = null,
    reason_created = null, reason_resolved = null,
    wait_duration_s = null, run_duration_s = null,
    queue_pending = null,
    normalized_name = null, max_run_time_s = null, image_name = null,
  } = fields;

  const params = [
    task_id, run_id,
    task_queue_id, task_group_id, priority, original_priority,
    metadata_name, scheduler_id, project_id,
    tags ? JSON.stringify(tags) : null,
    worker_group,
    task_created, scheduled, started, resolved,
    reason_created, reason_resolved,
    queue_pending,
    wait_duration_s, run_duration_s,
    normalized_name, max_run_time_s, image_name,
  ];

  // When a run event arrives, try to promote the run_id=NULL placeholder first.
  // This avoids creating a duplicate row for the same task.
  if (run_id != null) {
    const promoteRes = await pool.query(PROMOTE_PLACEHOLDER_SQL, params);
    if (promoteRes.rowCount > 0) return;
  }

  await pool.query(UPSERT_SQL, params);
}

const ENRICH_SQL = `
UPDATE task_events SET
  metadata_name     = COALESCE($2, task_events.metadata_name),
  tags              = COALESCE($3, task_events.tags),
  task_created      = COALESCE($4, task_events.task_created),
  original_priority = COALESCE(task_events.original_priority, $5),
  task_queue_id     = COALESCE($6, task_events.task_queue_id),
  task_group_id     = COALESCE($7, task_events.task_group_id),
  scheduler_id      = COALESCE($8, task_events.scheduler_id),
  project_id        = COALESCE($9, task_events.project_id),
  normalized_name   = COALESCE($10, task_events.normalized_name),
  max_run_time_s    = COALESCE($11, task_events.max_run_time_s),
  image_name        = COALESCE($12, task_events.image_name)
WHERE task_id = $1;
`;

export async function enrichTaskRows(pool, taskId, enrichment) {
  const {
    metadata_name = null, tags = null,
    task_created = null, original_priority = null,
    task_queue_id = null, task_group_id = null,
    scheduler_id = null, project_id = null,
    normalized_name = null, max_run_time_s = null, image_name = null,
  } = enrichment;

  await pool.query(ENRICH_SQL, [
    taskId,
    metadata_name,
    tags ? JSON.stringify(tags) : null,
    task_created, original_priority,
    task_queue_id, task_group_id,
    scheduler_id, project_id,
    normalized_name, max_run_time_s, image_name,
  ]);
}

const UPDATE_PRIORITY_BY_TASK_SQL = `
UPDATE task_events
SET priority = $2
WHERE task_id = $1;
`;

export async function updatePriorityByTask(pool, taskId, newPriority) {
  await pool.query(UPDATE_PRIORITY_BY_TASK_SQL, [taskId, newPriority]);
}

const UPDATE_PRIORITY_BY_GROUP_SQL = `
UPDATE task_events
SET priority = $2
WHERE task_group_id = $1;
`;

export async function updatePriorityByGroup(pool, taskGroupId, newPriority) {
  await pool.query(UPDATE_PRIORITY_BY_GROUP_SQL, [taskGroupId, newPriority]);
}

const UNENRICHED_TASKS_SQL = `
SELECT DISTINCT task_id
FROM task_events
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
