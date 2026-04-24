# Queue Forecasting — Spec

## Overview

A standalone tool that listens to Mozilla's Taskcluster Pulse event stream,
collects task lifecycle data, and predicts how long tasks will take to run
and wait in queue. Lives in `tools/queue-forecasting/` within the Taskcluster
monorepo.

**V1 design (gated on feature maturity)**

Phase 1 experiments showed direct LightGBM under-performs the percentile
baseline on both targets. Phase 2 validated a residual architecture where the
baseline percentile prediction is fed to LightGBM as an input feature and the
model learns a log-ratio correction. This architecture clears the MAE target on
both run_duration (−6.3%) and wait_time (−15.3%), but wait-time ratio-accuracy
on long waits (30m+ bucket: 38.6% within-2x) is not yet user-acceptable.

This spec describes the **intended** V1 design. Production deployment is
explicitly gated on closing the long-tail ratio-accuracy gap — primarily via
queue-velocity features (active worker count, tasks_completed_in_last_N_min)
that would let the model distinguish a fast-draining queue from a stalled one.
Training pipeline, schema, serving flow, and artifacts are all defined here
because they are stable; the sequencing is: (1) feature work, (2) re-evaluate,
(3) if 30m+ bucket reaches within-2x ≥ 50% and overall ratio-accuracy is
user-acceptable, proceed to ONNX export + Node inference wiring.

The **baseline percentile stats are a first-class serving artifact**, not just
an experimentation aid — they will be exported nightly alongside the ONNX
models and loaded by the predictor at startup.

## Environment

- **Runtime:** Node.js (ESM) for collection and real-time inference;
  Python for nightly model training
- **Database:** Postgres 15 (shared with Taskcluster; all tables prefixed
  `queue_forecast_` to avoid collisions)
- **Deployment:** Docker Compose (collector + predictor + trainer + postgres)
- **Data source:** Taskcluster Pulse (AMQP) — real-time lifecycle events
- **Supplemental data:** Taskcluster Queue API — task definitions, queue depth

## Goals

### V1 (intended design; production gated on feature maturity)

1. **Per-task run duration prediction** — given a newly pending run, predict
   execution time (p50/p90) using a residual LightGBM model: a percentile
   baseline provides a memorized p50, which is fed as an input feature to
   LightGBM that learns a log-ratio correction on top.
2. **Per-task wait time prediction** — predict queue wait time (p50/p90)
   using the same residual architecture: percentile baseline (keyed on
   queue + pending bucket, priority, etc.) provides p50, LightGBM corrects.
   Goals 1+2 compose into an ETA.
3. **Prediction API** — expose predictions for newly pending runs with model
   version and confidence metadata. TC UI as first consumer.
4. **Queue depth time-series collection** — the collector writes a
   `queue_forecast_queue_depth_samples` row per queue every 5 minutes from
   V1 onward, seeding the data needed for V2 queue load prediction.
5. **Worker-count time-series collection** — a dedicated `worker-counter`
   service polls the Taskcluster `worker-manager.listWorkerPoolsStats` API
   every 5 minutes and writes per-pool worker counts to
   `queue_forecast_worker_counts`. Collection begins in V1 so that enough
   history is available when queue-velocity features are introduced in
   Phase 3a training. See Collection §"Worker Count Sampling".

### V2 (features ship later, data collected from V1)

6. **Queue-level forecasting** — "if I submit to this queue now, how long
   will it wait?" and "what is the expected drain time for the current
   backlog?" Reuses the wait-time model with hypothetical inputs.
7. **Queue load prediction** — predict pending count for a given queue at a
   given hour and day-of-week. Uses time-series queue depth data collected
   continuously from V1 onward (see goal 4).

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
    predictor_kind               TEXT NOT NULL,
    input_features               JSONB,

    PRIMARY KEY (task_id, run_id, model_version, predictor_kind)
);
CREATE INDEX idx_qf_run_predictions_run
    ON queue_forecast_run_predictions (task_id, run_id);
CREATE INDEX idx_qf_run_predictions_predicted_at
    ON queue_forecast_run_predictions (predicted_at);
