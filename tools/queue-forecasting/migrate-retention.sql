-- Retention Migration
-- Idempotent: safe to re-run.
-- Run against an existing forecasting DB before starting the retention service.
--
-- queue_forecast_tasks had no index on task_created, so the retention job's
-- "delete tasks older than N days" prune would seq-scan the whole table. This
-- adds it. (run_predictions.predicted_at and worker_counts.sampled_at are
-- already indexed by earlier migrations / init.sql.)
--
-- NOTE: a plain CREATE INDEX briefly blocks writes while it builds (~tens of
-- seconds on ~20M rows). On a live DB you may prefer to run the concurrent
-- form OUTSIDE a transaction instead of this file:
--
--   CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_qf_tasks_task_created
--       ON queue_forecast_tasks (task_created);

BEGIN;

CREATE INDEX IF NOT EXISTS idx_qf_tasks_task_created
    ON queue_forecast_tasks (task_created);

COMMIT;
