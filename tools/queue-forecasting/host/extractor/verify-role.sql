-- Phase 2b-1 Task 6: PROVE the privileges rather than granting them.
--
-- `pg_current_snapshot()` needs no explicit grant: it is a pg_catalog builtin
-- and PostgreSQL grants EXECUTE on functions to PUBLIC by default
-- (https://www.postgresql.org/docs/15/ddl-priv.html). phase0-setup.sh revokes
-- CREATE on schema public and ALL on the database from PUBLIC, neither of which
-- touches function EXECUTE.
--
-- So no GRANT EXECUTE belongs in any migration unless check 2 below FAILS on the
-- live cluster. A grant added defensively for a privilege already held is a
-- permanent line of unexplained SQL that a future reviewer has to disprove.

-- 1. The read-only default is in force on the LIVE cluster, not just in a file.
SELECT rolname, rolconfig FROM pg_roles WHERE rolname = 'forecast_experiment';

-- 2. The snapshot function is executable by the extraction role.
SELECT has_function_privilege(
  'forecast_experiment',
  'pg_catalog.pg_current_snapshot()',
  'EXECUTE'
) AS can_read_snapshot;

-- 3. And proven live, in the exact transaction shape D19 uses. Run this AS
--    forecast_experiment, not as a superuser: a superuser would prove nothing
--    about the role the extractor actually connects as.
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT pg_catalog.pg_current_snapshot()::text AS snapshot;
ROLLBACK;

-- 4. The write canary, in the shape `pg.py` uses it. Expected: refused, with
--    SQLSTATE 25006 (read_only_sql_transaction) or 42501
--    (insufficient_privilege). `WHERE false` is what makes it safe to run here:
--    if BOTH controls are somehow missing it updates zero rows.
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
UPDATE queue_forecast_worker_pools SET task_queue_id = task_queue_id WHERE false;
ROLLBACK;
