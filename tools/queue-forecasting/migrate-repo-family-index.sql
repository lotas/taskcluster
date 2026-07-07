-- Repo-family backfill performance: partial index so selectTasksNeedingRepoFamily()
-- scans only rows still needing derivation, instead of a full nested-loop probe of
-- every in-window task per batch (which degraded to ~109s/batch as the backfill filled
-- the table in — an O(n^2) tail).
--
-- CREATE INDEX CONCURRENTLY must NOT run inside a transaction block, so there is no
-- BEGIN/COMMIT here. It builds without blocking the live collector's writes.
--
-- Apply:
--   docker compose exec -T postgres psql -U postgres -d forecasting -f - < migrate-repo-family-index.sql
-- If a build is interrupted, drop the leftover invalid index before retrying:
--   DROP INDEX IF EXISTS idx_qf_tasks_needs_repo_family;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_qf_tasks_needs_repo_family
    ON queue_forecast_tasks (task_id)
    WHERE repo_family_derivation_version IS NULL;
