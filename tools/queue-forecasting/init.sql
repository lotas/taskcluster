CREATE TABLE IF NOT EXISTS task_events (
    -- ==========================================
    -- BLOCK 1: 8-BYTE TYPES (Timestamps & Floats)
    -- ==========================================
    task_created       TIMESTAMPTZ,
    scheduled          TIMESTAMPTZ,
    started            TIMESTAMPTZ,
    resolved           TIMESTAMPTZ,
    wait_duration_s    DOUBLE PRECISION,
    run_duration_s     DOUBLE PRECISION,

    -- ==========================================
    -- BLOCK 2: 4-BYTE TYPES (Integers)
    -- ==========================================
    run_id             INT,
    max_run_time_s     INTEGER,
    queue_pending      INTEGER,

    -- ==========================================
    -- BLOCK 3: VARIABLE LENGTH (Text & JSONB)
    -- ==========================================
    task_id            TEXT NOT NULL,
    task_queue_id      TEXT,
    task_group_id      TEXT,
    priority           TEXT,
    original_priority  TEXT,
    metadata_name      TEXT,
    normalized_name    TEXT,
    scheduler_id       TEXT,
    project_id         TEXT,
    worker_group       TEXT,
    image_name         TEXT,
    reason_created     TEXT,
    reason_resolved    TEXT,
    tags               JSONB,

    UNIQUE NULLS NOT DISTINCT (task_id, run_id)
);

-- ==========================================
-- INDEXES
-- ==========================================

-- 1. For the Collector: Fast lookups for the background API enrichment fetch
CREATE INDEX IF NOT EXISTS idx_task_events_unenriched
    ON task_events (task_id)
    WHERE metadata_name IS NULL;

-- 2. For the ML / Aggregation Pipeline:
-- This single index replaces all your previous composite indexes.
-- The nightly cron job will use this to instantly grab the last 30 days of clean data.
CREATE INDEX IF NOT EXISTS idx_task_events_nightly_sweep
    ON task_events (resolved)
    WHERE run_id IS NOT NULL
      AND run_duration_s IS NOT NULL
      AND reason_resolved = 'completed';
