-- Migration: task_events -> normalized two-table model
-- Run this against an existing database that has the task_events table.
-- The collector should be STOPPED before running this migration.

BEGIN;

-- ==========================================
-- Step 1: Create new tables
-- ==========================================

CREATE TABLE IF NOT EXISTS queue_forecast_tasks (
    task_created       TIMESTAMPTZ,
    enriched_at        TIMESTAMPTZ,
    max_run_time_s     INTEGER,
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

CREATE TABLE IF NOT EXISTS queue_forecast_task_runs (
    pending_at         TIMESTAMPTZ,
    started_at         TIMESTAMPTZ,
    resolved_at        TIMESTAMPTZ,
    wait_duration_s    DOUBLE PRECISION,
    run_duration_s     DOUBLE PRECISION,
    run_id             INT NOT NULL,
    queue_pending      INTEGER,
    task_id            TEXT NOT NULL
                       REFERENCES queue_forecast_tasks(task_id) ON DELETE CASCADE,
    priority_at_pending TEXT,
    reason_created     TEXT,
    reason_resolved    TEXT,
    PRIMARY KEY (task_id, run_id)
);

CREATE TABLE IF NOT EXISTS queue_forecast_run_predictions (
    predicted_at                 TIMESTAMPTZ DEFAULT now(),
    expected_completion_time     TIMESTAMPTZ,
    guaranteed_completion_time   TIMESTAMPTZ,
    wait_p50_s                   DOUBLE PRECISION,
    wait_p90_s                   DOUBLE PRECISION,
    run_p50_s                    DOUBLE PRECISION,
    run_p90_s                    DOUBLE PRECISION,
    run_id                       INT NOT NULL,
    task_id                      TEXT NOT NULL,
    model_version                TEXT NOT NULL,
    input_features               JSONB,
    PRIMARY KEY (task_id, run_id)
);

-- ==========================================
-- Step 2: Migrate data
-- ==========================================

-- A. Populate queue_forecast_tasks
--    DISTINCT ON grabs the most complete metadata per task_id
--    (latest run_id tends to have the richest enrichment)
INSERT INTO queue_forecast_tasks (
    task_id, task_queue_id, task_group_id, scheduler_id, project_id,
    metadata_name, normalized_name, original_priority,
    max_run_time_s, tags, task_created, enriched_at
)
SELECT DISTINCT ON (task_id)
    task_id, task_queue_id, task_group_id, scheduler_id, project_id,
    metadata_name, normalized_name, original_priority,
    max_run_time_s, tags, task_created,
    CASE WHEN metadata_name IS NOT NULL THEN now() END
FROM task_events
ORDER BY task_id, run_id DESC NULLS LAST
ON CONFLICT (task_id) DO NOTHING;

-- B. Populate queue_forecast_task_runs
--    Skip NULL run_id rows (task-defined placeholders with no actual run)
INSERT INTO queue_forecast_task_runs (
    task_id, run_id, priority_at_pending, reason_created, reason_resolved,
    pending_at, started_at, resolved_at, queue_pending,
    wait_duration_s, run_duration_s
)
SELECT
    task_id, run_id, priority, reason_created, reason_resolved,
    scheduled, started, resolved, queue_pending,
    wait_duration_s, run_duration_s
FROM task_events
WHERE run_id IS NOT NULL
ON CONFLICT (task_id, run_id) DO NOTHING;

-- ==========================================
-- Step 3: Create indexes
-- ==========================================

CREATE INDEX IF NOT EXISTS idx_qf_task_runs_training
    ON queue_forecast_task_runs (resolved_at)
    WHERE started_at IS NOT NULL
      AND run_duration_s IS NOT NULL
      AND reason_resolved IN ('completed', 'failed');

CREATE INDEX IF NOT EXISTS idx_qf_task_runs_unresolved
    ON queue_forecast_task_runs (pending_at)
    WHERE resolved_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_qf_tasks_unenriched
    ON queue_forecast_tasks (task_id)
    WHERE metadata_name IS NULL;

COMMIT;

-- ==========================================
-- Step 4: Verify (run these manually)
-- ==========================================

SELECT 'queue_forecast_tasks' AS tbl, count(*) FROM queue_forecast_tasks
UNION ALL
SELECT 'queue_forecast_task_runs', count(*) FROM queue_forecast_task_runs
UNION ALL
SELECT 'task_events (total)', count(*) FROM task_events
UNION ALL
SELECT 'task_events (with run_id)', count(*)
  FROM task_events WHERE run_id IS NOT NULL;

-- queue_forecast_task_runs count should match task_events-with-run_id count
-- queue_forecast_tasks count should match distinct task_id count

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

-- =====================================================
-- TABLE 6: Daily data-quality / health metrics
-- =====================================================
-- Stores per-day computed metrics derived from queue_forecast_task_runs +
-- queue_forecast_tasks. Used by the trainer to optionally filter anomalous
-- days from training/validation. Holdout is never filtered, only labeled.
--
-- The detector is data-driven: thresholds are absolute or relative to a
-- trailing-window median, NOT a hardcoded list of incident dates.
CREATE TABLE IF NOT EXISTS queue_forecast_daily_health (
    sample_date              DATE PRIMARY KEY,

    -- Raw counts (for transparency / re-derivation)
    n_total                  INTEGER NOT NULL,
    n_completed              INTEGER NOT NULL,
    n_failed                 INTEGER NOT NULL,
    n_exception              INTEGER NOT NULL,
    n_worker_shutdown        INTEGER NOT NULL,
    n_claim_expired          INTEGER NOT NULL,
    n_deadline_exceeded      INTEGER NOT NULL,
    n_canceled               INTEGER NOT NULL,
    n_started                INTEGER NOT NULL,            -- runs with started_at NOT NULL
    n_pending_no_start       INTEGER NOT NULL,            -- deadline-exceeded with started_at NULL
                                                          -- (proxy for "task defined but worker never picked up")

    -- Derived rates (NULL if n_total = 0)
    exception_rate           DOUBLE PRECISION,            -- (exception + worker_shutdown + claim_expired) / n_total
    stuck_pending_rate       DOUBLE PRECISION,            -- n_pending_no_start / n_total
    completion_rate          DOUBLE PRECISION,            -- n_completed / n_total
    wait_p99_s               DOUBLE PRECISION,            -- p99 of wait_duration_s among runs that started
    run_p99_s                DOUBLE PRECISION,            -- p99 of run_duration_s among completed runs

    -- Worker-count daily aggregates (NULL when n_worker_samples = 0)
    total_capacity_p50       INTEGER,
    total_capacity_min       INTEGER,
    total_running_p50        INTEGER,
    utilization_p50          DOUBLE PRECISION,
    n_worker_samples         INTEGER NOT NULL DEFAULT 0,

    -- Per-flag booleans. Each is independently triggerable so policies can
    -- subset (e.g. "only exclude on exception spikes, ignore wait p99").
    flag_exception_spike     BOOLEAN NOT NULL DEFAULT FALSE,
    flag_stuck_pending_spike BOOLEAN NOT NULL DEFAULT FALSE,
    flag_wait_p99_spike      BOOLEAN NOT NULL DEFAULT FALSE,
    flag_volume_anomaly      BOOLEAN NOT NULL DEFAULT FALSE,
    flag_low_completion      BOOLEAN NOT NULL DEFAULT FALSE,

    -- Worker-side flags
    flag_capacity_drop       BOOLEAN NOT NULL DEFAULT FALSE,
    flag_capacity_spike      BOOLEAN NOT NULL DEFAULT FALSE,
    flag_low_utilization     BOOLEAN NOT NULL DEFAULT FALSE,
    flag_sampler_offline     BOOLEAN NOT NULL DEFAULT FALSE,

    -- Aggregate convenience (recompute via UPDATE rather than GENERATED ALWAYS
    -- to keep this Postgres-version-agnostic and easy to evolve)
    is_anomalous             BOOLEAN NOT NULL DEFAULT FALSE,
    anomaly_reasons          TEXT[] NOT NULL DEFAULT '{}',

    -- Threshold values used at compute time (for auditability)
    threshold_snapshot       JSONB NOT NULL DEFAULT '{}'::jsonb,
    computed_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_qf_daily_health_anomalous
    ON queue_forecast_daily_health (sample_date)
    WHERE is_anomalous;

-- ==========================================
-- Stage 1.5: worker-side anomaly columns
-- Additive migration for existing daily-health tables. Safe to re-run.
-- ==========================================
ALTER TABLE queue_forecast_daily_health
    ADD COLUMN IF NOT EXISTS total_capacity_p50  INTEGER,
    ADD COLUMN IF NOT EXISTS total_capacity_min  INTEGER,
    ADD COLUMN IF NOT EXISTS total_running_p50   INTEGER,
    ADD COLUMN IF NOT EXISTS utilization_p50     DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS n_worker_samples    INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS flag_capacity_drop   BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS flag_capacity_spike  BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS flag_low_utilization BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS flag_sampler_offline BOOLEAN NOT NULL DEFAULT FALSE;
