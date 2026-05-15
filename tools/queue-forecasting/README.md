# Queue Forecasting

Standalone tool for predicting Taskcluster task wait times and run durations. Builds on Pulse-event collection, periodic worker-pool sampling, daily anomaly detection, a percentile baseline, and LightGBM quantile/residual models.

**This is not a Taskcluster microservice.** It runs independently for dataset building and model experimentation.

## Architecture

```
                  ┌──────────────────────────────────────────────┐
                  │              Postgres (port 5433)            │
                  │  queue_forecast_tasks                        │
                  │  queue_forecast_task_runs                    │
                  │  queue_forecast_worker_counts                │
                  │  queue_forecast_worker_pools                 │
                  │  queue_forecast_daily_health                 │
                  │  queue_forecast_run_predictions              │
                  └──────────────────────────────────────────────┘
                          ▲       ▲       ▲       ▲       ▲
                          │       │       │       │       │
   Pulse events ──► collector    │       │       │       │
                   (Node.js)     │       │       │       │
                                 │       │       │       │
   WorkerManager API ──► worker-counter   │       │       │
                         (Node.js, 5min)  │       │       │
                                          │       │       │
                         health-monitor ──┘       │       │
                         (Python, hourly)         │       │
                                                  │       │
                         predictor ───────────────┤       │ (read)
                         (Node.js, percentile)    │       │
                                                  │       │
                         trainer ─────────────────┘───────┘
                         (Python/LightGBM)
```

| Component | Language | What it does |
|---|---|---|
| **collector** | Node.js | Subscribes to 6 Pulse exchanges (task-defined → completed/failed/exception). Upserts tasks/runs synchronously; enriches with task metadata via Queue API. Maintains queue-depth counters periodically synced from the API. |
| **worker-counter** | Node.js | Polls WorkerManager `/worker-pools/stats` every 5 min for `runningCount`/`currentCapacity`. Computes `claimed_tasks` from in-flight rows in our DB. Daily dimension refresh classifies queues as `dynamic` / `static`. Anonymous — no Taskcluster credentials. |
| **health-monitor** | Python | Hourly job re-computes per-day metrics (volume, exception rate, wait p99, capacity drop/spike, utilization) over a trailing 7-day window. Flags days as anomalous; trainer + predictor consume those flags to filter contaminated history. |
| **predictor** | Node.js | Hierarchical percentile baseline (metadata_name → normalized_name → tags → queue → scheduler → global). Two output modes: per-day eval JSONs and an aggregate residual-feature NDJSON. Supports `--exclude-dates` to drop anomalous days from the 7-day history window. |
| **trainer** | Python / LightGBM | Quantile regression (p50, p90) over engineered features (queue depth, throughput, time-of-day, worker-pool kind). Residual mode trains on the log-ratio residual against the percentile baseline. |
| **live-predictor** | Node.js | Consumes the trainer's ONNX bundles and writes a prediction row to `queue_forecast_run_predictions` for every new task-pending event. Polls every few seconds (`LIVE_PREDICTOR_POLL_MS`, default 5000) for currently-unresolved unpredicted rows, keyset-paginated through any backlog. |

## First-time setup (new host / VM)

### 1. Clone & configure

```bash
git clone <repo-url>
cd tools/queue-forecasting

cp .env.example .env
# Fill in PULSE_*, TASKCLUSTER_*, DATABASE_URL credentials
```

Required env vars (in `.env`):

| Variable | Purpose |
|---|---|
| `PULSE_USERNAME` / `PULSE_PASSWORD` / `PULSE_HOSTNAME` / `PULSE_VHOST` | Mozilla Pulse RabbitMQ credentials (collector only) |
| `TASKCLUSTER_ROOT_URL` | e.g. `https://firefox-ci-tc.services.mozilla.com` |
| `TASKCLUSTER_CLIENT_ID` / `TASKCLUSTER_ACCESS_TOKEN` | Needs `queue:get-task:*` (collector only) |
| `DATABASE_URL` | e.g. `postgresql://postgres@postgres:5432/forecasting` (in-network) or `postgresql://postgres@localhost:5433/forecasting` (host) |

### 2. Start Postgres

```bash
docker compose up -d postgres
```

`init.sql` runs automatically on first volume creation, creating all 6 tables and indexes. The schema is also applied via `migrate.sql` (see *Updating an existing setup* below).

