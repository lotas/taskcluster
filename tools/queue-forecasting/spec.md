# Queue Forecasting — Spec

## Overview

A standalone tool that listens to Mozilla's Taskcluster Pulse event stream,
collects task lifecycle data, and predicts how long tasks will take to run
and wait in queue. Lives in `tools/queue-forecasting/` within the Taskcluster
monorepo.

## Environment

- **Runtime:** Node.js (ESM) for collection and real-time inference;
  Python for nightly model training
- **Database:** Postgres 15 (shared with Taskcluster; all tables prefixed
  `queue_forecast_` to avoid collisions)
- **Deployment:** Docker Compose (collector + predictor + trainer + postgres)
- **Data source:** Taskcluster Pulse (AMQP) — real-time lifecycle events
- **Supplemental data:** Taskcluster Queue API — task definitions, queue depth

## Goals

### V1 (ship first)

1. **Per-task run duration prediction** — given a newly pending run, predict
   execution time (p50/p90) using LightGBM trained on task identity, queue,
   tags, and other stored attributes.
2. **Per-task wait time prediction** — predict queue wait time (p50/p90)
   using queue depth, priority, time-of-day, and queue identity.
   Goals 1+2 compose into an ETA.
3. **Prediction API** — expose predictions for newly pending runs with model
   version and confidence metadata. TC UI as first consumer.

### V2 (data collection starts now, features ship later)

4. **Queue-level forecasting** — "if I submit to this queue now, how long
   will it wait?" and "what is the expected drain time for the current
   backlog?" Reuses the wait-time model with hypothetical inputs.
5. **Queue load prediction** — predict pending count for a given queue at a
   given hour and day-of-week. Requires time-series queue depth data
   collected from V1 onward.

### Non-goals

- Predicting from `task-defined` (dependency resolution is a different problem)
- Re-predicting while a task is already running
- Provisioning or autoscaling decisions
- Predicting for unscheduled tasks waiting on dependencies

## Data Volume (observed)

- ~250k task runs/day (~1.1M rows over first 5 days of collection)
- ~7.5M rows/month projected
- ~2GB/week raw
- tags JSONB field averages ~200 bytes per row

## Data Model

The system uses a normalized two-table model separating task-level definition
facts from run-level execution facts. This replaces the original single
`task_events` table. All table names are prefixed `queue_forecast_` to
avoid collisions in a shared database.

### `queue_forecast_tasks`

One row per `task_id`. Stores definition-time identity and metadata.
Column ordering optimized for Postgres tuple alignment (8-byte, 4-byte,
variable-length).

```sql
CREATE TABLE queue_forecast_tasks (
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
```

Notes:
- `normalized_name` is `metadata_name` with trailing hash suffixes stripped
  (e.g. `test-linux2404-64/opt-mochitest-1@a3b4c5d6e7f8` →
  `test-linux2404-64/opt-mochitest-1`). Must come from a deterministic,
  versioned normalization function.
- `tags` is raw JSONB preserved as-is from the task definition. All tag-based
  feature extraction (kind, os, test-type, worker-implementation, build type)
  happens at training time in Python, keeping the schema deployment-agnostic.
- `enriched_at` is set when the Queue API fetch fills in metadata_name and tags.

### `queue_forecast_task_runs`

One row per execution attempt `(task_id, run_id)`.

```sql
CREATE TABLE queue_forecast_task_runs (
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
```

Notes:
- `priority_at_pending` is a snapshot at enqueue time. We do not train on
  mutable "current priority".
- `queue_pending` is the approximate queue depth snapshot nearest to
  `pending_at`, sourced from in-memory counters seeded and periodically
  synced from the Queue API.
- `wait_duration_s = started_at - pending_at`
- `run_duration_s = resolved_at - started_at`
- Runs that never start keep both duration fields NULL.

### `queue_forecast_run_predictions`

Every prediction is logged before the outcome is known, enabling evaluation.

```sql
CREATE TABLE queue_forecast_run_predictions (
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
```

Notes:
- `expected_completion_time = pending_at + wait_p50_s + run_p50_s`
- `guaranteed_completion_time = pending_at + wait_p90_s + run_p90_s`
- `input_features` captures the exact feature vector fed to the model,
  enabling post-hoc debugging ("why did the model predict 45 minutes?").
