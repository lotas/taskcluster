# Plan: Trainer-side ONNX export + JS load smoke test

## Context

The goal is to start populating `queue_forecast_run_predictions` with predictions for every incoming task so we can measure candidate-model accuracy in real time. The two production candidates (`wait_time_residual_throughput_filtered_baseline.yaml` and `run_duration_residual.yaml`) are residual LightGBM models — they cannot serve from Node.js until ONNX inference is wired up.

The Explore pass confirmed **zero ONNX scaffolding exists today**: no Python export, no JS runtime, no category-vocabulary persistence, no `model_version` field, no live throughput-feature pipeline. There are seven distinct gaps before residual models can serve.

This plan tackles the **trainer-side prerequisites only**, plus a tiny JS smoke test that proves the artifacts round-trip into `onnxruntime-node`. Live serving (collector hook, predictions-table INSERT, baseline cache, throughput-feature live computation, schema migration) is a separate follow-up.

### Decisions already taken
- ONNX library: **onnxmltools** (`convert_lightgbm`).
- Export trigger: **always-on**; every `train.py` run produces `.onnx` alongside `.lgb`.
- `model_version` format: **`v_<YYYY-MM-DD>_<serving_hash>`** (e.g. `v_2026-05-13_3a9f2c81`). The `serving_hash` is **not** the existing `cache_key` — see §1a — it covers the full serving-relevant config so retraining the same data-shape with different hyperparameters or transforms publishes a distinct version.
- This plan does **not** populate `queue_forecast_run_predictions` yet — that's the follow-up.

---

## Changes

### 1. Add ONNX deps to trainer

**Files:** `trainer/pyproject.toml:5-12`, `trainer/uv.lock`

Add to dependencies:
- `onnxmltools` — LightGBM → ONNX conversion.
- `onnxconverter-common` — required transitive for `onnxmltools`.
- `onnx` — schema/serialization.
- `onnxruntime` — **required** for the Python parity test in §7 (loads the exported ONNX back and predicts). Not used at training time, but pulling it into the trainer image keeps the test self-contained.

**Lock file is non-optional:** `trainer/Dockerfile:9-11` runs `uv sync --frozen`, which fails the build if `pyproject.toml` lists a dep absent from `uv.lock`. After editing `pyproject.toml`, run `uv lock` (or `uv sync` without `--frozen` locally) inside `trainer/` to refresh `uv.lock`, and commit both files together. Verify by rebuilding the trainer image (`docker compose build trainer`) before merging.

Pin compatible versions against the installed `lightgbm` version (recorded in `manifest.lightgbm_version` from `train.py:189`); onnxmltools' LightGBM converter has known compatibility ranges.

### 1a. Define `serving_hash` (NEW)

**File:** `trainer/src/data_loader.py` (next to existing `cache_key`)

The existing `cache_key(c)` at `data_loader.py:24-38` is **query-shaping only** — it deliberately excludes model hyperparameters so cached parquet survives hyperparameter sweeps. That's wrong for `model_version`: two trains over the same data shape with different `model_params`, `quantiles`, `residual.transform`, `residual.baseline_feature`, `baseline_dir`, `anomaly_filter`, or `throughput_features` produce **different on-disk models** but would collide on `cache_key`.

Add a sibling function. **Critical: feature lists are NOT sorted in `serving_hash`** — `FeatureBuilder._derive` (`features.py:60-105`) builds `X` as `categorical_features + numeric_features` in the order they appear in the YAML, and that order is positional in the ONNX float tensor. Two configs with the same feature *set* in different order would produce identical hashes but require different input tensors — a silent serving bug. Sort stays in `cache_key` (which only governs parquet cache identity, where row content is invariant under column order).

