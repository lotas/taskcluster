-- Queue Forecasting Schema
-- Normalized two-table model: tasks (identity) + task_runs (execution)

-- ==========================================
-- TABLE 1: Task-level identity and metadata
-- ==========================================
CREATE TABLE IF NOT EXISTS queue_forecast_tasks (
    -- 8-byte types
    task_created       TIMESTAMPTZ,
    enriched_at        TIMESTAMPTZ,

    -- 4-byte types
    max_run_time_s     INTEGER,

    -- Variable-length
    task_id            TEXT PRIMARY KEY,
    task_queue_id      TEXT,
    task_group_id      TEXT,
    scheduler_id       TEXT,
    project_id         TEXT,
    metadata_name      TEXT,
    normalized_name    TEXT,
    original_priority  TEXT,
    tags               JSONB
);

-- ==========================================
-- TABLE 2: Per-run execution data
-- ==========================================
CREATE TABLE IF NOT EXISTS queue_forecast_task_runs (
    -- 8-byte types
    pending_at         TIMESTAMPTZ,
    started_at         TIMESTAMPTZ,
    resolved_at        TIMESTAMPTZ,
    wait_duration_s    DOUBLE PRECISION,
    run_duration_s     DOUBLE PRECISION,

    -- 4-byte types
    run_id             INT NOT NULL,
    queue_pending      INTEGER,

    -- Variable-length
    task_id            TEXT NOT NULL
                       REFERENCES queue_forecast_tasks(task_id) ON DELETE CASCADE,
    priority_at_pending TEXT,
    reason_created     TEXT,
    reason_resolved    TEXT,

    PRIMARY KEY (task_id, run_id)
);

-- ==========================================
-- TABLE 3: Prediction log (one per run)
-- ==========================================
CREATE TABLE IF NOT EXISTS queue_forecast_run_predictions (
    -- 8-byte types
    predicted_at                 TIMESTAMPTZ DEFAULT now(),
    expected_completion_time     TIMESTAMPTZ,
    guaranteed_completion_time   TIMESTAMPTZ,
    wait_p50_s                   DOUBLE PRECISION,
    wait_p90_s                   DOUBLE PRECISION,
    run_p50_s                    DOUBLE PRECISION,
    run_p90_s                    DOUBLE PRECISION,

    -- 4-byte types
    run_id                       INT NOT NULL,

    -- Variable-length
    task_id                      TEXT NOT NULL,
    model_version                TEXT NOT NULL,
    input_features               JSONB,

    PRIMARY KEY (task_id, run_id)
);

-- ==========================================
-- INDEXES
-- ==========================================

-- Training sweep: last N days of clean completed runs
CREATE INDEX IF NOT EXISTS idx_qf_task_runs_training
    ON queue_forecast_task_runs (resolved_at)
    WHERE started_at IS NOT NULL
      AND run_duration_s IS NOT NULL
      AND reason_resolved IN ('completed', 'failed');

-- Reconciler: find stuck/unresolved runs
CREATE INDEX IF NOT EXISTS idx_qf_task_runs_unresolved
    ON queue_forecast_task_runs (pending_at)
    WHERE resolved_at IS NULL;

-- Enrichment backfill: find tasks missing metadata
CREATE INDEX IF NOT EXISTS idx_qf_tasks_unenriched
    ON queue_forecast_tasks (task_id)
    WHERE metadata_name IS NULL;

-- ==========================================
-- TABLE 4: Worker-count time series
-- ==========================================
CREATE TABLE IF NOT EXISTS queue_forecast_worker_counts (
    sampled_at         TIMESTAMPTZ NOT NULL,
    task_queue_id      TEXT NOT NULL,
    running_workers    INTEGER,
    claimed_tasks      INTEGER,
    existing_capacity  INTEGER,
    source             TEXT NOT NULL,
    PRIMARY KEY (task_queue_id, sampled_at)
);

CREATE INDEX IF NOT EXISTS idx_qf_worker_counts_sampled_at
    ON queue_forecast_worker_counts (sampled_at);

-- ==========================================
-- TABLE 5: Worker pool classification (daily-refreshed dimension)
-- ==========================================
CREATE TABLE IF NOT EXISTS queue_forecast_worker_pools (
    task_queue_id   TEXT PRIMARY KEY,
    pool_kind       TEXT NOT NULL,     -- 'dynamic' | 'static' | 'unknown'
    provider_type   TEXT,              -- for dynamic: 'aws' | 'azure' | 'google' | 'static' | etc.
    refreshed_at    TIMESTAMPTZ NOT NULL
);
