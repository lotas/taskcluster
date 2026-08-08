# Bet 1 — Queue-Context Features (Design Spec)

Status: approved design, ready for implementation planning. Date: 2026-06-25.

## Context

After Phase 4 (priority-aware wait baseline) verified healthy in production, the
remaining wait-time failure is the **long tail**: completed-only `30m+` wait p90
miss is ~38%, and weak-fallback rows are worse. The wait-p90 guardrail already
fires on ~99.5% of strong (`queue+priority+bucket`) rows, so served p90 ≈ the
cell's empirical p90 — meaning the residual tail miss is **not** model undercut.
It is an **information** problem: at `pending_at` the model sees only the current
task's priority and the aggregate `queue_pending` depth, so it cannot separate a
long-waiter from a short-waiter. A `try` task with 50 low-priority tasks ahead
and one with 50 beta/autoland tasks ahead look identical today, yet wait wildly
differently.

This is Bet 1 of the tail-accuracy program (see `next-steps.md` §4.2–4.3). The
program's north star is a `mach try` group ETA = max over tasks of (wait + run);
Bet 1 makes individual-task wait-tail predictions accurate enough to later
compose. Bets 2 (distributional/survival modeling + live re-prediction) and 3
(group-max composition) are deferred.

## Objective & success metric

Prove that giving the wait model **what is ahead of the task** measurably moves
the **conditional** long-wait tail — not just band width — without regressing
aggregate calibration or adding dead-weight features.

- **Primary gate:** global completed-only `30m+` wait p90 miss drops materially
  from ~38% toward the §4.5 thresholds (`<35%` experimental, `<30%` broad),
  stable across walk-forward cohorts.
- **Must-not-regress guardrails:** overall wait p90 stays ~10–12% bad; wait p50,
  within-2x, and MAE do not regress on the primary aggregate; weak-fallback rows
  do not get worse.
- **Per-feature accounting:** ablation attributes the gain so dead weight is
  dropped (YAGNI).
- **Scope:** features feed the **wait config only**. Run duration is identity /
  multimodality-driven and stays on its current path.

This is a **`priority_at_pending` model, not a full priority-queue simulation**
(see Limitations). The proof must be read as "queue-ahead context at pending
time helps," not "we simulate the scheduler."

## Feature set

Priority ranks (canonical, numeric, higher = higher dispatch priority):
`highest=7, very-high=6, high=5, medium=4, low=3, very-low=2, lowest=1`,
legacy `normal→1` (alias for lowest), `null→unknown` (not ranked; handled
separately). Ranks are stored/used everywhere so "higher/same" is unambiguous.
Distinct `priority_at_pending` values will be verified against the DB before
implementation (`normal` did not appear in the 6.1M-row aggregation, so it is
rare/legacy; low risk).

All counts are over runs on the **same `task_queue_id`**, evaluated at the
target's `pending_at` (= `T`), point-in-time and leakage-safe (only state with
timestamp ≤ `T`), target row excluded.

### Tier A — priority backlog (the core bet)
- `pending_higher_priority_same_queue`
- `pending_same_priority_same_queue`
- `pending_lower_priority_same_queue` — **ablation-only**; counted but must earn
  inclusion with stable cross-cohort lift or be dropped (it does not block a
  higher task under priority dispatch; at best a saturation/arrival-regime proxy).
- `oldest_higher_or_equal_pending_age_same_queue` (seconds the front of the
  blocking queue has been stuck at `T`).

### Tier B — capacity normalization
- raw `running_workers`, `existing_capacity`, `claimed_tasks` (latest sample
  at/before `T`).
- `pending_higher_or_equal_per_capacity`, `pending_total_per_capacity`,
  `running_per_capacity` (utilization).
- `capacity_sample_age_s` — **dual role**: a numeric feature (trees can learn
  stale-sample behavior) **and** a freshness guard that drives low-confidence /
  audit behavior.