```python
def serving_hash(c: Config) -> str:
    """8-hex-char SHA256 over every config field that affects the served model.

    Includes everything cache_key covers, plus model_params, quantiles,
    residual block (transform, baseline_feature, baseline_file),
    baseline_dir, anomaly_filter, throughput_features, velocity_features.

    Feature lists are preserved in YAML order (NOT sorted) because the position
    of each feature in the input tensor is part of the serving contract.
    """
    payload = {
        "shaping": {
            "target":               c.target,
            "target_column":        c.target_column,
            "filters":              sorted(c.filters),            # set semantics
            "categorical_features": list(c.categorical_features), # ORDERED
            "numeric_features":     list(c.numeric_features),     # ORDERED
            "derived_features":     c.derived_features,
            "lookback_days":        c.lookback_days,
            "validation_days":      c.validation_days,
            "holdout_days":         c.holdout_days,
        },
        "model_type":          c.model_type,
        "model_params":        c.model_params,
        "quantiles":           list(c.quantiles),                 # ORDERED
        "residual":            c.residual,            # dict or None
        "baseline_dir":        c.baseline_dir,
        "anomaly_filter":      c.anomaly_filter,      # dict or None
        "throughput_features": c.throughput_features,
        "velocity_features":   getattr(c, "velocity_features", None),
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:8]
```

`cache_key` keeps its existing meaning (cache invalidation; column order does not affect cached parquet content). `serving_hash` is the new identity for `model_version`. Both go into the manifest so the relationship between cached training data and shipped model stays auditable.

### 1b. Artifact-content hash + training-data lineage (NEW)

`model_version` identifies a **config + as-of-date**, not the exact bytes shipped. The same (config, date) retrained later picks up new task rows from Postgres or a regenerated `baseline_predictions.ndjson`, producing different `.onnx` bytes under the same `model_version`. For prediction-table debugging ("which exact bytes scored this row?") that's not enough.

Add two more manifest fields:

- **`artifact_hash`** (string, 16 hex chars): SHA256 over the concatenated bytes of `<stem>_p50.onnx`, `<stem>_p90.onnx`, `<stem>_category_mappings.json`, `<stem>_feature_schema.json`, in that order. Computed in `train.py` *after* all four files are written, then folded into the manifest before the manifest itself is written. This is the byte-level identity of what would ship.

  **Loader contract:** `artifact_hash` is stored only in the manifest, which is **not** in `serving_artifacts`. A consumer that downloads only the four serving files needs to be able to verify them, so the loader contract is: **the JS runtime recomputes `artifact_hash` from the four files it loaded and compares to the value it got from the manifest (or from an out-of-band registry).** Document this in the future serving plan; don't bake the manifest into `serving_artifacts` because it carries training-only fields (`evaluation`, `windows.{train,val,holdout}`) that have no business in serving. The JS smoke test in §6 does this recomputation as a sanity check today.