```

Notes:
- `expected_completion_time = pending_at + wait_p50_s + run_p50_s`
- `guaranteed_completion_time = pending_at + wait_p90_s + run_p90_s`
- `input_features` captures the exact feature vector fed to the model,
  enabling post-hoc debugging ("why did the model predict 45 minutes?").
- `predictor_kind` is a TEXT discriminator identifying which predictor
  produced the row. Known values: `'baseline'`, `'residual_lightgbm'`,
  `'lightgbm_direct'`. Future variants (e.g. XGBoost) add new values.
- The primary key is `(task_id, run_id, model_version, predictor_kind)`.
  Old predictions are **not** overwritten when models are updated; each
  model version and predictor kind appends its own row. This enables
  side-by-side shadow-mode comparison across model versions without
  requiring a separate table.
- Retention: the 45-day rolling window applies, enforced via partition
  on `predicted_at` (same pattern as `queue_forecast_task_runs`).
- Lookup by run: use the `(task_id, run_id)` index; the API should
  select the row with the current `predictor_kind` and latest
  `model_version` for serving.

### `queue_forecast_queue_depth_samples`

Periodic snapshots of queue depth per `task_queue_id`, collected from V1
onward. Required for V2 queue load prediction (goal 6). Collection starts
immediately in V1 so that enough history exists when V2 features are built.

```sql
CREATE TABLE queue_forecast_queue_depth_samples (
    sampled_at     TIMESTAMPTZ NOT NULL,
    task_queue_id  TEXT NOT NULL,
    queue_pending  INTEGER NOT NULL,
    PRIMARY KEY (task_queue_id, sampled_at)
);
CREATE INDEX idx_qf_depth_samples_sampled_at
    ON queue_forecast_queue_depth_samples (sampled_at);
```

Notes:
- The collector writes one row per active `task_queue_id` every 5 minutes
  during its existing periodic sync cycle (the same cycle that refreshes
  in-memory pending counts from the Queue API). The 5-minute interval is
  cheap — ~100 queues × 12 samples/hour × 24 hours = ~28k rows/day.
- `queue_pending` is the same in-memory approximate counter used for the
  `queue_forecast_task_runs.queue_pending` snapshot — no additional API call.
- Retention: 45-day rolling window, same as the other tables.

### `queue_forecast_worker_counts`

Per-pool worker-count snapshots collected by the `worker-counter` service every
5 minutes. These time-series rows power the queue-velocity features added to the
wait-time model in Phase 3a.

```sql
CREATE TABLE queue_forecast_worker_counts (
    sampled_at         TIMESTAMPTZ NOT NULL,
    task_queue_id      TEXT NOT NULL,
    running_workers    INTEGER,         -- NULL for static pools (V1 accepted gap)
    claimed_tasks      INTEGER,         -- derived from queue_forecast_task_runs
    existing_capacity  INTEGER,         -- NULL for static pools
    source             TEXT NOT NULL,   -- 'tc_api' | 'prometheus_historical'
    PRIMARY KEY (task_queue_id, sampled_at)
);
CREATE INDEX idx_qf_worker_counts_sampled_at
    ON queue_forecast_worker_counts (sampled_at);
