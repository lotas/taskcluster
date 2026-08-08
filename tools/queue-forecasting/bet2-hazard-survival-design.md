# Bet 2 — Discrete-Time Hazard Model + Live Re-Prediction (Design Spec)

Status: approved design, ready for implementation planning. Date: 2026-07-22.

## Context

Bet 1 (queue-context features) is concluded. Over the full 15-cohort walk-forward
(2026-06-13→2026-06-27), the best feature combination found (`wait_qctx_d`,
priority+flow, capacity dropped) landed at a mean guarded 30m+ wait p90 miss of
**34.49%**, clearing the 15-cohort `<35%` gate on 9/15 cohorts (60%) — a real,
measurable improvement over the production baseline's 35.88%/5-15, but not a
reliable clear of the experimental gate, and nowhere near the `<30%` broad
threshold. Every reasonable recombination of capacity/priority/flow features has
now been tried; the result plateaus. This confirms the program's original
"ceiling insight": pending-time information has a real but bounded ceiling, and
we've reached it for this feature family.

This is Bet 2 of the tail-accuracy program (see `next-steps.md` §4.2–4.6 and
[[project_tail_accuracy_program]]). Bet 2 attacks the problem from a different
angle than Bet 1: instead of giving the model more information *at* `pending_at`,
it (a) fixes a real, previously-undiagnosed bug where currently-pending tasks are
silently excluded from training entirely (survivorship bias), and (b) unlocks a
new source of information that literally does not exist at `pending_at` — how
long a task has *already* waited without resolving.

**A north-star framing check happened during brainstorming and materially changed
scope**: the program's stated goal is a `mach try` group ETA given *once*, at
push creation time (`max over tasks of (wait + run)`). A live-updating number
that can say "2h" and then later say "actually 9h" would repeat the exact
failure mode `next-steps.md` §5 already names as why the predecessor systems
(Treeherder's `running_eta`, the removed `mach try` estimate) were pulled:
"implied more certainty than the data had." Live re-prediction is therefore
scoped here as an **internal/diagnostic capability that must not silently
contradict the creation-time estimate**, not a replacement headline number —
see Scope below.

## Objective & success metric

Prove that (1) properly censoring-aware training produces a materially better
*and* fully-calibrated wait-time distribution than the current point-quantile
models, and (2) that distribution supports genuine, correctly-conditioned
re-prediction for tasks already in progress of waiting — without requiring any
change to what's shown at pending time.

- **Primary gate:** the model's own tail calibration (see Evaluation) improves
  materially over the current guarded 30m+ wait p90 miss baseline (34.49%,
  the best Bet 1 result), across the same walk-forward cohort structure.
- **Secondary question, not a gate:** does a properly tail-calibrated hazard
  model still need the existing wait-p90 guardrail? Measure, don't assume.
- **Re-prediction validity:** for tasks that cross a bin boundary, the
  *conditional* survival distribution computed at that boundary must be as
  well-calibrated as the *marginal* distribution computed at `pending_at` — not
  just plausible-looking.
- **Scope:** `wait_time` only, consistent with Bet 1. `run_duration` is
  identity/multimodality-driven and stays on its current quantile-regression
  path — this work does not touch it.
- **Surface:** dashboard/diagnostic only, matching Bet 1's posture (see
  `next-steps.md` §5, "Defer ETA-Style UI Integration"). No Treeherder/`mach
  try` integration, no user-facing presentation-semantics work in this spec.

## Model architecture

A discrete-time hazard model: a sequence of independent binary LightGBM
classifiers, one per time bin, replacing the current two-quantile (p50/p90)
LightGBM setup for `wait_time`.

**Bin boundaries** (proposed, tunable during implementation): `0-5m, 5-15m,
15-30m, 30-60m, 60-120m, 120-240m, 240-480m, 480m+` (open-ended terminal bin).
Finer near the front where most mass sits; real, distinct bins reach into the
tail rather than collapsing everything past 30m into one bucket (the current
`evaluate.py` `WAIT_BUCKETS` shape, which is fine for reporting but too coarse
to model against). One more finite division than originally proposed
(`240-480m` added ahead of the terminal bin) — see the terminal-bin tail
policy below for why an open-ended bin alone can't be it.

