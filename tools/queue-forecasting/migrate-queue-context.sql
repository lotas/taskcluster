-- Bet 1: repo-family enrichment columns on queue_forecast_tasks.
-- Derivation happens at enrichment time (collector) and via backfill; we
-- store only the result + minimal evidence, NOT raw route arrays.
BEGIN;
ALTER TABLE queue_forecast_tasks
    ADD COLUMN IF NOT EXISTS repo_family                    TEXT,
    ADD COLUMN IF NOT EXISTS repo_family_source             TEXT,   -- source|route|scheduler|unknown
    ADD COLUMN IF NOT EXISTS repo_family_evidence           TEXT,   -- short matched token/path
    ADD COLUMN IF NOT EXISTS repo_family_derivation_version INTEGER;
COMMIT;
