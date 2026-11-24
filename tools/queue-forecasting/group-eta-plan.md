# Task-Group ETA — Fast-Path Plan

Date: 2026-11-16 (updated 2026-11-20). Status: P0-P2 live; see "Status and next steps". Owner: ykurmyza.

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

### P1 — Edge-free baselines — DONE 2026-11-17
- `trainer/scripts/group_eta_baselines.py` (`extract` per-day cache, `eval` walk-forward).
  Host run: `/tmp/qf-trainer-venv/bin/python -m scripts.group_eta_baselines ...` from trainer/,
  `DATABASE_URL` from ../.env with `@postgres:5432` -> `@127.0.0.1:5433`. Cache:
  `~/qf-group-eta/tasks/<day>.parquet` (09-20..11-15). Report: `~/qf-group-eta/report.md`.
- Definitions: t0 = earliest task_created; t_pred = decision task (task_id == task_group_id,
  97% of groups) resolved + 120s; label = completion of tasks created <= t_pred, minus t0.
  Groups need >= 5 known tasks, all resolved. Train 14d, calibrate on the last 4 present days.
- Estimators: B0 history (family × size), B1c = max over tasks of the per-(family,name)
  offset quantile (comonotone), B1i = independent product (over-covers, dropped), `*` =
  per-family multiplicative calibration.
- Offline result, try, n=31k, 10-08..11-12: **B1c\* w2x 0.731, cov90 0.893 (passes)**;
  B1c 0.783/0.944; B0 0.615/0.897; B1i 0.52/0.996. Weak spot: try groups with 200+ tasks
  under-covered after calibration (cov 0.78; 0.68 for 1000+). Late Oct dip (w2x 0.54-0.65,
  10-21..28), probably pre-outage degradation.

### P2 — Internal shadow estimates — LIVE since 2026-11-19
- `trainer/scripts/group_eta_live.py` (`fit`, `predict`) via `scripts/group_eta_live.sh`,
  compose service `group-eta` (trainer image, 6g). Cron (user's crontab): fit 04:15Z daily,
  predict every 10 min; log `~/qf-reports/group-eta.log`. Model + cache:
  `trainer/data/group_eta/` (gitignored). Live estimators: B0, B1c, B1c\*.
- Table `queue_forecast_group_predictions_shadow` (PK group+estimator, retention on predicted_at).
- Viewer: `scripts/group_eta_show.sh [hours=6] [family=try] [estimator=B1c*]`.
- Scoring: weekly report §6 (groups older than 1 day). The first full scorecard will be in the 11-24 report.
- Early live read 11-20 (try n=1285, groups >= 3h old): B1c w2x 0.795 / cov 0.927 (passes);
  B1c\* 0.781 / 0.945 (slightly wide). Try spans rose 11-18/19 (median 29 min vs 11-13);
  confirmed in offline data too, so this is a real shift.
- Fixed 11-20: decision tasks that expire at their 24h deadline stretched the scan window and
  caused a timeout. Groups whose decision task took >= 6h are now skipped.

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

## Update 2026-11-24

- **Week-1 live scorecard** (weekly report 11-24 §6, try n=3354, groups >= 1 day old): B1c w2x 0.754 / cov 0.918 /
  pinball 40.5 min; B1c\* 0.719 / 0.894 / 43.9; B0 0.359 / 0.886. Both B1c variants pass the try bars.
- **Size-aware calibration (B1c\*\*) tried and not deployed.** `fit_calibration_sized` in group_eta_baselines.py:
  per (family x size bucket) factors where the bucket has >= 30 calibration groups, family fallback otherwise.
  Offline eval 10-08..11-20 (cache extended to 11-23; report `~/qf-group-eta/report.md`, previous one kept as
  `report-2026-11-17.md`), try n=42.9k:

  | estimator | w2x | cov90 | pinball90 min | cov 200-1000 | cov 1000+ |
  |---|---|---|---|---|---|
  | B1c | **0.774** | 0.937 | **40.0** | 0.893 | 0.921 |
  | B1c\* | 0.731 | 0.889 | 47.1 | 0.773 | 0.695 |
  | B1c\*\* | 0.735 | 0.895 | 44.8 | 0.868 | 0.757 |

  B1c\*\* fixes most of B1c\*'s large-group under-coverage, but uncalibrated B1c beats both on w2x and pinball
  in every week 10-05..11-22. Calibration refits B1 on the window minus the 4 cal days, and that loss outweighs
  the correction. B1c is slightly over the 0.93 cov ceiling offline (0.937) but inside it live (0.918) and in the
  latest offline week (0.906). B1c\*\* stays offline-only (computed by `predict_all` when the fit has `cal_cs`;
  the live default `--estimators` is unchanged).
- **Decision proposal: share B1c for try** (p50-p90 range).
- Weak spot is unchanged and is not a calibration problem: try 200-1000 tasks w2x 0.60, 1000+ w2x 0.45.
  The levers are in-flight re-estimates (B2) and critical path (P3).

## Status and next steps (handoff 2026-11-20)

Nothing long-running is in flight; everything is committed (HEAD 97a694eb7f).

Next, in order:
1. **Size-aware calibration**: fit the `*` factors per (family × size bucket) with a
   family fallback when n is small. Re-run `eval` and compare against B1c/B1c\* by size bucket. If it wins,
   add it as a new live estimator (e.g. `B1c**`) next to B1c\*. Don't replace B1c\*
   until a week of live data agrees.
2. **11-24 weekly report**: read §6 (first full-day live scorecard), §5 (edge coverage
   ~1.0, tasks table growth). Decide which estimator is the one to share: B1c or B1c\*.
3. **Sharing with other teams** (the user is starting this): show the p50–p90 range for try only.
   Since 11-20 the dashboard has a section for this (`groups.html`), plus `GET /api/v0/group/<id>`; see below.
4. **In-flight re-estimates (B2)**: update the remaining time for running groups as tasks
   resolve. Not built.
5. **P3 critical-path replay** from ~12-07, once there are 3 weeks of `dependencies`.
6. **P4**: put the hazard model into walk-forward + shadow (still offline only).

Dashboard + API (2026-11-20):
- https://queue-forecasting.taskcluster.net/groups.html has the live group-ETA scorecard (default: groups ≥ 6h old;
  the weekly §6 report, which uses ≥ 1 day, stays the official one), try per-day/per-size tables, and recent groups.
  `shadow.html` shows qctx_d vs served against the v3 bars (14d, 6h cache). `status.html` is now a generated
  "live now" page; the old phase-2 doc moved to `archive.html`.
