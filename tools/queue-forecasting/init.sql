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
    tags               JSONB,
    repo_family        TEXT,
    repo_family_source TEXT,
    repo_family_evidence TEXT,
    repo_family_derivation_version INTEGER
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
    model_version                TEXT,                     -- legacy, kept nullable
    wait_model_version           TEXT,
    wait_artifact_hash           TEXT,
    duration_model_version       TEXT,
    duration_artifact_hash       TEXT,
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

-- Dashboard freshness: newest successful enrichment without scanning all tasks.
CREATE INDEX IF NOT EXISTS idx_qf_tasks_enriched_at
    ON queue_forecast_tasks (enriched_at DESC)
    WHERE enriched_at IS NOT NULL;

-- Repo-family backfill: find tasks still needing derivation. Partial + task_id-ordered
-- so selectTasksNeedingRepoFamily() is an O(remaining) index scan instead of a full
-- nested-loop probe of every in-window task each batch.
CREATE INDEX IF NOT EXISTS idx_qf_tasks_needs_repo_family
    ON queue_forecast_tasks (task_id)
    WHERE repo_family_derivation_version IS NULL;

-- Live predictor: throughput query support
CREATE INDEX IF NOT EXISTS idx_qf_tasks_task_queue_id
    ON queue_forecast_tasks (task_queue_id);

-- Trainer queue-context reference load (load_task_runs_for_queue_context):
-- bounded to [window_start - lookback_days, as_of) x still-open-or-recently-exited.
-- Without these, both sides of the join scanned the full history of an
-- ever-growing table on every cohort (confirmed via EXPLAIN: multi-TB read
-- profile). idx_qf_tasks_task_created (below) covers the tasks-side floor.
CREATE INDEX IF NOT EXISTS idx_qf_task_runs_pending_at
    ON queue_forecast_task_runs (pending_at);
CREATE INDEX IF NOT EXISTS idx_qf_task_runs_coalesce_exit
    ON queue_forecast_task_runs (COALESCE(started_at, resolved_at));

-- Retention: prune tasks older than the retention window by task_created
-- (deleting old tasks cascades to their task_runs via the FK).
CREATE INDEX IF NOT EXISTS idx_qf_tasks_task_created
    ON queue_forecast_tasks (task_created);

CREATE INDEX IF NOT EXISTS idx_qf_task_runs_resolved_at
    ON queue_forecast_task_runs (resolved_at)
    WHERE resolved_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_qf_task_runs_started_at
    ON queue_forecast_task_runs (started_at)
    WHERE started_at IS NOT NULL;

-- Live predictor: predicted_at lookup for the dashboard
CREATE INDEX IF NOT EXISTS idx_qf_run_predictions_predicted_at
    ON queue_forecast_run_predictions (predicted_at);

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
--
-- Anomaly classification matrix
-- -----------------------------
-- Flags are orthogonal; combinations distinguish operational regimes from
-- training-data quality problems. Selected meaningful combinations:
--
--   volume_anomaly (high) + capacity_spike
--       -> legitimate task spike. Workers scaled up to meet real demand.
--          Not a quality problem; useful regime for training.
--
--   wait_p99_spike + capacity_drop
--       -> real capacity shortage. Tasks queued because workers disappeared.
--          Bad day for training (workers not representative).
--
--   wait_p99_spike + capacity_spike + low_utilization
--       -> scheduling failure. Workers exist but aren't claiming tasks
--          (provisioner/queue mismatch, broken claim path). Hard exclude.
--
--   capacity_spike + low_utilization (alone)
--       -> wasteful over-provisioning. Operational concern, NOT a data
--          quality problem; informational only (does not flip is_anomalous).
--
--   volume_anomaly (low n_total) + sampler_offline
--       -> data loss. Worker-counter or task collector dropped samples;
--          excludes day from training.
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

    -- Worker-count daily aggregates (NULL when n_worker_samples = 0).
    -- All "total_*" values aggregate across queues at each timestamp first,
    -- then take a percentile/min over time.
    total_capacity_p50       INTEGER,                     -- p50 over time of sum(existing_capacity) across queues
    total_capacity_min       INTEGER,                     -- min over time
    total_running_p50        INTEGER,                     -- p50 over time of sum(running_workers)
    utilization_p50          DOUBLE PRECISION,            -- p50 over time of sum(running)/sum(capacity)
    n_worker_samples         INTEGER NOT NULL DEFAULT 0,  -- count of distinct sampled_at values

    -- Per-flag booleans. Each is independently triggerable so policies can
    -- subset (e.g. "only exclude on exception spikes, ignore wait p99").
    flag_exception_spike     BOOLEAN NOT NULL DEFAULT FALSE,
    flag_stuck_pending_spike BOOLEAN NOT NULL DEFAULT FALSE,
    flag_wait_p99_spike      BOOLEAN NOT NULL DEFAULT FALSE,
    flag_volume_anomaly      BOOLEAN NOT NULL DEFAULT FALSE,
    flag_low_completion      BOOLEAN NOT NULL DEFAULT FALSE,

    -- Worker-side flags. capacity_drop and sampler_offline indicate genuine
    -- training-data-quality problems and contribute to is_anomalous by
    -- default. capacity_spike and low_utilization are informational
    -- (operational classifications, not data quality) and only contribute
    -- when callers opt in via the trainer's anomaly_filter.flag_subset.
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
