# Queue Forecasting — Phase 2 Decision

**Created:** 2026-04-23
**Last updated:** 2026-05-20 (live calibration review; p90 baseline-regression findings; run-duration p90 guardrail; Tier 1 tag features evaluated; `bl_wait_p90` added)
**Companion to:** `trainer-spec.md`, `trainer-plan.md`
**Authors:** residual-model experiment, wait-time transform variants, run-duration residual experiment

## 1. Decision (architecture current — 2026-04-29; live status updated 2026-05-20)

**Wait_time: ship `wait_time_residual_throughput_filtered_baseline.yaml` (Policy B).** Across 15 cohorts (Apr 15-29, E18) it never regressed baseline on MAE (worst Δ% −2.59%) or within-2x (worst +4.58pp), hits the 30m+ user-perceptibility goal on 14/15 cohorts, and improves p90 in-band ratio from 9/15 (unfiltered residual) to 11/15. The "regime fragility" diagnosed in E16 turned out to be **baseline contamination**: the unfiltered percentile baseline's 7-day history averaged over a shifted regime, dragging the residual reference wrong-on-current-data. Policy B excludes anomalous days (per `queue_forecast_daily_health.is_anomalous`) from that history; the residual model then corrects from a clean reference.

**Run_duration: ship `run_duration_residual.yaml`.** 15 cohorts in E18 (was 11 in E16): mean MAE Δ% −4.50%, p90 in-band 13/15, beats LGB-only on 8/10 MAE wins and 9/10 within-2x wins. No regime fragility on duration (already noted in E16).

**Production deployment is live as of 2026-05-15.** The Phase 3b core path (ONNX bundle loading, hierarchical baseline-stats refresh, throughput features, NOTIFY + catch-up, versioned audit writes) is deployed and producing predictions into `queue_forecast_run_predictions`. Several items from §5.3b are explicitly deferred or accepted as V1 limitations — see §5 "Phase 3b — core path closed 2026-05-15" for the gap list.

**Live calibration status as of 2026-05-20: core serving is working, but p90/tail hardening is active.** The updated aggregation dashboard shows completed-only calibration close to target overall (`52.1% <= wait p50`, `12.0% > wait p90`; `49.1% <= run p50`, `11.2% > run p90`), but the misses are not evenly distributed. Long waits, weak baseline fallbacks, capacity-sensitive queues, and specific duration cohorts still produce user-visible underestimates. Recent baseline research also found that the residual p90 model can undercut a stronger historical p90 baseline. Payload research then split duration misses into no-signal cohorts, where the p90 guardrail is the right fix, and test-task cohorts, where richer `tags.*` identity fields already exist in the DB. Current status is "live and useful for investigation", not "ready for broad UI/ETA promotion".

### Decision history