### 3. Start the collection services

Recommended: bring up everything via the `full` profile.

```bash
docker compose --profile full up -d
```

Or pick services individually:

```bash
docker compose --profile collector       up -d   # task data
docker compose --profile worker-counter  up -d   # worker counts (every 5 min)
docker compose --profile health-monitor  up -d   # daily anomaly detector (hourly)
docker compose --profile live-predictor  up -d   # real-time ONNX predictions
```

Check health:

```bash
docker compose ps
docker compose logs -f collector worker-counter health-monitor
```

### 4. Wait for data

The forecasting models need ≥14 days of task data and worker-count history. The collector typically captures ~200k–300k task rows per day at fxci scale.

While waiting, you can run smoke tests against Postgres:

```bash
DATABASE_URL=postgresql://postgres@localhost:5433/forecasting node test/smoke.js
```

### 5. First training run

Once you have ~3 weeks of data, see *Training* below.

## Updating an existing setup

```bash
# 1. Pull the latest code
git pull

# 2. Rebuild any changed images
docker compose up -d --build

# 3. Apply additive schema migrations (idempotent — safe to re-run)
docker compose exec -T postgres psql -U postgres -d forecasting < migrate.sql

# 4. Re-run the anomaly detector to backfill any new flag columns
#    (e.g. when worker-count flags were added)
docker compose run --rm --entrypoint uv trainer \
  run python -m scripts.compute_daily_health \
  --from 2026-03-23 --to $(date -u +%F)

# 5. Restart long-running services
docker compose --profile full up -d
```

## Training workflows

There are three nested workflows, each building on the previous:

1. **Single training run** for one config + one as-of-date.
2. **Walk-forward sweep** running step 1 across many cohorts × many configs.
3. **Comparing policies** for the anomaly filter (unfiltered / A / B / C) over the same sweep.

### Configs

Live in `trainer/configs/`. Each YAML defines target column, time windows, features, model params, and optional residual / anomaly_filter / baseline_dir blocks.

| Config | Target | Architecture | Notes |
|---|---|---|---|
| `wait_time.yaml` | wait | LGB-only | Direct prediction, no baseline |
| `wait_time_residual.yaml` | wait | residual (log-ratio vs baseline) | Vanilla |
| `wait_time_residual_throughput.yaml` | wait | residual + throughput features | Production candidate (unfiltered) |
| `wait_time_residual_throughput_filtered.yaml` | wait | residual + throughput, **Policy A** | Drops anomalous days from train+val |
| `wait_time_residual_throughput_filtered_baseline.yaml` | wait | residual + throughput, **Policy B** | Drops anomalous days from baseline history |
| `wait_time_residual_throughput_filtered_both.yaml` | wait | residual + throughput, **Policy C** | A + B combined |
| `run_duration.yaml` | duration | LGB-only | Direct prediction |
| `run_duration_residual.yaml` | duration | residual | Production candidate |

### 1. Single training run

```bash
# Today's as-of date
./scripts/run_training.sh configs/wait_time_residual_throughput.yaml

# Pin a specific date (must be UTC midnight)
./scripts/run_training.sh configs/wait_time_residual_throughput.yaml --as-of-date 2026-04-21
```