- One prediction per run. If models are updated, the old prediction is
  overwritten.

### Indexes

```sql
-- Training sweep: grab last N days of clean completed runs
CREATE INDEX idx_qf_task_runs_training
    ON queue_forecast_task_runs (resolved_at)
    WHERE started_at IS NOT NULL
      AND run_duration_s IS NOT NULL
      AND reason_resolved IN ('completed', 'failed');

-- Reconciler: find stuck runs
CREATE INDEX idx_qf_task_runs_unresolved
    ON queue_forecast_task_runs (pending_at)
    WHERE resolved_at IS NULL;

-- Enrichment backfill: find tasks missing metadata
CREATE INDEX idx_qf_tasks_unenriched
    ON queue_forecast_tasks (task_id)
    WHERE metadata_name IS NULL;
```

## Collection

### Architecture

Data ingestion is **Pulse-first, API-reconciled**. The collector subscribes
to all task lifecycle events via AMQP and upserts into the normalized tables.
A separate reconciler repairs missed or incomplete state via the Queue API.

The collector must **not assume event order**. Any event may arrive before
another event for the same task or run. Every handler must:
- upsert the row if it does not exist,
- fill only the fields it knows,
- avoid overwriting a later lifecycle state with an earlier one.

### Queue Pending Counters

The collector maintains in-memory pending counts per `task_queue_id`:
- Seeded from the Queue API on first encounter via `taskQueueCounts()`
- Incremented on `task-pending`, decremented on `task-running`
- Periodically synced against the API (every 60s) to correct drift
- Snapshot written to `queue_forecast_task_runs.queue_pending` at
  `task-pending` time

These are approximate values — documented as such. Good enough for modeling.

### Event Routing

#### `task-defined`
- **UPSERT** into `queue_forecast_tasks`
- Extracts: `task_queue_id`, `scheduler_id`, `project_id`, `tags`
- No row created in `queue_forecast_task_runs` — a run has not been
  enqueued yet
- Triggers background API enrichment if `metadata_name` is NULL

#### `task-pending`
- **UPSERT** into `queue_forecast_tasks` (in case `task-defined` was missed)
- **UPSERT** into `queue_forecast_task_runs` for `(task_id, run_id)`
- Captures: `pending_at`, `priority_at_pending`, `queue_pending` snapshot,
  `reason_created`
- Triggers prediction via `predictor.js`, stores result in
  `queue_forecast_run_predictions`

#### `task-running`
- **UPSERT** into `queue_forecast_tasks` (in case earlier events were missed)
- **UPDATE** `queue_forecast_task_runs` for `(task_id, run_id)`
- Captures: `started_at`
- Computes `wait_duration_s` if `pending_at` is already set

#### `task-completed` / `task-failed`
- **UPSERT** into `queue_forecast_tasks`
- **UPDATE** `queue_forecast_task_runs` for `(task_id, run_id)`
- Captures: `resolved_at`, `reason_resolved`
- Computes `run_duration_s` if `started_at` is already set

#### `task-exception`
- Same as completed/failed for runs with a `run_id`
- Special case: exception with no `run_id` (deadline-exceeded before any run
  started) — update the last known run in `queue_forecast_task_runs` if one
  exists, otherwise no run row to update

#### `task-priority-changed` / `task-group-priority-changed`
- Updates `queue_forecast_tasks` only (informational, not used for training
  since we snapshot `priority_at_pending` at enqueue time)

### Background API Enrichment

On every event, if the task's `metadata_name` is NULL in `queue_forecast_tasks`:
- Check in-memory cache first (keyed by `task_id`)
- If not cached, fetch task definition from Queue API
- Fill: `metadata_name`, `normalized_name`, `original_priority`,
  `max_run_time_s`, `tags`, `task_created`
- Cache the enrichment data so subsequent run events for the same task
  don't require another API call
- Concurrency-limited (max 50 in-flight fetches)

## Reconciliation

Taskcluster Pulse is at-most-once delivery. Events can be dropped, and
automated retries may not publish `task-exception` for dead runs. The
reconciler ensures training data stays clean.

### Reconciler Job

Runs as a background cron (every 15 minutes).

#### Stuck Runs
1. Query `queue_forecast_task_runs` for rows where `resolved_at IS NULL`
   and either:
   - `pending_at + max_run_time_s + 1 hour < now()` (when max_run_time_s
     known via join to `queue_forecast_tasks`)
   - `pending_at + INTERVAL '24 hours' < now()` (fallback)