```

Notes:
- `running_workers` and `existing_capacity` come from `worker-manager.listWorkerPoolsStats`
  (`runningCount` and `currentCapacity` fields). Dynamic pools only — static
  pools carry `NULL` for both columns in V1.
- `claimed_tasks` is derived from our own `queue_forecast_task_runs` table:
  rows with `started_at IS NOT NULL AND resolved_at IS NULL` per `task_queue_id`.
  No external API call is required — this is a key simplification that makes the
  "busy-worker" signal free from data already collected.
- `source = 'tc_api'` for live samples from the `worker-counter` service.
  Historical coverage before the service started can be backfilled from
  Prometheus with `source = 'prometheus_historical'` (see Collection §"Worker
  Count Sampling" and Deferred §"Prometheus backfill").
- Row volume: ~100 pools × 12 samples/hour × 24 h ≈ 28 k rows/day — identical
  in order of magnitude to `queue_forecast_queue_depth_samples`.
- Retention: 45-day rolling window, same as other time-series tables.

### `queue_forecast_worker_pools`

Dimension table describing each known worker pool, refreshed daily by the
`worker-counter` service from `worker-manager.listWorkerPools`.

```sql
CREATE TABLE queue_forecast_worker_pools (
    task_queue_id   TEXT PRIMARY KEY,
    pool_kind       TEXT NOT NULL,   -- 'dynamic' | 'static' | 'unknown'
    provider_type   TEXT,            -- e.g. 'aws' | 'azure' | 'google' | 'static' | NULL
    refreshed_at    TIMESTAMPTZ NOT NULL
);
```

Notes:
- `pool_kind` distinguishes dynamic (autoscaled) from static (fixed-size) pools.
  Static pools drive the `NULL` treatment for `running_workers` / `existing_capacity`
  in `queue_forecast_worker_counts`.
- `provider_type` is surfaced as a categorical feature (`provider_type`) in the
  wait-time model's velocity feature block (see ML Pipeline §"Wait Time Model"
  feature table).
- The table is upserted daily; stale rows are overwritten on the next refresh.

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

### Queue Depth Sampling

During each periodic sync cycle the collector also writes a row to
`queue_forecast_queue_depth_samples` for every `task_queue_id` that
has been seen. This happens every 5 minutes, piggybacking on the
existing 60-second counter-refresh cycle (every ~5 refreshes):
- No additional Queue API calls — uses the same in-memory counters
- Provides the time-series queue depth data required for V2 queue load
  prediction (see Goals §V2)
- See `queue_forecast_queue_depth_samples` in the Data Model section for
  the schema and row-volume estimates

### Worker Count Sampling

A dedicated **`worker-counter`** service (Node.js, sibling to `collector` and
`predictor`) collects per-pool worker counts independently of the Pulse
collector. Failure in either service does not cascade to the other.

**Signals collected (every 5 minutes):**

| Signal | Source | Notes |
|--------|--------|-------|
| `running_workers` | `worker-manager.listWorkerPoolsStats` → `runningCount` | Dynamic pools only |
| `existing_capacity` | `worker-manager.listWorkerPoolsStats` → `currentCapacity` | Dynamic pools only |
| `claimed_tasks` | SQL on `queue_forecast_task_runs` | All pools; see derivation below |

`claimed_tasks` derivation (no external API call):
```sql
SELECT task_queue_id, count(*) AS claimed_tasks
FROM queue_forecast_task_runs
WHERE started_at IS NOT NULL AND resolved_at IS NULL
GROUP BY task_queue_id;
```
Because these rows already exist in our own database, the busy-worker count
comes free from data the collector already maintains.

**Cadence and access:**
- Polls `worker-manager.listWorkerPoolsStats` every **5 minutes** for all known
  dynamic pools.
- **Anonymous API access** — the `listWorkerPoolsStats` endpoint is publicly
  readable; no credentials are required.
- Refreshes `queue_forecast_worker_pools` dimension table **daily** from
  `worker-manager.listWorkerPools` to track pool additions, removals, and
  type changes.

**Static pools:** `running_workers` and `existing_capacity` are written as
`NULL` for V1. Static pools have a fixed, known size and are not the primary
driver of the queue-velocity problem that these features are meant to address.

**Failure isolation:** `worker-counter` runs as a separate process. A crash
or API timeout in `worker-counter` does not affect Pulse event processing in
`collector`. Missed samples leave gaps in `queue_forecast_worker_counts` that
the trainer handles via the `[T-10m, T]` lookback window (NULL when no sample
exists).

**`source` column values:**
- `'tc_api'` — live samples from the `worker-counter` service.
- `'prometheus_historical'` — backfill rows written by a future standalone
  script querying PromQL. See Deferred §"Prometheus backfill" for details.

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
| `bl_duration_p50` | numeric | baseline_stats | Baseline percentile p50 — the residual input feature |
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

**Training target (residual):** `log((run_duration_s + 1) / (bl_duration_p50 + 1))`.
At inference, inverse-transformed as `exp(model_raw) * (bl_duration_p50 + 1) - 1`.

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
| `bl_wait_p50` | numeric | baseline_stats | Baseline percentile p50 — the residual input feature |
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

**Training target (residual):** `log((wait_duration_s + 1) / (bl_wait_p50 + 1))`.
At inference, inverse-transformed as `exp(model_raw) * (bl_wait_p50 + 1) - 1`.

**Queue-velocity features (V1 collection; Phase 3a inclusion):**

The following features are derived from `queue_forecast_worker_counts` joined
with `queue_forecast_worker_pools` at training-data export time. Collection
begins in V1 (via the `worker-counter` service) so that enough history exists
when Phase 3a training starts. The features are **not included in the initial
model** — they are added in Phase 3a after sufficient data has accumulated.

For a row pending at time `T` in queue `Q`, the trainer NDJSON export joins the
most recent worker-count sample in `[T-10m, T]`:

| Feature | Type | Derivation |
|---------|------|------------|
| `running_workers_now` | numeric | Latest `running_workers` for Q in `[T-10m, T]` |
| `claimed_tasks_now` | numeric | Latest `claimed_tasks` for Q in `[T-10m, T]` |
| `idle_workers_now` | numeric | `max(running_workers_now − claimed_tasks_now, 0)` |
| `utilization_now` | numeric | `claimed_tasks_now / max(running_workers_now, 1)` |
| `provision_lag_now` | numeric | `existing_capacity − running_workers` (dynamic only; NULL for static) |
| `running_workers_1h_avg` | numeric | Mean of `running_workers` over `[T-1h, T)` |
| `running_workers_1h_delta` | numeric | `running_workers_now − running_workers_1h_avg` |
| `tasks_per_worker` | numeric | `queue_pending / max(running_workers_now, 1)` |
| `pool_kind` | categorical | From `queue_forecast_worker_pools` |
| `provider_type` | categorical | From `queue_forecast_worker_pools` |

All ten features are NULL-safe: LightGBM routes NULL values through its default
branches, so static pools (where `running_workers` is NULL) and gaps between
samples degrade gracefully rather than dropping rows.

**Velocity features are wait-time-only.** Worker availability matters a great
deal for how long a task waits in queue; it matters very little for how long a
task runs once a worker has claimed it. The run duration model does not include
these features.

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

1. Export baseline percentile stats from the training window into
   `baseline_stats.json`. The baseline is computed before training so
   its p50 values are available as input features.
2. Python trainer queries Postgres for the relevant lookback window
3. Joins each row with its baseline p50 and computes the residual
   training target: `log((y + 1) / (bl_p50 + 1))`
4. Trains a fresh LightGBM model from scratch (discards yesterday's model)
   using `objective=quantile` with `alpha=0.5` for p50, `alpha=0.9` for p90
   (two training passes per model, or a single multi-quantile model)
5. Exports to ONNX format alongside `category_mappings.json` and the
   already-generated `baseline_stats.json`
6. Writes all three artifacts to the shared volume as a version-tagged
   bundle: `run_duration_model.onnx`, `wait_time_model.onnx`,
   `baseline_stats.json`, `category_mappings.json`

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

The `predictor.js` service loads both `.onnx` model files **and the baseline
stats** into memory at startup. The V1 serving architecture is two-stage:

**Stage 1 — baseline percentile lookup (sub-ms, in-memory):**

The predictor first computes the baseline p50 prediction for the task, using
the same hierarchical lookup as `src/predictor.js:predictWaitFromStats` and
`predictDurationFromStats`:
- Wait time: keyed on `(task_queue_id, pending_bucket, priority_at_pending)`
  with fallback to coarser keys
- Run duration: keyed on `(metadata_name)` with fallback to `(normalized_name,
  task_queue_id)` and broader cohorts

The baseline stats (`baseline_stats.json`) are exported nightly by the
training pipeline alongside the ONNX models, and hot-reloaded atomically
together with the models (they are version-coupled).

**Stage 2 — residual LightGBM correction (sub-ms, in-process ONNX):**

When a `task-pending` event arrives:

1. Collector upserts the run into `queue_forecast_task_runs`
2. Collector calls the predictor with the task/run features
3. Predictor performs Stage 1: compute baseline p50 (`bl`) from
   `baseline_stats.json` for the relevant target
4. Predictor applies feature engineering:
   - Categorical encoding (string → int32 via `category_mappings.json`,
     loaded alongside the ONNX model; see §"Category Mapping Sidecar")
   - Cyclical time encoding from `pending_at`
   - Build type regex extraction from `metadata_name`
   - Baseline p50 (`bl_wait_p50` / `bl_duration_p50`) appended as a
     numeric feature
5. Runs both ONNX models (run duration + wait time) in-process
6. Inverse-transforms model output:
   `y_hat = exp(model_raw) * (bl + 1) - 1`
7. Composes the ETA:
   - `expected_completion_time = pending_at + wait_p50 + run_p50`
   - `guaranteed_completion_time = pending_at + wait_p90 + run_p90`
8. Writes prediction to `queue_forecast_run_predictions` with
   `predictor_kind = 'residual_lightgbm'`

**Fallback:** if the ONNX model is unavailable, times out, or the category
mapping rejects the row (cold-start miss on a required feature), the predictor
falls back to the Stage 1 baseline prediction and writes it with
`predictor_kind = 'baseline'`. The baseline is always computed first, so
fallback adds no extra latency to the happy path.

Inference latency target: low single-digit milliseconds per prediction.
No network calls to Python. Baseline stats are loaded in-memory (sub-ms
lookup via pre-built Map).

### Category Mapping Sidecar

LightGBM categorical features are int32-coded during training. The Python
trainer exports a `category_mappings.json` alongside each ONNX model
containing the string-to-int32 mapping for every categorical feature.

Format:

```json
{
  "task_queue_id": { "gecko-1/opt": 0, "gecko-1/debug": 1, "_unknown_": -1 },
  "priority_at_pending": { "high": 0, "normal": 1, "_unknown_": -1 },
  "...": { "...": 0 }
}
```

**Cold-start code:** The reserved code `-1` always means "value not seen at
training time." Python and Node use the same rule. LightGBM treats negative
int32 category codes as missing values and routes them through its
default "unknown" branches (the same path as NaN for numeric features).

**Node.js inference rule:** look up the feature value in the mapping; if
absent, substitute `-1`. Do NOT use `null`, `undefined`, or the string
value — use the integer `-1` explicitly. This is a strict contract
between the Python exporter and the Node.js inference path.

**Parity requirement:** Float/double precision can drift between Python
and ONNX inference. Automated parity tests between Python predictions
and Node.js ONNX predictions are a strict requirement before any model
is deployed. Parity tests must include at least one cold-start row per
categorical feature (i.e. a row where the categorical value was not seen
at training time, mapped to `-1`).

### Model Hot-Reload

The predictor watches the shared model volume for new `.onnx` files.
When the nightly trainer writes a new model:
- Predictor detects the new file (filesystem watch or polling)
- Loads new model, `category_mappings.json`, and `baseline_stats.json`
  into memory — all three are version-coupled and must be swapped
  together atomically
- Swaps atomically (old model + old baseline serve requests until new
  set is fully loaded and parity-checked)
- Logs the model version transition

The `baseline_stats.json` is coupled to the ONNX model because the
baseline lookup is a first-class input feature: a model trained on one
set of baseline statistics must not be run with a different baseline at
serving time. The nightly training pipeline exports all three artifacts
(`run_duration_model.onnx`, `wait_time_model.onnx`, `baseline_stats.json`)
as a single version-tagged bundle.

### Cold Start Handling

When LightGBM encounters a categorical value it has never seen during
training (e.g., a brand new `metadata_name` or `task_queue_id`):

- The Python trainer exports `category_mappings.json` (see §"Category
  Mapping Sidecar") with the string-to-int32 mapping for every categorical
  feature. The reserved code `-1` means "value not seen at training time."
- The Node.js predictor looks up each categorical feature value in its
  mapping. If absent, it substitutes the integer `-1` — not null, not
  the string value, not any other sentinel. LightGBM treats negative
  int32 category codes as missing and routes them through the model's
  default "unknown" branches.
- A cold-start row still gets a LightGBM prediction — it relies more
  heavily on features the model has seen (e.g. `task_queue_id`, `tags`,
  `scheduler_id`) and less on the unseen feature. The residual
  architecture helps here: the baseline p50 is always available via
  percentile lookup, so even a fully cold-start row gets a reasonable
  ETA from Stage 1, and Stage 2 applies whatever correction it can.
- If the category mapping rejects a row on a required feature (e.g. the
  queue itself is brand new and the baseline has no stats for it),
  the predictor falls back to the baseline-only prediction
  (`predictor_kind = 'baseline'`) rather than producing a poorly-supported
  residual correction.
- The `input_features` JSONB in `queue_forecast_run_predictions` should
  flag which features were cold-start (mapped to `-1`), enabling
  evaluation of cold-start accuracy.
- After one nightly retrain cycle, the new task type enters the training
  data and gets proper coverage.

## ML Pipeline Architecture Options

The sections above describe the ML algorithm, features, and training strategy
independently of where training and inference run. There are three viable
deployment architectures. All share the same data model, collection layer,
and evaluation methodology — they differ only in who trains the model and
where inference happens.

### Shared Component: Daily Data Export (approaches A and B)

Both bugbug-based approaches require a daily Taskcluster task that
exports training data from Postgres and publishes it as a TC artifact.

```
┌─────────────┐    daily TC task    ┌──────────────────────┐
│  Postgres    │ ──────────────────→ │ training_data.json.zst│
│  (collector) │   SQL query +       │ (TC artifact, 7-day  │
│              │   zstd compress     │  expiry, TC-indexed)  │
└─────────────┘                     └──────────────────────┘
```

- Runs as a scheduled TC task (not in docker-compose)
- Queries the training SQL from the run duration and wait time model
  sections above, exports as newline-delimited JSON compressed with
  zstandard (`.json.zst`)
- Published as a public TC artifact, indexed via `project.queue-forecasting.data.latest`
- Estimated size: 1-3 GB compressed for a 30-day window (~7.5M rows)
- Artifact expiry: 7 days (training only needs the latest snapshot)

This aligns with bugbug's existing data pipeline pattern — every data
source in bugbug (Bugzilla, Mercurial, CI failures) follows the same
retrieval-task → artifact → training-task flow.

### Approach A: Full bugbug Integration (training + serving)

**Data flow:**
```
Node.js collector → Postgres → daily export task → TC artifact
  → bugbug data-retrieval task downloads artifact
  → bugbug training task (XGBoost) → model stored as pickle
  → bugbug HTTP service serves predictions
  → Node.js services call bugbug HTTP API
