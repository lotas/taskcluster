-- Move dependency edges inline: queue_forecast_tasks.dependencies TEXT[]
-- (supersedes the queue_forecast_task_deps table from migrate-task-deps.sql).
-- Why: the edge table measured ~173 B/edge incl. PK index => ~26 GB at 60-day
-- retention. Inline arrays reuse the enrichment UPDATE and carry no index.
--
-- Step 1 (before restarting the collector with the inline db.js):
--   docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U postgres -d forecasting -v step=1 < migrate-task-deps-inline.sql
-- Step 2 (after restart; copies already-collected edges, then drops the table):
--   docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U postgres -d forecasting -v step=2 < migrate-task-deps-inline.sql
--
-- Roll back: ALTER TABLE queue_forecast_tasks DROP COLUMN dependencies;

SET lock_timeout = '5s';

\if :{?step}
\else
  \echo 'set -v step=1 or -v step=2'
  \quit
\endif

SELECT (:step = 1) AS is_step1 \gset
\if :is_step1
  ALTER TABLE queue_forecast_tasks ADD COLUMN IF NOT EXISTS dependencies TEXT[];
\else
  SELECT to_regclass('queue_forecast_task_deps') IS NOT NULL AS has_table \gset
  \if :has_table
    -- Copy first and commit: DROP TABLE needs AccessExclusive on the FK's referenced
    -- table (queue_forecast_tasks), which deadlocks with collector UPDATEs if done in
    -- the same transaction as the copy (seen 2026-11-17).
    SET lock_timeout = '60s';
    UPDATE queue_forecast_tasks t
       SET dependencies = d.deps
      FROM (SELECT task_id, array_agg(dependency_task_id ORDER BY dependency_task_id) AS deps
              FROM queue_forecast_task_deps GROUP BY task_id) d
     WHERE t.task_id = d.task_id AND t.dependencies IS NULL;
    -- Collected with zero deps under the old schema: dependency_count = 0, no rows.
    UPDATE queue_forecast_tasks
       SET dependencies = '{}'
     WHERE dependency_count = 0 AND dependencies IS NULL;
    -- Short lock wait; rerun step 2 if it times out (the copy is idempotent).
    SET lock_timeout = '2s';
    DROP TABLE queue_forecast_task_deps;
  \endif
\endif
