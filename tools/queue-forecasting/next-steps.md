# Queue Forecasting Next Steps

## Goal

Understand why live predictions miss, improve the model inputs where the likely gaps are already visible, and defer ETA-style UI integration until the accuracy story is clearer.

## Latest Live Evidence

Snapshot: `aggregations.html` generated `2026-05-27 15:07:02Z`, data range `2026-05-15` to `2026-05-27`.

- Completed-only calibration is close overall: wait `53.7% <= p50`, `11.7% > p90`; run `49.2% <= p50`, `8.9% > p90`.
- All-resolved vs completed-only mostly affects run duration. `failed` rows have `26.6%` run bad, and `claim-expired` rows have `62.8%` run bad, so completed-only should be the default model-quality lens.
- Wait misses are still a long-tail problem: completed-only `<1m` waits are only `5.5%` bad, while `30m+` waits are `49.5%` bad.
- Run duration is good overall, but long actual-run buckets are still under-covered: completed-only `30-60m` runs are `28.8%` bad and `60m+` runs are `32.7%` bad.
- Weak baseline fallback is a strong failure signal. Completed-only wait fallback to `queue` is `82.8%` bad and `priority+bucket` is `53.1%` bad. Run fallback to `task_queue_id` is `32.3%` bad and `kind+test-type` is `28.8%` bad.
- Priority is meaningful but the current feature is too local. Completed-only `very-high` wait is `16.0%` bad and `high` wait is `12.7%` bad, but this only tells the model the current task's priority. It does not tell the model how much higher-priority work is queued ahead on the same worker pool.
- `project_id` was always `none` (never populated upstream); removed from dashboard. DB column kept for future use.
- Top completed-only run misses are concentrated in code-review publication and mozlint-style tasks. Top wait misses include code-review publication, backfill/retrigger, and Windows/browsertime tasks with very long actual wait tails.

Baseline research update: `baseline-research.md` shows that the model sometimes makes p90 materially worse than the historical baseline, not just on Code Review.

- Run p90 regressions are visible across several exact-name cohorts. Examples from the last-7-day query: `source-test-python-mozbuild-3.9-windows11-64/opt` has raw model p90 miss `46.3%` vs baseline p90 miss `8.9%`; `build-linux64-asan/opt` is `29.0%` vs `9.0%`; `Code review publication (production)` is `25.7%` vs `11.2%`.
- Run p50 is much less concerning. The worst p50 model-vs-baseline gaps exist, but most high-volume rows are only a few seconds worse or nearly tied, so near-term work should stay focused on p90/tail coverage.
- Wait p90 has the same regression pattern. Many `queue+bucket` baseline cohorts beat the model by large margins, for example `releng-hardware/win11-64-24h2-hw` at medium priority: model p90 miss `57.1%` vs baseline p90 miss `28.8%`.
- Strong baseline rows are not the problem. `metadata_name` + `queue+bucket` rows are close to calibrated (`10.2%` run p90 miss, `10.9%` wait p90 miss). Fallback rows remain risky: duration fallback to `task_queue_id` is `28.7%` run p90 miss, and wait fallback to `queue` is `78.6%` wait p90 miss.

Payload exploration update: `payload-feature-exploration.md` splits top-miss cohorts into two useful groups.

- Code Review, mozlint, and build tasks have little or no useful pre-run payload signal; duration is driven by external state such as push size, changed files, cache warmth, sccache hits, or runtime service behavior. The p90 guardrail is the right first fix for these cohorts.
- Test tasks already carry stronger identity features in `tags`: `test-suite` has 54 values and `test-platform` has 128 values, both covering `57.2%` of tagged tasks; `test-variant` has 44 values and covers `25.0%`; `retrigger` is binary and covers `95.9%`.
- These tag fields were already stored in the DB and supported by the trainer/serving feature builders, but were not listed in the active configs.

Tag feature walk-forward result (2026-05-20): 14-cohort sweep (May 6-19) found tag features (`test-suite`, `test-platform`, `test-variant`, `retrigger`) are a mild win for run-duration (MAE ~0.7% better) but regress wait-time MAE (~5.8% worse). Tags kept in run-duration config only. Wait time is queue/capacity-driven; test identity adds noise. `bl_wait_p90` added to the wait config as a numeric feature.

