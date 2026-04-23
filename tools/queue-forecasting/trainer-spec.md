# Queue Forecasting — Trainer Spec (Phase 1)

Companion to `spec.md`. This document specifies the **first pass** at the
Python training pipeline: a hybrid experimentation-and-production scaffold
that answers "does LightGBM beat the percentile baseline, and by how much?"
while laying down structure we can keep for the nightly retrain pipeline
later.

Assumes familiarity with the overall design in `spec.md`. See that document
for the broader goals, schema, and deployment architecture options.

## Goal

Train LightGBM quantile regression models for both targets (run duration and
queue wait time), evaluate them on held-out data, and produce enough signal
to decide whether to invest in the full nightly pipeline (ONNX export,
real-time inference, hot-reload).

## Current baseline (numbers to beat)

Measured on 2026-04-20 holdout (`src/predictor.js`):

| Target | Within-2x | MAE |
|---|---|---|
| Run duration | 87.4% | 150.2s |
| Wait time | 50.3% | 193.2s |

Run duration baseline is dominated by `metadata_name` exact-match percentiles
(93% coverage). Wait time baseline uses `task_queue_id + pending_bucket` —
the obvious 2-factor interaction, which is why LightGBM should improve on
it significantly.

## Scope

### In scope (Phase 1)

- Python 3.13 trainer under `tools/queue-forecasting/trainer/`
- LightGBM quantile regression for both run duration and wait time
- Two models per target: p50 and p90 (separate trained models, same config)
- Parquet-cached data loading from Postgres
- Feature engineering: categorical casts, tag JSONB extraction, build-type
  regex, cyclical time encoding
- Per-day holdout evaluation (MAE, within-2x, pinball loss, p90 calibration)
- Dockerized: `docker compose run --rm trainer --config ...`
- Model abstraction layer so XGBoost can be swapped in later

### Out of scope (deferred)

- ONNX export (needed only when wiring inference into Node.js)
- Category mapping sidecar (only needed for ONNX)
- XGBoost implementation (pluggable interface is enough for now)
- Real-time inference path
- Writes to `queue_forecast_run_predictions`
- Nightly cron scheduling
- Model hot-reload in the predictor
- Parity tests between Python and ONNX runtime

## Directory layout

```
tools/queue-forecasting/
├── src/                              # existing Node.js (collector, baseline predictor)
├── trainer/                          # NEW
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── configs/
│   │   ├── run_duration.yaml
│   │   └── wait_time.yaml
│   ├── src/
│   │   ├── __init__.py
│   │   ├── data_loader.py
│   │   ├── features.py
│   │   ├── model.py
│   │   ├── train.py
│   │   └── evaluate.py
│   └── data/                         # gitignored
│       ├── cache/                    # Parquet caches
│       └── models/                   # Trained models + manifests, per run date
└── ...
```

Training cache and model outputs live under `trainer/data/` and are
volume-mounted so they persist across container runs.

## Module responsibilities

Each module has one clear purpose and can be tested independently.

### `data_loader.py`

- Query Postgres with the config's training filter and lookback window
- Cache result as Parquet under
  `data/cache/<target>_lb<N>_asof<ISO8601>_<cfg8>.parquet`
  where `<cfg8>` is the first 8 hex chars of
  `sha256(canonical_json(query_shaping_config))`. "Query-shaping config"
  includes: `target`, `target_column`, `filters`, selected columns
  (derived from `categorical_features` + `numeric_features`), and
  lookback/window dates. It does **not** include model hyperparameters
  or output paths. This ensures a filter or column-list change produces
  a different cache key automatically.
- Subsequent loads with the same query-shape hit the cache (sub-second)
- `--refresh-cache` flag forces a re-query even if the cache is present

**Interface:** `load(config) -> pd.DataFrame`

### `features.py`

