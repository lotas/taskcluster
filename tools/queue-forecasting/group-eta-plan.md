# Task-Group ETA — Fast-Path Plan

Date: 2026-11-16. Status: proposed. Owner: ykurmyza.

Goal: produce *internal* group-completion estimates (p50/p90 ranges) as soon as
possible, measure them honestly, and get to a user-visible experiment
only once they clear bars. Builds on `next-steps.md` §4.4/§6 and
`bet2-hazard-survival-design.md`; keeps group work outside the v3 task-level
contract (05aa4eda) — it gets its own contract later (auto-research Phase 7).

## What we already have (verified 2026-11-16)

- Every task row: `task_group_id`, `task_created` (≈ defined time), `repo_family`,
  `normalized_name`, `max_run_time_s`; runs carry `pending_at`, `started_at`,
  `resolved_at`, `reason_resolved`.
- So **pre-pending blocked time = `pending_at - task_created` is already derivable**,
  and group span = `max(resolved_at) - min(task_created)` is computable for the
  last ~60 days (retention).
- Group profile, 2026-11-12 00–12Z, groups with ≥20 tasks:

  | family | groups | median tasks | median span | p90 span |
  |---|---|---|---|---|
  | try | 176 | 52 | 44 min | 193 min |
  | autoland | 97 | 62 | 234 min | 418 min |
  | release_beta | 17 | 452 | 306 min | 328 min |

- Per-task wait p50/p90 (served + qctx_d shadow) and run p50/p90 exist live.
- Hazard model (Bet 2) exists in trainer, offline only — not walk-forwarded, not shadowed.

## What's missing

1. **Dependency edges.** The collector fetches the full task definition during
   enrichment (`src/collector.js` `backgroundApiFetch`) but drops `dependencies`,
   `requires`, `deadline`. 60-day retention means every day not collected is lost.
2. Group-level labels + eval harness (none).
3. Composition logic (per-task distributions → group-max distribution) — unspecced.
4. A tail model good enough that the max isn't dominated by misses
   (live 30m+ wait p90 coverage ≈ 0.54 on 11-02..11-10).

## Plan

### P0 — Collect edges — DONE (live 2026-11-16 14:38Z)
- `queue_forecast_tasks.dependencies TEXT[]` + `requires`, `deadline`, `dependency_count`,
  written by the enrichment UPDATE from `taskDef` (no extra API calls). NULL count =
  not collected; 0 = no deps. Full coverage from 2026-11-17.
- First shipped as an edge table; moved inline 2026-11-17 09:31Z after measuring
  ~173 B/edge incl. PK (~26 GB at 60d). Inline is ~65 MB/day before TOAST.
- Weekly report §5 tracks coverage. No backfill: history starts 2026-11-16.

### P1 — Group dataset + edge-free baselines (days 1–4, existing data)
Script `trainer/scripts/group_eta_dataset.py` (run as a normal foreground
job, not a CC background shell — see OOM note) → per-group parquet:
group id, family, n_tasks, created_at, task-set signature (sorted normalized names hash),
actual completion, span, per-task created/pending/started/resolved offsets.

Estimators, all predicted "at group creation" (t0 + 10 min, once task definitions settle):
- **B0 history**: span quantiles by `repo_family × n_tasks bucket`.
- **B1 offset-max**: for each task, historical quantiles of
  `resolved_at - group_created` by `normalized_name × repo_family`;
  group estimate = quantile of max over tasks (Monte Carlo from per-task marginals).
  Offsets implicitly include upstream dependency depth, so no edges needed.
- **B2 live-remaining** (in-flight updates, internal only): for an in-flight group,
  remaining = max over unresolved tasks of (B1 offset remaining, or served
  wait+run p50/p90 once pending).

Eval: walk-forward by day over Oct 6–28 + Nov 3 onward (skip degraded Oct 29–Nov 2).
Metrics per family (try first): MAE on span, within-2x, p90 coverage, pinball p90.
Proposed internal bars for try: p90 coverage in [0.85, 0.93], within-2x ≥ 0.70.

### P2 — Internal shadow estimates (days 4–7)
- Small job (in live-predictor or a cron) writes B0/B1 group estimates at t0+10min to
  `queue_forecast_group_predictions_shadow`; scored nightly against actual completion.
- Section in weekly report. Dashboard-only, labelled experimental, shown as a range.
  This is the "start giving estimates" milestone — internal audience only.

### P3 — Critical-path replay (after ≥2–3 weeks of edges, ~Dec 7+)
Per `next-steps.md` §6: (a) critical path with actual durations → graph sufficiency;
(b) predicted run p50/p90; (c) + predicted wait; (d) queue-pressure scenarios.
Compare to B1; adopt only if it beats B1 on try p90 coverage/within-2x.

### P4 — Tail model in the loop (parallel, from week 2)
- Add hazard config to walk-forward + live shadow (needs predictor support).
- Composition: qctx_d for centre, hazard for tail; feed into the P1/P3 group-max.

### P5 — User-facing experiment (gated)
Only after two consecutive weeks of P2 shadow meeting bars on try, and a separate
group contract. Range + confidence, never a single "finished at" timestamp
(`next-steps.md` §5).

## Open decisions (user)
- Approve P0 schema + collector restart.
- Internal bars for P2 (numbers above are proposals).
- Who is the first internal audience for P2 estimates.