2. Fetch true state from Queue API `taskStatus()` for each stuck task
3. If API shows terminal: update `queue_forecast_task_runs` with correct
   timestamps and resolution
4. If API shows the run was silently dropped: set
   `reason_resolved = 'reconciler-dropped'` so training explicitly
   excludes it

#### Missing Enrichment
1. Query `queue_forecast_tasks` for rows where `metadata_name IS NULL`
   and `enriched_at IS NULL` and task first seen more than 5 minutes ago
2. Fetch task definition from Queue API
3. Fill metadata fields

This merges the current backfill sweep into the reconciler — one repair
job instead of two.

## Machine Learning Pipeline

### Algorithm: LightGBM

We use LightGBM (Light Gradient Boosting Machine), a gradient-boosted
decision tree algorithm. For tabular data with high-cardinality categories
(like `task_queue_id` and `metadata_name`), tree-based models outperform
neural networks in both speed and accuracy.

LightGBM builds hundreds of shallow decision trees sequentially. Each tree
corrects the errors of the previous ones. This naturally captures feature
interactions — for example, learning that high queue depth on weekends
affects wait time differently than on weekdays.

### Two Separate Models

The system trains two independent models nightly. They have different
targets, training filters, feature sets, and lookback windows.

#### Run Duration Model (`run_duration_model.onnx`)

Predicts how long a task will execute once a worker picks it up.

**Target:** `run_duration_s`

**Training filter:**
```sql
SELECT r.run_duration_s, r.queue_pending,
       t.task_queue_id, t.metadata_name, t.normalized_name,
       t.scheduler_id, t.max_run_time_s, t.tags,
       r.pending_at
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.resolved_at > now() - INTERVAL '30 days'
  AND r.started_at IS NOT NULL
  AND r.run_duration_s IS NOT NULL
  AND r.reason_resolved IN ('completed', 'failed')
```

**Lookback:** 30 days. Run times are tied to code and payloads, relatively
stable over time.

**Why include `failed`:** A test that runs for 25 minutes then fails still
took 25 minutes. Excluding failures would bias the model toward only
successful (often shorter) runs.

**Exclude:** `worker-shutdown`, `claim-expired`, `malformed-payload`,
`reconciler-dropped` — these are infrastructure artifacts, not workload
runtime.

**Features:**

| Feature | Type | Source | Notes |
|---------|------|--------|-------|
| `metadata_name` | categorical | tasks | Most specific identifier (~5k unique/day) |
| `normalized_name` | categorical | tasks | Groups retriggered variants |
| `task_queue_id` | categorical | tasks | Worker pool identity (~50-100 unique) |
| `scheduler_id` | categorical | tasks | Broad cohort (gecko-level-1, etc.) |
| `max_run_time_s` | numeric | tasks | Declared timeout, correlates with task weight |
| `tags->>'kind'` | categorical | tasks.tags | mochitest, build, signing, etc. |
| `tags->>'test-type'` | categorical | tasks.tags | mochitest, wpt, reftest, etc. |
| `tags->>'os'` | categorical | tasks.tags | linux, windows, macos |
| `tags->>'project'` | categorical | tasks.tags | try, autoland, mozilla-central |
| `tags->>'worker-implementation'` | categorical | tasks.tags | docker-worker vs generic-worker |

**Build type extraction:** The Python trainer extracts `debug` vs `opt` from
`metadata_name` via regex (e.g. `test-linux2404-64/debug-...` → `debug`).
This is one of the strongest run duration predictors in Firefox CI.

#### Wait Time Model (`wait_time_model.onnx`)

Predicts how long a task will sit in queue before a worker picks it up.

**Target:** `wait_duration_s`

**Training filter:**
```sql
SELECT r.wait_duration_s, r.queue_pending, r.priority_at_pending,
       t.task_queue_id, t.scheduler_id, t.tags,
       r.pending_at
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.resolved_at > now() - INTERVAL '14 days'
  AND r.started_at IS NOT NULL
  AND r.queue_pending IS NOT NULL
```

**Lookback:** 14 days. Wait times reflect current infrastructure capacity
and are highly recency-sensitive. Stale capacity data hurts more than
limited sample size.