**Per-bin classifier `i`** answers: *"does this task **start** within bin `i`,
given it survived (unstarted) to bin `i`'s start?"* The event is *start*, not
*resolve*: `wait_time` is `started_at - pending_at` (`collector.js`'s
`computeDurations`), so a task that resolves without ever starting (canceled,
deadline-exceeded while pending, worker-shutdown with no claim, etc.) never
produces a wait event — see the competing-risk fate below.

**Training-set construction (fixes a real censoring bug in the current
pipeline):**
- **Fate is determined using only events at or before the row's split
  cutoff** (see cutoff rule below) — `started_at`/`resolved_at` values that
  fall *after* cutoff are ignored for fate purposes, even though the row's
  true (future-relative-to-cutoff) outcome is already sitting in the
  database. A train-window row that actually started 40 days later, past
  `train_end` but still before the global `as_of_date` `data_loader.py`
  bounds the query by, must not be labeled with its real elapsed wait —
  that reintroduces the exact future-leakage the split-cutoff exists to
  prevent, just at fate-assignment instead of the SQL filter. Concretely,
  every row has exactly one of three fates, as of the moment it exits
  observation *or* the split cutoff, whichever is earlier:
  1. **Started at or before cutoff**, at elapsed `w = wait_duration_s`: a
     genuine wait event. At risk for every bin up to and including the one
     containing `w`; label 1 for that bin, 0 for every earlier bin it
     survived.
  2. **Resolved without starting, at or before cutoff**
     (`resolved_at IS NOT NULL AND started_at IS NULL`, `resolved_at <=
     cutoff`) at elapsed `r = resolved_at - pending_at`: a **competing-risk
     exit**, not a wait event. At risk for every bin up to the one containing
     `r`, labeled 0 throughout (it never starts) — then removed from the risk
     set. It must not be treated as "survived forever" past `r`.
  3. **Still pending as of cutoff** — either genuinely still pending, or its
     real `started_at`/`resolved_at` lies after cutoff (in which case it is
     treated identically to genuinely-still-pending: cutoff hasn't seen the
     event yet) — elapsed `e = cutoff - pending_at`: right-censored. At risk
     for every bin fully below `e`; the bin containing `e` is excluded
     (label not yet knowable), and no label is assigned past it.
- A row is "at risk" for bin `i` (`[t_i, t_{i+1})`) if its exit-elapsed time
  (whichever fate applies) is `>= t_i`.
- This requires a real change to the wait hazard configs' filters, not just
  the one the earlier draft named: today's wait configs (e.g.
  `wait_qctx_d_priority_flow.yaml:9-12`) filter on both `r.started_at IS NOT
  NULL` **and** `r.wait_duration_s IS NOT NULL` — either filter alone already
  excludes every currently-pending row, and the first also excludes every
  resolved-without-starting row. Both must be dropped and replaced with the
  event/censor-aware inclusion rule above: pull every row in the split's
  `pending_at` window regardless of `started_at`/`resolved_at`, and let the
  per-row fate (1/2/3 above) — not a SQL presence/absence filter — determine
  risk-set membership and labels.
- **Cutoff is per-split, not global.** `train.py`'s `_split_by_pending_at`
  already slices rows into train/val/holdout by `pending_at`, but
  `data_loader.py`'s query only bounds `pending_at < as_of_date` — one global
  cutoff shared by every row regardless of split. If risk-set/censoring
  computation (fate 3 above) also used that single global `as_of_date`, a
  training-window row would be censored using information available at the
  *holdout's* boundary, not its own — letting train/val rows "know" more
  about their long-tail outcomes than a model retrained at
  `train_end`/`val_end` would ever see. The cutoff for computing fate
  2/3 status must be assigned per row, after splitting:
  - `train_end` for rows with `pending_at` in `[train_start, train_end)`
  - `val_end` for rows with `pending_at` in `[val_start, val_end)`
  - `hold_end` (`== as_of_date`) for rows with `pending_at` in `[hold_start,
    hold_end)`
  This is new row-level logic in `train.py`, computed right after
  `_split_by_pending_at` assigns each row to a split — not a change to the
  single-cutoff SQL query in `data_loader.py`.

