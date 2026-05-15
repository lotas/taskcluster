-- Live Predictor Migration
-- Idempotent: safe to re-run.
-- Run against an existing forecasting DB before starting the live-predictor service.

BEGIN;

-- ==========================================================================
-- queue_forecast_run_predictions — additive columns for two model versions
-- ==========================================================================

-- Old single-version column kept for backward compat with any code that
-- still references it; new writes go to the two columns below.
ALTER TABLE queue_forecast_run_predictions
    ALTER COLUMN model_version DROP NOT NULL;

ALTER TABLE queue_forecast_run_predictions
    ADD COLUMN IF NOT EXISTS wait_model_version     TEXT,
    ADD COLUMN IF NOT EXISTS wait_artifact_hash     TEXT,
    ADD COLUMN IF NOT EXISTS duration_model_version TEXT,
    ADD COLUMN IF NOT EXISTS duration_artifact_hash TEXT;

CREATE INDEX IF NOT EXISTS idx_qf_run_predictions_predicted_at
    ON queue_forecast_run_predictions (predicted_at);

-- Catch-up reuses the existing partial index `idx_qf_task_runs_unresolved`
-- (defined in init.sql: `WHERE resolved_at IS NULL`). That covers both
-- pending-and-unstarted rows AND started-but-not-yet-resolved rows, which
-- is what the live predictor needs (per the design: predict regardless of
-- whether the task has started by the time we get to it).

-- ==========================================================================
-- Throughput query support — index task_queue_id on tasks, and a non-partial
-- index on resolved_at so the live throughput query can prune by time first.
-- ==========================================================================

CREATE INDEX IF NOT EXISTS idx_qf_tasks_task_queue_id
    ON queue_forecast_tasks (task_queue_id);

CREATE INDEX IF NOT EXISTS idx_qf_task_runs_resolved_at
    ON queue_forecast_task_runs (resolved_at)
    WHERE resolved_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_qf_task_runs_started_at
    ON queue_forecast_task_runs (started_at)
    WHERE started_at IS NOT NULL;

COMMIT;