```

**What lives where:**

| Component | Location | Owner |
|-----------|----------|-------|
| Collector, reconciler | `tools/queue-forecasting/` (TC repo) | TC team |
| Data export task | `tools/queue-forecasting/` (TC repo) | TC team |
| Data retrieval script | bugbug repo | bugbug team |
| Model class + training | bugbug repo | bugbug team |
| HTTP prediction endpoint | bugbug HTTP service | bugbug team |
| Prediction API (proxy) | `tools/queue-forecasting/` (TC repo) | TC team |

**What needs to be added to bugbug:**
1. **Data retrieval script** — downloads the `training_data.json.zst`
   artifact from TC index, decompresses, yields records. Similar to
   existing `bugbug/bugzilla.py` retrieval pattern.
2. **Model class** — extends `bugbug.model.Model`, defines feature
   extraction from the exported task/run records. Uses XGBoost
   (bugbug's standard) with quantile regression for p50/p90.
3. **Training task** — entry in `infra/data-pipeline.yml` depending on
   the data retrieval task.
4. **HTTP endpoint** — new route in `http_service/bugbug_http/app.py`
   that accepts task features and returns wait time + run duration
   predictions.

**Prediction flow:**
1. `task-pending` event arrives at collector
2. Collector upserts run, then calls bugbug HTTP API with features
3. bugbug API enqueues prediction job (Redis + RQ)
4. Collector polls for result (bugbug's standard async pattern)
5. Result written to `queue_forecast_run_predictions`

**Pros:**
- No Python or ML code in the TC repo
- Leverages existing Mozilla ML infrastructure (CI, monitoring,
  deployment, model management)
- bugbug team already maintains training orchestration and HTTP serving
- Existing patterns for model rollback and evaluation

**Cons:**
- **Network latency**: bugbug uses async polling (enqueue → poll for
  result). At ~250k predictions/day (~3/sec sustained), each prediction
  incurs HTTP round-trips instead of sub-ms local inference.
  Batching can amortize this but adds complexity.
- **XGBoost vs LightGBM**: bugbug standardizes on XGBoost. XGBoost
  requires manual categorical encoding (label encoding or one-hot)
  where LightGBM handles high-cardinality categoricals natively.
  Quality is comparable for tabular data, but feature engineering
  is more involved.
- **External service dependency**: bugbug HTTP downtime means no new
  predictions. Stale predictions in `queue_forecast_run_predictions`
  remain available but won't update.
- **Cross-team coordination**: model changes require PRs to bugbug repo
  and alignment with bugbug release cadence.

**Cost summary:**

| Cost | Estimate |
|------|----------|
| Data export artifact storage | ~1-3 GB/day, 7-day expiry = ~7-21 GB peak |
| Network transfer (export → bugbug) | ~1-3 GB/day (TC-internal, free) |
| bugbug training compute | 1 TC task/day, ~10-30 min |
| HTTP API calls | ~250k/day, async polling |

### Approach B: Mixed Mode (bugbug training, ONNX local inference)

**Data flow:**
```
Node.js collector → Postgres → daily export task → TC artifact
  → bugbug data-retrieval task downloads artifact
  → bugbug training task → ONNX export as TC artifact
  → Node.js predictor downloads ONNX model + category mappings
  → Local inference via onnxruntime-node