- **`training_lineage`** (object): captures inputs that the config doesn't pin. **All "did the inputs change?" questions must be answerable by content hash, not filename.** The main parquet cache is only one of *several* data inputs — the feature pipelines for throughput and velocity load their own separately-cached parquet files, and the anomaly-filter codepath queries `queue_forecast_daily_health` on every run. All of these can mutate while the main cache stays unchanged.
  ```json
  {
    "training_cache_file": "wait_time_lb14_asof2026-05-13_<cache_key>.parquet",
    "training_cache_content_sha256": "<hex>",
    "training_row_counts": { "train": ..., "val": ..., "holdout": ... },
    "training_excluded_dates": ["2026-04-23", "..."],         // empty list if no training-side anomaly filtering
    "anomaly_filter_basis": {                                  // null when anomaly_filter is disabled or mode=baseline-only
      "mode": "training",                                      // "training" | "both"
      "flag_subset": ["flag_volume_anomaly", "..."],           // null → "is_anomalous=TRUE"
      "query": "SELECT sample_date FROM queue_forecast_daily_health WHERE is_anomalous = TRUE"
    },
    "engineered_feature_inputs": {                             // omit keys whose features are disabled
      "throughput_runs": {                                     // present iff throughput_features.enabled
        "file": "data/cache/throughput_runs_<from>_<to>.parquet",
        "content_sha256": "<hex>"
      },
      "worker_counts": {                                       // present iff velocity_features.enabled
        "file": "data/cache/worker_counts_<from>_<to>.parquet",
        "content_sha256": "<hex>"
      },
      "worker_pools": {                                        // present iff velocity_features.enabled
        "source": "queue_forecast_worker_pools",
        "row_count": 650,
        "content_sha256": "<hex>"                              // hash of sorted-rows snapshot, see below
      }
    },
    "baseline_ndjson_meta": {                                  // null for non-residual configs
      "file": "data/baseline_filtered/baseline_predictions.ndjson",
      "from_date": "2026-04-22",
      "to_date":   "2026-05-12",
      "exclude_dates": ["2026-04-23", "..."],
      "generated_at": "2026-05-13T03:14:00Z",
      "content_sha256": "<hex>"
    }
  }
  ```

  - `training_cache_file` + `training_cache_content_sha256`: the main parquet's filename and content hash. Filename is config-shape + date derived (`cache_key`) — regenerating from different Postgres rows produces the same filename, so the filename alone is not lineage. Compute the SHA256 inside `train.py` after the cache write/read in `data_loader.load`.
  - `training_row_counts` is already in `manifest.windows.{train,val,holdout}.rows` — just cross-reference, don't duplicate.
  - **`training_excluded_dates`**: the exact list of dates returned by `data_loader.load_anomalous_dates(c)` (`data_loader.py:271-296`) — but only when `anomaly_filter.mode` ∈ {`training`, `both`} (`train.py:94-104` is where these dates actually get applied to filter `train_df`/`val_df`). Sorted ISO strings. Empty list when not applicable. Without this, two runs of `wait_time_residual_throughput_filtered_both.yaml` on the same date can produce different `train_df`/`val_df` with identical `training_cache_content_sha256` and no record of why.
  - **`anomaly_filter_basis`**: the SQL condition actually used. `flag_subset` (or null), plus the literal condition string built at `data_loader.py:288-292`. Future debugging: "why did this excluded-date set look weird?" needs to know whether it came from `is_anomalous=TRUE` (default) or a custom flag subset.
  - **`engineered_feature_inputs`**:
    - `throughput_runs`: when `c.throughput_features.enabled`, hash the parquet at `load_task_runs_for_throughput`'s cache path (`data_loader.py:221`). Wait config uses this; duration config doesn't.
    - `worker_counts`: when `c.velocity_features.enabled`, hash the parquet at `load_worker_counts`'s cache path (`data_loader.py:148`). Production candidates don't enable velocity_features, but the manifest schema must support it for future configs.
    - `worker_pools`: `load_worker_pools` (`data_loader.py:177-196`) reads `queue_forecast_worker_pools` directly with **no caching**. To hash it, materialize it once in `train.py` (sort by `task_queue_id`, write to a tmp parquet or in-memory bytes via `df.to_parquet`), hash, then discard. Records the dimension-table state at train time even though there's no on-disk artifact.
  - `baseline_ndjson_meta.content_sha256`: SHA256 of the NDJSON file's bytes (full hex). The other fields come from `<NDJSON>.meta.json` (written by `scripts/ensure_baseline_ndjson.sh:66-79`).

  **Implementation note:** computing these hashes adds I/O. The main cache and the engineered-feature caches are already read or written during training, so hashing on-the-fly (streaming `hashlib.sha256` over file bytes) is cheap. Avoid round-tripping through a temp file for `worker_pools` — hash the bytes returned by `df.to_parquet(buffer)` directly.

Together: `model_version` answers "what config + when?", `artifact_hash` answers "are these the exact bytes?", `training_lineage` answers "what data + auxiliary inputs + anomaly filtering did this model see?" — with hashes, not filenames, so any byte-level change in any input shows up. The future predictions table will store `model_version` + `artifact_hash` so a row can be traced to a specific deployment.

### 2. ONNX export on the quantile model

**File:** `trainer/src/model.py`

`LightGBMQuantileModel` does **not** persist feature names today — `__init__` (`model.py:29-32`) stores only `alpha`, `params`, `booster`. Need to add a field so `save_onnx` can build the ONNX input shape and the schema sidecar can emit `feature_order`.