## External Feedback Cross-Check - 2026-05-27

Aryx and Florian surfaced two different predecessor estimate systems. Both failed in ways that are directly relevant to Q3 planning.

- **Treeherder job-details ETA, removed in 2017:** `running_eta` was a recent average duration keyed by repository + job signature. The UI showed "time remaining" for running jobs and "typically takes" for pending jobs. It was removed as a "mostly broken feature" before the backend `JobDuration` model and ingestion path were deleted.
- **`mach try` terminal estimates, removed in 2025:** D257817 removed a graph-duration estimate based on static BigQuery-exported mean task durations (`task_duration_history.json`), local task dependency longest-path calculation, and graph percentile files. It did not model live queue pressure, priority, capacity, tree state, or future arrivals.
- **Current model is better but still incomplete:** it uses `task_queue_id`, `queue_pending`, `priority_at_pending`, recent per-queue throughput, historical baselines, and worker-count data. It predicts at pending time and writes p50/p90 per task. It is not a static duration file or a single longest-path estimate.
- **The real missing wait-time signal is queue semantics:** the model knows the current task's priority, but not the composition of the backlog ahead of it. Florian's example (try delayed by beta/autoland/release traffic on shared pools) is not directly observable in the current feature set.
- **The real missing task-group signal is dependency lifecycle:** Aryx's example (blocked tasks that never become runnable, or later tasks that become runnable first) requires dependency edges and pre-pending lifecycle state. Current task-level wait prediction starts at `pending_at`; task-group ETA needs data before that point.
- **Product lesson:** do not expose a deterministic ETA or "finished around HH:MM" surface. Any user-facing experiment should be framed as task-level p50/p90 ranges with confidence/gating, and task-group / `mach try` ETA should remain out of scope until dependency and queue-pressure modeling are proven.

## 1. Improve Prediction Diagnostics — DONE 2026-05-19

Added explainability-oriented cuts to `aggregations.html`. Verify on the next live-dashboard refresh.

- [x] Split all metrics into completed-only vs all-resolved (each H2 section renders both flavors as H3 sub-tables; the new `bandCountsSql` emits `_all_` and `_done_` columns in one query).
- [x] Breakdowns by `reason_resolved`, `task_queue_id`, `scheduler_id`, `priority_at_pending`, `project_id`, wait/run baseline level (from `input_features.baselines.{wait,duration}.level`), and wait/run baseline sample-size bucket (`<10`, `10-99`, `100-999`, `1k-10k`, `10k+`).
- [x] Top-miss tables (`actual > p90`, completed-only), wait and run, top 25 by miss count, showing metadata_name + normalized_name + miss count + n eligible + miss rate + miss-rows' actual p50/p90 + median predicted p90.
- [x] Wait actual-duration buckets (`<1m`, `1-5m`, `5-30m`, `30m+`).
- [x] Run actual-duration buckets (`<1m`, `1-5m`, `5-30m`, `30-60m`, `60m+`).

Current outcome: misses are mostly explained by long waits, weak baseline fallback, priority/capacity-sensitive pools, non-completed run-duration noise, and p90 model predictions that can undercut a better historical baseline.

## 2. Stabilize Run-Duration p90 Against Baselines

The Code Review investigation found a general model issue: the p90 model can compress multimodal/tail-heavy tasks toward the middle and perform worse than the exact-name baseline p90.

- [x] Add `bl_duration_p90` to the run-duration training feature set.
- [x] Add a live-serving p90 guardrail for strong duration baselines: when the duration baseline level is `metadata_name`, sample size is sufficient, and the baseline p90 is finite, serve `max(raw_model_p90, baseline_p90)`.
- [x] Preserve raw model p90 and guardrail details in prediction audit data.
- [x] Add guarded p90 trainer metrics alongside raw model p90 metrics.
- Verify the impact after deployment using the guardrail query in `baseline-research.md`. The current live data has no guarded rows yet.
- Add a recurring diagnostic that ranks cohorts where raw model p90 is worse than baseline p90. This should be visible in either the dashboard or trainer manifests so regressions are not discovered by ad hoc SQL.
- Decide whether the same guardrail should apply to other strong duration baseline levels after measuring precision/coverage. Do not apply it to weak fallbacks like `task_queue_id`, `kind+test-type`, or `scheduler_id` without separate evidence.