```

**What lives where:**

| Component | Location | Owner |
|-----------|----------|-------|
| Collector, reconciler, predictor | `tools/queue-forecasting/` (TC repo) | TC team |
| Data export task | `tools/queue-forecasting/` (TC repo) | TC team |
| Data retrieval + training | bugbug repo | bugbug team |
| ONNX model artifact | TC artifact storage | produced by bugbug |

**What needs to be added to bugbug (same as A, plus):**
- ONNX export step after training. bugbug does not support ONNX today.
  XGBoost models can be converted via `onnxmltools` or `skl2onnx`, but
  this is less battle-tested than LightGBM's ONNX export path.
- Category mapping sidecar (`category_mappings.json`) exported alongside
  the ONNX model.
- Parity tests between Python XGBoost predictions and ONNX runtime
  predictions (float precision can drift).

**Prediction flow:**
1. `task-pending` event arrives at collector
2. Collector calls local `predictor.js` (same as current spec)
3. Predictor runs ONNX model in-process, sub-ms latency
4. Result written to `queue_forecast_run_predictions`

**Model hot-reload:**
- Predictor polls TC index for new ONNX artifact (or watches a local
  volume synced from TC artifacts)
- Loads new model + category mappings atomically

**Pros:**
- Sub-ms local inference preserved — no runtime dependency on bugbug
- Leverages bugbug's training orchestration and CI
- Model is a static artifact — predictor is self-contained after download

**Cons:**
- **ONNX export is new to bugbug** — needs to be implemented and
  maintained. Adds a capability bugbug doesn't currently have.
- **XGBoost ONNX maturity**: XGBoost → ONNX conversion exists but is
  less mature than LightGBM → ONNX. Quantile regression ONNX export
  may need validation.
- **Category mapping sidecar**: same complexity as approach C (the
  Node.js predictor must replicate categorical encoding).
- **Cross-team dependency for training changes**, but not for runtime.

**Cost summary:**

| Cost | Estimate |
|------|----------|
| Data export artifact storage | ~1-3 GB/day, 7-day expiry |
| ONNX model artifact storage | ~10-50 MB/day, 7-day expiry |
| bugbug training compute | 1 TC task/day, ~10-30 min |
| Network transfer at inference | None (local) |

### Approach C: All-in-TC Standalone (current spec baseline)

**Data flow:**
```
Node.js collector → Postgres
  → Nightly Python trainer (docker-compose) queries Postgres directly
  → LightGBM training → ONNX export to shared volume
  → Node.js predictor loads ONNX, runs local inference