- **Capacity-null semantics (not just "no sample"):** static pools write a
  sample with `running_workers`/`existing_capacity` = NULL but a real
  `claimed_tasks` (worker-counter.js). So a sample existing is not the same as
  capacity being known. Emit `capacity_null_reason ∈ {ok, no_sample,
  static_pool_null, zero_capacity}` and a `capacity_denominator_source` (which
  field backed the denominator). `*_per_capacity` features are **NULL** (never
  impute 0 — that makes the ratio explode) whenever `existing_capacity` is
  NULL/0; the model consumes the reason flag instead. `capacity_unknown` is set
  for `no_sample` (no sample within ~15 min) *and* `static_pool_null`. Static
  pools are also identifiable via `queue_forecast_worker_pools.pool_kind`.

### Tier C — flow / drain
- `arrivals_15m_same_queue`, `arrivals_60m_same_queue` (total inflow, context).
- `arrivals_higher_or_equal_15m_same_queue`,
  `arrivals_higher_or_equal_60m_same_queue` — priority-aware inflow that will
  block the target, symmetric with the priority-aware starts below (resolves the
  arrivals/starts asymmetry from review).
- `starts_higher_or_equal_15m_same_queue` (drain rate of work that actually
  blocks this task).
- *Overlaps the existing `throughput` feature; the ablation resolves whether it
  is redundant. Drop if so.*

### Tier D — repo-family (Florian's shared-pool scenario)
- `repo_family` of this task (categorical: `try / autoland / central / beta /
  release / other / unknown`).
