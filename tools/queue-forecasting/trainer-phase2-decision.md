# Queue Forecasting — Phase 2 Decision

**Date:** 2026-04-23
**Companion to:** `trainer-spec.md`, `trainer-plan.md`
**Authors:** residual-model experiment, wait-time transform variants, run-duration residual experiment

## 1. Decision

**Wait_time: residual architecture is regime-fragile and CANNOT ship as the single production model. Production candidate is now `wait_time` (LGB-only) or a hybrid; vanilla residual and residual_throughput are NOT viable defaults given current evidence.**

**Run_duration: residual ships. Confirmed across 11 cohorts (§6 / E16).**

### What changed (2026-04-27 evidence)

Extending the wait sweep to 14 cohorts (Apr 14 – Apr 27) revealed a **systematic monotonic decline in residual p90 coverage** since Apr 23:

| Cohort | LGB-only p90 | Residual p90 | Throughput p90 |
|---|---|---|---|
| Apr 14-22 | 0.92 (stable, in band) | 0.87-0.90 (in band) | 0.87-0.89 (in band) |
| Apr 23 | 0.942 | 0.858 | 0.849 |
| Apr 24 | 0.865 | **0.737** | **0.733** |
| Apr 25 | 0.877 | **0.659** | **0.667** |
| Apr 26 | 0.881 | **0.616** | **0.624** |
| Apr 27 | 0.864 | **0.517** | **0.547** |

LGB-only stays in band across all 14 cohorts; residual variants fail catastrophically on the 4 most recent cohorts. The same pattern holds for within-2x regressions — residuals concentrate failures on Apr 25-27, LGB-only's regressions are sporadic across the range.