```

This is the architecture described in the preceding sections. The Python
trainer lives in `tools/queue-forecasting/` alongside the Node.js code,
runs as a docker-compose service on a nightly cron.

**Pros:**
- Full control over the entire pipeline — no external dependencies
- LightGBM with native categorical support (no manual encoding needed
  for high-cardinality features like `metadata_name`)
- Sub-ms local inference
- Self-contained: one `docker-compose up` runs everything
- Simpler debugging — all code in one repo

**Cons:**
- Own the entire ML pipeline: training infrastructure, monitoring,
  model versioning, rollback
- Python code in the TC repo (TC is primarily Node.js and Go)
- Must build training orchestration, evaluation automation, and
  model management from scratch

**Cost summary:**

| Cost | Estimate |
|------|----------|
| Training compute | docker-compose container, ~10-30 min/day |
| Storage | ONNX models on local/shared volume, ~50 MB |
| External dependencies | None |

### Comparison Matrix

| Dimension | A: Full bugbug | B: Mixed mode | C: Standalone |
|-----------|---------------|---------------|---------------|
| Inference latency | ~100ms+ (HTTP poll) | Sub-ms (local ONNX) | Sub-ms (local ONNX) |
| Runtime dependency | bugbug HTTP service | None (static artifact) | None |
| Training orchestration | bugbug (existing) | bugbug (existing) | Self-built |
| ML framework | XGBoost | XGBoost | LightGBM |
| Categorical handling | Manual encoding | Manual encoding | Native |
| Python in TC repo | No | No | Yes |
| New bugbug work | Model + endpoint | Model + ONNX export | None |
| Operational ownership | Shared (TC + bugbug) | Shared (training only) | TC team only |

### Recommendation

Start with **Approach C** (standalone) to validate the model quality and
prediction pipeline end-to-end with minimal cross-team coordination. The
evaluation metrics (within-2x rate, pinball loss, p90 calibration) will
determine whether the ML approach works before investing in infrastructure
integration. If the model proves valuable, migrate training to bugbug
(Approach B) to offload pipeline maintenance, with Approach A as an
option if local inference complexity becomes a burden.

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
the row where `predictor_kind = 'residual_lightgbm'` and `model_version`
matches the currently loaded model. If no LightGBM prediction is present
(e.g. only a baseline fallback row exists), it returns the baseline row.
If the run exists but has no prediction at all (race condition or missed
event), it generates one on the fly and includes `predictorKind` in the
response so callers know which predictor was used.

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

### Evaluation population vs serving population

Evaluation during offline model development uses a held-out time window
where only `reason_resolved = 'completed'` runs are evaluated (the
"primary" slice from the trainer spec). This is an apples-to-apples
comparison against the baseline, which also uses only completed runs.

However, the model serves predictions to **all pending runs**, including
those that will eventually fail, be retried, or be exceptional. Before
a new model is promoted from shadow-mode to production, evaluation must
be run on the **serving population**: all pending runs with observable
ground truth (wait time: `started_at IS NOT NULL`; run duration:
`run_duration_s IS NOT NULL`). This is the "supplemental" slice
(`reason_resolved IN ('completed', 'failed')`) from the trainer spec.

**Promotion rule:** the primary slice (completed-only) drives the
go/no-go decision for Phase 1. The supplemental slice is the gate for
promoting a shadow-mode candidate to production — it is the population
the model actually serves and is therefore the definitive accuracy signal
for deployment.

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
    rp.predictor_kind,
    r.wait_duration_s   AS actual_wait,
    r.run_duration_s    AS actual_run,
    r.pending_at,
    r.resolved_at,
    r.reason_resolved,
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
  AND rp.predictor_kind = 'residual_lightgbm'   -- filter to production predictor
  AND rp.model_version  = $2                     -- pin to a specific version
```

