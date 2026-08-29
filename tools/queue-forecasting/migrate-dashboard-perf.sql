-- Dashboard freshness queries need the newest successful enrichment. Without
-- this index, PostgreSQL scans the entire retained task table each refresh.
--
-- CONCURRENTLY keeps collector/enrichment writes available while the index is
-- built on an existing production database. Run this file outside a transaction.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_qf_tasks_enriched_at
    ON queue_forecast_tasks (enriched_at DESC)
    WHERE enriched_at IS NOT NULL;