This is **regime drift**, not noise. Something in wait-time dynamics shifted around Apr 22-23 — likely an external event (worker pool capacity / cluster issue / something the model can't see). The residual architecture, which is anchored to the percentile baseline, cannot track it because the baseline's 7-day percentile history is averaging over both regimes. LGB-only is unanchored and adapts.

### Implications for production

`wait_time_residual_throughput` and `wait_time_residual` were the leading candidates 3 days ago. They are now both **regime-fragile** and unsafe for production deployment as single models. p90 coverage at 0.55 means the predicted upper bound covers only half of actuals — exactly the "WTF on long waits" failure mode we were trying to avoid.

`wait_time` (LGB-only) is the only config that maintains p90 calibration through regime shifts. Its weakness (sporadic within-2x regressions, mean −2.71pp) is real but bounded; the residual variants' worst-case is unbounded under regime drift.

**Production options (revised):**

A. **Ship LGB-only.** Accept the within-2x volatility as the price of regime robustness. Stable p90 across all cohorts.
B. **Build hybrid: throughput primary + LGB-only fallback under regime detection.** Detect drift via rolling p90-coverage-vs-actual on a held-back validation set; switch to LGB-only when residual is failing. Captures the throughput within-2x stability on normal days AND the LGB-only regime robustness on stressed days. Significantly more complex.
C. **Investigate the regime cause.** If we identify the operational signal driving the shift (e.g. worker pool config change, sustained outage), we might capture it as a feature and recover the residual architecture. Until then, residual is broken on production-current data.

For run_duration, residual remains the answer — 9/11 MAE wins, 10/11 within-2x wins, p90 in band 11/11. No regime-fragility observed for the duration target.

**Production deployment still gated on:**
1. Decision between A (LGB-only) and B (hybrid). The case for B is much stronger now than it was — LGB-only has known volatility issues, residual has known regime issues, and they fail on different cohorts.
2. If B: design + implement the regime detector + ensemble.
3. Phase 3b production-path work (ONNX, Node inference, hot-reload, versioned predictions).

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

---

## 6. Experiment log

Append-only, reverse-chronological. Each entry records the exact config, cohort windows, aggregate and per-bucket metrics (wait-model only), the comparison reference, and the concrete next action it triggered.

**Conventions:**
- "Cohort" = `train [start, end), val day, holdout [start, end)` over `pending_at` in UTC.
- "Primary slice" = `reason_resolved = 'completed'` unless noted.
- Numbers are manifest `evaluation.primary.aggregate` values.
- `w/in2x` = within-2x ratio; `p90cov` = fraction of actuals ≤ predicted p90 (target band [0.85, 0.95]).
- **Same-cohort comparisons only.** Across cohorts the baseline itself shifts materially (see E10 vs E4) — comparing across runs from different dates is not valid.

### 2026-04-27 — Walk-forward extension reveals regime drift

#### E16: Walk-forward sweep across 14 cohorts (Apr 14 – Apr 27) + duration sweep across 11 cohorts (Apr 17 – Apr 27)

Extended E13 with 3 more recent cohorts (Apr 25, 26, 27). Added duration sweep at the same time. **Reverses the E13 conclusion that residual_throughput is a viable single-model production candidate.**

**Wait time, 14 cohorts:**

| Config | Mean MAE Δ% | Worst MAE Δ% | Mean w/in2x pp | Worst w/in2x pp | p90 in-band | Best 30m+ wins |
|---|---|---|---|---|---|---|
| `wait_time` (LGB-only)             | −20.10% | **+39.37%** | −2.71pp | −9.08pp  | **13/14** | **10/14** |
| `wait_time_residual`               | −17.24% | +6.67%      | −0.13pp | −18.05pp | 10/14 | 0/14 |
| `wait_time_residual_throughput`    | −20.80% | +3.08%      | +2.28pp | **−15.56pp** | 9/14  | 4/14 |

Win counts (14 cohorts):
- best_MAE: wait_time=7, throughput=6, residual=1
- best_within_2x: throughput=10, wait_time=4, residual=0
- best_30m+_within_2x: wait_time=10, throughput=4, residual=0

**The decisive new evidence: p90 coverage by cohort for residual variants.** Stable 0.87-0.89 through Apr 22, then monotonic decline:

```
        LGB-only   Residual   Throughput
Apr 22  0.916      0.873      0.870
Apr 23  0.942      0.858      0.849
Apr 24  0.865      0.737      0.733
Apr 25  0.877      0.659      0.667
Apr 26  0.881      0.616      0.624
Apr 27  0.864      0.517      0.547
```

LGB-only stays in band; residual variants drop to ~52% p90 coverage by Apr 27. Within-2x regressions concentrate similarly: residual configs regress on Apr 25/26/27; LGB-only's regressions are scattered (Apr 15, 16, 19, 21, 22, 23, 26).

**Conclusion: regime drift broke the residual architecture.** The percentile baseline averages over a regime that has shifted; the log_ratio residual cannot push predictions far enough up from the baseline reference to catch the new long-wait reality. LGB-only is unanchored and adapts.

**Run duration, 11 cohorts:**

| Config | Mean MAE Δ% | Worst MAE Δ% | p90 in-band | Best MAE wins | Best within-2x wins |
|---|---|---|---|---|---|
| `run_duration` (LGB-only)      | +1.48% (regression) | +13.03% | 11/11 | 2/11 | 1/11 |
| `run_duration_residual`        | **−3.93%**          | +12.03% | 11/11 | **9/11** | **10/11** |

Duration shows no regime fragility — both configs maintain p90 calibration across all cohorts. Residual is the cleaner choice. Phase 2 conclusion holds for duration.

**Implications:**

1. **Residual architecture for wait is regime-fragile.** Cannot ship as single model.
2. **LGB-only for wait is the only regime-robust option** at current numbers. Sporadic within-2x regressions are bounded; residual's catastrophic failures are unbounded under sustained regime shift.
3. **Hybrid (option B) is now the leading sophisticated option**, not a marginal one. Throughput on normal days, LGB-only on detected-drift days. Trigger: rolling p90-coverage tracking.
4. **Investigate the regime shift.** Apr 22-23 onward shows changed wait dynamics. Could be worker pool, cluster, deploy, demand spike. If we identify and feature-ize the cause, residual may recover.
5. **Duration target is unaffected** — ship `run_duration_residual`.

**Next actions:**
- Determine WHY the regime shifted around Apr 22-23. Worth checking: TC worker-manager logs, any cluster events, sustained capacity issues.
- Decide A (LGB-only single) vs B (hybrid + regime detection) for wait production.
- p90 calibration experiment for LGB-only — its mean p90 is 0.91, want closer to 0.90 (slight over-coverage is acceptable; under-coverage is the failure mode we just diagnosed).

### 2026-04-24 — Phase 3a feature work

#### E13: Walk-forward sweep across 11 cohorts (Apr 14 – Apr 24)  ⚠ SUPERSEDED by E16

The E13 conclusions ("ship throughput as default") have been **superseded** by E16's extended sweep. The "throughput is robust" property held only on cohorts where the holdout did not include post-Apr-22 dates. Once 4 more cohorts were added (Apr 24-27), residual variants showed catastrophic p90 collapse and within-2x regression. Keep E13 as historical context but do not treat it as current guidance.

The decisive experiment for the regime question. Three configs × 11 cohorts × each holdout a 5-day window ending on the cohort's `as_of_date`.

**Per-config summary (11 complete cohorts each):**

| Config | MAE Δ% mean | MAE Δ% worst | w/in2x pp mean | w/in2x pp worst | p90 cov mean | p90 in-band | 30m+ w/in2x mean | 30m+ ≥50% |
|---|---|---|---|---|---|---|---|---|
| `wait_time` (LGB-only)              | −27.87%  | −0.12%   | **−3.28**  | −9.08pp  | 92.01%   | 10/11 | **56.69%** | **8/11** |
| `wait_time_residual` (log_ratio)    | −21.25%  | **+6.67%** (regressed) | +3.08    | −1.42pp  | 87.08%   | 10/11 | 53.14%     | 9/11 |
| `wait_time_residual_throughput`     | −24.56%  | −0.22%   | **+5.46**  | +0.98pp  | 86.63%   | 9/11  | 54.70%     | 9/11 |

**Per-cohort win counts (of 11 cohorts):**

| Metric | wait_time | wait_time_residual | wait_time_residual_throughput |
|---|---|---|---|
| best_MAE               | **6** | 1 | 4 |
| best_within_2x         | 1     | 0 | **10** |
| best_30m+_within_2x    | **7** | 0 | 4 |

**Conclusions:**

1. **Regime dependence is real and confirmed.** Single-cohort conclusions are not trustworthy. Apr 18-22 (E4) said residual won the tail; across 11 cohorts, LGB-only wins the tail 7/11 times.

2. **`wait_time_residual` (vanilla log_ratio) is dominated.** Throughput wins everything: MAE (lower worst-case, similar mean), within-2x (mean +5.46pp vs +3.08pp), 30m+ (tied). No reason to keep vanilla residual as a separate production candidate.

3. **Three distinct profiles emerged:**
   - **LGB-only: high ceiling, high variance.** Best tail (30m+), best MAE wins most often. Downside: within-2x mean regresses (−3.28pp) and worst-case is ugly (−9.08pp on one cohort).
   - **residual_throughput: low variance, consistently good.** Within-2x wins 10/11 cohorts. Never worse than baseline on MAE (worst: −0.22%). Safest choice.
   - **vanilla residual: dominated.**

4. **No single config wins on all metrics across all cohorts.** The architecture question is no longer "which one?" but "how do we handle the regime split?"

**Candidate production paths (in order of preference):**

A. **Ship `residual_throughput` as the single wait model.** Rationale: best-in-class on within-2x (the user-perceptibility metric), never worse than baseline on MAE, meets 30m+ target on 9/11 cohorts. Concedes ~2pp of 30m+ win-rate to LGB-only but avoids LGB-only's 9pp within-2x downside risk. Simple serving path. **Recommended.**

B. **Hybrid: residual_throughput + LGB-only for long-predicted waits.** When the primary (residual_throughput) predicts above a threshold (e.g. ≥20m), run LGB-only too and use the higher prediction (or a blend). Closes the 30m+ gap at the cost of two models in the serving path. Re-visit if A's 30m+ miss rate (~2/11) is unacceptable.

C. **More feature work.** Tree-status, landing-queue, queue-level historical drift. Diminishing-returns territory after throughput already closed most of the gap.

**Duration (1 cohort only — need more runs for stability):** `run_duration_residual` wins MAE (−6.27%) vs `run_duration` (+5.63% regression). Consistent with the single-cohort E7 result from yesterday. Residual is still the answer for duration, but we haven't stress-tested it across cohorts the way we just did for wait.

**Next action: if A is acceptable, proceed to production path work (Phase 3b). If you want the hybrid, Phase 3a gets a Phase 3a-8 item for the ensemble design.**

#### Same-cohort comparison on Apr 19-23 (summary across E10, E11, E12)

All three runs on identical windows (train [2026-04-04, 2026-04-18), val 2026-04-18, holdout [2026-04-19, 2026-04-24), hold=892k rows, primary slice completed-only).

| Config | MAE | w/in-2x | p90cov | <1m MAE | <1m w/in2x | 30m+ MAE | 30m+ w/in2x |
|---|---|---|---|---|---|---|---|
| Baseline                                    | 811.9s | 48.5% | —     | 56.1s  | 41.8% | 6925s | 17.8% |
| **LGB-only** (E12)                          | 748.5s | **54.3%** | **86.5%** | 116.7s | 41.4% | **5153s** | **65.9%** |
| Residual log_ratio (E11)                    | 737.9s | 47.0% | 73.7% | **28.2s** | 41.8% | 6328s | 26.2% |
| Residual + throughput (E10)                 | **706.8s** | 49.4% | 73.3% | 22.8s  | **43.2%** | 6141s | 26.0% |

**Plot twist: LGB-only dominates 30m+ within-2x on this cohort (65.9% vs 26.2% for residual) — the reverse of yesterday's Apr 18-22 result where residual won the tail.**

**Interpretation (working theory — regime hypothesis):**
- Apr 23 specifically had a long-tail event. The 30m+ bucket went from 6% of cohort (Apr 18-22) to 9% (Apr 19-23), and baseline 30m+ within-2x dropped from 22.8% to 17.8% — the baseline got catastrophically worse at predicting long waits on Apr 23.
- Residual architecture **anchors** to baseline_p50 via `log_ratio`. When baseline is catastrophically wrong on the tail, residual inherits that wrongness.
- LGB-only is unanchored — free to predict far from baseline when features warrant. On this stressed cohort, that's the right bet.
- p90 coverage tells the same story: LGB-only 86.5% (in band) vs residual 73.7% (out of band).

**Implications:**
- Residual is NOT a universal win. It wins on normal-regime cohorts (Apr 18-22) and loses on stressed-regime cohorts (Apr 19-23).
- Phase 2 "ship residual" conclusion needs re-examination. A single cohort snapshot can swing 39pp on the critical 30m+ bucket.
- Throughput features (E10) help short-wait MAE significantly but don't rescue 30m+ under the residual architecture — the anchoring effect is dominant there.
- Need to evaluate across **multiple holdout windows** (e.g. all 5-day windows ending on each of the last 10-15 days) to understand regime-dependence before deciding architecture.

**Candidate next directions:**
- **Walk-forward evaluation**: train separately with as_of_date sliding across 10-15 recent days; see how residual vs LGB-only vs throughput ranking shifts per cohort. Identifies whether residual is the "average winner" or a same-day artifact.
- **Regime-aware ensembling**: detect unusual queue behavior (high `queue_pending_delta_60m`, low `queue_tasks_completed_15m`, etc.) and shift toward LGB-only predictions when detected.
- **Bucket-conditional model selection**: always use LGB-only for predicted-long tasks (>30m expected by baseline), residual for short. Simple gate.
- **Widen training data**: today's val is 63k rows (Apr 18) vs yesterday's 160k (Apr 17). Tiny validation sets may drive early-stopping instability. Try validation_days=3 to stabilize.

#### E12: `wait_time.yaml`  (LGB-only, no residual)  — Apr 19-23 cohort
- Cohort: train [2026-04-04, 2026-04-18), val 2026-04-18, holdout [2026-04-19, 2026-04-24)
- Rows: train=2,535,783 val=63,605 hold=892,080
- Aggregate: **MAE=748.5s w/in2x=54.3% p90cov=86.5%**
- Δ vs same-cohort baseline: MAE −7.8%, w/in2x **+5.8pp** (clears the spec +5pp target!), p90cov in band
- Per-bucket (LGB vs same-cohort baseline):
  | bucket | n       | base MAE | lgb MAE  | base w/in2x | lgb w/in2x |
  |---     |---      |---       |---       |---          |---         |
  | <1m    | 389,488 | 56.1s    | 116.7s   | 41.8%       | 41.4%      |
  | 1-5m   | 201,803 | 175.9s   | 277.2s   | 64.7%       | **69.9%**  |
  | 5-30m  | 141,964 | **517.5s** | 794.6s | 60.0%       | 60.3%      |
  | 30m+   |  75,978 | 6925s    | **5153s** | 17.8%      | **65.9%**  |
- Interpretation: continues yesterday's pattern of "LGB-only catastrophe on <1m/1-5m MAE" (116.7s and 277.2s — both worse than baseline by 2x). But on today's cohort, **30m+ within-2x lands at 65.9% — clearing the production target of ≥50%**. LGB-only is NOT usable as-is because of short-wait MAE, but its long-tail behavior is substantially better than residual's on this regime.

#### E11: `wait_time_residual.yaml` on today's cohort (re-run of Phase 2 winner)
- Cohort: same as E12
- Aggregate: **MAE=737.9s w/in2x=47.0% p90cov=73.7%**
- Δ vs same-cohort baseline: MAE −9.1%, w/in2x **−1.5pp (regression)**, p90cov **out of band**
- Per-bucket (res vs same-cohort baseline):
  | bucket | n       | base MAE | res MAE | base w/in2x | res w/in2x |
  |---     |---      |---       |---      |---          |---         |
  | <1m    | 389,488 | 56.1s    | **28.2s** | 41.8%     | 41.8%      |
  | 1-5m   | 201,803 | 175.9s   | **114.8s** | 64.7%    | 60.2%      |
  | 5-30m  | 141,964 | **517.5s** | 578.6s | 60.0%     | 52.8%      |
  | 30m+   |  75,978 | 6925s    | **6328s** | 17.8%    | 26.2%      |
- Interpretation: same architecture that scored 85.8% p90 coverage yesterday scores 73.7% today, on a cohort that's shifted by one day. Within-2x went from +2.9pp (yesterday) to −1.5pp (today). **This single cohort shift cost roughly 12pp of p90 coverage and 4pp of within-2x.** Strong evidence the Phase 2 "winner" is cohort-fragile.

#### E10: `wait_time_residual_throughput.yaml`  (Apr 19-23 cohort)
First run with DB-derived throughput/drain features: `queue_tasks_started_{15,60}m`, `queue_tasks_completed_{15,60}m`, `queue_avg_wait_{15,60}m`, `queue_avg_run_time_{15,60}m` (leakage-gated to `resolved_at < pending_at`). Per-row loops vectorized via `np.searchsorted` over per-queue cumulative arrays (earlier impl took ~20 min, vectorized ~0.5s at 100k rows).

- Cohort: train [2026-04-04, 2026-04-18), val 2026-04-18, holdout [2026-04-19, 2026-04-24)
- Rows: train=2,535,783 val=63,605 hold=892,080
- Aggregate: **MAE=706.8s w/in2x=49.4% p90cov=73.3%**
- Δ vs today's baseline: MAE −12.9%, w/in2x +0.9pp, **p90cov out of band**
- Per-bucket (LGB vs same-cohort baseline):
  | bucket | n       | base MAE | lgb MAE  | base w/in2x | lgb w/in2x |
  |---     |---      |---       |---       |---          |---         |
  | <1m    | 389,488 | 56.1s    | **22.8s** (−59%) | 41.8%  | **43.2%**  |
  | 1-5m   | 201,803 | 175.9s   | **100.2s** (−43%)| 64.7%  | **65.7%**  |
  | 5-30m  | 141,964 | **517.5s** | 537.6s (+4%)   | **60.0%** | 54.7%   |
  | 30m+   |  75,978 | 6925s    | **6141s** (−11%) | 17.8%  | **26.0%**  |
- Interpretation:
  - Throughput features deliver big wins on short-wait MAE (<1m: −59%, 1-5m: −43%) — the "queue is draining fast" signal directly helps.
  - 5-30m bucket regresses slightly on within-2x (−5.3pp); still the weakest bucket.
  - 30m+ within-2x improved (+8.2pp) but still far from ≥50% target.
  - **p90 coverage tanked to 73.3%** — residual p90 is over-confident. Possibly throughput features let p90 lean on recent fast-drain windows and miss long-tail cases.
- Cannot declare win/loss until same-cohort comparison (E11).
- Next: E11 (wait_time_residual.yaml + wait_time.yaml on today's 2026-04-24 run_dir) to enable 3-way.

#### E9: `backfill_claimed_tasks.py`  (infra, not a model run)
Computed historical `claimed_tasks` from `queue_forecast_task_runs` and wrote to `queue_forecast_worker_counts` with `source='db_derived'` for the full data range. Used `generate_series` over 5-min steps joined to runs where `started_at ≤ T AND (resolved_at > T OR resolved_at IS NULL)`. Replaces Prometheus backfill path (no API access).

#### E8: `worker-counter` service launched
Live 5-min polling of `worker-manager.listWorkerPoolsStats` started. Initial sample: 558 rows, 531 dynamic pools + 122 static pools classified in `queue_forecast_worker_pools`. Source column value `tc_api`. Live collection ongoing.

### 2026-04-23 — Phase 2 residual experiments

#### E7: `run_duration_residual.yaml`
- Cohort: train [2026-03-24, 2026-04-17), val 2026-04-17, holdout [2026-04-18, 2026-04-23)
- Rows: train=5,023,092 val=156,506 hold=757,919
- Aggregate: **MAE=130.1s w/in2x=89.7% p90cov=87.9%**
- Δ vs baseline: MAE **−6.3%**, w/in2x +1.0pp
- Δ vs LGB-only: MAE −11.3%, w/in2x +0.6pp
- Spec go/no-go: MAE ≥5% ✅, p90 in band ✅
- Interpretation: **clean win**. Phase 1 "clean miss" reverses — the baseline's `metadata_name` exact-match percentile becomes a feature instead of a competitor, and LightGBM adds small but consistent corrections.

#### E6: `wait_time_residual_logdiff.yaml`  (`log_diff` transform)
- Cohort: Apr 18-22 (same as E4/E5)
- Aggregate: **MAE=519.9s w/in2x=54.6% p90cov=85.8%**
- Matches E4 (`log_ratio`) to the last decimal.
- Interpretation: confirmed algebraic identity `log1p(y) - log1p(bl) ≡ log((y+1)/(bl+1))`. Useful as a sanity check but no new information.

#### E5: `wait_time_residual_additive.yaml`  (additive transform)
- Cohort: Apr 18-22
- Aggregate: **MAE=549.9s w/in2x=53.4% p90cov=90.9%**
- Δ vs E4 (log_ratio): MAE **+5.8% (worse)**, w/in2x **−1.2pp (worse)**, p90cov **+5.1pp (better)**
- Per-bucket (vs same-cohort baseline):
  | bucket | base MAE | add MAE  | base w/in2x | add w/in2x |
  |---     |---       |---       |---          |---         |
  | <1m    | 32.0s    | 39.9s    | 43.3%       | 43.1%      |
  | 1-5m   | 117.6s   | 136.8s   | 67.5%       | 68.3%      |
  | 5-30m  | 423.2s   | 528.0s   | 62.3%       | 63.1%      |
  | 30m+   | 8175s    | 6652s    | 22.8%       | **45.3%**  |
- Interpretation: additive loses on MAE and w/in2x across most buckets, but wins on **30m+ within-2x (+22.5pp)** and **p90 calibration**. Logged as an option if p90 coverage becomes the binding constraint or if the 30m+ long-tail win becomes more important than short-bucket gains. Not the default winner.

#### E4: `wait_time_residual.yaml`  (log_ratio transform) — the Phase 2 incumbent winner
- Cohort: train [2026-04-04, 2026-04-18), val 2026-04-18, holdout [2026-04-19, 2026-04-24) (i.e. Apr 18-22 cohort measured before today's data rolled)
- Rows: train=2,615,709 val=160,628 hold=775,042
- Aggregate: **MAE=519.9s w/in2x=54.6% p90cov=85.8%**
- Δ vs same-cohort baseline: MAE **−15.3%** ✅, w/in2x +2.9pp (target +5pp — fail), p90cov in band ✅
- Δ vs E2 (LGB-only): MAE −3.6%, w/in2x +11.9pp
- Per-bucket:
  | bucket | n    | base MAE | res MAE | base w/in2x | res w/in2x |
  |---     |---   |---       |---      |---          |---         |
  | <1m    | 357k | 32.0s    | **29.4s** | 43.3%     | **44.8%**  |
  | 1-5m   | 182k | 117.6s   | **105.3s** | 67.5%    | **69.8%**  |
  | 5-30m  | 127k | **423.2s** | 478.3s  | 62.3%     | **65.1%**  |
  | 30m+   |  43k | 8175s    | **6525s** | 22.8%     | **38.6%**  |
- Interpretation: **MAE spec cleared**; within-2x improvement below original +5pp target. Architecture validated. Production deployment gated: user bar is ratio-accuracy at 30m+ (current 38.6% far from acceptable). Drove Phase 3a feature work.

#### E3: `run_duration.yaml`  (LGB-only, Phase 1)
- Cohort: Apr 18-22
- Aggregate: MAE=146.6s w/in2x=89.1% p90cov=88.0%
- Δ vs baseline: MAE **+5.6% (worse)**, w/in2x +0.4pp
- Verdict: clean miss; baseline `metadata_name` exact-match percentile is hard to beat without using it as a feature. Drove E7 residual experiment.

#### E2: `wait_time.yaml`  (LGB-only, Phase 1)
- Cohort: Apr 18-22
- Aggregate: MAE=539.1s w/in2x=42.7% p90cov=94.2%
- Δ vs baseline: MAE −12.2% (below 15% spec), w/in2x **−9.0pp (regression)**
- Per-bucket catastrophe in <1m: within-2x 23.5% (baseline 43.3%). Drove E4 residual experiment (baseline-as-feature specifically to rescue <1m).

#### E1: Baseline percentile predictor — `predictor.js --pending-eval-date`
Reference point, not a model run. Per-day JSONs under `trainer/data/baseline/*.json`. Aggregate over Apr 18-22, completed-only:
- Wait: MAE=613.7s w/in2x=51.7%
- Duration: MAE=138.8s w/in2x=88.7%
- Cohort filter matched to trainer (`started_at NOT NULL AND queue_pending NOT NULL AND wait_duration_s ≥ 0` for wait; standard filter for duration). Without matching cohort the baseline appeared 10% worse (MAE 677s for wait) — a noisy-no-queue-context artifact.

### Shared observations (valid across experiments)

- **Cohort shifts day-to-day are large.** Apr 18-22 vs Apr 19-23 shows baseline wait MAE moving 613.7s → 811.9s and <1m MAE 32.0s → 56.1s. Cross-cohort comparisons are not valid — always compare within a single run_dir.
- **The 30m+ bucket is the production gate.** Current best (E4): within-2x 38.6%. Target: ≥50%. Gap attributable to feature-saturation on existing inputs (queue_pending, priority, time-of-day) — direct velocity signal is expected to close most of it.
- **p90 calibration is sensitive.** Varies 73.3% (E10) → 85.8% (E4) → 90.9% (E5) → 94.2% (E2) across otherwise-related configs. Worth tracking; a future experiment dedicated to calibration (e.g. training with alpha=0.92 for p90) may be warranted.
- **Per-row loops over 5M rows at ~200µs/row = ~17 min.** Vectorize via `np.searchsorted` over per-queue cumulative arrays — cuts to seconds (confirmed in E10 vectorization pass).