**Survival curve:** `S(t) = ∏ⱼ (1 - hazardⱼ(x))` over all bins up to `t`. Any
quantile (p50, p90, p95, ...) is read off `S(t)` by interpolation — full
multi-quantile coverage from one set of models, not a separately-trained head
per quantile.

**Terminal-bin tail policy.** An unbounded final bin has no distribution to
interpolate within — any quantile landing past the last finite boundary
(e.g. p95/p99 on slow queues) is undefined, not just imprecise, from the
step function alone. Adopt an explicit parametric tail for the (still
necessarily open) terminal bin: assume a constant hazard rate within it,
fit from the bin's own observed/censored exits, which implies an
exponential tail beyond the last finite boundary `t_last`:
`S(t) = S(t_last) * exp(-λ(t - t_last))` for `t > t_last`. This gives any
quantile beyond `t_last` a defined, auditable value instead of a silent
extrapolation with no stated assumption — and lets a p99 miss be
attributed to "discrete-bin model" vs. "tail-policy assumption" in the
evaluation writeup.

## Feature set and re-prediction mechanism

**Features:** identical categorical/numeric feature framework as today,
including Bet 1's queue-context features — every bin's classifier trains on the
same feature set, computed once at `pending_at`.

**Deliberately deferred to a later iteration: no live feature refresh.** A
re-prediction at the 20-minute mark still uses the original pending-time
feature snapshot (queue depth, priority backlog, etc. as they were when the
task pended), not a freshly recomputed one. Refreshing queue-context features
live would mean re-running that computation at arbitrary query times instead of
once at `pending_at` — real added complexity, deferred until the core hazard
model is proven.

**No explicit "elapsed wait so far" feature is needed.** This was necessary for
the (rejected) quantile-regression-plus-feature approach; the hazard model
captures it structurally. Re-prediction means starting the survival-curve
product from whichever bin the task currently occupies, using the *same*
trained models — not a different model, not an extra input.

**Serving:**
- At `pending_at`: compute `S(t)` from bin 0 onward using the pending-time
  snapshot; read off p50/p90 as today. This is what's shown to users (via the
  dashboard) — nothing changes about the creation-time number's semantics.
- **Re-prediction is snapped to bin boundaries, not computed at arbitrary
  elapsed time.** `S(t)` is a step function defined only at bin boundaries
  `t_0, t_1, ..., t_n` — the per-bin hazard model has no notion of survival
  probability *inside* a bin, so `S(e)` for `e` strictly inside `[t_i,
  t_{i+1})` is undefined without an added within-bin interpolation
  assumption. To avoid inventing one: the periodic checker (below) fires when
  a still-pending task's elapsed time crosses bin boundary `t_i`, and
  re-prediction uses `e = t_i` — the boundary just crossed — not the
  checker's actual wall-clock elapsed time at the moment it happens to run.
  `S(t | survived to t_i) = S(t) / S(t_i)` for `t >= t_{i+1}` is then read
  straight off the existing step function; no interpolation needed. (A finer
  within-bin estimate would require an explicit within-bin hazard-shape
  assumption and is deferred — boundary-snapping is sufficient for the
  dashboard-only scope.)
- Operationally: something needs to periodically check tasks still pending
  past each bin boundary and compute/store an updated distribution for them,
  tagged clearly as a re-prediction, in its own table — never written into
  or conflict-resolved against the original creation-time row (see storage
  below). Whether the checker itself lives as a `live-predictor` extension, a
  new small service, or is computed on-demand at dashboard-generation time is
  an implementation-time decision, not fixed here — the dashboard-only scope
  means there's no latency requirement forcing a particular architecture.
