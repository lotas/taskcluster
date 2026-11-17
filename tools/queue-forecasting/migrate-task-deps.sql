-- SUPERSEDED 2026-11-17 by migrate-task-deps-inline.sql (edges now live in
-- queue_forecast_tasks.dependencies TEXT[]). Kept for history; do not re-apply the
-- CREATE TABLE part on a fresh DB.
-- Task dependency edges + scheduling fields for task-group ETA (group-eta-plan.md P0).
-- Idempotent: safe to re-run. Apply BEFORE restarting the collector with the
-- matching src/db.js change, otherwise enrichment UPDATEs fail on unknown columns:
--   docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U postgres -d forecasting < migrate-task-deps.sql
--
-- dependency_count semantics: NULL = edges not collected (task enriched before this
-- migration, or never enriched); 0 = collected, no dependencies; N = N unique deps.
--
-- Edges cascade-delete with their task row, so the retention service (which trims
-- queue_forecast_tasks by task_created) bounds this table with no extra target.
-- dependency_task_id deliberately has no FK: the dependency may be outside our
-- collection window or deleted first by retention.
--
-- Roll back:
--   DROP TABLE queue_forecast_task_deps;
--   ALTER TABLE queue_forecast_tasks DROP COLUMN requires, DROP COLUMN deadline, DROP COLUMN dependency_count;

-- ADD COLUMN (nullable, no default) is metadata-only but needs a brief exclusive
-- lock; fail fast rather than queue behind the collector's writes.
SET lock_timeout = '5s';

BEGIN;

ALTER TABLE queue_forecast_tasks
    ADD COLUMN IF NOT EXISTS requires         TEXT,
    ADD COLUMN IF NOT EXISTS deadline         TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS dependency_count INTEGER;

CREATE TABLE IF NOT EXISTS queue_forecast_task_deps (
    task_id             TEXT NOT NULL
                        REFERENCES queue_forecast_tasks(task_id) ON DELETE CASCADE,
    dependency_task_id  TEXT NOT NULL,
    PRIMARY KEY (task_id, dependency_task_id)
);

-- Reverse lookup ("what does this task unblock?") for critical-path replay.
CREATE INDEX IF NOT EXISTS idx_qf_task_deps_dependency
    ON queue_forecast_task_deps (dependency_task_id);

COMMIT;
