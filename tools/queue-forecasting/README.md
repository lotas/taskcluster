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

## Running Tests

### Smoke Tests (requires Postgres)

```bash
export DATABASE_URL=postgresql://postgres@localhost:5433/forecasting
node test/smoke.js
```

Tests upsert idempotency, out-of-order event handling, priority update scoping, enrichment, and the placeholder/run row distinction.

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

- **Collector** (`src/collector.js`): Long-running process subscribing to 8 Pulse exchanges. Upserts event data synchronously, fires background API fetches for task metadata enrichment.
- **Predictor** (`src/predictor.js`): CLI backtest tool. Predicts run durations using hierarchical cohort matching (metadata_name → kind+test-type → task_queue_id → global median).
- **Database**: Single `task_events` table with two row types — placeholder rows (`run_id IS NULL`) for task-level data and run rows (`run_id IS NOT NULL`) for per-run lifecycle data.