**Why resolution doesn't matter:** Once a run started, queue wait is
observed regardless of whether it later completed or failed.

**Features:**

| Feature | Type | Source | Notes |
|---------|------|--------|-------|
| `task_queue_id` | categorical | tasks | Most important baseline |
| `priority_at_pending` | categorical | task_runs | Critical for scheduling order |
| `queue_pending` | numeric | task_runs | Backlog depth at enqueue time |
| `scheduler_id` | categorical | tasks | Cohort behavior |
| `max_run_time_s` | numeric | tasks | Task weight signal |
| `tags->>'kind'` | categorical | tasks.tags | Workload type |
| `tags->>'os'` | categorical | tasks.tags | Platform |
| `tags->>'project'` | categorical | tasks.tags | try vs autoland behave differently |
| `hour_sin`, `hour_cos` | numeric | derived | Cyclical encoding of hour-of-day (UTC) |
| `day_sin`, `day_cos` | numeric | derived | Cyclical encoding of day-of-week |

**Cyclical time encoding:**
```
hour_sin = sin(2 * pi * hour / 24)
hour_cos = cos(2 * pi * hour / 24)
day_sin  = sin(2 * pi * day_of_week / 7)
day_cos  = cos(2 * pi * day_of_week / 7)
```

This ensures the model understands that 23:00 and 00:00 are adjacent,
and Friday and Monday are close.

### Training Strategy

**Sliding window retrain**, not incremental learning. Every night:

