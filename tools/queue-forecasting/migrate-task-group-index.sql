-- Index for per-group lookups (API /api/v0/group/:id, groups.html label joins).
-- CONCURRENTLY: no long lock against the collector's writes. Must run outside a
-- transaction. Re-run is a no-op. If a previous attempt was interrupted, drop the
-- INVALID leftover first: DROP INDEX CONCURRENTLY idx_qf_tasks_task_group_id;
--   docker exec -i queue-forecasting-postgres-1 psql -U postgres -d forecasting < migrate-task-group-index.sql
SET lock_timeout = '5s';
SET maintenance_work_mem = '1GB';
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_qf_tasks_task_group_id
    ON queue_forecast_tasks (task_group_id);
