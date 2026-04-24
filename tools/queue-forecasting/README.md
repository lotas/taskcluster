# Queue Forecasting Data Collector

Standalone tool that connects to Mozilla's production Pulse (RabbitMQ), consumes task lifecycle events from the queue service, enriches them with full task metadata via the Queue API, and writes structured records to a local Postgres database. Includes a backtest predictor CLI for experimenting with forecasting models.

**This is not a Taskcluster microservice.** It runs independently for dataset building and model experimentation.

## Setup

### 1. Start Postgres

```bash
cd tools/queue-forecasting
docker compose up -d
```

This starts Postgres 15 on port **5433** (to avoid collision with the monorepo's standard Postgres on 5432).

### 2. Install Dependencies

From the monorepo root (this tool is a Yarn workspace):

```bash
cd ../..
yarn install
```

### 3. Verify Imports

```bash
node tools/queue-forecasting/test/import-check.js
```

## Running the Collector

### Required Environment Variables

| Variable | Description |
|----------|-------------|
| `PULSE_USERNAME` | Pulse RabbitMQ username |
| `PULSE_PASSWORD` | Pulse RabbitMQ password |
| `PULSE_HOSTNAME` | Pulse RabbitMQ hostname |
| `PULSE_VHOST` | Pulse RabbitMQ vhost (required) |
| `TASKCLUSTER_ROOT_URL` | e.g., `https://firefox-ci-tc.services.mozilla.com` |
| `TASKCLUSTER_CLIENT_ID` | Needs `queue:get-task:*` scope |
| `TASKCLUSTER_ACCESS_TOKEN` | Matching access token |
| `DATABASE_URL` | e.g., `postgresql://postgres@localhost:5433/forecasting` |

### Start Collecting

```bash
export PULSE_USERNAME=...
export PULSE_PASSWORD=...
export PULSE_HOSTNAME=...
export PULSE_VHOST=...
export TASKCLUSTER_ROOT_URL=https://firefox-ci-tc.services.mozilla.com
export TASKCLUSTER_CLIENT_ID=...
export TASKCLUSTER_ACCESS_TOKEN=...
export DATABASE_URL=postgresql://postgres@localhost:5433/forecasting
cd tools/queue-forecasting
node src/collector.js
```

The collector creates a durable queue in RabbitMQ and survives restarts. Stop with Ctrl+C (SIGINT) for graceful shutdown.

## Running the Predictor

After data has been collecting for at least a day:

```bash
export DATABASE_URL=postgresql://postgres@localhost:5433/forecasting
node src/predictor.js --date 2026-03-18
```

This runs backtests against completed tasks resolved on the given date and prints accuracy statistics.

### Other Tools

```bash
node src/predict-sample.js   # Predict a single currently-pending task
node src/diagnose.js          # Print database diagnostics and queue-pending analysis
```

## Running the Worker Counter

The worker counter polls the Taskcluster WorkerManager API every 5 minutes to collect per-queue worker metrics and stores them as a time series. It also derives "busy worker" counts by querying our own `queue_forecast_task_runs` table for in-flight runs.

### What it collects

| Column | Source |
|--------|--------|
| `running_workers` | `runningCount` from WorkerManager `/worker-pools/stats` (workers in "running" state) |
| `existing_capacity` | `currentCapacity` from same endpoint (total capacity not yet stopped) |
| `claimed_tasks` | Count of runs where `started_at IS NOT NULL AND resolved_at IS NULL` in our local DB |

Rows land in `queue_forecast_worker_counts` (one row per queue per 5-min slot, upserted idempotently). A daily dimension refresh classifies each `task_queue_id` as `'dynamic'` (managed by WorkerManager) or `'static'` (self-hosted) in `queue_forecast_worker_pools`.

The service operates **anonymously** — no Taskcluster credentials are required.

### How to run

```bash
cd tools/queue-forecasting
docker compose --profile worker-counter up -d
```

To run alongside the collector:

```bash
docker compose --profile collector --profile worker-counter up -d
```

### Expected row rate

Approximately 50–100 rows per 5-minute sample (one row per active queue). At that rate, 1 month of data is ~432 000–864 000 rows.

### Environment variables

| Variable | Description |
|----------|-------------|
| `TASKCLUSTER_ROOT_URL` | e.g., `https://firefox-ci-tc.services.mozilla.com` |
| `DATABASE_URL` | e.g., `postgresql://postgres@localhost:5433/forecasting` |

## Running Tests

### Smoke Tests (requires Postgres)

```bash
export DATABASE_URL=postgresql://postgres@localhost:5433/forecasting
node test/smoke.js
```

Tests upsert idempotency, out-of-order event handling, FK constraints, enrichment, priority snapshot immutability, and CASCADE deletes.

## Running Everything in Docker

### 1. Create `.env`

Copy `.env.example` to `.env` and fill in your Pulse and Taskcluster credentials.

### 2. Start Postgres + Collector

```bash
cd tools/queue-forecasting
docker compose --profile collector up -d
```

This starts Postgres and the collector. The collector waits for Postgres to be healthy before starting.

### 3. Run the Predictor

```bash
PREDICT_DATE=2026-03-19 docker compose run --rm predictor
```

### 4. Stop Everything

```bash
docker compose down           # keep data
docker compose down -v        # destroy data volume (resets schema)
```

## Architecture

- **Collector** (`src/collector.js`): Long-running process subscribing to 6 Pulse exchanges (`task-defined`, `task-pending`, `task-running`, `task-completed`, `task-failed`, `task-exception`). Upserts task and run data synchronously, fires background API fetches for task metadata enrichment. Maintains in-memory queue-depth counters seeded and periodically synced from the Queue API.
- **Predictor** (`src/predictor.js`): CLI backtest tool. Predicts run durations using hierarchical cohort matching (metadata_name -> normalized_name -> kind+test-type -> task_queue_id -> scheduler_id -> global median). Also predicts wait times using queue depth bucketing.
- **Database**: Normalized two-table model:
  - `queue_forecast_tasks` — one row per `task_id`, stores task-level identity and metadata (queue, scheduler, tags, etc.)
  - `queue_forecast_task_runs` — one row per `(task_id, run_id)`, stores per-run execution data (timestamps, durations, queue depth snapshot)
  - `queue_forecast_run_predictions` — one row per `(task_id, run_id)`, stores predictions for evaluation

## Migration

If upgrading from the old single-table `task_events` schema, run the migration script:

```bash
docker compose exec -T postgres psql -U postgres -d forecasting < migrate.sql
```

See `migrate.sql` for details and verification queries.
