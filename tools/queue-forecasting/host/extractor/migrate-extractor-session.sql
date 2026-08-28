-- Phase 2b-1 Task 6, the one real change. NOT APPLIED BY THE SETUP SCRIPT:
-- it needs a superuser credential and applying DDL to a live database is the
-- operator's call.
--
-- WHY THIS IS LOAD-BEARING AND NOT AN OPTIMISATION. `temp_file_limit` (20GB) and
-- `work_mem` (512MB) are already set on the role by phase0-setup.sh, and both are
-- enforced PER PROCESS. Parallel workers are separate processes, so with the
-- server's default four workers per gather a single query can spill roughly five
-- times the limit. Setting the worker count to zero is what makes the limit
-- already on the role behave like a limit.
--
-- PostgreSQL documents the multiplication explicitly:
--   https://www.postgresql.org/docs/15/runtime-config-resource.html
ALTER ROLE forecast_experiment SET max_parallel_workers_per_gather = 0;

-- Verification, to be run after the ALTER. `rolconfig` is the live value; a
-- setting correct in this file and absent from the cluster is the exact failure
-- shape this project keeps finding, so NC17b asserts the cluster and not the
-- file.
SELECT rolname, rolconfig FROM pg_roles WHERE rolname = 'forecast_experiment';