The `predictor_kind` filter selects predictions from the production
predictor; omit it (or use `IN ('residual_lightgbm', 'baseline')`) to
include shadow-mode predictions for comparison. The `reason_resolved`
column is included so the caller can slice into the primary
(completed-only) and supplemental (completed + failed) populations.

### Rollout

**Feature-maturity gate (prerequisite to all phases below):** No phase below
begins until the long-tail ratio-accuracy gap is closed. Specifically, the
30m+ wait bucket must reach within-2x ≥ 50% (current: 38.6%). The primary
path is queue-velocity feature experiments (active worker count,
tasks_completed_in_last_N_min). See `trainer-phase2-decision.md §5` for
the full sequencing.

| Phase | What ships | Predictions visible? |
|-------|-----------|---------------------|
| **Phase 1** | Collector, reconciler, nightly trainer, predictor | Stored only. Internal evaluation. |
| **Phase 2** | Prediction API | Debug/internal consumers. TC UI behind flag. |
| **Phase 3** | TC UI integration | Default-on for supported queues. |

No model is exposed to users without passing automated evaluation on
the metrics above, including the per-bucket within-2x thresholds.

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

Keep the last 7 days of `.onnx` model files, `category_mappings.json`,
and `baseline_stats.json` on the shared volume. These three artifacts
are version-coupled and must be retained together. Allows quick rollback
if a nightly model degrades. Older artifacts are deleted.

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
    predictor_kind               TEXT NOT NULL,
    input_features               JSONB,

    PRIMARY KEY (task_id, run_id, model_version, predictor_kind)
);
CREATE INDEX idx_qf_run_predictions_run
    ON queue_forecast_run_predictions (task_id, run_id);