- Blocking-first composition: `pending_try_higher_or_equal_same_queue`,
  `pending_autoland_higher_or_equal_same_queue`,
  `pending_release_beta_higher_or_equal_same_queue` (the causal "ahead of me by
  family"). Total-family composition kept as lower-weight context.

### Coverage / hygiene
- `backlog_coverage_ratio` = `reconstructed_pending_total_including_target` /
  `queue_pending`. **Denominator alignment matters:** `queue_pending` is the
  queue's own snapshot captured on `task-pending` and **includes the target**,
  while the Tier-A/D features exclude it. So the coverage numerator uses an
  explicit `reconstructed_pending_total_including_target` (the same point-in-time
  pending set, target *included*) to compare like-with-like. (Equivalently,
  subtract the target from `queue_pending`; the include-target numerator is
  clearer.) **Available to the model**, but watched as hygiene: it must not
  become a magic correction unless it proves stable. Partial-collection zeros
  must not masquerade as real queue shape — a `backlog_coverage` flag marks
  materially-undercounted rows for low-confidence handling.

## Data sources, reconstruction & leakage discipline

### Point-in-time "ahead of me" at `T` (set definitions)
A row `s` is **pending at `T`** iff `s.pending_at <= T AND (exit IS NULL OR
exit > T)` where `exit = COALESCE(s.started_at, s.resolved_at)`. A run leaves the
pending state when it starts, OR — if it never starts — when it is resolved
(canceled / claim-expired / deadline-exceeded before claim). Using
`started_at`-only would count a resolved-without-start run as pending *forever*;
that was a real bug found by the Task-11 integration smoke (inflated
`backlog_coverage_ratio` to ~100×) and corrected here. `starts_higher_or_equal`
still keys on `started_at` (a start event) and arrivals still key on
`pending_at`; only the pending-at-`T` membership uses `exit`.

**Open data-quality caveat (both-NULL runs):** a reference run with neither
`started_at` nor `resolved_at` (`exit` NULL) is treated as still-pending. In live
serving that is correct (they are genuinely pending now). In *historical*
training reconstruction a both-NULL run is usually a **collection gap** (we
missed its start/resolve events), not a truly-eternal pending task, so it can
still over-count backlog. Watch `backlog_coverage_ratio` on production data: if
it stays ≫1 after this fix, both-NULL collection gaps need handling (e.g. drop
both-NULL runs whose `pending_at` is far before the window edge). Deferred —
needs a data-quality decision, not auto-applied.

- **higher-priority ahead** = `{ s ≠ target : rank(s) > rank(target),
  s.pending_at <= T, s pending at T }` — higher priority jumps ahead regardless
  of arrival time, so same-instant higher-priority rows count.
- **same-priority ahead** = `{ s ≠ target : rank(s) == rank(target),
  s pending at T, s ordered before target by FIFO }`, where FIFO order is by
  `(pending_at, task_id, run_id)`. For `s.pending_at < T`: included if pending.
  For `s.pending_at == T` (same instant): included only if its tie-order is
  before the target's.
- **lower-priority ahead** (ablation-only) = `{ s ≠ target : rank(s) <
  rank(target), s.pending_at <= T, s pending at T }`.

**Tie rule (explicit approximation):** Taskcluster gives no queue-insertion
sequence number in our data, so same-priority tasks sharing an identical
`pending_at` are ordered by `(pending_at, task_id, run_id)` as an approximation.
Same-timestamp cascades would otherwise undercount "same-priority ahead." Rows
resolved by the tie-break (i.e. a same-`pending_at` cohort larger than 1) are
flagged so we can audit how often the approximation is load-bearing.

**Collation invariant (serving==training):** the `task_id` ordering in the tie-break
MUST agree between trainer and live. Python compares `str(task_id)` (Unicode
code-point); Postgres's default `en_US.UTF8` collation case-folds and disagrees,
so the live `BACKLOG_SQL` tie-break uses `task_id COLLATE "C"` (byte order ==
code-point for ASCII task_ids). A cross-language real-data parity check
(420/420 over 21 features × 20 rows incl a same-instant cohort) confirms the
match — found and fixed an off-by-one skew here; do not revert to a default-
collation comparison.

**Train/serve completeness watermark (review P2):** the historical sweep sees
every same-`T` sibling at once, but the live predictor polls unresolved rows as
pulse ingests them (~5s cadence), so at the instant a target is scored its
same-instant peers may not be in the DB yet — live would understate
same/higher-priority backlog relative to training. The live path therefore
applies a **prediction watermark**: a row is scored for queue-context only once
`now − pending_at ≥ W` (`QUEUE_CONTEXT_PREDICT_DELAY_S`, a few×ingest-lag), so
the same-`T` cohort has landed. Training has full as-of-`T` visibility for free;
the watermark exists solely to make the live path converge to that same
visibility. Residual late arrivals are caught by `backlog_coverage` rather than
silently trusted. (Fallback if watermarking proves insufficient: drop same-`T`
peers symmetrically in *both* paths — preserves parity at the cost of the
cascade signal.)

### Capacity
Latest `queue_forecast_worker_counts` sample with `sampled_at <= T` (never the
nearest-after — that leaks the future). Emit `capacity_sample_age_s`,
`capacity_null_reason`, and `capacity_denominator_source` per the Tier B
null-semantics: a sample existing does not imply capacity is known (static pools
carry `existing_capacity = NULL` with a real `claimed_tasks`). `*_per_capacity`
is NULL (not imputed) when `existing_capacity` is NULL/0; `capacity_unknown`
covers both `no_sample` and `static_pool_null`.

### Flow
`arrivals_*` = count of runs on Q with `pending_at ∈ (T−w, T]`. `starts_*` =
count with `started_at ∈ (T−w, T]` (and rank ≥ target for the higher-or-equal
variant). Leakage-safe (only ≤ `T`).

### Priority as-of pending_at
Both target and backlog rows use their stored `priority_at_pending`. Mid-pending
`task-priority-changed` events are **not** reconstructed in v1 (see Limitations).

### Repo-family derivation
Re-enrich the training window via the TC queue API (`task(taskId)` → `routes` +
`metadata.source`). Derivation precedence:
1. `metadata.source` hg path (`/try`, `/integration/autoland`, `mozilla-central`,
   `releases/mozilla-beta`, `releases/mozilla-release`),
2. `routes` (`*.v2.<project>.*`),
3. `scheduler_id` coarse fallback,
4. `unknown`.

Persist on `queue_forecast_tasks`: `repo_family`, `repo_family_source`
(`source|route|scheduler|unknown`), `repo_family_derivation_version`, and a
**short matched token/path** as evidence (NOT full route arrays — this table is
not a second task-definition store). **No payloads, no scopes.** Bound the
re-enrich window to non-expired task definitions.

## Pipeline — one builder, serving == training

A single queue-context feature builder owns the feature definitions and a
**versioned schema**: `queue_context_feature_version`, written into both the
training NDJSON and the live audit object. Later feature-order tweaks then stay
comparable across walk-forward runs instead of silently invalidating old ones.

The builder is called from:
- **Trainer (historical reconstruction):** the `predictor.js` NDJSON path.
- **Live-predictor (current state at `pending_at`):** computes the same features
  from current DB state.

Identical feature definitions are a hard rule — a serving/training mismatch
corrupts the log-ratio anchor (the Phase 4 lesson). Features are added to the
**wait config** `feature_order` only; the wait model is retrained on enriched
NDJSON; run-duration is untouched. Forward collection adds route/source capture
+ repo-family derivation at enrichment time so new tasks need no API re-fetch.
The live path writes a `queue_context_at_pending` audit object (with the schema
version) on the prediction for diagnostics.

## Backfill strategy & performance

Per-row correlated counting over millions of runs is O(n²). Use a per-queue
**event sweep**, O(n log n):
- Merge `(pending_at, +1)` / `(started_at, −1)` events in time order, with a
  deterministic intra-timestamp sub-order `(task_id, run_id)`.
- Maintain running per-priority-rank active counts. At each target's `T`: add
  all entries with `pending_at < T`, remove rows with `started_at <= T`, then
  evaluate **same-priority ahead** separately via the intra-`T` ordering, and
  **higher-priority** as all higher-rank rows with `pending_at <= T` still
  pending, excluding the target.
- **Oldest-pending age needs more than counts (review P2):** per-rank active
  *timestamp* structures (a min-`pending_at` heap or sorted deque per rank), with
  removal on `started_at <= T`, are required to read
  `oldest_higher_or_equal_pending_age_same_queue`. Counts alone cannot give it.
  The implementation must maintain these structures alongside the counts.
- The same sweep yields arrivals/starts windows and oldest-pending age.
- **Fixture tests are mandatory** for: start removals (a peer that started before
  `T` must not count), same-`T` tie ordering (cohort > 1 ordered deterministically),
  rank-threshold boundaries (higher vs same vs lower at adjacent ranks), and the
  oldest-age heap removal path. These are the spots most likely to silently
  poison the ablation.

Runs as a batch step in the trainer over the walk-forward window; results land
in the training NDJSON (no new training-time storage). The live path computes
the same quantities from current unresolved rows + latest worker sample.

## Evaluation

- **Ablation order:** `current → +capacity → +priority-ahead → +flow →
  +repo-family-blocking → all`. Tier A (priority-ahead) is the core bet;
  Tier D is a scenario-specific refinement.
- **Harness:** existing `walk_forward.sh`; score the primary aggregate + tail
  slices per cohort.
- **Primary gate:** global completed-only `30m+` wait p90 miss materially down
  (`<35%` → `<30%`), no regression on overall p90 (~10–12%), p50/within-2x, or
  MAE, stable across cohorts.
- **Tier D acceptance slice (secondary, not a primary gate):** high-volume
  mixed-tenant **shared pools** (e.g. `gecko-t/*` carrying try + autoland/beta).
  If Tier D does not lift that slice cross-cohort, it does not ship even though
  it is in the build. The core proof does not depend on Tier D — promoting it to
  primary would make Bet 1 hostage to the riskiest enrichment path.
- **Hygiene tracking:** feature freshness / unknown rates (`capacity_sample_age`,
  `repo_family` unknown %, `backlog_coverage`). A half-populated feature needs a
  low-confidence path, not silent zeros.
- **Instrumentation:** add the `30m+` tail p90 miss-rate as a first-class column
  in the walk-forward summary and the dashboard, so tail regressions stop being
  ad-hoc SQL.

## Limitations & carried risks

- **Priority-change reconstruction deferred (v1 limitation).** This is a
  `priority_at_pending` model, not a full priority-queue simulation. Mid-pending
  priority changes are not modeled; the §4.2 "stop treating priority-changed as
  no-ops" extension would need a priority-change event log we do not fully have.
- **Coverage undercount.** Our reconstructed counts come from our own collected
  rows; mitigated by `queue_pending` normalization + `backlog_coverage` flag, not
  by silently trusting partial counts.
- **API re-enrichment cost / task-definition expiry.** Bounded to a non-expired
  window.
- **Flow vs existing throughput redundancy.** Resolved by ablation.
- **Tie-rule approximation** for same-`pending_at` cohorts; flagged and audited.

## Out of scope (deferred to later bets)

- Distributional / survival modeling + live re-prediction (Bet 2).
- Group-max completion composition + `guaranteed_completion_time` (Bet 3).
- Tree-status (TreeHerder) features (next feature wave).
- Dependency / blocked-work lifecycle (§4.4).