1. Python trainer queries Postgres for the relevant lookback window
2. Trains a fresh LightGBM model from scratch (discards yesterday's model)
3. Uses `objective=quantile` with `alpha=0.5` for p50, `alpha=0.9` for p90
   (two training passes per model, or a single multi-quantile model)
4. Exports to ONNX format
5. Writes `run_duration_model.onnx` and `wait_time_model.onnx` to a
   shared volume

**Why not incremental:** Decision tree incremental learning leads to tree
bloat (slowing inference) and struggles to adapt when new queue names or
task types appear. A fresh retrain automatically forgets outdated patterns.

### Feature Engineering (Python)

All feature engineering happens in the Python training script:

- **Categorical handling:** High-cardinality strings cast to Pandas
  `category` dtype. LightGBM handles these natively without one-hot encoding.
- **Tag extraction:** `tags->>'kind'`, `tags->>'os'`, etc. extracted from
  JSONB into typed columns. Deployment-specific — only the trainer knows
  which tag keys matter.
- **Build type:** Regex extraction of `debug`/`opt` from `metadata_name`.
- **Time features:** Cyclical encoding derived from `pending_at` timestamp.
- **NULL handling:** LightGBM handles NaN/NULL natively for both numeric
  and categorical features.

## Real-Time Inference

### ONNX Runtime in Node.js

The `predictor.js` service loads both `.onnx` model files into memory
using `onnxruntime-node`. When a `task-pending` event arrives:

1. Collector upserts the run into `queue_forecast_task_runs`
2. Collector calls the predictor with the task/run features
3. Predictor applies the same feature engineering as training:
   - Categorical encoding (string -> integer mapping, loaded alongside
     the ONNX model as a JSON sidecar file)
   - Cyclical time encoding from `pending_at`
   - Build type regex extraction from `metadata_name`
4. Runs both models (run duration + wait time) in-memory
5. Composes the ETA:
   - `expected_completion_time = pending_at + wait_p50 + run_p50`
   - `guaranteed_completion_time = pending_at + wait_p90 + run_p90`
6. Writes prediction to `queue_forecast_run_predictions`

Inference latency target: low single-digit milliseconds per prediction.
No network calls to Python. No database reads for historical stats.

### Category Mapping Sidecar

LightGBM categorical features are integer-coded during training. The
Python trainer must export a `category_mappings.json` alongside each
ONNX model containing the string-to-integer mapping for every categorical
feature. The Node.js predictor loads this at startup and on model reload.

**Parity requirement:** Float/double precision can drift between Python
and ONNX inference. Automated parity tests between Python predictions
and Node.js ONNX predictions are a strict requirement before any model
is deployed.

### Model Hot-Reload

The predictor watches the shared model volume for new `.onnx` files.
When the nightly trainer writes a new model:
- Predictor detects the new file (filesystem watch or polling)
- Loads new model + category mappings into memory
- Swaps atomically (old model serves requests until new one is ready)
- Logs the model version transition

### Cold Start Handling

When LightGBM encounters a categorical value it has never seen during
training (e.g., a brand new `metadata_name` or `task_queue_id`):

- LightGBM treats unseen categoricals as a separate "unknown" bucket
  and routes them through decision tree branches based on other features
- This means a brand new task type still gets a prediction — it just
  relies more heavily on `task_queue_id`, `tags`, `scheduler_id`,
  and other features the model has seen
- The `input_features` JSONB in `queue_forecast_run_predictions` should
  flag which features were unknown, enabling evaluation of cold-start
  accuracy
- After one nightly retrain cycle, the new task type enters the
  training data and gets proper coverage

## API

### Prediction Endpoint

```
GET /v1/predict/:taskId/:runId
```

**Response:**
```json
{
  "taskId": "VGx8Q3kRTe2...",
  "runId": 0,
  "prediction": {
    "waitTime": {
      "p50_seconds": 142.3,
      "p90_seconds": 412.8
    },
    "runDuration": {
      "p50_seconds": 1823.7,
      "p90_seconds": 2401.2
    },
    "eta": {
      "expected": "2026-03-27T14:32:00Z",
      "guaranteed": "2026-03-27T15:05:00Z"
    },
    "modelVersion": "2026-03-27-nightly",
    "predictedAt": "2026-03-27T14:00:12Z"
  }
}
```

This endpoint reads from `queue_forecast_run_predictions`. If the
prediction already exists (generated at `task-pending` time), it returns
it. If the run exists but has no prediction yet (race condition or missed
event), it generates one on the fly.

### Queue Status Endpoint (V2)

```
GET /v1/queue/:taskQueueId/estimate
```

Returns predicted wait time for a hypothetical new task entering this
queue right now, using current `queue_pending` count and the wait-time
model. Deferred to V2 but the data model supports it from day 1.

## Evaluation

Every prediction is stored in `queue_forecast_run_predictions` before
the outcome is known. A daily evaluation job compares predictions
against actuals.

### Methodology

- **Strict time-split only.** Never random split. Train on days 1-N,
  evaluate on day N+1. Random splitting leaks future information.
- Evaluation runs automatically after each nightly training cycle.

### Metrics

| Metric | Description |
|--------|-------------|
| **Within-2x rate** | % of `eta_estimate` predictions within 0.5x-2x of actual total time. Target: >80% |
| **Pinball loss (p50)** | Measures median prediction accuracy. Lower is better. |
| **Pinball loss (p90)** | Measures upper-bound prediction accuracy. |
| **p90 calibration** | Does the p90 prediction actually cover ~90% of observed durations? |
| **Coverage** | % of pending runs that received a prediction (vs cold-start fallback) |
| **Fallback rate** | % of predictions where key features were unseen by the model |

### Slices

Metrics must be computed across slices, not just globally:
- By `task_queue_id` (top 20 queues by volume)
- By `priority_at_pending`
- By `tags->>'project'` (try vs autoland vs mozilla-central)
- By cold-start status (was `metadata_name` in the training set?)

A model that looks great globally but fails on the highest-volume queue
is not deployable.

### Evaluation Query

```sql
SELECT
    rp.task_id,
    rp.run_id,
    rp.wait_p50_s,
    rp.run_p50_s,
    rp.expected_completion_time,
    rp.guaranteed_completion_time,
    rp.model_version,
    r.wait_duration_s   AS actual_wait,
    r.run_duration_s    AS actual_run,
    r.pending_at,
    r.resolved_at,
    t.task_queue_id,
    t.tags
FROM queue_forecast_run_predictions rp
JOIN queue_forecast_task_runs r
  ON rp.task_id = r.task_id AND rp.run_id = r.run_id
JOIN queue_forecast_tasks t
  ON rp.task_id = t.task_id
WHERE r.resolved_at IS NOT NULL
  AND r.started_at IS NOT NULL
  AND r.resolved_at >= $1::date
  AND r.resolved_at < $1::date + INTERVAL '1 day'
```

### Rollout

| Phase | What ships | Predictions visible? |
|-------|-----------|---------------------|
| **Phase 1** | Collector, reconciler, nightly trainer, predictor | Stored only. Internal evaluation. |
| **Phase 2** | Prediction API | Debug/internal consumers. TC UI behind flag. |
| **Phase 3** | TC UI integration | Default-on for supported queues. |

No model is exposed to users without passing automated evaluation on
the metrics above.

## Data Retention

### Raw Data

`queue_forecast_tasks` and `queue_forecast_task_runs` enforce a rolling
**45-day** retention window. 45 days provides margin beyond the 30-day
training window for debugging, evaluation lookback, and reconciliation
of late-arriving events.

To avoid expensive row-by-row `DELETE` operations:
- `queue_forecast_task_runs` is partitioned by week on `pending_at` using
  Postgres native range partitioning
- Expired data is dropped by detaching and destroying the oldest partition
- A weekly cron handles partition management (create next week's
  partition, drop partitions older than 45 days)

`queue_forecast_tasks` rows are cleaned up via `CASCADE` when their last
associated run partition is dropped. Alternatively, a lightweight sweep
deletes orphaned `queue_forecast_tasks` rows with no remaining
`queue_forecast_task_runs` references.

### Predictions

`queue_forecast_run_predictions` follows the same 45-day retention,
partitioned on `predicted_at`.

### Model Artifacts

Keep the last 7 days of `.onnx` model files and `category_mappings.json`
on the shared volume. Allows quick rollback if a nightly model degrades.
Older artifacts are deleted.

## Migration from `task_events`

The existing `task_events` table contains ~1.1M rows (5 days of data).
This migration splits it into the normalized two-table model without
data loss.

### Step 1: Create the new tables

```sql
CREATE TABLE queue_forecast_tasks (
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

CREATE TABLE queue_forecast_task_runs (
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

CREATE TABLE queue_forecast_run_predictions (
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
```

### Step 2: Migrate the data

```sql
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
ORDER BY task_id, run_id DESC NULLS LAST;

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
WHERE run_id IS NOT NULL;
```

### Step 3: Create indexes

```sql
CREATE INDEX idx_qf_task_runs_training
    ON queue_forecast_task_runs (resolved_at)
    WHERE started_at IS NOT NULL
      AND run_duration_s IS NOT NULL
      AND reason_resolved IN ('completed', 'failed');

CREATE INDEX idx_qf_task_runs_unresolved
    ON queue_forecast_task_runs (pending_at)
    WHERE resolved_at IS NULL;

CREATE INDEX idx_qf_tasks_unenriched
    ON queue_forecast_tasks (task_id)
    WHERE metadata_name IS NULL;
```

### Step 4: Verify and cutover

```sql
-- Verify row counts
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
```

Once verified:
1. Stop the collector
2. Run the migration
3. Deploy updated collector that writes to the new tables
4. Verify new events land correctly
5. Rename or drop `task_events` when confident

### Step 5: Add partitioning (post-migration)

After the initial migration is stable, convert `queue_forecast_task_runs`
to range-partitioned on `pending_at` by week. This is a separate step
because partitioning an existing table requires recreating it.

```sql
-- Create partitioned version
CREATE TABLE queue_forecast_task_runs_part (
    LIKE queue_forecast_task_runs INCLUDING ALL
) PARTITION BY RANGE (pending_at);

-- Create weekly partitions
CREATE TABLE queue_forecast_task_runs_w2026_12
    PARTITION OF queue_forecast_task_runs_part
    FOR VALUES FROM ('2026-03-23') TO ('2026-03-30');
CREATE TABLE queue_forecast_task_runs_w2026_13
    PARTITION OF queue_forecast_task_runs_part
    FOR VALUES FROM ('2026-03-30') TO ('2026-04-06');
-- ... etc

-- Migrate data, swap tables
```

## Deferred / Future Work

- **Queue depth time-series** (`queue_forecast_queue_depth_samples` table) —
  needed for goal 5 (queue load prediction by time/day). Start collecting
  once V1 prediction pipeline is stable.
- **Queue drain forecasting** — builds on wait-time model + queue depth
  data. V2 feature.
- **Trend/regression detection** — daily cohort rollups comparing trailing
  7-day vs 28-day quantiles. Requires stable evaluation pipeline first.
- **LightGBM shadow mode comparison** — running a new model version
  side-by-side with production and auto-promoting only if it wins on
  evaluation metrics.
- **TC UI integration** — wiring the prediction API into the task
  detail view.
