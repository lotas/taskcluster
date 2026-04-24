# Queue Forecasting — Phase 2 Decision

**Date:** 2026-04-23
**Companion to:** `trainer-spec.md`, `trainer-plan.md`
**Authors:** residual-model experiment, wait-time transform variants, run-duration residual experiment

## 1. Decision

**Architecture validated; production gated on feature-maturity work.**

The residual approach — baseline percentile prediction as an input feature, LightGBM learning a log-ratio correction — outperforms both the baseline-only predictor and model-only LightGBM on both targets. It meets the MAE spec threshold for both targets and improves within-2x for both. However, wait-time ratio-accuracy on long waits (30m+ bucket: 38.6% within-2x) is not yet user-acceptable — roughly 61% of predictions in that bucket fall outside the 0.5x–2x band. Production deployment is gated on closing this gap. The primary path is queue-velocity feature experiments (active worker count, tasks_completed_in_last_N_min) that would let the model distinguish a fast-draining queue from a stalled one.

## 2. Evidence

Five-day holdout (Apr 18-22), cohort-matched, primary slice (`reason_resolved = 'completed'`).

### Run duration

| Metric | Baseline | LGB-only | **Residual** | Δ vs Baseline | Spec |
|---|---|---|---|---|---|
| MAE | 138.8s | 146.6s | **130.1s** | **−6.3%** | ≥5% ✅ |
| within-2x | 88.7% | 89.1% | **89.7%** | **+1.0pp** | (MAE primary) |
| p90 coverage | — | 88.0% | 87.9% | — | [85, 95]% ✅ |

Phase 1 classified duration as a "clean miss" because LightGBM-only lost by +5.6% MAE to the baseline's `metadata_name` exact-match. Residual reverses that verdict — same memorization becomes an input to the model rather than a competitor to it.

### Wait time

| Metric | Baseline | LGB-only | **Residual (log_ratio)** | Δ vs Baseline | Spec |
|---|---|---|---|---|---|
| MAE | 613.7s | 539.1s | **519.9s** | **−15.3%** | ≥15% ✅ |
| within-2x | 51.7% | 42.7% | **54.6%** | **+2.9pp** | +5pp ❌ |
| p90 coverage | — | 94.2% | 85.8% | — | [85, 95]% ✅ (edge) |

Wait clears the MAE spec (−15.3%) and moves within-2x from regression to improvement, but the +2.9pp gain does not reach the original +5pp target. This is carried as a known gap, not a blocker.

### Per-bucket wait breakdown

| Bucket | n | % | Base MAE | LGB MAE | **Res MAE** | Base w/in-2x | LGB w/in-2x | **Res w/in-2x** |
|---|---|---|---|---|---|---|---|---|
| <1m   | 357k | 50% | 32.0s | 35.9s | **29.4s** | 43.3% | 23.5% | **44.8%** |
| 1-5m  | 182k | 26% | 117.6s | **82.3s** | 105.3s | 67.5% | **76.1%** | 69.8% |
| 5-30m | 127k | 18% | **423.2s** | 455.5s | 478.3s | 62.3% | 53.7% | **65.1%** |
| 30m+  | 43k | 6% | 8175s | 6956s | **6525s** | 22.8% | 27.0% | **38.6%** |

Residual wins on 82% of the cohort (<1m, 1-5m, 30m+) on either MAE or within-2x or both. The one bucket where it loses to baseline on MAE (5-30m) is the smallest except for the tail.

**30m+ ratio-accuracy is the production blocker.** The 38.6% within-2x in the 30m+ bucket means ~61% of long-wait predictions are ratio-wrong by a user-visible margin — the kind of error where actual wait is 30 minutes but the predictor shows 1 minute. This is the specific gap that blocks production. The 30m+ bucket is 6% of volume but disproportionately the cases users care about most (long-running or delayed tasks where an ETA is most meaningful).

### Transform variants (tested, rejected)

Both `additive` (`y_t = y - bl`) and `log_diff` (`y_t = log1p(y) - log1p(bl)`) were trained and evaluated against `log_ratio`:

- `log_diff` is algebraically identical to `log_ratio`; numbers match to the last decimal.
- `additive` regresses MAE (+5.8%) and within-2x (−1.2pp) vs `log_ratio`. Only win: p90 calibration improves from 85.8% → 90.9%, closer to the ideal 90%. Logged as an option if p90 calibration becomes a higher priority than MAE.

## 3. Chosen design

**Residual LightGBM with `log_ratio` transform.** Both targets use the same shape:

- **Input features** include the baseline p50 prediction (`bl_wait_p50` for wait, `bl_duration_p50` for duration) as a numeric feature, alongside the existing categorical and numeric features from the Phase 1 spec.
- **Training target:** `y_t = log((y + 1) / (bl + 1))`
- **Inverse at inference:** `y_hat = exp(model_raw) * (bl + 1) - 1`
- **Two quantile models per target** (p50, p90), trained independently with `alpha ∈ {0.5, 0.9}`.
- **Baseline remains part of the serving path.** The serving flow computes the baseline prediction first (percentile lookup), feeds it into the LightGBM model, inverse-transforms the output. This is not a replacement for the baseline — it is a layered system where the baseline provides memorization and the model provides correction.

Configs in use:
- `configs/run_duration_residual.yaml`
- `configs/wait_time_residual.yaml`

## 4. Known gaps

1. **30m+ ratio-accuracy blocks production (feature-coverage gap, not architecture).** 38.6% within-2x in the 30m+ bucket means ~61% of long-wait predictions are ratio-wrong by a user-visible margin. The root cause is feature saturation: `queue_pending`, `priority_at_pending`, time-of-day, and tags cannot distinguish a queue with 500 tasks and 200 active workers (fast drain) from a queue with 500 tasks and 0 workers (stalled). Queue-velocity features — active_worker_count and tasks_completed_in_last_N_min, derivable from `queue_forecast_task_runs` via trailing-window aggregation over `started_at` / `resolved_at` — are the highest-leverage next experiment.

2. **Wait within-2x below the +5pp spec target.** Attained +2.9pp; target was +5pp. The gap is concentrated in two places:
   - 1-5m bucket: residual regresses vs LightGBM-only (−6.3pp) because the residual pulls toward baseline memorization, partially undoing LightGBM-only's strength there.
   - 5-30m bucket: neither variant beats baseline on within-2x by a margin large enough to move the aggregate.

3. **5-30m wait MAE regression.** Residual MAE (478s) is 13% worse than baseline (423s). Same root cause as gap 1 — feature saturation in the medium-wait range. Fix is feature-side. Candidate features: queue velocity over the last N minutes, recent p50-drift per queue, tree-closure / landing-queue signal.

4. **Wait p90 coverage at lower edge.** 85.8% for `log_ratio` is in the acceptable [85, 95]% band but tight against the lower bound. `additive` gets 90.9% at the cost of MAE. Tunable either via transform choice or by over-training the p90 quantile (higher `alpha` — e.g. 0.92).

## 5. Next phase

### Phase 3a — feature maturity (prerequisite for production)

Work that must complete before any model is exposed to users:

1. **Queue-velocity feature extraction.** Implement active_worker_count and
   tasks_completed_in_last_N_min as derived features in the trainer, computed
   via trailing-window aggregation over `queue_forecast_task_runs.started_at`
   and `queue_forecast_task_runs.resolved_at`. Include these fields in the
   NDJSON training export.
2. **Re-train residual wait model with velocity features.** Keep the existing
   `log_ratio` architecture; add velocity features to the wait model's feature
   set.
3. **Re-evaluate with emphasis on per-bucket within-2x, especially 30m+.**
4. **Exit criterion:** 30m+ within-2x ≥ 50% AND aggregate within-2x ≥ 60%
   (threshold subject to user sign-off before Phase 3b begins).
5. **If velocity features alone do not close the gap:** try a hurdle model
   (<1m classifier + ≥1m regressor) and/or add tree-status / landing-queue
   signal as additional features before reconsidering architecture.

### Phase 3b — production path (only after Phase 3a exits successfully)

1. **ONNX export** from Python trainer for both p50 and p90 models of both
   targets, with category-mapping sidecar JSONs (`category_mappings.json`)
   and `baseline_stats.json` as a version-tagged bundle.
2. **Parity tests** between Python LightGBM predictions and ONNX-runtime
   predictions (required before any model ships — float precision differences
   are the usual failure mode). Must include cold-start rows (categorical
   values mapped to `-1`).
3. **Node.js inference wiring** in `src/predictor.js` via `onnxruntime-node`:
   load both models at startup, replicate the `FeatureBuilder` transforms in JS
   (including the baseline-as-feature join and the `log_ratio` inverse), write
   predictions to `queue_forecast_run_predictions` on each `task-pending` event.
4. **Versioned writes to `queue_forecast_run_predictions`** with
   `predictor_kind = 'residual_lightgbm'` and a dated `model_version`.
5. **Model + baseline hot-reload** — predictor watches the models volume for new
   `.onnx` files, swaps the ONNX model, `category_mappings.json`, and
   `baseline_stats.json` atomically (they are version-coupled).
6. **Nightly training cron** in docker-compose, producing new models daily.

**Shadow mode** is a Phase 3b tool to validate new model versions under live
traffic once the first production-quality model is deployed. It is not part of
Phase 3a.

## Recommendation

Architecture is the right shape. Numbers are not user-acceptable yet. The residual + log_ratio + baseline-as-serving-artifact design is confirmed — no architecture rework needed. The next work is feature experiments (queue velocity first); production path is deferred until the long-tail ratio-accuracy meets user bar (30m+ within-2x ≥ 50%).