- In `LightGBMQuantileModel.fit` (`model.py:34-58`), after the call to `lgb.train`, store:
  ```python
  self.feature_names_ = list(X_train.columns)
  ```
  This locks in the exact training-frame column order. (Avoids reliance on `booster.feature_name()`, which doesn't always preserve original casing/spelling — and we need to match the JS feature-vector construction byte-for-byte.)
- Add `save_onnx(self, path: Path) -> None` to `LightGBMQuantileModel`. Internally:
  - Build `initial_types=[("input", FloatTensorType([None, len(self.feature_names_)]))]`.
  - Call `onnxmltools.convert_lightgbm(self.booster, initial_types=initial_types, target_opset=<pin>)`.
  - Write `.onnx` bytes to `path`.
- For the residual subclass `ResidualLightGBMQuantileModel` (`model.py:89-152`), `save_onnx` exports the **raw transformed-space** model (inherits the base method via the wrapped booster). The `log_ratio` inverse runs in JS using `feature_schema.json` (see §5). No ONNX-side post-processing.
- Both quantile boosters export independently: `<stem>_p50.onnx` and `<stem>_p90.onnx` next to the existing `.lgb` files.

**Known compatibility concern:** LightGBM's categorical splits become `Equal`/`Set`-style nodes in ONNX. `onnxmltools` supports this but a round-trip test (see §7) is mandatory to confirm prediction parity within tolerance.

### 3. Category-vocabulary sidecar

**Files:** `trainer/src/features.py`, `trainer/src/train.py`

- `FeatureBuilder._categories` (`features.py:28`) currently holds vocab in memory; only persistence today is implicit inside the `.lgb` text's `pandas_categorical:` line.
- Add `FeatureBuilder.dump_categories(self, path: Path) -> None` that writes JSON:
  ```json
  { "task_queue_id": ["proj-a/linux-large", "..."], "tags.kind": ["build", "test", "..."], ... }
  ```
  Index position in each list is the integer code the model sees. Missing/unseen values → `-1` (pandas-Categorical convention; pin this in a docstring).
- `train.py:163-166` (the save loop) writes `<stem>_category_mappings.json` once per config, alongside the models.

### 4. Feature-schema sidecar (the JS contract)

**File:** `trainer/src/train.py`

Write `<stem>_feature_schema.json` per config (single file, not per-quantile) containing:
```json
{
  "model_version": "v_2026-05-13_3a9f2c81",
  "target": "wait_time",
  "feature_order": ["task_queue_id", "scheduler_id", "...", "bl_wait_p50", "queue_tasks_started_15m", "..."],
  "categorical_features": ["task_queue_id", "scheduler_id", "priority_at_pending", "tags.kind", "..."],
  "numeric_features": ["queue_pending", "max_run_time_s", "hour_sin", "...", "bl_wait_p50", "queue_tasks_started_15m", "..."],
  "derived_features": {
    "cyclical_time": { "source": "pending_at" },
    "build_type_regex": { "source": "metadata_name", "pattern": "/(debug|opt)[-/]" }
  },
  "residual": {
    "transform": "log_ratio",
    "baseline_feature": "bl_wait_p50",
    "baseline_file": "baseline_predictions.ndjson",
    "baseline_dir": "data/baseline_filtered"
  },
  "anomaly_filter": { "enabled": true, "mode": "baseline" },
  "throughput_features": { "enabled": true, "windows_minutes": [15, 60] },
  "velocity_features": null,
  "quantile_models": {
    "0.5": "wait_time_residual_throughput_filtered_baseline_p50.onnx",
    "0.9": "wait_time_residual_throughput_filtered_baseline_p90.onnx"
  },
  "category_mappings_file": "wait_time_residual_throughput_filtered_baseline_category_mappings.json",
  "cold_start_code": -1
}
```

`feature_order` is authoritative — it's `self.feature_names_` captured at `fit()` time per §2, which is the order `FeatureBuilder._derive` produces in `features.py:60-105`. The JS side must reproduce this ordering exactly when building the input tensor.

**Engineered-feature config blocks are echoed raw** (`throughput_features`, `velocity_features`, `derived_features`, `anomaly_filter`, `residual`). The Python trainer computes these features via `data_loader.add_throughput_features` at `data_loader.py:355-366` (windows from `c.throughput_features.windows_minutes`). Without these blocks in the sidecar, the JS-side feature builder (future work) can't know which windows to compute or which derived features to apply — listing only the final numeric column names like `queue_tasks_started_15m` is not enough because the window size is encoded in the column name and the JS side would have to parse it back out. Echoing the raw YAML blocks avoids that fragility.

Non-residual configs simply set `residual: null` and omit the `anomaly_filter` block. Configs without throughput features set `throughput_features: null`.

### 5. Manifest additions

**File:** `trainer/src/train.py:186-229`

Add to the manifest dict:
- `model_version: "v_{as_of_date}_{serving_hash}"` — built once near top of `_build_manifest`, alongside the existing `config_hash` (`train.py:189`).
- `serving_hash`: the new 8-hex hash from §1a, kept as its own field for traceability (don't lose the standalone hash inside the composite version string).
- `cache_key` (rename the existing `config_hash` field to `cache_key`, OR keep `config_hash` and add `serving_hash` separately): be explicit about which hash is which. Recommendation: **keep `config_hash` unchanged for backward compat with the dashboard** and **add `serving_hash` as a new field**. Add an inline comment that future code should prefer `serving_hash` for model identity.
- `serving_artifacts`: list of relative filenames *needed at serve time*: `[<stem>_p50.onnx, <stem>_p90.onnx, <stem>_category_mappings.json, <stem>_feature_schema.json]`. This is what a JS consumer (or any other serving runtime) downloads.
- `training_artifacts`: list of relative filenames produced by training but not needed at serve time: `[<stem>_p50.lgb, <stem>_p50.lgb.meta, <stem>_p90.lgb, <stem>_p90.lgb.meta]`. Keeping `.lgb.meta` listed avoids the prior plan's bug of pretending those files don't exist.

(Earlier draft used a single `artifacts` key; splitting into `serving_artifacts` and `training_artifacts` makes the consumer contract explicit and avoids the omission of `.lgb.meta`.)

Keep all existing manifest keys unchanged for backward compat with `src/dashboard-gen.js:185-207`.

### 6. JS-side load smoke test

**Files:** `test/onnx-smoke.js` (NEW), `package.json`, `yarn.lock` (monorepo root)

- Add `onnxruntime-node` to `tools/queue-forecasting/package.json` dependencies (will also be needed by the future serving path; safe to install now).
- **Update root `yarn.lock`:** the collector's Dockerfile (`Dockerfile:8-25`) installs Node deps from the monorepo root `yarn.lock` via `yarn install` against a stitched-together `package.json`. Adding a new dep requires running `yarn install` at the repo root after editing the workspace `package.json`, then committing the updated `yarn.lock`. Without this, the queue-forecasting Docker image build will still resolve from the stale lockfile and not install `onnxruntime-node`.
- New script `test/onnx-smoke.js` that:
  1. Locates the latest `trainer/data/models/<YYYY-MM-DD>/` (reuse the directory-walking logic from `src/dashboard-gen.js:185-207`).
  2. For each `*_feature_schema.json` it finds:
     - Loads the schema, category mappings, both `_p50.onnx` / `_p90.onnx`, and the manifest.
     - **Recomputes `artifact_hash`** from the four serving files (same concat order as §1b) and asserts it matches `manifest.artifact_hash`. This exercises the loader contract — if the JS recomputation diverges from the trainer's, every prediction would be served against unverified bytes.
     - Constructs a synthetic feature vector: numerics filled with `0.0` (or a plausible non-zero for `bl_*_p50` like `60.0` and `max_run_time_s` like `3600.0`); categoricals as the first index in their mapping list. Cyclical features computed for a fixed timestamp.
     - Runs both models via `onnxruntime-node`, asserts output is a finite Float32 of expected shape, prints `(p50_raw, p90_raw)`.
     - For residual models, applies the `log_ratio` inverse using the synthetic baseline and prints the un-transformed value.
  3. Exits non-zero on any error.
- Add a run line to `README.md` under the existing test section.

This script **does not write to the predictions table** and does **not** require the collector. It's a build-time artifact verifier.

### 7. Tests

- `trainer/tests/test_model.py`: new test exercising the **exact JS-side encoding path**, not just the in-Python predict. The bug we're guarding against: ONNX takes a `FloatTensor`, but `booster.predict()` in Python normally consumes a pandas DataFrame with `categorical` dtype and gets the integer codes implicitly. If the JS side encodes categoricals differently from how `onnxmltools` baked the splits, predictions silently diverge.

  Test plan:
  1. Build a small training frame with at least one categorical column (e.g. 50 rows, 1 categorical with 4 levels `["a","b","c","d"]`, 2 numerics) and 2 quantiles.
  2. Fit `LightGBMQuantileModel`, then `save_onnx(p50_path)` and `save_onnx(p90_path)`.
  3. **Reference path:** build `X_test` so its categorical column is `pd.Categorical(values, categories=training_vocab)` — *not* `astype("category")` from raw strings, which would auto-add any unseen values to the vocabulary and silently bypass the `-1` cold-start path. Pin the categories to the training vocab so unseen values become `NaN` (pandas code `-1`). Then `y_ref = booster.predict(X_test)`.
  4. **JS-equivalent path:** manually convert `X_test` to a `np.float32` array by:
     - For each categorical column, use the same vocabulary `FeatureBuilder.dump_categories` would write: `cat_codes = pd.Categorical(X_test[col], categories=vocab).codes` → `int` array with `-1` for null/unseen.
     - Cast all columns (categoricals-as-codes + numerics) to `float32`.
     - Stack in `feature_order` produced from `self.feature_names_`.
  5. Run `session.run(None, {"input": x_float32})` and compare to `y_ref`: assert `max_abs_diff < 1e-4` for p50, p90.
  6. **Unseen-category row:** append a row with an unseen value `"z"` for the categorical column. **Crucially, the reference `X_test` keeps its training-vocab `pd.Categorical` dtype, so `"z"` becomes `NaN` (code `-1`) in the reference path too** — not a freshly-created `"z"` category. Re-run both paths. Assert they still agree within `1e-4`. This proves the `-1` cold-start code path matches the ONNX-baked categorical decision nodes; if the reference path silently widens the vocabulary, the test passes vacuously and the JS path's `-1` handling goes uncovered.
  7. **Residual round-trip:** repeat steps 1-5 with `ResidualLightGBMQuantileModel`, including the baseline column. Assert the *raw* (transformed-space) outputs match between ONNX float tensor and `booster.predict()`, then apply the JS-side `log_ratio` inverse and assert agreement with `model.predict(X_test)` (which applies the inverse internally).
- `trainer/tests/test_features.py`: new test for `dump_categories` round-trip — fit on a small frame, dump to tmpfile, reload JSON, confirm category lists match `_categories` in sorted order.
- `test/onnx-smoke.js`: described in §6; runs against actual artifacts produced by a successful `run_training.sh` invocation.
- Existing tests (`trainer/tests/`, `test/smoke.js`, `test/import-check.js`) must keep passing.

---

## Critical files

| File | Why |
|---|---|
| `trainer/src/model.py` | Add `save_onnx` to `LightGBMQuantileModel` and ensure `ResidualLightGBMQuantileModel` inherits/overrides cleanly. |
| `trainer/src/train.py` | Drive ONNX export from the save loop, write feature-schema + category-mapping sidecars, extend manifest with `model_version`, `serving_hash`, `artifact_hash`, `serving_artifacts`, `training_artifacts`, `training_lineage`. |
| `trainer/src/features.py` | Expose `dump_categories`. Confirm feature ordering inside `_derive` is deterministic and matches `feature_order`. |
| `trainer/pyproject.toml` | Add `onnxmltools`, `onnxconverter-common`, `onnx`, `onnxruntime` deps. |
| `trainer/uv.lock` | Regenerate (`uv lock`) — Dockerfile uses `uv sync --frozen`. |
| `trainer/src/data_loader.py` | Add `serving_hash` next to existing `cache_key`. |
| `package.json` (tools/queue-forecasting) | Add `onnxruntime-node` dep. |
| `yarn.lock` (repo root) | Refresh via root `yarn install` — Dockerfile installs from root. |
| `test/onnx-smoke.js` | NEW — JS-side ONNX-load verifier. |
| `trainer/tests/test_model.py` | NEW test: ONNX round-trip parity (plain + residual). |
| `trainer/tests/test_features.py` | NEW test: category-mapping dump/load. |
| `README.md` | Document `node test/onnx-smoke.js` under tests. |

## Reused existing code (do not reinvent)

- `cache_key` calculator: `trainer/src/data_loader.py:24-38`. Keep unchanged; cache invalidation still uses it. The new `serving_hash` is a sibling, not a replacement.
- Manifest scaffold: `trainer/src/train.py:186-229`. Append to it; don't rewrite.
- Per-quantile save loop: `trainer/src/train.py:163-166`. Add ONNX writes inside this loop.
- Categorical fitting + transform: `trainer/src/features.py:31-57`. Read `_categories` from the fitted builder when dumping.
- Latest-models directory walker: `src/dashboard-gen.js:185-207`. Reuse the directory-resolution pattern for the JS smoke test.

---

## Verification

End-to-end check after implementation:

0. **Docker images still build (lockfile sanity):**
   ```
   docker compose build trainer
   docker compose build collector       # Node image; uses the same Dockerfile as predictor/dashboard-gen
   ```
   Both must succeed. `uv sync --frozen` (trainer/Dockerfile:11) will fail in the trainer image if `uv.lock` doesn't match `pyproject.toml`. The Node image's root `yarn install` (Dockerfile:25) will fail (or silently skip the new dep) if `yarn.lock` wasn't refreshed.

1. **Unit tests pass:**
   ```
   docker compose run --rm --entrypoint uv trainer run pytest tests/ -v
   ```
   `--entrypoint uv` overrides the trainer service's default entrypoint of `["uv","run","python","-m","src.train"]` (docker-compose.yml:84). The service's `working_dir` is `/app/trainer`, so the `tests/` path is relative to that. New ONNX round-trip + `dump_categories` tests must both pass.

2. **Full training run produces ONNX artifacts:**
   ```
   ./scripts/run_training.sh trainer/configs/wait_time_residual_throughput_filtered_baseline.yaml
   ./scripts/run_training.sh trainer/configs/run_duration_residual.yaml
   ```
   Then list `trainer/data/models/<today>/`:
   - `wait_time_residual_throughput_filtered_baseline_{p50,p90}.{lgb,onnx}`
   - `wait_time_residual_throughput_filtered_baseline_category_mappings.json`
   - `wait_time_residual_throughput_filtered_baseline_feature_schema.json`
   - `wait_time_residual_throughput_filtered_baseline_manifest.json` with new keys: `model_version`, `serving_hash`, `artifact_hash`, `serving_artifacts`, `training_artifacts`, `training_lineage`
   - Same six files for `run_duration_residual` (with `training_lineage.baseline_ndjson_meta` populated since it's a residual config).

3. **Manifest inspection:**
   ```
   jq '.model_version, .serving_hash, .config_hash, .artifact_hash, .serving_artifacts, .training_artifacts, .training_lineage' \
     trainer/data/models/<today>/wait_time_residual_throughput_filtered_baseline_manifest.json
   ```
   Confirm:
   - `model_version` matches `v_<today>_<8-hex>`,
   - `serving_hash` differs from `config_hash` whenever the config has non-trivial `model_params` / `residual` / `anomaly_filter` blocks,
   - `artifact_hash` is a 16-hex string,
   - `training_artifacts` lists both `.lgb` and `.lgb.meta` files,
   - `training_lineage.training_cache_content_sha256` is a 64-hex string,
   - `training_lineage.training_excluded_dates` is a list (possibly empty) and `anomaly_filter_basis` is populated when the config's `anomaly_filter.mode` ∈ {`training`, `both`} (the wait baseline-filtered config uses `mode: baseline` so this stays empty/null for that one — but the duration config or a `both` variant should populate it),
   - `training_lineage.engineered_feature_inputs.throughput_runs.content_sha256` is present and 64-hex for the wait config (which enables throughput features),
   - `training_lineage.baseline_ndjson_meta` is populated (with its own `content_sha256`) for residual configs, or null otherwise.

3a. **Serving-hash sensitivity checks:**
   Each of these must yield a different `serving_hash` from the unmodified run:
   - `model_params.num_leaves: 63` → `127`
   - `residual.transform: log_ratio` → `additive`
   - Reorder `numeric_features` list (swap any two entries) — the new ordered hash must change even though `cache_key` (which sorts) does not.

3b. **Artifact-hash deterministic checks** (no "may" — all three are exact assertions):

   1. **Manifest matches reality.** From the host shell:
      ```bash
      DIR=trainer/data/models/<today>
      STEM=wait_time_residual_throughput_filtered_baseline
      EXPECTED=$(jq -r .artifact_hash "$DIR/${STEM}_manifest.json")
      ACTUAL=$(cat "$DIR/${STEM}_p50.onnx" \
                   "$DIR/${STEM}_p90.onnx" \
                   "$DIR/${STEM}_category_mappings.json" \
                   "$DIR/${STEM}_feature_schema.json" \
               | sha256sum | cut -c1-16)
      [ "$EXPECTED" = "$ACTUAL" ] || { echo "artifact_hash mismatch"; exit 1; }
      ```

   2. **Hash reacts to any serving-file byte change.** Copy the four files to a temp dir, append a single byte to the `_p50.onnx` copy, recompute the hash, assert it differs from the manifest value. Confirms the hash is actually content-addressed (catches accidental hashing-of-filenames bugs).

   3. **Training lineage matches actual file content.** Recompute SHA256 of:
      - The parquet at `manifest.training_lineage.training_cache_file` — assert it equals `training_cache_content_sha256`.
      - For configs with `throughput_features.enabled`: the parquet at `manifest.training_lineage.engineered_feature_inputs.throughput_runs.file` — assert it equals `engineered_feature_inputs.throughput_runs.content_sha256`.
      - For configs with `velocity_features.enabled`: the parquet at `engineered_feature_inputs.worker_counts.file` — assert match.
      - For residual configs: the file at `manifest.training_lineage.baseline_ndjson_meta.file` — assert it equals `baseline_ndjson_meta.content_sha256`.

   4. **Anomaly-filter lineage matches reality.** For configs with `anomaly_filter.mode` ∈ {`training`, `both`}, run the SQL recorded in `manifest.training_lineage.anomaly_filter_basis.query` against the current `queue_forecast_daily_health` table and confirm the resulting date set equals `training_excluded_dates`. (This check is time-sensitive — if the health table has been recomputed since training, expect a diff, which itself confirms the lineage is useful.)

   These four checks together guarantee that "lineage" actually identifies the bytes and date-sets used — not just filenames.

4. **JS smoke test loads and infers:**
   ```
   node test/onnx-smoke.js
   ```
   Must print non-NaN p50/p90 raw outputs for both targets and the residual-inverse value for the residual configs. Exit code 0.

5. **Dashboard still renders:**
   ```
   node src/dashboard-gen.js --once
   ```
   `--once` (`dashboard-gen.js:975-978`) skips the 5-minute regeneration loop and exits after one render. `loadLatestManifests` (`src/dashboard-gen.js:185-207`) must tolerate the new manifest fields with no errors, and the `*_manifest.json` filename pattern is unchanged.

6. **Existing smoke test unaffected:**
   ```
   DATABASE_URL=... node test/smoke.js
   ```
   No regressions (collector code paths untouched in this plan).

---

## Out of scope (explicit, for the follow-up plan)

- `queue_forecast_run_predictions` schema migration (add `predictor_kind`, change PK, add indexes).
- Wiring ONNX inference into `src/collector.js` `handleTaskPending`.
- Live baseline-percentile cache in JS (per Phase 3b task #4).
- Live throughput-feature computation (SQL view or JS port of `queue_throughput.py`).
- Policy B `is_anomalous` consultation in the live path.
- The actual INSERT into `queue_forecast_run_predictions`.

These all become tractable once this plan ships — they depend on the artifacts produced here.