- **Storage: a new table, not a variant write into
  `queue_forecast_run_predictions`.** That table is
  `PRIMARY KEY (task_id, run_id)` and `predict.js`'s insert path uses
  `ON CONFLICT (task_id, run_id) DO NOTHING` — a second write for the same
  run (the re-prediction) would either silently no-op against the original
  row or collide with it if the key were loosened instead. Add
  `queue_forecast_run_repredictions`, keyed
  `PRIMARY KEY (task_id, run_id, bin_index)` (`bin_index` = which boundary
  triggered this re-prediction), storing the same `wait_p50_s`/`wait_p90_s`
  -shaped columns plus `elapsed_s` and `predicted_at`, with `(task_id,
  run_id)` referencing `queue_forecast_task_runs`. `wait_p50_s`/`wait_p90_s`
  here carry the **same semantics as the original row: total elapsed wait
  since `pending_at`**, not remaining wait from the re-prediction point —
  quantiles are read off the conditional survival curve `S(t |
  survived to t_i)`, whose `t` axis is still elapsed-since-`pending_at`, the
  same axis the original bin boundaries and the creation-time prediction
  use. This keeps the two tables directly comparable ("originally predicted
  X, now updated to Y") without a units mismatch. `elapsed_s` (`= t_i`) is
  stored alongside so remaining wait can be derived (`wait_p50_s -
  elapsed_s`) by any consumer that wants it, rather than baking a second,
  differently-scoped pair of columns into the table. The dashboard reads the
  original from `queue_forecast_run_predictions` and, if present, the latest
  row from `queue_forecast_run_repredictions` for that run — the two are
  never merged into one row.

## Evaluation methodology

New calibration checks, since this model produces a full distribution rather
than two point quantiles:

- **Per-bin hazard calibration:** does the predicted probability of starting
  in each bin match the observed frequency?
- **Multi-quantile coverage:** check p50/p75/p90/p95 coverage simultaneously
  from the same survival curve — this is what lets deep tail quantiles come
  from composing bins rather than extrapolating a single quantile regressor
  (the design doc's original "never train a p99.9 head" concern).
- **Re-prediction calibration:** for tasks crossing a bin boundary, is the
  *conditional* distribution computed at that boundary as well-calibrated as
  the *marginal* one from `pending_at`? This is what actually tests whether
  re-prediction adds value versus just looking plausible.
- **Guardrail necessity (open question, not decided here):** measure whether
  this model's own p90 needs the existing wait-p90 guardrail at all, now that
  training handles censoring properly. Report the finding; don't assume it
  either way.
- **Apples-to-apples with Bet 1's numbers:** read the model's p90 off `S(t)`
  for the completed-only primary slice, apply the exact same `evaluate.py`
  bucket/guarded methodology used for Bet 1 (30m+ bucket, guarded by the same
  baseline-floor mechanism unless the guardrail-necessity check above says
  otherwise), and report the resulting 30m+ wait p90 miss rate on the same
  walk-forward cohort structure. This is the concrete, comparable number behind
  the primary gate above (34.49% is the bar to beat).
- Reuse the existing walk-forward harness shape (train/val/holdout per
  `as_of_date` cohort, same anomaly filtering) with these new metrics layered
  on top of — not replacing comparability with — the existing pinball-loss/
  coverage metrics where meaningful.

## Explicitly out of scope

- User-facing UI or presentation semantics (ranges vs. points, when a
  refreshed estimate is shown to end users). Dashboard/diagnostic only.
- Live feature refresh at re-prediction time.
- `run_duration` — unaffected, stays on its current quantile-regression path.
- Bet 3 (group-max composition) — this work enables it (full per-task
  distributions instead of point estimates) but does not implement it.
- Retiring the existing wait-p90 guardrail — a question this work should
  answer with evidence, not a decision made in advance.