Stateful builder — vocabulary fit on train only, applied verbatim to val
and holdout. This prevents category-code drift across splits (a queue
that's code 5 in train cannot become code 8 in holdout).

- Tag JSONB extraction (`tags.kind`, `tags.os`, `tags.project`, `tags.test-type`,
  `tags.worker-implementation`)
- Build-type regex extraction from `metadata_name` (debug/opt)
- Cyclical time encoding from `pending_at` (`hour_sin`, `hour_cos`, `day_sin`, `day_cos`)
  — only for wait time model
- Cast categorical columns to `pandas.Categorical` using the fixed
  vocabulary learned during `fit`; unseen values in val/holdout become
  NaN (LightGBM handles natively as "unknown")
- Record per-split stats: which features are categorical vs numeric,
  cardinalities, NULL rates, and — critically — **per-feature unseen-rate**
  on val/holdout (the real cold-start metric)

**Interface:**

```python
@dataclass
class Split:
    X: pd.DataFrame        # feature matrix, LightGBM-ready
    y: pd.Series           # target column (guaranteed non-null by the
                           # loader's config filters)
    meta: pd.DataFrame     # non-feature columns used for slicing/reporting:
                           #   pending_at, reason_resolved, task_id, run_id
                           # resolved_at is intentionally NOT here — the
                           # evaluator never needs to check "has ground
                           # truth?" because the loader's filters enforce
                           # that upstream (see Evaluation Protocol)
    stats: dict            # per-split feature stats (cardinalities, NULL
                           # rates, unseen rates for val/holdout)

class FeatureBuilder:
    def __init__(self, config): ...
    def fit_transform(self, df) -> Split    # called once, on train
    def transform(self, df) -> Split        # called on val and holdout
```

The `meta` DataFrame is row-aligned with `X` and `y`. It carries the
columns `evaluate.py` needs for slicing — `pending_at` (for per-day
breakdown) and `reason_resolved` (for primary vs supplemental slices) —
without putting them into the feature matrix. `task_id` and `run_id`
are included for per-row debugging.

Typical use from `train.py`:

```python
builder = FeatureBuilder(config)
train = builder.fit_transform(train_df)
val   = builder.transform(val_df)
hold  = builder.transform(hold_df)
```

### `model.py`

Abstract interface plus LightGBM implementation. Designed so XGBoost can
slot in as a sibling subclass with no changes elsewhere.

```python
class QuantileModel(ABC):
    def __init__(self, alpha: float, params: dict): ...
    @abstractmethod
    def fit(self, X_train, y_train, X_val, y_val): ...
    @abstractmethod
    def predict(self, X) -> np.ndarray: ...
    @abstractmethod
    def save(self, path: Path): ...
    @classmethod
    @abstractmethod
    def load(cls, path: Path) -> "QuantileModel": ...

class LightGBMQuantileModel(QuantileModel):
    # objective='quantile', alpha=<0.5 or 0.9>
    # Uses LightGBM's native categorical support via dtype='category'
    # Early stopping on validation set
    ...

# Deferred (no implementation yet, just pattern):
# class XGBoostQuantileModel(QuantileModel): ...
```

One instance = one quantile. The trainer creates two instances (p50 and p90)
and saves each to its own file.

### `train.py`

CLI entrypoint. Orchestrates:

1. Load config YAML
2. Call `data_loader.load(config)` to pull all required rows
3. Split the DataFrame by `pending_at` into `train_df`, `val_df`,
   `hold_df` (see Evaluation Protocol below for bounds)
4. `train = builder.fit_transform(train_df)`; `val = builder.transform(val_df)`;
   `hold = builder.transform(hold_df)` — `FeatureBuilder` is fit once on
   train, applied to the other splits. No features are recomputed
   downstream.
5. For each quantile in config: instantiate model,
   `fit(train.X, train.y, eval_set=(val.X, val.y))`, save
6. Call `evaluate.evaluate(models, hold, config, baseline_dir)` —
   the `Split` object carries `meta.reason_resolved` and
   `meta.pending_at` so the evaluator can slice to primary
   (completed-only) and supplemental (completed + failed) populations
   and compute per-day breakdowns without ever touching the raw frame
7. Write manifest JSON alongside models

**CLI:**

```
python -m trainer.src.train --config configs/wait_time.yaml [--refresh-cache]
```

### `evaluate.py`

Holdout evaluation. Reports metrics per-day and aggregate, broken out
into two slices:

- **Primary (completed-only)** — the apples-to-apples comparison against
  the Node.js percentile baseline. This is the go/no-go metric for
  Phase 1.
- **Supplemental (completed + failed)** — full production population.
  Reported for visibility; not used for the go/no-go decision.

Metrics (computed identically for both slices):

- **MAE** (mean absolute error, seconds)
- **Within-2x** rate — defined in Evaluation Protocol below, matches
  the zero-handling rule in `src/predictor.js`
- **Pinball loss** at the trained quantile (p50 model → pinball-0.5;
  p90 model → pinball-0.9)
- **p90 calibration** — `mean(actual <= pred_p90)`; target ~0.90

Also reads per-day baseline JSONs (one per holdout day) and prints
per-day + aggregate deltas. Baseline and trainer aggregate the same
way (per-row, not per-day-mean) to keep numbers comparable.

**Interface:** `evaluate(models, hold: Split, config, baseline_dir) -> MetricsReport`

The evaluator operates purely on the already-transformed `Split`. It
never sees `hold_df` and never calls `FeatureBuilder`, so there is no
way for feature recomputation to drift from what the model was trained
on. Primary vs supplemental slicing is done via `hold.meta.reason_resolved`;
per-day slicing is done via `hold.meta.pending_at.dt.floor("D")`.

## Config files

One YAML per target. Driven entirely by config; no hardcoded feature lists
in `train.py`.

### `configs/wait_time.yaml`

```yaml
target: wait_time
target_column: wait_duration_s

lookback_days: 14
holdout_days: 5            # configurable — will try 7 later
validation_days: 1
as_of_date: 2026-04-24     # exclusive upper bound on pending_at. See "Time bounds" below for the null rule.

filters:
  - "r.started_at IS NOT NULL"
  - "r.queue_pending IS NOT NULL"
  - "r.wait_duration_s IS NOT NULL"
  - "r.wait_duration_s >= 0"

categorical_features:
  - task_queue_id
  - scheduler_id
  - priority_at_pending
  - tags.kind
  - tags.os
  - tags.project
  - tags.worker-implementation

numeric_features:
  - queue_pending
  - max_run_time_s
  - hour_sin
  - hour_cos
  - day_sin
  - day_cos

derived_features:
  cyclical_time: { source: pending_at }

model_type: lightgbm
quantiles: [0.5, 0.9]
model_params:
  num_leaves: 63
  learning_rate: 0.05
  n_estimators: 500
  early_stopping_rounds: 20
  min_data_in_leaf: 100
```

### `configs/run_duration.yaml`

Key differences from wait time:

- `target_column: run_duration_s`
- `lookback_days: 30`
- Filters: `reason_resolved IN ('completed', 'failed')`, `run_duration_s IS NOT NULL`,
  `started_at IS NOT NULL`
- Features drop `priority_at_pending`, `queue_pending`, cyclical time
- Features add `metadata_name`, `normalized_name`, `tags.test-type`,
  `build_type` (derived)
- Derived features include `build_type_regex: { source: metadata_name,
  pattern: "(debug|opt)" }`

**Deliberate population mismatch between training and primary evaluation:**
The duration filter above keeps both `completed` and `failed` runs in
the *training* data — a 25-minute test that fails still took 25 minutes,
and excluding failures biases training toward only successful (often
shorter) runs. But the *primary evaluation slice* is `completed` only
(to match the Node.js baseline, which also evaluates only completed
runs). This is intentional. The supplemental slice includes failures so
the model's behavior on that population is still measured; it just
doesn't feed the go/no-go decision. This asymmetry is noted here so
nobody reads the two values as aligned by default.

## Evaluation protocol

This is a **pending-time forecaster**: predictions are made the instant
a run enters `pending`. All splitting, training, and evaluation is
anchored on `pending_at` — never `resolved_at`.

### Time bounds

All window bounds are **half-open intervals** `[start, end)` over
`pending_at`, using UTC. `as_of_date` is an ISO instant (e.g.
`2026-04-24T00:00:00Z`) and is the exclusive upper bound. Configs may
specify a date-only string (`2026-04-24`); the loader interprets this
as `T00:00:00Z` of that day.

If `as_of_date` is null, the loader normalizes it to **today's UTC
midnight** (the most recent past midnight relative to wall-clock time
at invocation). This guarantees every holdout day is a complete
`[D 00:00Z, D+1 00:00Z)` window and drops any partial current-day
data on the floor. Consequences:

- Training run at any time on 2026-04-23 with null `as_of_date`
  resolves to `2026-04-23T00:00:00Z`. Holdout is Apr 18 → Apr 22
  (five complete days). Apr 23 data is excluded entirely.
- To include Apr 23 in evaluation, wait until Apr 24 or set
  `as_of_date: 2026-04-24` explicitly.

Partial-day holdouts are disallowed because they silently poison
per-day metric aggregation (one short day pulls down counts and
distorts aggregates) and break the baseline's per-whole-day JSON
contract.

Given `as_of_date = A`, `lookback_days = L`, `validation_days = V`,
`holdout_days = H`:

```
train:   [A - (L + V + H) days,  A - (V + H) days)
val:     [A - (V + H) days,      A - H days)
holdout: [A - H days,            A)
```

Training is clipped to `max(train_start, earliest_available_pending_at)`
if the database has less than `L` days of history. The materialized
windows (actual start/end + row counts) are recorded in the manifest.

### Feature-available time (no leakage)

At the moment a prediction is made, only information known at
`pending_at` is legitimate input. This matters for the **baseline**
percentile history as well as anything a future model might use:

- For a prediction on a run whose `pending_at` falls in day D, history
  used to compute percentiles must come from rows with
  `resolved_at < D 00:00Z` (start of D).
- This is slightly conservative — it's the start-of-day cutoff rather
  than a per-row `resolved_at < pending_at` cutoff — but it prevents
  leakage and is cheap to compute. Per-row cutoffs are deferred until
  shown to matter.

The trainer enforces this by construction (training set is entirely
before the validation window; validation is entirely before holdout).
The baseline must enforce the same rule — see "Baseline export" below.

### Evaluation population

For each holdout day D, the evaluation set = runs where
`pending_at ∈ [D 00:00Z, D+1 00:00Z)`.

Primary evaluation filters further to `reason_resolved = 'completed'`
— the population the Node.js baseline uses. This is the apples-to-apples
slice and the go/no-go metric for Phase 1.

Supplemental evaluation adds `reason_resolved = 'failed'` for visibility
but is **not** used to decide the Phase 1 outcome.

Rows without ground truth for the target are excluded **upstream by
the loader's config filter**, not by the evaluator. Per target:

- **Wait time**: the loader's filter requires `started_at IS NOT NULL
  AND wait_duration_s IS NOT NULL`. Any row satisfying these has
  observable wait duration, even if the run is still executing and
  `resolved_at` is NULL.
- **Run duration**: the loader's filter requires `reason_resolved IN
  ('completed', 'failed') AND run_duration_s IS NOT NULL AND
  started_at IS NOT NULL`. These jointly imply the run is resolved.

Because the evaluator only ever sees rows that have already passed
these filters, `Split.meta` does not need to carry `resolved_at` —
the evaluator never has to re-check "is there ground truth?". If a
future target requires different filtering, the config changes and
the invariant is still maintained at load time.

### Within-2x rule (zero handling)

Matches `src/predictor.js:471-474` exactly:

```
if predicted > 0 AND actual > 0:
    within_2x = max(pred/actual, actual/pred) <= 2
else:
    row is counted in n but excluded from the within_2x numerator
    and denominator
```

Wait time can legitimately be zero, so without this rule the ratio is
undefined. This definition is documented here as the single source of
truth for both sides of the comparison.

## Data loading

### Query template

Per config, the loader emits a query like:

```sql
SELECT r.<target_column> AS y,
       r.pending_at,
       r.resolved_at,
       r.reason_resolved,
       r.queue_pending,
       r.priority_at_pending,
       t.task_queue_id,
       t.scheduler_id,
       t.metadata_name,
       t.normalized_name,
       t.max_run_time_s,
       t.tags
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON r.task_id = t.task_id
WHERE r.pending_at >= $train_start
  AND r.pending_at <  $as_of_date
  AND <config.filters joined by AND>
```

Only the columns the config needs are selected. For the wait model,
`metadata_name`/`normalized_name` are skipped; for the duration model,
`queue_pending`/`priority_at_pending` are skipped. `reason_resolved`
is always selected — it's used at split time to build the primary vs
supplemental evaluation populations.

The query pulls the full `[train_start, as_of_date)` range in one go;
the trainer splits into train/val/holdout after the DataFrame is loaded.

### Splitting

Pure `pending_at`-based split using the half-open bounds defined under
"Evaluation protocol":

```
[------- train -------][--- val ---][------ holdout ------)
train_start         val_start     hold_start            as_of
```

LightGBM uses `(X_val, y_val)` as the `eval_set` for early stopping.
No shuffling, no random splits anywhere in the pipeline.

## Feature engineering

### Tag extraction

JSONB `tags` column is already a Python dict after `pandas.read_parquet`
(pandas handles JSON fields correctly when round-tripping through Parquet
— verify this in implementation; if not, cast to dict explicitly).

For each `tags.<key>` in the config, extract into a new column, cast
`Categorical`, missing values become NaN (LightGBM handles natively).

### Build type (duration model only)

```python
df["build_type"] = df["metadata_name"].str.extract(r"/(debug|opt)[-/]", expand=False)
df["build_type"] = df["build_type"].astype("category")
```

Rows that don't match (e.g. non-test tasks) get NaN and LightGBM
routes them through a default split.

### Cyclical time (wait model only)

```python
hour = df["pending_at"].dt.hour
dow = df["pending_at"].dt.dayofweek
df["hour_sin"] = np.sin(2 * np.pi * hour / 24)
df["hour_cos"] = np.cos(2 * np.pi * hour / 24)
df["day_sin"] = np.sin(2 * np.pi * dow / 7)
df["day_cos"] = np.cos(2 * np.pi * dow / 7)
```

### Categorical handling

Every feature in `categorical_features` is cast to `pandas.Categorical`
using the train-fit vocabulary via `FeatureBuilder` (see `features.py`
above). LightGBM's `categorical_feature='auto'` detects these and uses
native categorical splits — no one-hot encoding, no manual integer
encoding. This is one of the main reasons we chose LightGBM over
XGBoost for v1.

Unseen categorical values in val/holdout become NaN and are routed
through LightGBM's default "missing" branches. `FeatureBuilder` tracks
the unseen-rate per column per split so cold-start performance is
measurable (see manifest).

## Evaluation

### Metrics

Computed per holdout day and aggregated across the holdout window.
Aggregation is per-row (concatenate all holdout rows, then compute),
not per-day-mean — this matches how the baseline aggregates and keeps
comparisons consistent.

For each quantile model:

- **n**: number of predictions in the slice
- **MAE**: `mean(|pred - actual|)`
- **Within-2x**: see Evaluation Protocol above for the exact zero-handling
  rule (matches `src/predictor.js`)
- **Pinball loss** at target quantile q:
  `mean(max(q*(actual-pred), (q-1)*(actual-pred)))`
- **p90 coverage** (for the p90 model): `mean(actual <= pred_p90)` — target ~0.90

Every metric is reported twice per model: once on the primary slice
(`reason_resolved = 'completed'`) and once on the supplemental slice
(`reason_resolved IN ('completed', 'failed')`).

### Output

Numbers below are illustrative only (not from a real run):

```
=== Wait Time Model — Holdout Evaluation ===
Config: configs/wait_time.yaml
Windows (UTC), lookback_days=14, validation_days=1, holdout_days=5:
  train:   [2026-04-04T00Z, 2026-04-18T00Z)   14d, 2.35M rows
  val:     [2026-04-18T00Z, 2026-04-19T00Z)    1d, 171k rows
  holdout: [2026-04-19T00Z, 2026-04-24T00Z)    5d, 823k rows

--- Primary slice: reason_resolved = 'completed' ---
Per-day (p50 model):
              N       MAE    w/in-2x  pinball-p50  pinball-p90  p90-cov
Apr 19 Sun   72k    142s    58.4%        71.0         38.2       88.1%
Apr 20 Mon  154k    156s    54.8%        78.1         41.9       89.4%
...
Aggregate   ...

Baseline (--pending-eval-date, completed-only, same holdout days):
  Aggregate: 193.2s MAE, 50.3% within-2x

Delta (LightGBM - baseline):
  MAE:    -22.9%
  w/in-2x: +6.8pp

--- Supplemental slice: reason_resolved IN ('completed','failed') ---
(same table structure; no baseline delta reported)
```

Baseline numbers are read from per-day JSON files under
`trainer/data/baseline/` (one per holdout day, produced by
`predictor.js --pending-eval-date D --output-json ...`). The trainer
aggregates these identically to how it aggregates its own per-day
numbers, so the deltas compare like to like.

## Artifacts

### Model files

Per training run:

```
trainer/data/models/2026-04-24/
├── wait_time_p50.lgb
├── wait_time_p90.lgb
├── wait_time_manifest.json
├── run_duration_p50.lgb
├── run_duration_p90.lgb
└── run_duration_manifest.json
```

Directory name is the `as_of_date` of the training run. LightGBM's
native text format (`.lgb` via `booster.save_model()`). Readable,
versionable, no pickle security risk.

### Manifest

One JSON per target per training run. Field values below are
illustrative:

```json
{
  "target": "wait_time",
  "config_path": "configs/wait_time.yaml",
  "config_hash": "a3f1c28e",
  "trained_at": "2026-04-24T02:11:03Z",
  "model_type": "lightgbm",
  "lightgbm_version": "4.5.0",
  "windows": {
    "as_of_date": "2026-04-24T00:00:00Z",
    "lookback_days": 14,
    "validation_days": 1,
    "holdout_days": 5,
    "train":   { "start": "2026-04-04T00:00:00Z", "end": "2026-04-18T00:00:00Z", "rows": 2347102 },
    "val":     { "start": "2026-04-18T00:00:00Z", "end": "2026-04-19T00:00:00Z", "rows": 171032 },
    "holdout": { "start": "2026-04-19T00:00:00Z", "end": "2026-04-24T00:00:00Z", "rows": 823419 }
  },
  "features": {
    "categorical": [...],
    "numeric": [...],
    "cardinalities": { "task_queue_id": 73, "scheduler_id": 12, ... },
    "null_rates":    { "tags.test-type": 0.31, ... },
    "unseen_rates_holdout": { "task_queue_id": 0.002, "metadata_name": 0.047, ... }
  },
  "model_params": {...},
  "quantiles": [0.5, 0.9],
  "evaluation": {
    "primary": {
      "slice": "reason_resolved = 'completed'",
      "per_day": [...],
      "aggregate": {...},
      "baseline_delta": {...}
    },
    "supplemental": {
      "slice": "reason_resolved IN ('completed','failed')",
      "per_day": [...],
      "aggregate": {...}
    }
  }
}
```

Enough to reproduce the run and diff between runs.

## Dockerization

### `docker-compose.yml` additions

New `trainer` service:

```yaml
trainer:
  build:
    context: ../..
    dockerfile: tools/queue-forecasting/trainer/Dockerfile
  depends_on:
    postgres:
      condition: service_healthy
  profiles:
    - trainer
    - full
  env_file:
    - .env
  environment:
    DATABASE_URL: postgresql://postgres@postgres:5432/forecasting
  volumes:
    - ./trainer:/app/trainer
    - ./trainer/data:/app/trainer/data
  entrypoint: ["uv", "run", "python", "-m", "trainer.src.train"]
```

Update the existing `predictor` service so it can write baseline
JSONs to the same shared directory the trainer reads from:

```yaml
predictor:
  # ... existing fields unchanged ...
  volumes:
    - ./src:/app/tools/queue-forecasting/src
    - ./trainer/data/baseline:/app/tools/queue-forecasting/trainer/data/baseline
```

The second mount is the only addition. The path `/app/tools/queue-forecasting/trainer/data/baseline`
inside the predictor container is where `--output-json` writes; it
resolves to `./trainer/data/baseline/` on the host, which the
trainer mounts at `/app/trainer/data/baseline/`.

### `trainer/Dockerfile`

```dockerfile
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app/trainer
COPY tools/queue-forecasting/trainer/pyproject.toml \
     tools/queue-forecasting/trainer/uv.lock ./
RUN uv sync --frozen --no-install-project

COPY tools/queue-forecasting/trainer /app/trainer

ENV PYTHONPATH=/app
```

Source is volume-mounted in compose so iteration doesn't require rebuild;
bake-in keeps the image self-contained for reproducibility.

### Baseline generation (separate container)

The trainer image is Python-only. It does not contain Node.js and
cannot execute `src/predictor.js`. Baseline JSONs are generated by
the existing `predictor` service (which already has Node + the
collector's `src/` mounted) and the trainer reads the resulting
files from a shared volume.

Both containers mount the same `./trainer/data/baseline/` directory:

- `predictor` writes `<D>.json` files there via
  `--pending-eval-date D --output-json ...`
- `trainer` reads them during evaluation

At trainer startup, it scans `trainer/data/baseline/` for one JSON
per holdout day. If any are missing it **exits with a clear error**
telling the user which days to generate, rather than silently
skipping baseline comparison.

### Orchestration script

`tools/queue-forecasting/scripts/run_training.sh` wraps the two-step
workflow so the user only invokes one thing:

```bash
#!/usr/bin/env bash
# Usage: run_training.sh configs/wait_time.yaml
set -euo pipefail
CONFIG="$1"

# Step 1: resolve the holdout days from the config (tiny helper, no DB
# access required — just parses the config and computes the window).
# Note on compose semantics: --entrypoint takes a single executable;
# positional args after the service name become that entrypoint's argv.
# Passing "uv run python ..." as a single --entrypoint string would make
# docker look for an executable literally named "uv run python ...".
HOLDOUT_DAYS=$(docker compose run --rm \
  --entrypoint uv \
  trainer \
  run python -m trainer.src.resolve_holdout_days --config "$CONFIG")

# Step 2: generate per-day baselines via the predictor service.
for d in $HOLDOUT_DAYS; do
  OUT="trainer/data/baseline/$d.json"
  if [[ -f "$OUT" ]]; then
    echo "baseline exists: $OUT"
    continue
  fi
  docker compose run --rm predictor \
    node src/predictor.js \
      --pending-eval-date "$d" \
      --output-json "/app/tools/queue-forecasting/$OUT"
done

# Step 3: train + evaluate.
docker compose run --rm trainer --config "$CONFIG"
```

### Usage

```bash
# Full pipeline: baselines + training + evaluation
./scripts/run_training.sh configs/wait_time.yaml
./scripts/run_training.sh configs/run_duration.yaml

# Train only (skips baseline generation; errors if baselines missing)
docker compose run --rm trainer --config configs/wait_time.yaml

# Refresh Parquet cache
docker compose run --rm trainer --config configs/wait_time.yaml --refresh-cache

# Regenerate baselines only (useful after predictor.js changes)
rm -f trainer/data/baseline/*.json
./scripts/run_training.sh configs/wait_time.yaml   # will now regenerate

# Drop into a shell for ad-hoc exploration
docker compose run --rm --entrypoint bash trainer
```

The compose update mounts `./trainer/data` into the `predictor`
service as well, so both containers see the same `baseline/`
directory on the host.

Follows the same profile convention as the existing `collector` and
`predictor` services.

## Dependencies

`pyproject.toml` (managed by `uv`):

```toml
[project]
name = "queue-forecasting-trainer"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
    "lightgbm>=4.5.0",
    "pandas>=2.2.0",
    "numpy>=1.26.0",
    "pyarrow>=15.0.0",          # Parquet I/O
    "psycopg[binary]>=3.2.0",   # Postgres driver
    "pyyaml>=6.0.0",
    "scikit-learn>=1.5.0",      # utilities (splits, metrics)
]

[tool.uv]
dev-dependencies = [
    "pytest>=8.0",
    "ruff>=0.5.0",
]
```

XGBoost is deliberately not added yet — it comes in when the XGBoost
`QuantileModel` subclass is implemented.

## Baseline export from Node.js

Two additions to `src/predictor.js`:

### `--pending-eval-date D` mode

Mirrors the trainer's evaluation semantics. Given date `D`:

- **Evaluation set**: runs where `pending_at ∈ [D 00:00Z, D+1 00:00Z)`
  AND `reason_resolved = 'completed'` AND `run_duration_s` / `wait_duration_s`
  is populated (depending on target).
- **History cutoff for percentile stats**: rows with
  `resolved_at < D 00:00Z`. This is the feature-available-time rule
  from the Evaluation Protocol section — the baseline must not peek
  at any resolution that happened after the prediction would have
  been made.
- **History lookback window**: same trailing 7 days used by
  `--date` mode, but clipped to `resolved_at < D 00:00Z`.

The existing `--date D` mode (evaluate by `resolved_at`) remains for
historical continuity, but is **not** used for Phase 1 go/no-go
comparisons. All model-vs-baseline numbers in the trainer output
come from `--pending-eval-date`.

### `--output-json <path>` mode

When combined with `--pending-eval-date`, writes a single-day JSON blob
containing **raw numerators and denominators per metric**. This is
required for correct cross-day aggregation: the within-2x rule
(Evaluation Protocol) excludes rows where either predicted or actual
is ≤ 0, so its denominator differs from the overall row count. A
per-day percentage cannot be re-aggregated correctly — raw counts can.

```json
{
  "mode": "pending-eval-date",
  "eval_date": "2026-04-20",
  "eval_window": {
    "pending_start": "2026-04-20T00:00:00Z",
    "pending_end":   "2026-04-21T00:00:00Z"
  },
  "history_cutoff": "2026-04-20T00:00:00Z",
  "history_lookback_days": 7,
  "slice": "reason_resolved = 'completed'",

  "duration": {
    "n": 162041,
    "mae": {
      "eligible_n":    162041,
      "sum_abs_error": 24344560.4
    },
    "within_2x": {
      "eligible_n": 162005,
      "hit_n":      141625
    }
  },
  "wait": {
    "n": 162041,
    "mae": {
      "eligible_n":    162041,
      "sum_abs_error": 31306320.2
    },
    "within_2x": {
      "eligible_n": 159722,
      "hit_n":      80342
    }
  }
}
```

Field values are illustrative. `n` is total evaluated rows (for
reporting and sanity checks). The `eligible_n` under each metric is
the denominator actually used for that metric:

- `mae.eligible_n` — rows with a valid prediction and actual (normally
  equals `n`; may be smaller if a prediction was NULL)
- `within_2x.eligible_n` — rows where both predicted and actual are
  strictly positive (per the zero-handling rule)
- `within_2x.hit_n` — rows meeting the within-2x criterion

### Aggregation formulas

Across K holdout days, the trainer computes:

```
aggregate_mae       = sum(day.mae.sum_abs_error)   / sum(day.mae.eligible_n)
aggregate_within_2x = sum(day.within_2x.hit_n)     / sum(day.within_2x.eligible_n)
```

The trainer's own per-day metrics on the holdout are emitted in the
same shape. This is what "aggregate identically to the baseline" means
— identical formulas over the same raw-count fields.

### Invocation pattern

For each holdout day, the trainer's orchestration runs:

```bash
node src/predictor.js \
  --pending-eval-date 2026-04-19 \
  --output-json trainer/data/baseline/2026-04-19.json
```

Five invocations for a 5-day holdout. The Python evaluator reads all
five JSONs, applies the formulas above, and compares against its own
aggregates computed the same way. No Python reimplementation of the
percentile logic, and no aggregation drift.

## Success criteria for Phase 1

Go/no-go is decided on the **primary slice** (`reason_resolved =
'completed'`, pending-at-anchored, same cohort as the baseline
`--pending-eval-date` run).

A "go" signal to invest in the full pipeline (ONNX export, inference
wiring, nightly scheduling) is:

- **Wait time**: MAE improves by ≥15% and within-2x improves by ≥5pp
  over baseline (aggregate across holdout)
- **Run duration**: MAE improves by ≥5% (harder because baseline is strong)
- Improvement is consistent across at least 3 of the 5 holdout days
  (not driven by a single outlier day)
- p90 coverage is within [85%, 95%] for both models

The supplemental slice (completed + failed) is reported alongside but
does **not** feed into the go/no-go decision — changing the population
at the same time as changing the model would make "did it improve?"
unanswerable.

If any of the above fail, we stop, inspect feature importance /
residuals, and iterate before committing to the broader pipeline.

## Deferred work (references `spec.md` for full design)

- ONNX export + category mapping sidecar
- XGBoost `QuantileModel` subclass
- Node.js predictor integration via `onnxruntime-node`
- Parity tests (Python vs ONNX)
- `queue_forecast_run_predictions` writes
- Nightly cron / scheduling
- Model hot-reload
- Migration to bugbug (Approaches A/B in `spec.md`)
