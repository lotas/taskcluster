-- load_task_runs_for_queue_context (trainer/src/data_loader.py) had no lower
-- bound on pending_at, so every cohort's reference-run query scanned the FULL
-- history of queue_forecast_task_runs and queue_forecast_tasks (confirmed via
-- EXPLAIN: Seq Scan on both sides, multi-TB cumulative read profile) — cost
-- that only grows as more days of data accumulate. The query now floors
-- pending_at/task_created at (window_start - lookback_days); these indexes
-- make that bounded range cheap to find instead of a full scan.
--
-- CREATE INDEX CONCURRENTLY must NOT run inside a transaction block, so there
-- is no BEGIN/COMMIT here. Builds without blocking the live collector.
--
-- Apply:
--   docker compose exec -T postgres psql -U postgres -d forecasting -f - < migrate-queue-context-perf.sql
-- If a build is interrupted, drop the leftover invalid index before retrying:
--   DROP INDEX IF EXISTS idx_qf_task_runs_pending_at;
--   DROP INDEX IF EXISTS idx_qf_task_runs_coalesce_exit;
-- idx_qf_tasks_task_created already exists (added for retention pruning) and
-- covers the tasks-side floor — no new index needed there.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_qf_task_runs_pending_at
    ON queue_forecast_task_runs (pending_at);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_qf_task_runs_coalesce_exit
    ON queue_forecast_task_runs (COALESCE(started_at, resolved_at));