CREATE INDEX idx_qf_run_predictions_predicted_at
    ON queue_forecast_run_predictions (predicted_at);
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

- **Queue drain forecasting** — builds on the wait-time model + queue
  depth time-series (collected from V1 onward via
  `queue_forecast_queue_depth_samples`). V2 feature.
- **Queue load prediction model** — predicts pending count for a given
  queue at a given hour and day-of-week. Uses the time-series data being
  collected continuously from V1 onward. V2 feature.
- **Trend/regression detection** — daily cohort rollups comparing trailing
  7-day vs 28-day quantiles. Requires stable evaluation pipeline first.
- **bugbug migration** — V1 starts standalone (Approach C) to validate
  model quality. Once evaluation confirms the approach works, migrate
  training to bugbug (Approach B or A) to leverage existing Mozilla ML
  infrastructure. See "ML Pipeline Architecture Options" above for the
  full comparison. Decision point: after Phase 1 evaluation metrics are
  stable.
- **Shadow mode comparison** — running a new model version or predictor
  kind side-by-side with production by writing additional rows to
  `queue_forecast_run_predictions` (different `predictor_kind` and/or
  `model_version` values). Auto-promotion if the candidate wins on
  evaluation metrics. The multi-row PK added in V1 makes this possible
  without schema changes.
- **Prerequisites to production** (from Phase 2 known gaps; these block
  Phase 3a and must be resolved before any model ships to users):
  - **Queue-velocity features (primary prerequisite)** — the 10 features
    described in ML Pipeline §"Wait Time Model" (velocity feature block),
    derived from `queue_forecast_worker_counts` and `queue_forecast_worker_pools`
    (see Data Model for schemas). These are the targeted fix for the 30m+
    ratio-accuracy gap (current: 38.6% within-2x; required: ≥ 50%).
    Status:
    - Collection: in progress — `worker-counter` service running as of 2026-04-24
      (see Collection §"Worker Count Sampling").
    - Feature extraction: pending — the trainer NDJSON export path needs to join
      `queue_forecast_worker_counts` + `queue_forecast_worker_pools` at
      feature-extraction time.
    - Re-train + re-evaluate: pending.
    - Exit criterion: 30m+ within-2x ≥ 50%, aggregate within-2x ≥ 60%.
  - within-2x calibration improvements via loss-function tuning or
    bucket-conditional quantile choice (secondary; pursue if velocity
    features alone do not close the gap)
  - p90 coverage tightening toward 90% (transform choice or alpha tuning)
  - XGBoost `QuantileModel` subclass (pluggable interface already exists;
    experiment once the production path is unblocked)
- **TC UI integration** — wiring the prediction API into the task
  detail view.
- **Lando landing queue as a leading indicator** — Lando's merge queue
  shows what's about to land and therefore what will be scheduled soon.
  This is a forward-looking signal the current models lack — today the
  wait-time model only reacts to `queue_pending` at enqueue time. A
  periodic snapshot of the Lando queue depth (and optionally the repos
  being landed) could feed into the wait-time model and V2 queue load
  prediction. Complexity: requires a new data source (Lando API), and
  the signal is indirect — a landing doesn't map 1:1 to specific task
  queues without understanding the push-to-taskgraph relationship.
- **Tree status and sheriff activity** — tree closures halt new tasks,
  and sheriff-initiated backfills cause sudden load spikes. Both are
  regime changes that dramatically shift queue behavior. TreeHerder
  exposes tree status (open / closed / approval-required) via API.
  Adding tree state as a categorical feature to both models would help
  them distinguish normal load from closure-recovery bursts. Backfill
  detection is harder — may require identifying sheriff-triggered task
  groups via `scheduler_id` or push metadata.
- **Prometheus historical backfill** — the `worker-counter` service
  begins collecting from 2026-04-24 onward. Historical coverage from
  before that date can be backfilled via a standalone script that queries
  PromQL metrics (`fxci_queue_running_workers`,
  `fxci_queue_claimed_tasks`, `fxci_worker_manager_existing_capacity`)
  and writes rows to `queue_forecast_worker_counts` with
  `source = 'prometheus_historical'`. The `source` column makes the
  multi-origin picture first-class in the schema — live and backfilled
  rows coexist without special-casing in the trainer. The backfill script
  is future work and does not block live collection or Phase 3a training.
- **Guiding principle: TC-only first, extend if needed** — V1
  deliberately uses only Taskcluster-internal data (Pulse events, Queue
  API). The evaluation pipeline (within-2x rate, pinball loss, p90
  calibration) provides an objective checkpoint: if TC-only features
  don't meet accuracy targets, that is the signal to integrate external
  sources like Lando and TreeHerder.