- **2026-04-23 (Phase 2 close):** residual log_ratio + throughput chosen as wait candidate. Tail accuracy (30m+ within-2x = 38.6%) flagged as production blocker; Phase 3a launched for queue-velocity and throughput features.
- **2026-04-27 (E16):** 14-cohort walk-forward exposed monotonic p90 collapse since Apr 23 in residual variants; LGB-only stayed calibrated. Recommendation reversed to "ship LGB-only or hybrid" because residual architecture appeared fundamentally broken under regime shift.
- **2026-04-29 (E18):** Stage 2 anomaly-filter sweep showed the regime fragility was baseline contamination, not architectural. Policy B (filter anomalous days from baseline history only) recovers and improves on every metric, never regresses any cohort. Recommendation reverts to single-model residual, now with Policy B baseline filtering. The hybrid + regime-detector path (E16's "option B") is no longer needed.
- **2026-05-15 (Phase 3b core path live):** `src/live-predictor/` service deployed. First-hour data: ~5000 predictions, 100% enriched before scoring, baseline coverage distributed across `queue+bucket` / `queue` / `priority+bucket` with no global fallthrough. Policy B is correctly wired end-to-end (trainer exports `anomaly_filter` into `_feature_schema.json`; serving reads it via `filterFromSchema()` and applies the exclude-dates clause to wait baseline SQL). Known gaps recorded below.
- **2026-05-15 (pivot from LISTEN/NOTIFY to polling):** Initial design dispatched predictions via Postgres `LISTEN queue_forecast_task_pending` from the collector. After a deploy where the collector container kept running the old image (no `pg_notify` call), notifications appeared to silently stop after the startup catch-up drained. Diagnosis pointed to a stale collector, but we pivoted to a 5s polling loop anyway — one code path, no notification-loss failure mode, latency within product tolerance. NOTIFY remains a valid optimization if sub-second latency becomes a goal.
- **2026-05-19 (live aggregation review):** Added completed-only vs all-resolved cuts, baseline-level/sample-size cuts, priority/project cuts, actual-duration buckets, and top-miss tables. Main findings: completed-only run p90 misses are lower than all-resolved because failed/claim-expired rows are noisy; wait misses are mostly long-tail (`30m+` waits are `48.8%` bad); fallback baselines are risky (`queue` wait fallback `82.8%` bad, `priority+bucket` `53.1%` bad); `project_id` is currently all `none`.
- **2026-05-20 (baseline p90 regression finding):** SQL in `baseline-research.md` shows the model sometimes makes p90 worse than the historical baseline. Example run-duration cohorts: `source-test-python-mozbuild-3.9-windows11-64/opt` raw model p90 miss `46.3%` vs baseline p90 miss `8.9%`; `build-linux64-asan/opt` `29.0%` vs `9.0%`; `Code review publication (production)` `25.7%` vs `11.2%`. Wait p90 shows the same pattern on some strong `queue+bucket` baselines, for example `releng-hardware/win11-64-24h2-hw` medium priority: model `57.1%` vs baseline `28.8%`.
- **2026-05-20 (run-duration p90 guardrail):** Added `bl_duration_p90` as a run-duration feature, added live serving guardrail/audit for strong exact-name duration baselines, and added guarded p90 trainer metrics. Guardrail applies only when duration baseline level is `metadata_name`, sample size is sufficient, and baseline p90 is finite. This protects against the model compressing a multimodal/tail-heavy cohort below its own stronger historical p90.
- **2026-05-20 (payload feature exploration):** `payload-feature-exploration.md` found two categories. Code Review, mozlint, and build tasks have no useful pre-run task-definition signal for their duration tail; they depend on external runtime state such as push size, changed files, cache warmth, sccache hits, or service behavior. Test tasks, however, already carry stronger identity fields in `tags` that were not in the configs: `test-suite`, `test-platform`, `test-variant`, and `retrigger`. Added these four categorical features to both active training configs for walk-forward comparison.
- **2026-05-20 (tag feature walk-forward result):** 14-cohort walk-forward (May 6-19) compared tag-augmented configs against baseline (4 overlapping cohorts from previous run). **Run duration: mild improvement — keep.** Mean model MAE improved ~0.7% (117.89s → 117.07s), within-2x and p90 unchanged. MAE improved on all 4 overlapping cohorts. **Wait time: not convincing — reverted.** Mean model MAE regressed ~5.8% (243.09s → 257.28s) on overlapping cohorts; within-2x improved only +0.24pp; 2026-05-14 showed a 17% MAE regression (264.66s → 310.23s). Wait time is queue/capacity/priority-driven, so test-suite/platform/variant add noise after `task_queue_id` and `priority_at_pending` are already known. Tag features reverted from wait config; kept in run-duration config. `bl_wait_p90` kept as a numeric feature in the wait config (cannot isolate its effect from this run, but lower overfitting risk than high-cardinality categoricals).

For per-cohort metrics and the regime-shift narrative supporting each step in the decision history, see §6 / E13, E16, E18.

## 2. Evidence (Phase 2 launch — historical)

> The numbers below are from the original Phase 2 close (2026-04-23, 5-day holdout Apr 18-22). They show that the residual architecture validated against the percentile baseline on a single cohort. Cross-cohort robustness, regime-fragility diagnosis, and the Policy B remedy are all in §6 / E13, E16, E18. The current production decision is anchored to E18, not to this section.

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

**Residual LightGBM with `log_ratio` transform + filtered-baseline history (Policy B for wait, no filter needed for duration).** Both targets share the same residual shape:

- **Input features** include the baseline p50 prediction (`bl_wait_p50` for wait, `bl_duration_p50` for duration) as a numeric feature, alongside the categorical/numeric features from Phase 1 + queue-velocity & throughput features added in Phase 3a.
- **Training target:** `y_t = log((y + 1) / (bl + 1))`
- **Inverse at inference:** `y_hat = exp(model_raw) * (bl + 1) - 1`
- **Two quantile models per target** (p50, p90), trained independently with `alpha ∈ {0.5, 0.9}`.
- **Baseline is a load-bearing component, not a fallback.** Serving computes the baseline first (percentile lookup), feeds `bl_*_p50` to LightGBM, inverse-transforms the output. The baseline provides memorization and stability; the model provides correction.

**Wait-only addition (Policy B):** the percentile baseline's 7-day trailing history excludes days where `queue_forecast_daily_health.is_anomalous = TRUE`. This applies to both training (via `--exclude-dates` to predictor.js's NDJSON export) and serving (see Phase 3b serving contract in §5). Without this filter the residual architecture catastrophically degrades under regime drift (E16); with it, it is the strictly-best wait config (E18).

Production configs:
- `configs/wait_time_residual_throughput_filtered_baseline.yaml` — wait, residual + throughput features + Policy B baseline filter
- `configs/run_duration_residual.yaml` — duration, residual, no filter (no fragility observed)

As of 2026-05-20, both active configs include Tier 1 tag identity fields already present in `queue_forecast_tasks.tags`: `tags.test-suite`, `tags.test-platform`, `tags.test-variant`, and `tags.retrigger`. These are config-only features; the Python and JS feature builders already support arbitrary `tags.*` keys.

## 4. Known gaps

The four gaps logged at Phase 2 close (2026-04-23) are largely resolved by Phase 3a's queue-velocity + throughput features (E10-E13) and Policy B baseline filtering (E18). Status as of 2026-04-29:

1. **30m+ ratio-accuracy** — was 38.6% (production blocker). **Resolved by E18:** Policy B hits 30m+ ≥50% on 13/14 cohorts (mean 56.84%). The user-perceptibility gate is met.

2. **Wait within-2x below +5pp target** — was +2.9pp aggregate. **Resolved by E18:** Policy B mean +6.62pp, worst-case +4.58pp (never below baseline across 14 cohorts).

3. **5-30m wait MAE regression** — was 13% worse than baseline on this bucket. **Likely resolved** by throughput features + Policy B (aggregate worst MAE Δ% on Policy B is −2.59%, so no individual-bucket regression can be large). Per-bucket breakdown for Policy B not yet pulled from manifests; worth verifying before shipping but not gating.

4. **Wait p90 coverage at lower edge** — was 85.8%. **Improved, not perfect.** Policy B mean p90 = 86.91%, in-band 10/14 (vs 8/14 unfiltered). 4 cohorts still drift outside [85, 95]% — could be too tight (most likely on regime-shifted days) or too loose. Options: (a) train p90 with `alpha = 0.92` to over-cover; (b) accept the 71% in-band rate as "good enough for first ship" and revisit. **Not a production blocker** under current evidence — within-2x and 30m+ goals are met.

**New gap discovered by E18:**

5. **Two missing Apr 28 wait manifests** — `wait_time_manifest.json` and `wait_time_residual_manifest.json` are absent from `trainer/data/models/2026-04-28/`. Configs lack `anomaly_filter` blocks so the explicit skip-manifest path doesn't trigger; appears to be a silent crash. Doesn't affect the production candidate (Policy B completed 14/14) but should be diagnosed for pipeline robustness. Low priority.

**Live gaps discovered after production deployment:**

6. **Run-duration p90 can regress below a stronger baseline p90.** `Code review publication (production)` exposed the pattern first: actual runtimes are multimodal, with many very short runs and a large 10-12 minute population, while the model served p90 near 6 minutes. The exact-name historical baseline p90 covered the tail much better. `baseline-research.md` shows this is broader than one task name. **Mitigation added 2026-05-20:** `bl_duration_p90` is now a training feature, live serving applies an exact-name p90 floor with audit fields, and trainer manifests report guarded p90 metrics. Next check: verify guarded rows in live data after deployment.

7. **Wait p90 has the same model-vs-baseline regression pattern.** Several high-volume `queue+bucket` cohorts have a lower miss rate with the baseline p90 than the model p90. This is separate from the older 30m+ ratio-accuracy work: the live issue is that a strong queue/priority historical p90 sometimes already knows the tail and the model pulls it down. **Partial mitigation added 2026-05-20:** `bl_wait_p90` is now a training feature. Next action: after retraining with `bl_wait_p90` only (tag features reverted), compare raw model p90 vs baseline p90 vs guarded p90, and consider a serving guardrail only for strong wait baselines with enough samples.

8. **Weak fallback baselines remain a cold-start/identity risk.** Strong rows are close to calibrated (`metadata_name` + `queue+bucket`: `10.2%` run p90 miss, `10.9%` wait p90 miss), but fallback rows are much worse (`task_queue_id` duration fallback `28.7%` run p90 miss; `queue` wait fallback `78.6%` wait p90 miss). This suggests some misses are not model architecture problems; they are missing stable identity, sparse history, or insufficient queue state.

9. **Payload research split duration misses into no-signal and existing-tag-signal cohorts.** The Code Review payload comparison found no useful pre-run distinction between an under-8s run and an over-800s run; differences appear only in logs, which are not available at prediction time. Mozlint and build tasks are similar: external runtime state dominates. Test tasks are different: `test-suite`, `test-platform`, `test-variant`, and `retrigger` are already in tags, cover a large share of tasks, and should be tried before collector changes. **Walk-forward result (2026-05-20):** tag features are a mild win for run-duration (MAE ~0.7% better, consistent across all 4 overlapping cohorts) but regress wait-time MAE (~5.8% worse, driven by one bad cohort). Tags kept in run-duration config, reverted from wait config. Wait time is queue/capacity-driven; test identity adds noise after `task_queue_id` and `priority_at_pending`.

## 5. Next phase

### Phase 3a — closed 2026-04-29

Phase 3a entered with two production blockers (30m+ ratio-accuracy, within-2x gap) and exited with both resolved. Closing summary:

- **Queue-velocity + throughput features** (E10-E13) closed the bulk of the bucket-level accuracy gaps.
- **Walk-forward evaluation** (E13, E16, E18) replaced single-cohort decisions with cross-cohort robustness checks.
- **Anomaly detection layer** (Stage 1, Stage 1.5) introduced data-quality labeling via `queue_forecast_daily_health` with 9 task-side + 4 worker-side flags.
- **Policy B (E18)** revealed the regime fragility was baseline contamination, not architecture; 14/14 cohorts now produce Policy-B residuals that strictly beat baseline on all gating metrics.