- `src/api.js` (compose `api`, proxied by Caddy at `/api/*`): `GET /api/v0/task/<taskId>`, `/api/v0/group/<taskGroupId>`,
  `/api/v0/health`. Read-only, statement_timeout 5s, 10 req/s per IP. Group lookups use
  `idx_qf_tasks_task_group_id` (migrate-task-group-index.sql, 129 MB, built 11-20).
- The walk-forward table had been reading a stale 2026-08 CSV. dashboard-gen now mounts `trainer/walk_forward_summary.csv`.

Playground (2026-11-20): `playground.html` + `GET /api/v0/playground/{task,group}/<id>`.
- A stored prediction is returned if there is one (`source: stored`). Otherwise the api fetches what is missing
  from Taskcluster (anonymous client) and asks the internal `playground-scorer` service
  (`src/playground-scorer.js`; no port, not proxied; never writes) to compute one. The result is cached for
  5 min in memory and **never stored**, so the served/shadow eval tables stay pure.
- Task modes:
  - `at-pending`: the run is in the DB and its pending time is inside 59 days. Scored at the real pending time.
    Baseline stats are current, so the result approximates the live value: 11-20 check, 30 runs, ratio vs stored
    within ±3%, median 1.000.
  - `hypothetical-now`: old, unscheduled or unseen tasks. Scored as if pending now, using the current
    `taskQueueCounts`.
- Group: the same B0/B1c/B1c* estimators, ported to Node (`src/scorer/group-eta.js`). The model comes from
  `live_model.json`, written by `group_eta_live.py fit` (or `export-json`), and holds only the quantile tables
  (6 MB, vs a 200 MB pkl). Parity on 11-20: 1191/1191 estimates exact against the shadow rows
  (`scripts/check_group_eta_parity.js`). Large groups are slow, because `listTaskGroup` pages are fetched one
  after another: 4k tasks took ~18 s. Pages are ~5 MB (1000 full task definitions) and took 0.8-3.8 s each on
  11-23, so listing uses its own client (20 s timeout, 1 retry). Each page is reduced to slim rows as it arrives:
  two concurrent 7.6k-task groups peaked at 205 MB RSS in the api (limit 512 MB). Computed groups use a separate
  32-entry cache (~1.5 MB each). Past the 10-page / 10k-task cap the group is listed but not estimated
  (`mode: truncated`), since its decision task and original graph are unknown.
- Limits:
  - Per-IP compute budget of 6/min (burst 3), plus 60/min globally and 2 concurrent. Stored answers and cache
    hits are free. Admission checks concurrency first (free), then the per-IP bucket, then the global
    bucket (refunding the per-IP token if the global one refuses), so refused requests cost the client nothing.
  - Ids must match Taskcluster's v4 slugid regex.
  - Caddy adds a CSP on the playground (script-src 'self', no inline script), and site-wide nosniff,
    no-referrer and DENY framing.
  - The api no longer gets `.env`, so it holds no TC or Pulse credentials.
- If the scorer's first stats refresh fails, it retries in-process (backoff 1 min doubling to 30 min) with
  `/score` returning 503, rather than exiting and re-running the heavy queries on every container restart.
- Hypothetical task rows are not in the DB, so `getQueueContext(..., {targetInDb:false})` adds the target to
  `backlog_coverage_ratio`'s numerator. The live path is unchanged (default `targetInDb: true`).
- The scorer loads its models at startup, and `daily_walk_forward.sh` restarts it with live-predictor. Its
  BaselineStats refresh takes ~5 min and runs every 6h.

Watch items:
- The tasks table was 13 GB on 11-17. Inline deps add about 65 MB/day before TOAST. Disk was 57 GB free on 11-17.
- A collector restart causes a few hours of ResourceNotFound backfill retries (benign).
- The live job doesn't take the heavy-training lock. It's capped at 6g, and the fit uses ~1-2 GB.

## Decisions taken
- P0 schema + collector restart: approved 11-16.
- Internal bars for try: cov90 in [0.85, 0.93], w2x >= 0.70 (accepted).
- First audience for P2: the user, who then shares with other teams to get feedback.
