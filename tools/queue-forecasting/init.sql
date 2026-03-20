CREATE TABLE IF NOT EXISTS task_events (
  task_id         TEXT    NOT NULL,
  run_id          INT,

  -- Task identity & features (populated on first event, from API fetch)
  task_queue_id   TEXT,
  task_group_id   TEXT,
  priority        TEXT,
  original_priority TEXT,
  metadata_name   TEXT,
  normalized_name TEXT,
  scheduler_id    TEXT,
  project_id      TEXT,
  tags            JSONB,
  worker_group    TEXT,
  max_run_time_s  INTEGER,
  image_name      TEXT,

  -- Task-level timestamp (same across all runs for a task)
  task_created    TIMESTAMPTZ,

  -- Run-level lifecycle timestamps (populated as events arrive; NULL on placeholder rows)
  scheduled       TIMESTAMPTZ,
  started         TIMESTAMPTZ,
  resolved        TIMESTAMPTZ,

  -- Resolution
  reason_created  TEXT,
  reason_resolved TEXT,

  -- Computed (filled on resolution, only when both scheduled and started are present)
  wait_duration_s DOUBLE PRECISION,
  run_duration_s  DOUBLE PRECISION,

  UNIQUE NULLS NOT DISTINCT (task_id, run_id)
);

CREATE INDEX IF NOT EXISTS idx_task_events_task_queue_id ON task_events (task_queue_id);
CREATE INDEX IF NOT EXISTS idx_task_events_resolved ON task_events (resolved);
CREATE INDEX IF NOT EXISTS idx_task_events_metadata_name ON task_events (metadata_name);
CREATE INDEX IF NOT EXISTS idx_task_events_task_group_id ON task_events (task_group_id);

-- Composite index for predictor duration queries (task_queue_id + resolved range)
CREATE INDEX IF NOT EXISTS idx_task_events_tqid_resolved ON task_events (task_queue_id, resolved)
  WHERE run_id IS NOT NULL AND reason_resolved = 'completed';

-- Composite index for predictor duration queries (metadata_name + resolved range)
CREATE INDEX IF NOT EXISTS idx_task_events_mname_resolved ON task_events (metadata_name, resolved)
  WHERE run_id IS NOT NULL AND run_duration_s IS NOT NULL AND reason_resolved = 'completed';

-- Composite expression index for tag-based cohort queries (kind + test-type + resolved)
CREATE INDEX IF NOT EXISTS idx_task_events_tags_kind_testtype ON task_events ((tags->>'kind'), (tags->>'test-type'), resolved)
  WHERE tags IS NOT NULL AND run_id IS NOT NULL AND run_duration_s IS NOT NULL AND reason_resolved = 'completed';

-- Composite index for predictor duration queries (normalized_name + resolved range)
CREATE INDEX IF NOT EXISTS idx_task_events_nname_resolved ON task_events (normalized_name, resolved)
  WHERE run_id IS NOT NULL AND run_duration_s IS NOT NULL AND reason_resolved = 'completed';

-- Composite index for predictor duration queries (scheduler_id + resolved range)
CREATE INDEX IF NOT EXISTS idx_task_events_schedid_resolved ON task_events (scheduler_id, resolved)
  WHERE run_id IS NOT NULL AND run_duration_s IS NOT NULL AND reason_resolved = 'completed';

-- Composite index for predictor duration queries (image_name + resolved range)
CREATE INDEX IF NOT EXISTS idx_task_events_imgname_resolved ON task_events (image_name, resolved)
  WHERE run_id IS NOT NULL AND run_duration_s IS NOT NULL AND reason_resolved = 'completed';

-- Partial index for backfill sweep (unenriched rows including placeholders)
CREATE INDEX IF NOT EXISTS idx_task_events_unenriched ON task_events (task_id)
  WHERE metadata_name IS NULL;