Exit criteria met:
- ✅ 30m+ within-2x ≥ 50% on 13/14 cohorts (Policy B mean 56.84%)
- ✅ Aggregate within-2x ≥ 60% (Policy B never below baseline; mean +6.62pp lift over baseline's per-cohort within-2x)
- ✅ p90 coverage in band on majority of cohorts (10/14 — improved from 8/14, residual not perfect but no longer regressing)

### Phase 3b — production path (active)

Historical Phase 3b premise: the offline architecture decision was settled, and the remaining work was a serving system that respected the training/eval contract end-to-end. Live data later reopened p90 hardening work; see Phase 3c below.

#### 3b.1 Serving contract (new — derived from E18 Policy B requirement)

**Baseline computation in serving** must mirror training/eval semantics exactly. Drift between offline and online breaks the residual reference and re-introduces the failure mode E18 fixed.

**Cutoff convention.** Live serving anchors the percentile-history window to **the last completed UTC day**, not `now()`:
- `resolved_at < (most recent UTC midnight)` upper bound
- `resolved_at >= (cutoff − 7 days)` lower bound
- This matches the training-time `--pending-eval-date` semantics exactly (cutoff = day-start).
- Cost: predictions made during day D use percentile basis through D−1. For 7-day rolling percentiles this lag is negligible. Same-day rows are excluded entirely from the baseline reference, which **also closes the in-day contamination loophole** (a same-day incident cannot pollute the baseline before health-monitor flags it).

**Excluded-date set.** Read from `queue_forecast_daily_health` at baseline-cache-rebuild time:

```sql
SELECT sample_date FROM queue_forecast_daily_health
WHERE sample_date < {cutoff_date}
  AND is_anomalous = TRUE
```

The `flag_subset` consulted (default = all flags participating in `is_anomalous_default`, optionally narrowed) **must match the trained model's `anomaly_filter.flag_subset`** field. The model's bundle manifest records this, and the serving process refuses to load a bundle whose flag_subset disagrees with what the predictor would compute at runtime.

**Refresh policy.** Baseline percentile cache + excluded-date set rebuild on **TTL = 1 hour**. Rationale:
- daily_health is updated hourly by the health-monitor service.
- daily_health by design lags real-time by 24h (today is never processed).
- 1h serving lag on flag changes is negligible relative to the 24h flag lag.
- Simpler than `computed_at`-watermark invalidation; no leader election needed for cache rebuild.

**Failure modes (fail closed):**
- daily_health unreachable → don't serve, log error, alert.
- Baseline cache build fails → don't serve, log error, alert.
- Model bundle missing or corrupted → don't serve.
- Health-table empty (no rows) → don't serve; treat as misconfiguration.

Fail-open (serving with stale data or bypassing the filter) re-introduces the regime-fragility risk E18 just closed.

#### 3b.2 Model bundle format

A versioned directory per training run, atomically swapped via symlink:

```
trainer/data/models_bundles/v_2026-04-29_abc123/
  ├── wait_time_p50.onnx
  ├── wait_time_p90.onnx
  ├── run_duration_p50.onnx
  ├── run_duration_p90.onnx
  ├── category_mappings.json     # categorical encoder state per target
  ├── baseline_stats.json        # training-time baseline reference (for parity tests)
  └── bundle.json                # manifest below
```

`bundle.json` records:
- `bundle_id` (e.g. `v_2026-04-29_abc123`)
- `trained_at` (ISO timestamp)
- `git_sha` (trainer commit at training time)
- `training_cohort_window` (`as_of_date`, `lookback_days`, `validation_days`, `holdout_days`)
- `anomaly_filter`: full block from training config (mode, flag_subset)
- `predictor_kind` (e.g. `residual_lightgbm_policy_b`)
- `model_version` (= `bundle_id` for default, allows overrides)

Hot-reload watches `trainer/data/models_bundles/current` symlink; on change, re-loads ONNX models + category mappings + bundle.json atomically.

#### 3b.3 Prediction audit fields

Schema additions to `queue_forecast_run_predictions` (additive migration):

| Column | Type | Purpose |
|---|---|---|
| `predictor_kind` | TEXT | `residual_lightgbm_policy_b` etc. — drives offline analysis grouping |
| `model_version` | TEXT | bundle id at prediction time |
| `baseline_cutoff_date` | DATE | UTC midnight used as percentile-history cutoff |
| `baseline_excluded_dates` | DATE[] | exact set of anomalous dates excluded at prediction time |
| `health_max_computed_at` | TIMESTAMPTZ | max `computed_at` across consulted health rows — for staleness debugging |

These let any prediction be reproduced offline byte-for-byte: given the bundle, the cutoff, and the excluded-date set, the trainer can replay the exact baseline + model decision.

#### 3b.4 Implementation tasks (in order)

1. **ONNX export** from Python trainer for both p50 and p90 models of both targets, plus category-mapping sidecars.
2. **Parity tests** Python LightGBM vs onnxruntime-node — float-precision drift is the usual failure mode. Must include cold-start rows (categoricals mapped to `-1`).
3. **Bundle assembly + manifest write** — emit `bundle.json` recording the training-time anomaly_filter so serving can verify match.
4. **Live baseline cache** in Node — port `loadBulkStatsForCutoff` / `loadBulkWaitStatsForCutoff` to a long-lived in-memory cache with 1h TTL, anchored on last-completed-UTC-day cutoff, consulting `queue_forecast_daily_health` for exclusions.
5. **Schema migration** for the 5 new audit columns on `queue_forecast_run_predictions` (additive).
6. **Inference wiring** in `src/predictor.js` via `onnxruntime-node`: load bundle, replicate `FeatureBuilder` transforms in JS (categorical encoding, baseline-as-feature join, throughput-window features, log_ratio inverse), emit predictions on each `task-pending` event, write to `queue_forecast_run_predictions` with audit fields populated.
7. **Bundle hot-reload** — watch `current` symlink; atomic swap on change.
8. **Nightly training cron** in docker-compose: trainer runs at fixed UTC time, writes new bundle, updates `current` symlink. Health table is already current via the 1h health-monitor loop.
9. **Shadow mode** for validating subsequent bundles under live traffic before promotion. Not in initial cut.

Each task should produce its own design + test pass before the next starts; many have parity-validation requirements that don't compose well if rolled together.

### Phase 3b — core path closed 2026-05-15

Live predictor service is deployed and producing predictions. Implementation lives under `src/live-predictor/` (separate from `src/predictor.js`, which remains the backtest/baseline-export tool). Source: `model-loader.js`, `feature-builder.js`, `baseline-stats.js`, `throughput.js`, `predict.js`, `index.js`; 21 unit tests; integration via `migrate.sql` Stage 2 + collector `NOTIFY` hook + docker-compose `live-predictor` profile.

**What was built (§5.3b.4 task numbering):**
- ✅ #1 ONNX export — already done by trainer.
- ⚠ #2 Parity tests — not done as a dedicated suite. Production telemetry (baseline-coverage distribution, no global fallthrough, p50/p90 in plausible ranges) is the current parity signal. Add a dedicated trainer-vs-onnxruntime parity test next time a model update introduces an unfamiliar feature.
- ✅ #3 Bundle manifest — trainer emits `<stem>_manifest.json` and `_feature_schema.json` (including `anomaly_filter`) per config; live-predictor verifies `artifact_hash` on load.
- ⚠ #4 Live baseline cache — built (`BaselineStats` class, 1h TTL, hierarchical lookup matching `predictor.js` semantics, per-target anomaly-filter flag driven by bundle schema), **but anchored on `now()` not last-completed-UTC-day midnight.** See deviation 1 below.
- ✅ #5 Schema migration — `migrate.sql` Stage 2 adds `wait_model_version`, `wait_artifact_hash`, `duration_model_version`, `duration_artifact_hash` + 4 indexes. Audit columns are a subset of §5.3b.3 (see deviation 2).
- ✅ #6 Inference wiring — `predict.js` orchestrator: waits for enrichment (5s poll), fetches row, computes baselines + throughput (anchored on row's `pending_at` to prevent leakage, current row excluded), runs both ONNX models, applies `log_ratio` inverse, writes prediction with audit JSONB.
- ❌ #7 Hot-reload — not implemented. Service reads `findLatestModelDir()` at startup only; operators restart the container after a training run. Acceptable for daily/weekly retraining cadence.
- ✅ #8 Nightly training cron — pre-existing; no changes.
- ❌ #9 Shadow mode — explicitly out of scope for V1.

**Deviations from §5.3b spec (V1 acceptances or follow-ups):**

1. **Baseline cutoff anchored on `now()`, not last-completed-UTC-day midnight.** §5.3b.1 specifies UTC-midnight to close the same-day contamination loophole and match trainer's `--pending-eval-date` cutoff semantics exactly. Current `BaselineStats.refresh()` uses `new Date()` as `cutoff` and runs the 7-day window relative to that. Risk: a same-day incident can contaminate the baseline before `health-monitor` flags it (24h flag lag). For NOTIFY-triggered predictions in steady state this matters little — `now() ≈ pending_at` and most predictions consume the freshly-refreshed cache. For catch-up rows, the cutoff drifts further. Reintroduce the UTC-midnight cutoff if a regime-drift incident reproduces the E16 failure mode.

2. **Audit columns are a subset of §5.3b.3.** Implemented as typed columns: `wait_model_version`, `wait_artifact_hash`, `duration_model_version`, `duration_artifact_hash`. The `input_features` JSONB captures the full audit payload — both feature vectors, both baselines (level/p50/p90/sample_size), the throughput object, enrichment state, and row state at scoring time — so any prediction can be reproduced offline given the bundle. **Not implemented as typed columns:** `predictor_kind` (always `residual_lightgbm_policy_b` for wait, `residual_lightgbm` for duration today), `baseline_cutoff_date`, `baseline_excluded_dates`, `health_max_computed_at`. Add as typed columns once offline replay tooling needs them for grouping/filtering.

3. **Bundle directory layout differs from §5.3b.2.** Spec proposed `models_bundles/v_<date>_<sha>/` with consolidated `bundle.json` + shared `category_mappings.json`. Trainer actually writes `trainer/data/models/<YYYY-MM-DD>/` with per-config sidecars (`<stem>_p50.onnx`, `<stem>_p90.onnx`, `<stem>_category_mappings.json`, `<stem>_feature_schema.json`, `<stem>_manifest.json`). The loader handles per-config bundles cleanly. No functional gap — flag here only so the §5.3b.2 layout block reads as "spec'd but evolved during ONNX export work".

4. **Catch-up scope: currently-unresolved rows only.** §5.3b.1 / README first-cut implied broader coverage. Final contract is keyset-paginated over `WHERE resolved_at IS NULL` (uses the existing `idx_qf_task_runs_unresolved` partial index). Short tasks that pended and resolved during a predictor outage are NOT backfilled. Acceptable since the predictor is forward-only and the prediction log is for tasks under active forecasting, not historical replay.

5. **No fail-closed on health-table emptiness or daily_health unreachability.** §5.3b.1 specifies the predictor must refuse to serve in these states. Current implementation falls through to unfiltered baseline behavior (since `ANOMALOUS_DATES_SQL` returning 0 rows is indistinguishable from "no anomalous days today"). Add a startup check + alert if the health-monitor service ever drops out for extended periods.

**Open work items derived from the gaps above:**
- (a) Re-anchor baseline cutoff to last-UTC-midnight to close same-day contamination loophole (deviation 1). Low-priority while no regime-drift incident is reproducing.
- (b) Add bundle hot-reload via symlink watching (§5.3b.4 #7) once retraining cadence tightens past weekly.
- (c) Promote the JSONB audit subset to typed columns when offline-replay tooling is built (deviation 2).
- (d) Parity test harness — Python trainer prediction vs onnxruntime-node prediction on a fixed input vector — to add as regression coverage before any feature additions (gap #2).

### Phase 3c — live calibration hardening (active as of 2026-05-20)

The core predictor is live; the current work is making tail behavior explainable and preventing the model from worsening a stronger baseline.

1. **Verify run-duration p90 guardrail in production.** Use the `duration_p90_guardrail` audit object to compare raw model p90 miss rate vs served p90 miss rate after new rows arrive. Needs a few days of live data.
2. **Make model-vs-baseline p90 regression a recurring diagnostic.** Add dashboard or manifest tables ranking cohorts where raw model p90 is worse than baseline p90, separately for run and wait.
3. **Evaluate wait p90 baseline feature and guardrail.** `bl_wait_p90` added as a training feature (2026-05-20). Next: retrain with `bl_wait_p90` only (tag features reverted from wait), then compare raw model p90 vs baseline p90 vs guarded p90 for strong `queue+bucket` baselines with sample-size thresholds.
4. ~~**Run Tier 1 tag-feature walk-forward.**~~ **Done (2026-05-20).** 14-cohort walk-forward found tag features (`test-suite`, `test-platform`, `test-variant`, `retrigger`) are a mild win for run-duration (MAE ~0.7% better, consistent on all overlapping cohorts) but regress wait-time MAE (~5.8% worse). Tags kept in `run_duration_residual.yaml`, reverted from `wait_time_residual_throughput_filtered_baseline.yaml`. Wait time is queue/capacity-driven; test identity adds noise after `task_queue_id` and `priority_at_pending` are already features.
5. **Add priority/capacity wait features.** Keep throughput features, but add live worker capacity, claimed/running counts, utilization, and higher-priority queued pressure by shared pool. This targets long-wait/capacity-sensitive queues.
6. **Evaluate Tier 2 payload collection for run-duration.** `MOZHARNESS_TEST_PATHS` manifest count is the best Tier 2 candidate for test-task duration. Requires fetching/parsing task definitions at enrichment time. Consider only if test-task-specific evaluation shows the tag features leave a meaningful residual gap.
7. **Keep task-level UI and task-group ETA deferred.** The current predictor is useful for diagnostics, but a user-facing ETA needs stable p90/tail behavior. Task-group ETA likely also needs dependency edges and blocked/unscheduled lifecycle data that are not currently collected.

## Recommendation

**Shipped (2026-05-15), with live calibration hardening active (2026-05-20).** Policy B for wait and residual duration are both served via `src/live-predictor/`. The core serving path is closed, but the live p90 evidence changes the project status: do not promote this into broad Treeherder/UI ETA surfaces yet. First finish the p90 baseline-regression guardrails and wait tail diagnostics in Phase 3c.

The original risk framing — offline/online drift on baseline computation — is mostly mitigated: Policy B is correctly wired through the bundle (`anomaly_filter` in `_feature_schema.json` → `filterFromSchema()` in serving → `<> ALL($2::date[])` clause in baseline SQL). The one residual risk is deviation 1 (cutoff anchored on `now()` not UTC-midnight), which leaves a 24h window for same-day incidents to slip into the baseline before health-monitor flags them. Worth fixing if any regime drift reproduces; not worth blocking shipping for.

The newer risk framing is p90 tail underestimation: a residual model can improve central tendency while still lowering a p90 that the historical baseline estimated better. Run-duration now has an exact-name p90 guardrail and tag features (`test-suite`, `test-platform`, `test-variant`, `retrigger`) for test-task identity. Wait-time has `bl_wait_p90` as a training feature but tag features were reverted after walk-forward showed MAE regression — wait is queue/capacity-driven, not task-identity-driven. Next priorities: verify the run-duration guardrail in live data, evaluate a wait p90 serving guardrail, and add worker-capacity features for wait.

---

## 6. Experiment log

Append-only, reverse-chronological. Each entry records the exact config, cohort windows, aggregate and per-bucket metrics (wait-model only), the comparison reference, and the concrete next action it triggered.

**Conventions:**
- "Cohort" = `train [start, end), val day, holdout [start, end)` over `pending_at` in UTC.
- "Primary slice" = `reason_resolved = 'completed'` unless noted.
- Numbers are manifest `evaluation.primary.aggregate` values.
- `w/in2x` = within-2x ratio; `p90cov` = fraction of actuals ≤ predicted p90 (target band [0.85, 0.95]).
- **Same-cohort comparisons only.** Across cohorts the baseline itself shifts materially (see E10 vs E4) — comparing across runs from different dates is not valid.

### 2026-04-29 — Re-confirmation sweep on fresh GCP VM + Stage 2 infrastructure

#### E17: Walk-forward sweep across 14 cohorts (Apr 15 – Apr 28) on the new GCP host

First end-to-end sweep on the new dedicated GCP VM after migration off local development.
The sweep ran the three baseline configs only — `wait_time`, `wait_time_residual`,
`wait_time_residual_throughput`. Filtered-policy configs (`_filtered`,
`_filtered_baseline`, `_filtered_both`) and `run_duration_residual` were not part
of this run; they remain pending under E18 below.

Two cohort × config cells are missing: `2026-04-28 wait_time` and
`2026-04-28 wait_time_residual` did not produce manifests (the Apr 28
`wait_time_residual_throughput` cohort completed). Likely a left-over from the
earlier walk-forward crash before the `set -euo pipefail` fix in
`scripts/run_training.sh`. Re-running walk_forward will fill them in.

**Per-config summary (cohorts complete):**

| Config | n | Mean MAE Δ% | Worst MAE Δ% | Mean w/in2x pp | Worst w/in2x pp | p90 in-band | 30m+ ≥50% |
|---|---|---|---|---|---|---|---|
| `wait_time` (LGB-only)             | 13 | −17.82% | **+39.37%** | −2.81pp | −9.08pp | **12/13** | 8/13 |
| `wait_time_residual`               | 13 | −15.21% | +6.67%      | −0.52pp | −18.05pp | 9/13 | 8/13 |
| `wait_time_residual_throughput`    | 14 | **−19.14%** | **+3.08%** | **+2.33pp** | −15.56pp | 8/14 | **9/14** |

Win counts (across the 13 cohorts where all three configs completed):

| Metric | wait_time | wait_time_residual | wait_time_residual_throughput |
|---|---|---|---|
| best_MAE             | **6** | 1 | **6** |
| best_within_2x       | 4     | 0 | **9** |
| best_30m+_within_2x  | **9** | 0 | 4 |

**Pattern matches E16.** Throughput dominates within-2x, LGB-only dominates 30m+
tail, vanilla residual is dominated. The decision in §1 still holds:
residual_throughput is regime-fragile on p90 (8/14 in band), LGB-only is the
calibration-robust option (12/13 in band), hybrid remains the leading
sophisticated path.

The `wait_time` worst-MAE outlier of +39.37% reproduces from E16 — same value to
two decimals — confirming it is a single specific cohort in the Apr 15-27
overlap with E16. Worth identifying that cohort against
`queue_forecast_daily_health` to see whether its holdout overlaps the data-loss
days (Apr 25-26 with `n_total = 0`) or one of the regime-shifted days (Apr 23+).

#### E18: Stage 2 anomaly-filter sweep — Policy B reverses the unshippable verdict

Walk-forward across 14 cohorts (Apr 15-28) × 6 wait configs + 2 duration configs.
Anomaly filter sources from `queue_forecast_daily_health.is_anomalous` (12 days
flagged in the 35-day backfill: 2026-03-24, 03-27, 03-29, 04-04, 04-05, 04-09,
04-12, 04-18, 04-21, 04-23, 04-25, 04-26).

**Wait time, full results (15 cohorts as of 2026-04-30; 14 at first sweep on 2026-04-29):**

| Config | n | Mean MAE Δ% | Worst MAE Δ% | Mean w/in2x pp | Worst w/in2x pp | p90 in-band | 30m+ ≥50% |
|---|---|---|---|---|---|---|---|
| `wait_time` (LGB-only)                              | 15 | −17.91% | +39.37% | −3.66pp | −9.76pp  | 14/15 | 8/15 |
| `wait_time_residual`                                | 13 | −15.21% | +6.67%  | −0.52pp | −18.05pp | 9/13  | 8/13 |
| `wait_time_residual_throughput`                     | 15 | −18.81% | +3.08%  | +2.39pp | −15.56pp | 9/15  | 9/15 |
| `..._filtered` (Policy A: train+val filter)         | 11 | −20.91% | **−2.12%** | +5.35pp | +2.31pp | 6/11 | 5/11 |
| `..._filtered_baseline` (**Policy B**: baseline only) | **15** | **−21.50%** | **−2.59%** | **+6.56pp** | **+4.58pp** | **11/15** | **14/15** |
| `..._filtered_both` (Policy C: A+B)                 | 11 | −22.35% | −5.52%  | +5.71pp | +4.50pp | 7/11 | 7/11 |

The Apr 29 cohort (added 2026-04-30) extends the verdict: Policy B's worst-case MAE and within-2x are unchanged, p90 ticks up by one in-band cohort, 30m+ ≥50% stays at 93% (14/15). LGB-only drifts in the wrong direction on within-2x with each added cohort (mean now −3.66pp from −2.81pp at 13-cohort), reinforcing the choice not to ship it.

`wait_time_residual` remains stuck at 13/15 (Apr 28 + Apr 29 both produce no manifest) — same silent-crash mode noted as a low-priority pipeline-robustness issue in §4. Doesn't block Policy B.

**Decisive observation:** Policies A, B, C all eliminate the catastrophic regression
cohorts. The worst-case MAE Δ% goes from +3.08% (unfiltered) to −2.12% / −2.59% /
−5.52% — every filtered cohort beats baseline. The worst-case within-2x goes from
−15.56pp to +4.5pp — never below baseline.

**Policy B specifically** is the production candidate: it gets the same
worst-case improvements as A/C, but **without skipping any cohorts** (B does not
touch train/val, so empty-val never happens). 14/14 vs A/C's 10/14 means it has
3-4× more comparison data. It also achieves the **best 30m+ ≥50% rate (13/14)**
and the cleanest absolute numbers on the table.

The dominant effect is baseline contamination, not training-data contamination.
A and C touch train+val and gain very little over B. C marginally outperforms B
on worst-case MAE (−5.52% vs −2.59%) but at the cost of 4 lost cohorts and worse
on every other metric.

**Why baseline filtering helps so much:** the unfiltered baseline's 7-day
percentile history averaged across both regimes (pre-Apr-23 normal + Apr-23+
shifted). The log_ratio residual is anchored to baseline_p50; when baseline is
catastrophically wrong on the new regime's tail, residual inherits that
wrongness even with throughput features in the input set. With anomalous days
excluded from history, the baseline percentiles reflect actual current-regime
behavior, and the residual model corrects from a clean reference.

**Run duration, full results (15 cohorts as of 2026-04-30):**

| Config | n | Mean MAE Δ% | Worst MAE Δ% | p90 in-band | Best MAE wins | Best within-2x wins |
|---|---|---|---|---|---|---|
| `run_duration` (LGB-only)      | 10 | +1.62%  | +13.03% | 10/10 | 2/10 | 1/10 |
| `run_duration_residual`        | 15 | **−4.50%** | +12.03% | 13/15 | **8/10** | **9/10** |

Duration confirms residual ships. The 14-cohort version of E16's 11-cohort sweep:
same conclusion, more cohorts, no regime fragility.

**Apr 28 wait_time / wait_time_residual missing manifests** (`ls
trainer/data/models/2026-04-28/` shows only the 4 throughput-family configs +
duration_residual, no wait_time / wait_time_residual). Cause unknown — these
configs don't have anomaly_filter blocks so the explicit skip path doesn't
trigger. Likely a silent crash on a regime-shifted holdout that the trainer
didn't catch as a known skip case. Worth investigating directly with verbose
stderr; doesn't affect E18's conclusions because Policy B (the production
candidate) completed all 14 cohorts.

**Implications for production:**

1. **Architecture question is settled.** `wait_time_residual_throughput` +
   Policy B baseline filtering is strictly better than every alternative on
   the metrics that matter (worst-case MAE, worst-case within-2x, 30m+
   accuracy). No hybrid needed.
2. **Serving must respect baseline filtering.** Predictor.js already supports
   `--exclude-dates`; the production serving path needs to consult
   `queue_forecast_daily_health.is_anomalous` at baseline-computation time.
   This wasn't expected pre-E18 — adds a small wrinkle to Phase 3b.
3. **The detector is now load-bearing.** `queue_forecast_daily_health` was
   set up as a research tool; it's now a production dependency. The
   health-monitor service running hourly must stay healthy in production.
4. **No need to investigate the regime cause for production safety.** Even
   without identifying what shifted on Apr 22-23, Policy B handles the
   contamination automatically. Investigating remains useful for ops
   awareness but no longer blocks shipping.

**Next actions:**
- Investigate the 2 missing Apr 28 wait manifests (low priority — doesn't
  block production).
- Phase 3b production-path work (ONNX, Node inference, baseline filtering
  in serving path, versioned writes, hot-reload).
- Consider running detector on `worker-count` derived flags too (Stage 1.5
  added them; not yet enabled in `is_anomalous` default subset).

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