What this script does (all auto-resolved — you don't run any prerequisite commands):

1. **Resolve holdout days** from the config (no DB access, pure config math).
2. **Resolve excluded dates** for Policy B/C — queries `queue_forecast_daily_health` for anomalous days, builds a `--exclude-dates ...` flag list for the predictor.
3. **Resolve baseline directory** from the config (default `data/baseline`, Policy B/C use `data/baseline_filtered`).
4. **Ensure aggregate residual NDJSON exists** — for any config with a `residual:` block, generates `<baseline_dir>/baseline_predictions.ndjson` covering the cohort's full training window if missing. Idempotent: skips when the file already exists. Delete the file to force regeneration.
5. **Generate per-day baseline JSONs** for each holdout day via the Node predictor, into the configured baseline dir, applying `--exclude-dates`.
6. **Train + evaluate** — Python trainer reads the NDJSON, builds features, trains p50 + p90 quantile models, evaluates on holdout, writes `<as_of>/<config>_manifest.json` + `.lgb` model files.

### 2. Walk-forward sweep

```bash
./scripts/walk_forward.sh \
  --from 2026-04-15 --to 2026-04-28 \
  --configs configs/wait_time.yaml,configs/wait_time_residual_throughput.yaml,configs/run_duration_residual.yaml
```

Resume-safe: any `(date, config)` cell whose manifest already exists is skipped. To force re-run, delete the manifest first.

Before the cohort loop, walk-forward does a one-time pre-pass: for each unique `(baseline_dir, exclude_dates)` group across the configs, it generates the aggregate residual NDJSON covering the entire sweep window — so individual cohorts don't each regenerate it.

Then aggregate results into a CSV:

```bash
docker compose run --rm --entrypoint uv trainer \
  run python -m scripts.summarize_walk_forward \
  --from 2026-04-15 --to 2026-04-28 \
  --configs '*' \
  --output trainer/walk_forward_all.csv
```

The CSV includes a `cohort_is_anomalous` column joined from `queue_forecast_daily_health` so you can slice metrics by holdout-day quality.

### 3. Comparing anomaly-filter policies

Pass all four policy configs in a single sweep:

```bash
./scripts/walk_forward.sh \
  --from 2026-04-15 --to 2026-04-28 \
  --configs configs/wait_time_residual_throughput.yaml,configs/wait_time_residual_throughput_filtered.yaml,configs/wait_time_residual_throughput_filtered_baseline.yaml,configs/wait_time_residual_throughput_filtered_both.yaml
```

Walk-forward auto-detects that there are two distinct `(baseline_dir, exclude_dates)` groups — `data/baseline/` (no exclusions, used by unfiltered + Policy A) and `data/baseline_filtered/` (with the daily-health excluded-dates list, used by Policy B + C) — and pre-generates the aggregate NDJSON for each group exactly once.

## Anomaly detection

Daily health metrics live in `queue_forecast_daily_health`. The `health-monitor` service refreshes the trailing 7 days hourly. UPSERT semantics make re-runs free.

### Flags

| Flag | Threshold | Default in `is_anomalous`? |
|---|---|---|
| `flag_volume_anomaly` | n_total < 0.5× or > 2× trailing-7d median | yes |
| `flag_exception_spike` | exception_rate > 0.10 abs **or** > 2× median | yes |
| `flag_stuck_pending_spike` | stuck_pending_rate > 0.10 abs **or** > 3× median | yes |
| `flag_wait_p99_spike` | wait_p99_s > 3× median | yes |
| `flag_low_completion` | completion_rate < 0.70 abs | yes |
| `flag_capacity_drop` | total_capacity_p50 < 0.5× trailing median | yes |
| `flag_sampler_offline` | n_worker_samples < 0.5× expected (288/day) | yes |
| `flag_capacity_spike` | total_capacity_p50 > 2× trailing median | informational |
| `flag_low_utilization` | utilization_p50 < 0.4 | informational |

### Classification matrix

| Pattern | Flags fired | Likely cause |
|---|---|---|
| Legitimate task spike | `volume_anomaly` (high) + `capacity_spike` | Demand surge; capacity scaled |
| Real capacity shortage | `wait_p99_spike` + `capacity_drop` | Provisioner / quota / outage |
| Scheduling failure | `wait_p99_spike` + `capacity_spike` + `low_utilization` | Workers exist but can't pick up tasks |
| Wasteful over-provisioning | `capacity_spike` + `low_utilization` | Capacity grew but no demand |
| Data loss | `volume_anomaly` (low) + `sampler_offline` (often) | Pulse / sampler dropped events |

### Tuning the trainer's filter

In a config's `anomaly_filter` block:

```yaml
anomaly_filter:
  enabled: true
  mode: training            # training | baseline | both
  # flag_subset: [...]      # optional: only these flags trigger filtering
```

`mode`:
- `training` — drops anomalous days from train+val (Policy A)
- `baseline` — drops anomalous days from the percentile baseline's history (Policy B)
- `both` — A + B (Policy C)

Holdout is **never** filtered.

### One-shot detector run

```bash
docker compose run --rm --entrypoint uv trainer \
  run python -m scripts.compute_daily_health --from 2026-03-23 --to 2026-04-29
```

Idempotent — UPSERTs by `sample_date`. Today is intentionally not processed (partial-day data trips `flag_volume_anomaly` falsely); the loop service handles yesterday on its first tick after midnight UTC.

## Predictor (standalone)

Three CLI modes:

```bash
# Mode 1: backtest by resolve date
docker compose run --rm predictor node src/predictor.js --date 2026-04-21

# Mode 2: pending-eval (apples-to-apples with trainer evaluation)
docker compose run --rm predictor node src/predictor.js \
  --pending-eval-date 2026-04-21 \
  --output-json /app/tools/queue-forecasting/trainer/data/baseline/2026-04-21.json \
  [--exclude-dates 2026-04-21,2026-04-23]

# Mode 3: aggregate NDJSON for residual training
docker compose run --rm predictor node src/predictor.js \
  --export-baseline-predictions \
  --from 2026-03-23 --to 2026-04-29 \
  --output /app/tools/queue-forecasting/trainer/data/baseline/baseline_predictions.ndjson \
  [--exclude-dates ...]
```

Other helpers:

```bash
docker compose run --rm predictor node src/predict-sample.js   # single pending task
docker compose run --rm predictor node src/diagnose.js          # DB diagnostics
```

### Live Predictor

The `live-predictor` service consumes the trainer's ONNX bundles and writes a
prediction row to `queue_forecast_run_predictions` for every new task-pending
event. It polls Postgres on a fixed interval (`LIVE_PREDICTOR_POLL_MS`,
default 5000) for currently-unresolved unpredicted rows, keyset-paginated
through any backlog. Predictions land within a poll interval of pending.

Start it: `docker compose --profile live-predictor up -d live-predictor`.
Required artifacts: the latest `trainer/data/models/<date>/` must contain the
two production bundles (`wait_time_residual_throughput_filtered_baseline_*`
and `run_duration_residual_*`).

## Database tables

| Table | Grain | Source |
|---|---|---|
| `queue_forecast_tasks` | one row per `task_id` | collector (Pulse + Queue API) |
| `queue_forecast_task_runs` | one row per `(task_id, run_id)` | collector |
| `queue_forecast_run_predictions` | one row per `(task_id, run_id)` | trainer / predictor |
| `queue_forecast_worker_counts` | one row per `(task_queue_id, sampled_at)` (5-min) | worker-counter |
| `queue_forecast_worker_pools` | one row per `task_queue_id` | worker-counter (daily refresh) |
| `queue_forecast_daily_health` | one row per `sample_date` | health-monitor |

Schema in `init.sql`; additive migrations in `migrate.sql`. Test suites drop and recreate the `public` schema — never run them against production data.

## Tests

```bash
# Node smoke tests (idempotency, FK constraints, enrichment)
DATABASE_URL=postgresql://postgres@localhost:5433/forecasting node test/smoke.js

# Python trainer unit tests
cd trainer
uv run pytest
uv run ruff check .
```

## Stopping & cleanup

```bash
docker compose down              # stop services, keep data volume
docker compose down -v           # destroy data volume (resets schema — don't do this lightly)

# Remove a single service:
docker compose stop health-monitor
docker compose rm -f health-monitor
```

## Troubleshooting

**`train.py: error: the following arguments are required: --config`** — you ran a non-train command via the trainer service, but the entrypoint is hard-coded to `python -m src.train`. Override with `--entrypoint uv trainer run python -m <module>`.

**Walk-forward stops silently after one cohort, no error** — was the symptom when `resolve_excluded_dates` printed nothing (Policy A by design) and `set -euo pipefail` killed the script on grep's rc=1. Fixed in `scripts/run_training.sh` with `{ grep ... || true; }`.

**Predictor writes baseline to `data/baseline_filtered/` but file is missing on host** — make sure the predictor service mounts the parent `data/` directory (not just `data/baseline/`). Already fixed in `docker-compose.yml`.

**Apr 25–26 (or any day) shows `n=0`, `anom=False`** — earlier bug in the detector skipped the volume check when `n_total = 0`. Fixed; n=0 days now flag `volume_anomaly`. Re-run `compute_daily_health.py` to refresh.

**Postgres on host port 5432 vs 5433** — this tool deliberately uses 5433 to avoid collision with the monorepo's standard Postgres on 5432.

**`FileNotFoundError: .../trainer/data/baseline/baseline_predictions.ndjson`** — only happens if you bypass `run_training.sh` / `walk_forward.sh` (both auto-generate the file). To force regeneration, delete the file and re-run the master script.