Expected outcome: reduce avoidable run p90 underestimation without relying on logs or post-run information.

## 3. Add Existing Tag Features Before New Payload Collection — DONE 2026-05-20

Used the useful pre-run signals already in `queue_forecast_tasks.tags`. Walk-forward found they help run-duration but hurt wait-time.

- [x] Inspect top run miss cohorts for useful pre-run payload/env fields.
- [x] Classify no-signal cohorts: Code Review, mozlint, and builds mostly depend on external runtime state, so do not spend more time on raw payload fingerprinting there.
- [x] Add Tier 1 tag features to both active training configs: `tags.test-suite`, `tags.test-platform`, `tags.test-variant`, and `tags.retrigger`.
- [x] Run 14-cohort walk-forward comparison (May 6-19) with tag-feature configs.

Walk-forward result:
- **Run duration: mild improvement, kept.** Mean model MAE improved ~0.7% (117.89s → 117.07s), within-2x and p90 unchanged. Consistent across all 4 overlapping cohorts.
- **Wait time: MAE regressed ~5.8%, reverted.** Mean model MAE went from 243.09s → 257.28s on overlapping cohorts. 2026-05-14 showed a 17% MAE regression (264.66s → 310.23s). Wait time is queue/capacity/priority-driven; test identity adds noise after `task_queue_id` and `priority_at_pending` are already known.

Current config state:
- `run_duration_residual.yaml`: tag features kept (`test-suite`, `test-platform`, `test-variant`, `retrigger`)
- `wait_time_residual_throughput_filtered_baseline.yaml`: tag features reverted; `bl_wait_p90` kept as numeric feature

Remaining:
- Compare tag-feature impact on test-task cohorts specifically (the global CSV dilutes signal because Code Review/build/mozlint have no test tags).
- Keep `MOZHARNESS_TEST_PATHS` manifest count as Tier 2 for run-duration only. It requires fetching/parsing task definitions at enrichment time and should wait until test-task-specific evaluation shows the tag features leave a meaningful residual gap.
- Do not use high-cardinality raw `tags.label`, raw command hashes, `createdForUser`, or stable-per-task payload fields such as `MOZHARNESS_CONFIG` unless later evidence shows they add non-redundant signal.

## 4. Q3 Work Plan: Model Wait-Time Queue Semantics

This is the strongest near-term model improvement candidate. Phase 2 remains valid on architecture (`wait_time_residual_throughput_filtered_baseline` + Policy B), but live data and sheriff feedback show the remaining wait-time misses are not mostly task identity problems. They are scheduling-context problems.

### 4.1 Finish p90 baseline hardening first

This protects users from avoidable p90 underestimation while the richer queue features are being collected.

- Treat `30m+` wait misses and weak wait-baseline fallback rows as the first target cohort.
- [x] Add `bl_wait_p90` as a numeric training feature in the wait config. Also generalized guarded-p90 trainer evaluation from run-duration-only to any target with a `bl_*_p90` column, so wait manifests now report guarded p90 metrics too.
- Run a clean walk-forward with `bl_wait_p90` only (tag features reverted) to isolate its effect on wait p90 coverage.
- Evaluate a guarded wait p90 variant against raw model p90 and baseline p90. Start only with strong `queue+bucket` baselines and a minimum sample-size threshold; do not guard weak fallback levels blindly.
- Add model-vs-baseline p90 regression diagnostics for wait, grouped by `task_queue_id`, `priority_at_pending`, wait baseline level, and actual wait bucket.
- Review wait baseline fallback behavior before retraining. Rows falling back to `queue` or `priority+bucket` are badly under-covered, so they may need separate cold-start handling or additional identity/context features.

### 4.2 Collect backlog composition at pending time

Current `queue_pending` is an aggregate count for the task queue. It does not tell the model whether a try task has 50 low-priority try tasks ahead of it or 50 higher-priority beta/release/autoland tasks ahead of it. Florian's beta/autoland/try example lives here.

Practical collection steps:

- Define a stable priority ordering for Taskcluster priority strings (`highest`, `very-high`, `high`, `medium`, `low`, `very-low`, `lowest`) and use it consistently in collector, trainer, and dashboard code.
- Add a source/repo-family derivation for `release`, `beta`, `central/main`, `autoland`, `try`, and `other`. Since `projectId` is not populated upstream, derive this from fields we can actually collect: task routes, task metadata/source, scheduler/task-group metadata, or known ID/name patterns. Record the derivation source and an `unknown` bucket.
- Store enough task-definition data to support that derivation: at minimum `routes`, `metadata.source` if present, `scheduler_id`, `task_group_id`, and original/effective priority.
- Stop treating `task-priority-changed` and `task-group-priority-changed` as pure no-ops for queue-context accounting. Keep `priority_at_pending` immutable for historical model rows, but maintain a current effective-priority counter for unresolved queued work.
- Add a `queue_context_at_pending` JSONB audit object, or a normalized side table, containing the snapshot used for prediction: pending/running counts by priority band, by source/repo family, and by `task_queue_id`.
- Seed the live counters from our own unresolved rows on startup, then reconcile total pending with `taskQueueCounts()` so drift is visible. If the aggregate API and local breakdown disagree materially, record the feature snapshot as low confidence rather than silently trusting it.

Candidate model features:

- `pending_higher_priority_same_queue`, `pending_same_priority_same_queue`, `pending_lower_priority_same_queue`
- `pending_higher_priority_per_capacity`, `pending_total_per_capacity`, `running_per_capacity`
- `pending_release_beta_same_queue`, `pending_autoland_same_queue`, `pending_try_same_queue`
- `arrivals_15m_by_priority`, `arrivals_60m_by_priority`, `starts_15m_by_priority`, `starts_60m_by_priority`
- `throughput_share_higher_priority_15m/60m`
- `oldest_pending_age_same_queue` and `oldest_higher_priority_pending_age_same_queue`, if cheaply available from our local rows

Evaluation plan:

- Run ablations in this order: current model; +capacity only; +priority backlog only; +repo-family backlog only; +all queue-context features.
- Score the global primary slice, but gate decisions on the slices that motivated this work: completed-only `30m+` wait, weak wait fallback rows, Windows hardware/GPU pools, signing pools, code-review/backfill/retrigger, and high-volume `try` tasks.
- Compare same-cohort against the current production config and the historical baseline, not across unrelated cohorts.
- Track feature freshness and unknown-rate. A feature that only exists for half the rows may still be useful, but it needs a clear low-confidence path.

### 4.3 Add worker-capacity and tree-status features as queue-context complements

Throughput features say what just happened; capacity features say why it may continue or stop. Tree status says whether the cause is likely to persist.

- Add live worker-capacity features to the wait model: running workers, claimed tasks, existing capacity, utilization, and recent capacity deltas by `task_queue_id`.
- Include static/dynamic pool classification from `queue_forecast_worker_pools` so the model can distinguish fixed hardware pools from autoscaled pools.
- Add capacity-drop/spike indicators from `queue_forecast_daily_health` and recent worker-count samples.
- Add tree status as a categorical/temporal feature. TreeHerder exposes tree status (`open`, `closed`, `approval-required`) via API for each tree (`autoland`, `mozilla-central`, `mozilla-beta`, `mozilla-release`, `try`). Collect into a `queue_forecast_tree_status` table at 5-min cadence. Derive features: `autoland_status` (categorical), `minutes_since_autoland_closure` (numeric, 0 if open), `any_production_tree_closed` (boolean). This directly captures Aryx's "tree closes → autoland drains → try gets capacity" scenario. Backfill from TreeHerder historical data for the training window.
- Keep existing throughput features; capacity/priority features should complement them, not replace them.

### 4.4 Measure dependency and blocked-work effects

Aryx's dependency example is not a normal pending-queue problem: tasks can be defined but blocked, fail before becoming runnable, or become runnable in a different order than users expect. This matters for future queue pressure and is mandatory for task-group ETA.

Practical collection steps:

- Add a dependency edge table populated from task definitions: `task_id`, `dependency_task_id`, `task_group_id`, and collection timestamp.
- Track task lifecycle before `pending_at`: defined time, dependency-resolved/runnable time if observable, pending time, started time, resolved time, and final reason.
- Add task-group membership snapshots so we can distinguish "not pending yet" from "not collected".
- Track blocked-but-defined counts by `task_queue_id`, priority, and source/repo family. Do not feed them into the served wait model until an offline experiment proves they help; start as diagnostics.
- Build labels for dependency outcomes: dependency failed, dependency completed, task canceled/exception before start, task never became pending, task became pending after a long block.

Evaluation plan:

- First answer whether blocked-work counters predict future arrivals on shared queues. If they do not, keep this data for task-group ETA only.
- Evaluate on cases where current pending depth is low but wait later stretches, and on cases where blocked/skipped work makes the queue drain faster than current depth implies.
- Keep task-level wait and task-group ETA experiments separate. The task-level predictor starts at `pending_at`; task-group ETA starts much earlier and needs a critical-path model.

### 4.5 Q3 exit criteria

Before considering a UI surface broader than the standalone dashboard:

- Completed-only overall wait p90 remains close to calibrated (`actual > p90` around 10-12%) for at least two consecutive live weeks.
- Completed-only `30m+` wait p90 miss rate materially improves from the current ~50%; target <35% for an experimental UI gate and <30% before broad promotion.
- High-volume weak-fallback rows have either improved calibration or a low-confidence/hide rule.
- Top wait-miss cohorts have an owner explanation: baseline undercut, weak identity, higher-priority backlog, capacity shortage, dependency/blocking, or external state.
- New queue-context features improve the target slices without regressing the primary aggregate or p50 calibration.

Expected outcome: reduce wait p90 misses on capacity-sensitive/shared pools and give a concrete answer to Florian's priority-backlog question instead of only saying "priority is a feature".

## 5. Defer ETA-Style UI Integration

Do not integrate into Treeherder, Queue UI, or `mach try` as an ETA yet.

- Keep exposing data through the standalone dashboard while diagnostics and feature work are still moving.
- Runtime prediction is the safer first candidate for task-level display; wait prediction should remain experimental until the queue-semantics work above is measured.
- Never present a single finish timestamp or "time remaining" as the primary output. The predecessor systems failed partly because the UI implied more certainty than the data had.
- If we test a task-level UI, show p50/p90 or a bounded range, include a low-confidence state, and hide/gate rows with weak baseline level, sparse sample size, stale queue context, or known-bad cohorts.
- Keep Treeherder's watch/notification workflow as the recommended way to know when a push is complete.
- Revisit UI only after completed-only calibration, long-wait tail coverage, and top-miss cohorts are stable enough to explain.

Expected outcome: avoid repeating the 2017 Treeherder `running_eta` and 2025 `mach try` estimate mistakes while still leaving a path for a narrow, calibrated task-level experiment.

## 6. Q3 Feasibility Study: Task-Group / `mach try` ETA

Task-group / push ETA is desirable, but it is a different product from per-task wait/run prediction. The removed `mach try` estimate was essentially static duration + dependency longest path; a credible replacement needs dependency lifecycle plus queue pressure.

- Current setup stores `task_group_id`, but does not store dependency edges, task-group membership snapshots, or the blocked/unscheduled lifecycle needed for a critical-path ETA.
- Add the dependency and lifecycle collection described in §4.4 before attempting task-group prediction.
- Build an offline replay dataset for recent try groups: selected tasks, dependencies, tasks optimized out, tasks never pending, tasks skipped because dependencies failed, and actual group completion time.
- Establish a baseline critical-path model using actual run durations and actual wait durations. This answers "is the collected graph sufficient?" before adding prediction error.
- Replace actual durations with current predicted run p50/p90, then add wait p50/p90, and measure error at each step. This separates graph/data problems from model-quality problems.
- Add queue-pressure scenarios to the replay: higher-priority beta/release/autoland arrivals during the try group, autoland drain during tree closure, and capacity loss/recovery on shared worker pools.
- Treat `mach try` ETA as a later product surface. Any replacement should show a calibrated range and confidence, not a single "finished around" timestamp.

Expected outcome: determine whether current and planned Q3 data can support task-group ETA, and list the missing lifecycle/dependency events if it cannot.
