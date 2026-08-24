# Auto-Research Loop — Design

Status: design v3 — revised 2026-08-24 after two review rounds. **Unattended execution is
not approved by this document**; see §14 for the staged gate.
Supersedes: `experiment-runner.md` (proposal, never implemented) — except for
its trusted-dispatcher boundary, which is restored here (§3.4).

## 1. Why

Two things block progress on forecasting accuracy:

1. **Iteration latency.** Every experiment costs a human round trip: review the
   diff, commit, push, SSH to the box, pull, launch training, wait minutes to
   hours, copy results back to the agent. The human is in the loop for steps
   that carry no judgment.
2. **Attention starvation.** Vulnerability work has dominated the last two
   months. Research stalls whenever the human is pulled away, because nothing
   advances without them.

These are separable, and the plan separates them. **Phases 0–3 remove the
latency with a human trigger and no autonomy at all.** Autonomy is a later,
separately gated capability (§14). If the containment work in §3 proves too
expensive, the loop still delivers most of its value as a fast,
reproducible, human-triggered experiment system.

### North star

Identify the **feature set** that makes task wait-duration, run-duration, and
task-group ETA predictions reliable — especially in the long tail, which is
what blocks a `mach try` group ETA. Per
`project-tail-accuracy-program-2026-06-25`, the tail is an *information*
problem first: at pending time the model cannot separate a long-waiter from a
short-waiter. The primary deliverable is `research/state/features.yaml` — an
evidence-backed registry of which features help, by how much, at which
prediction moment, and whether they can be served.

## 2. Scope

**In scope.** Hypothesis generation, feature implementation, ablation
scheduling, evaluation, and evidence recording, with a curated human review
surface.

**Out of scope.**

- Automatic promotion of a model to live serving. Always a human-approved PR.
- Porting anything back into the taskcluster monorepo. A later, curated act.
- A second VM, a DB replica, or distributed training.
- Replacing the existing trainer, walk-forward, or manifest formats.
- A general-purpose agent shell on the host.
- Group-ETA modelling. It has no labels or eval harness today; it is a
  composition (max over tasks), not a trained target. It gets its own phase
  (§14, Phase 7) rather than being folded into a contract split.

## 3. Authority and containment

The governing principle: **an agent must not be able to weaken the controls
that constrain it.** Filesystem separation alone does not achieve this, because
controls are also sourced from agent-writable git history.

### 3.1 Three repositories, not one

The containment problem is that controls sourced from an agent-writable repo
can be weakened by the agent. Branch protection is one way to prevent that;
**credential scoping across repository boundaries is another, and it does not
require a paid plan.** Personal private repositories on the Free plan cannot
enforce protected branches, required checks, or CODEOWNERS review, so this
design relies on none of them.

| Repo | Contents | Agent credential |
|---|---|---|
| `qf-research` | `trainer/src/**`, `trainer/configs/**`, hypotheses, pre-registrations, narrative projection, digests, docs, service-change proposals | `Contents: write` |
| `qf-service` | collector, `db.js`, `src/live-predictor/**`, `init.sql`, migrations, Dockerfiles, compose files, dependency lockfiles | `Contents: read` + `Issues: write` — **no** Contents write, Pull requests, Workflows, or Administration |
| `qf-platform` | dispatcher, runner, evaluator, `eval.parquet` builder, contracts, `verdict.py`, `checks.py`, independent derivation, migration linter, negative-control suite | **no credential** |

The agents need to *read* `qf-service` to write a correct patch and to keep
serving-parity implementations in step, and they need to file issues there.
Read and issue-filing are not authority over the code, so they are granted;
every mutation path is withheld.

`qf-platform` needs no credential at all: the trusted checkout at
`/srv/qf-platform` is **world-readable and root-owned**, so agents can read
contracts and evaluator source directly — reading a control is not weakening
it — while the dispatcher exposes the operations they need on it (`contract
show`, `verify`, `independent-derive`) as typed jobs.

This is strictly stronger than the path matrix it replaces. A path glob can
leave a newly introduced sensitive file unowned — which is why a default-human
rule is needed there. A repository boundary has no such failure mode: a new
file in `qf-platform` is unwritable by construction, with no rule to maintain.

Consequences, all load-bearing:

- **`qf-research` is untrusted input.** Nothing in it is a control. Trainer
  code from it runs only inside the sandbox, and its outputs are validated by
  trusted code from `qf-platform` (§8.5). CI on that repo is therefore
  advisory, and its unenforceability is irrelevant — nothing downstream trusts
  it.
- **No merge bot.** Agents push directly to `qf-research`. The bot, the path
  matrix, and self-owning CODEOWNERS all disappear.
- **Trusted checkouts are human-updated.** `/srv/qf-platform` and
  `/srv/queue-forecasting` are root-owned and pulled with **read-only deploy
  keys unreadable by `research`**. An agent can neither push to them nor alter
  what a human pulls.
- **The dispatcher resolves controls only from the trusted checkout.** A
  contract, evaluator, or linter path resolving inside the research worktree is
  refused, not read. This is what stops a research-repo file from
  impersonating a control, and it is the residual risk this split introduces:
  drift, if someone relocates a control into `qf-research` for convenience.
- **Service changes are proposed as artifacts.** With no write credential on
  `qf-service`, an agent cannot open a PR there. It commits
  `proposals/<date>-<slug>.patch` to `qf-research` and files an issue on
  `qf-service`; the human applies it. Volume is a handful per month.

If these repos ever move to an org with Team/Enterprise, add protected
branches, required checks from a designated App, and CODEOWNERS as defence in
depth — but the design must not come to depend on them.

**Ownership note (not a security issue):** personal private repos put Mozilla
work in a personal account. Acceptable for a staging area whose stated end
state is a curated port back into taskcluster; worth revisiting if this
becomes durable infrastructure.

### 3.2 Host layout

One host: the existing experimental server. No second VM.

| Concern | Unix user | Checkout | Container runtime | Postgres role |
|---|---|---|---|---|
| Service stack (collector, live-predictor, dashboard, health-monitor, retention) | `svc` | `/srv/queue-forecasting` ← `qf-service` | rootful Docker (existing) | `forecast_app` (read-write) |
| Trusted dispatcher, runner, evaluator (§3.4) | `root`-owned unit, drops to `svc` | `/srv/qf-platform` ← `qf-platform` | rootful Docker | `forecast_migrator` (deploys only) |
| Agent processes (Claude, Codex) | `research` | `/home/research/qf-research` | **none** | `forecast_experiment` (read-only), via the bounded query interface |

Invariants:

- The `research` user has **no** container-runtime access of any kind. A
  Compose *project name* is not a security boundary; access to a shared Docker
  daemon is equivalent to host root. Agents never invoke `docker`.
- The dispatcher's code and unit files are root-owned, sourced from
  `qf-platform`, and outside every agent-writable path.
- Experiment output never lands in `trainer/data/models/` — that path is what
  the live predictor reads.

### 3.3 Constraining the agent processes themselves

The agents run as ordinary shells outside any experiment container, so they
need their own limits:

- A systemd slice `qf-research.slice` with `MemoryMax`, `CPUQuota`, `TasksMax`,
  and `IOWeight`, sized so agent activity cannot starve the service stack.
- **Egress allowlist**, not a blanket deny: the agents are API clients and
  require `api.anthropic.com`, `api.openai.com`, and `github.com`. Everything
  else is denied. Experiment containers get **no** egress at all.
- No readable service credentials: `.env`, Pulse/Taskcluster/GCP credentials,
  SSH keys, and the trusted checkouts' deploy keys are unreadable by
  `research`. The agents hold only their own model API keys and two
  fine-grained GitHub tokens (§3.1): one with `Contents: write` on
  `qf-research`, and one with `Contents: read` + `Issues: write` on
  `qf-service`. No credential of any kind for `qf-platform`.
- Database access only through the bounded read-only query interface (§3.5),
  never a direct psql session, so query cost is capped and logged.

### 3.4 Trusted dispatcher (restored from the superseded proposal)

Agents do not launch training. They submit **typed jobs** to a root-owned
dispatcher over a unix socket. The dispatcher:

1. Validates the job against a JSON schema. Arguments are typed fields, never
   concatenated into a shell command.
2. Materialises the source with `git worktree add` at an **exact SHA** — no
   patches, no bundles. The bundle format from `experiment-runner.md` existed
   only to carry uncommitted laptop work; with agents owning a repo, a pinned
   SHA gives identical immutability with far less machinery.
3. Runs the trainer in a container: non-root, read-only source mount,
   run-private writable dir, no daemon socket, no egress, no service
   credentials, CPU/memory/PID/wall-clock limits.
4. Holds the **shared** heavy-training lock
   (`/tmp/queue-forecasting-walk-forward.lock`) — the same one
   `daily_walk_forward.sh` uses. A separate lock would allow two trainers to
   run concurrently and exhaust host memory; the hazard trainer already peaks
   at 99.9% of its 22 GB limit on a ~30 GB host.

Job kinds: `screen`, `confirm`, `probe` (restricted to
`research/experiments/`), `test`, `summarize`, `query`.

Whether the container runtime becomes rootless Podman for experiments is an
implementation choice (§16); the dispatcher boundary is required either way.

### 3.5 Database roles

- `forecast_app` — existing service identity (read-write).
- `forecast_experiment` — `CONNECT`, schema `USAGE`, `SELECT` only.
  `default_transaction_read_only=on`, plus `statement_timeout`,
  `idle_in_transaction_session_timeout`, `lock_timeout`, `temp_file_limit`,
  and a connection limit.
- `forecast_migrator` — dispatcher-only, for additive migrations.

**Prerequisite:** Postgres currently runs with
`POSTGRES_HOST_AUTH_METHOD=trust`, under which a read-only role is decoration —
any local connection can claim `postgres`. SCRAM must be enabled and the
existing volume's `pg_hba.conf` rewritten (changing the compose init
environment does not alter an initialised volume). See §13.

## 4. State

Two tiers, because git-backed JSONL cannot provide atomic dequeue, leases,
crash recovery, or exactly-once completion.

### 4.1 Live state — SQLite, owned by the dispatcher

`/var/lib/qf-platform/state.db` (WAL mode), writable only by the dispatcher.
Holds the job queue, leases, run state transitions, attempt counts, and
resource high-water marks. Atomic dequeue-with-lease; a crashed run's lease
expires and is reclaimed. Agents read it through the dispatcher; they never
write it directly.

It also holds the **bus itself**. The dispatcher exposes one append-only
operation, `note`, through which the leader submits each bus entry (and the
verbatim consult transcript it references). Entries are hash-chained on
arrival. Bodies are small text, so the storage cost is negligible next to run
artifacts — and persisting them here is what makes the reasoning record
survive a rewrite of `qf-research`.

### 4.2 Narrative projection — git, written by the leader, **untrusted**

At the end of each tick the leader exports a projection into `qf-research`:
`ledger.jsonl`, `bus.jsonl`, `queue-snapshot.jsonl`, `deploys.jsonl`,
`features.yaml`, and the generated digests.

This is **not** an audit authority. `qf-research` has no branch protection by
design (§3.1), so an agent can force-push or rewrite its entire history. That
does not compromise `qf-platform` or `qf-service` containment, but it does mean
the projection is a readable narrative that cannot be trusted after the fact.

**The authoritative record is the dispatcher's SQLite** (§4.1), which agents
cannot write. It holds, per job and per state transition, with timestamps:
`source_sha`, `prereg_hash`, `extract_hash`, `contract_hash`,
`evaluator_hash`, `verdict_hash`, `image_digest`, and the resulting verdict —
plus every bus entry and consult transcript, appended through `note`. Rows are
append-only and hash-chained, so tampering is detectable rather than merely
improbable, and the projection is fully reconstructible from it.

`features.yaml` is the one exception: it is a leader-curated synthesis, not a
transaction log, so a rewrite of `qf-research` loses its editorial content. Its
underlying evidence — which run, which verdict, which hashes — is recoverable
from SQLite, so it can be rebuilt, but not byte-for-byte.

The consequence for pre-registration (§8.4) is load-bearing: the authority of a
pre-registration is **the hash recorded in SQLite at job submission**, not the
committed file. Rewriting `preregs/H-NNNN.json` afterwards changes the
narrative and nothing else — the verdict still cites a hash that no longer
matches, and is void.

### 4.3 Artifact location

Resolving the earlier contradiction: run artifacts (models, caches, evaluation
artifacts, logs) live **outside git**, under `/var/lib/qf-runs/<run-id>/`. Only
small, reviewable records are committed: manifests, verdicts, pre-registrations,
and consult transcripts. Artifacts are retained 90 days, then pruned to
manifests plus the evaluation artifact (§8.5), whose retention is set by the
size estimate in §16.7 — reproducing a verdict depends on it, so it outlives
every other artifact.

### 4.4 `features.yaml` — the deliverable

```yaml
- name: pending_same_priority_same_queue
  definition: >
    Count of tasks pending on the same task_queue_id at the same priority at
    the target's reference instant, ordered ahead of it by FIFO.
  sources: [queue_forecast_tasks, queue_forecast_task_runs]
  availability_stage: pending        # created | scheduled | pending | running
  availability_justification: >
    Derived only from rows whose pending_at <= T; no field observed after T.
  serving:
    python_impl: trainer/src/queue_context.py
    js_impl: src/live-predictor/queue-context.js
    parity_verified: true
    parity_evidence: runs/2026-06-26-parity/420-of-420.json
    cost_ms_p95: 41
  coverage:
    collection_started_at: null      # null = present for all history
    min_history_days: 0
    daily_coverage_ref: coverage/pending_same_priority_same_queue.csv
  evidence:                          # per target, not global
    - target: wait_time
      run_id: qctx-b-confirm-20260715T...
      tier: confirm
      preregistration: preregs/H-0031.json
      effect:
        pinball_p90_guarded_delta_pct: -4.2
        30mplus_p90_miss_guarded_pp: -3.1
        within_2x_pp: +0.2
      ci95_paired: [-5.1, -3.2]
      verdict: PASS
  status:
    wait_time: adopted               # per target
    run_duration: candidate
```

`availability_stage` replaces the earlier boolean: a feature usable for run
duration at start time may be unavailable at pending time, and group ETA may
need information available at *creation*. `status` and `evidence` are per
target, because one registry serves three prediction moments.

### 4.5 `ledger.jsonl` and `bus.jsonl`

Ledger states: `PROPOSED → AGREED → PREREGISTERED → QUEUED → RUNNING →
SCREENED → CONFIRMED | REFUTED`, plus `BLOCKED_DATA` (with a concrete
`unblock_after`), `BLOCKED_HUMAN`, `SHELVED`.

Bus entries carry `seq`, `tick`, `author`, `kind`, `parent`, `refs`, `body`,
`transcript`, `hash`. `kind` ∈ `PROPOSE | CRITIQUE | REVISE | AGREE | DISAGREE
| DEADLOCK | VERDICT | TRIAGE | AUDIT | NOOP | DEPLOY`.

Reliability: single writer (the leader; `consult.sh` captures the copilot's
reply verbatim to a transcript file and the leader records a digest plus hash);
monotonic `seq` with `parent` links making the bus a replayable DAG;
`bus doctor` validates schema, sequence continuity, and that every `VERDICT`
and enqueue references its justifying consult, refusing the tick on corruption;
and a rolling `bus-summary.md` so a cold tick loads bounded context rather than
the whole history.

## 5. Roles

**Claude leads** — owns the tick, hypothesis selection, the ledger,
`features.yaml`, and every audit-projection write.

**Codex copilots**, invoked synchronously via `research/bin/consult.sh`
(`codex exec`, resumable sessions):

1. **Critique** every proposal before it consumes a slot.
2. **Independently verify** every result from the evaluation artifact (§8.5)
   using separate metric code — not by re-running `verdict.py`.
3. **Counter-propose.** Standing mandate to file its own hypotheses; the leader
   must triage each explicitly.
4. **Audit** daily, in place of a critique: review recent decisions and the
   ledger for drift, dead ends, and unexamined branches.

### 5.1 Disagreement

Up to 3 consult rounds. Then the leader proceeds and records the disagreement —
**except** for veto-class objections, which escalate:

1. Suspected leakage or a non-temporal split.
2. Availability-stage violation (feature not computable at the prediction
   moment, or no serving implementation).
3. Any change to an evaluation contract.
4. A destructive or non-additive database operation.
5. A new data-acquisition proposal. This class does not open an escalation
   issue — it goes to the proposal-artifact flow of §11, where the human
   applying the patch *is* the gate.

Escalation blocks the decision, not the program. The queue continues on
unblocked work.

## 6. The tick

`research/bin/tick.sh`, cron, as `research`.

1. `flock`; exit if a tick is live. Exit on `research/PAUSE` or exhausted daily
   token budget.
2. `git pull`; `bus doctor`; abort on corruption.
3. Load bounded context.
4. **Do exactly one thing**, first match wins: record a finished run's verdict
   (requiring the copilot's independent derivation to agree); triage an
   untriaged counter-proposal; apply a human reply from an escalation issue;
   pre-register and enqueue the next hypothesis if queue depth is below
   `max_queue_depth` (default 2); run the daily audit; otherwise `NOOP`.
5. Regenerate the projection and push it to `qf-research`. A rejected push
   means something landed concurrently: rebase and retry once, then abort the
   tick. Never force-push — not because it is prevented, but because the
   projection is meant to be readable history (its authority lives in SQLite,
   §4.2).

## 7. Job pinning

A tick pulls a mutable branch and the runner executes later, so every job
record pins its inputs. Without this, candidate and control can silently
differ in code or data.

Each job pins: `source_sha`, `image_digest`, `contract_hash`,
`resolved_config_hash`, `data_watermark` (max `pending_at` / resolution
timestamp included in the extract), `excluded_dates_hash` (the anomalous-day
set), `baseline_artifact_id`, and `extract_cache_key`.

The trainer already has a content-hashed Parquet extract cache
(`trainer/src/data_loader.py:90,184`), but its key does not include a data
watermark — the same window re-extracted later picks up late-arriving rows.
Therefore: **every job in one comparison shares a single frozen extract file**,
recorded by path and SHA-256 in each manifest, and the dispatcher refuses to
run a comparison whose members disagree on `extract_cache_key`.

## 8. Evaluation

### 8.1 Three tiers

- **Precheck** (seconds, no training): availability-stage check, Python/JS
  parity, temporal-split assertion, leakage tripwire (holdout AUC > 0.95 on
  this data is presumed leakage, per `project_random_split_leakage`).
- **Screen** (~1/6 cost): 3 fixed non-anomalous cohorts, subsampled holdout.
  May only **REFUTE** or **PROMOTE-TO-CONFIRM** — never confirm. Screen
  thresholds are calibrated against historical runs for *both* false negatives
  and false promotions, and the calibration is re-checked quarterly.
- **Confirm**: full walk-forward sweep under the target's contract.

### 8.2 Contracts, per target

Separate contracts: `contract.wait_time.yaml`, `contract.run_duration.yaml`.
(Group ETA has none yet — §14 Phase 7.) They live in `qf-platform`, so agents
cannot change them at all; a human commit is the only path. The repository
split also makes the earlier "no commit may edit a contract and record a
result together" check unnecessary — results live in `qf-research` and
contracts do not, so the two cannot share a commit.

### 8.3 Closing the p90-inflation hole

The earlier contract optimised 30m+ p90 miss while guarding only within-2× and
MAE — both computed from **p50** (`trainer/src/evaluate.py:22-38`). A candidate
could therefore emit an enormous p90, drive misses to near zero, and leave both
guardrails untouched. `compute_guarded_p90` floors p90 by p50, so the only
gaming vector is upward inflation — precisely the unguarded direction.

The machinery to close it already exists and is already aggregated into the
manifest (`evaluate.py:46-49,135-145`). The wait-time contract becomes:

- **Primary:** `pinball_p90_guarded` (lower is better). A proper scoring rule
  penalises inflation and under-coverage together.
- **Two-sided coverage:** `p90_coverage_guarded` must fall within
  `[0.88, 0.93]`. Over-coverage now fails.
- **Interval width:** median guarded p90 / median actual must not increase by
  more than a configured fraction versus control.
- **Reported, not optimised:** 30m+ guarded p90 miss, per-bucket MAE and
  within-2×, p50 MAE and within-2× as guardrails (unchanged).
- **Minimum tail sample counts:** a slice with fewer than N eligible rows
  yields `INCONCLUSIVE`, never `PASS`.

### 8.4 Statistical validity

Dozens of adaptive attempts against 16–17 cohorts will overfit the evaluation
set regardless of a frozen contract. Four mechanisms.

#### 8.4.1 Pre-registration

Before a confirm run the leader commits `preregs/H-NNNN.json`: hypothesis,
exact feature set, contract hash, cohort set, the inference parameters fixed
below, predicted direction, and predicted magnitude. The dispatcher records the
file's hash in SQLite at submission, and **that record — not the committed
file — is the authority** (§4.2). A verdict citing a hash that no longer
matches is void.

#### 8.4.2 The dependence problem, stated

Both production configs use `holdout_days: 5`
(`trainer/configs/wait_time_residual_throughput_filtered_baseline.yaml`,
`run_duration_residual.yaml`) while `daily_walk_forward.sh` advances cohorts
with `STEP_DAYS=1`. **Adjacent cohorts therefore share four-fifths of their
evaluation rows.** Two consequences:

1. Per-cohort deltas are not independent, so enumerating all sign assignments
   is computationally exact but statistically meaningless.
2. A sign-flip test is exact only under a **symmetric-sign** null, which is not
   the `H0: mean(d) >= 0` being claimed.

BH cannot repair invalid input p-values, so this must be fixed upstream of it.

#### 8.4.3 Unit of analysis: disjoint evaluation days

Each evaluation row must contribute exactly once.

`config.py:161-166` lays the windows out as `hold_end = A`,
`hold_start = A − H`, with validation immediately before the holdout. So a
cohort at `as_of = A` holds out `[A−5, A)`, and a day `d` is covered by every
cohort in `d+1 … d+5`. Attributing each day to the **newest** cohort covering
it therefore selects cohort `d+5` — which means **each cohort contributes its
oldest holdout day, `A−5`**, not its freshest.

That is the right day on the merits, not merely the one the rule happens to
pick: `A−5` is the first day after the validation block, so it is the
shortest-horizon evaluation available and the closest analogue to serving
immediately after a retrain.

The result is `n` disjoint evaluation days for `n` cohorts, with no row counted
twice. `eval.parquet` carries a `day` field and `evaluate.py` already emits
per-day metrics, so the decomposition needs no new measurement — only a
different aggregation.

`d_i` is redefined accordingly: the candidate-minus-control delta in the
target's primary metric on disjoint evaluation day `i`, both computed from the
same frozen extract (§7).

#### 8.4.4 Inference under residual serial dependence

Disjoint days remove row reuse but not serial correlation — queue regimes
persist across days. Primary inference is therefore a **moving-block
bootstrap** (MBB) on the daily delta series:

- *Block length* `L`, **pre-registered**, chosen by a fixed rule applied to the
  *control* series only so that selection cannot depend on the candidate's
  outcome. Default rule: `L = ceil(n^(1/3))`; the alternative permitted rule is
  the first lag at which the control series' autocorrelation crosses zero.
  Whichever is chosen is named in the pre-registration and cannot change
  afterwards.
- *Null:* `H0: mean(d) >= 0`, one-sided, direction fixed by pre-registration.
- *p-value:* resample the centered series `d_i − mean(d)` by moving blocks;
  `p` is the fraction of resample means at or below `mean(d)`. Valid for
  `H0: mean(d) >= 0` under stationarity and weak dependence — the assumptions
  are stated so they can be checked, not assumed away.
- *Interval:* MBB percentile 95% CI on `mean(d)`, reported alongside.
- *Recorded in the verdict:* `n`, `L`, and the effective sample size
  `n_eff ≈ n / L`. A verdict without them is malformed.

**Pre-registered robustness check.** A strictly non-overlapping variant — every
fifth cohort's full five-day holdout, giving `floor(n/5)` fully independent
units — is computed alongside. At three or four units it cannot reach `q =
0.05`, so it is **not** a test: it is a sign-agreement check. If its point
estimate disagrees in sign with the primary estimate, the verdict is
`INCONCLUSIVE` regardless of the primary p-value.

The superseded sign-flip permutation test is retained only as a diagnostic,
labelled as testing a symmetric-sign null, and never feeds BH.

#### 8.4.5 Multiplicity

Benjamini–Hochberg at `q = 0.05`, consuming only MBB p-values from §8.4.4. The
**family** is `(target, primary metric)`: every confirm test ever run for
`wait_time` on guarded pinball loss forms one family, growing over the life of
the program. Guardrail and secondary metrics are constraints, not tests, and
are excluded. BH is recomputed whenever a verdict is added.

#### 8.4.6 Adoption gate, and what dependence costs it

`CONFIRMED` is provisional. `adopted` additionally requires passing on
evaluation days **after** the pre-registration timestamp — days that did not
exist when the hypothesis was frozen and therefore cannot have been selected
on.

The earlier "roughly two weeks" was wrong once dependence is priced in.
The gate requires **at least 21 disjoint post-registration evaluation days and
at least 6 effective units** (`n_eff = n / L`). At `L ≈ 3` that is three weeks
minimum, and longer whenever the estimated block length is larger.

**Retention caps the achievable power, and the cap differs by target.** A
cohort at `as_of = D` reaches back `lookback + validation + holdout` days, and
`RETENTION_DAYS` is 60, giving a maximum `as_of` span of roughly `60 − reach`:

| Target | Reach | Nominal max as_of span | Nominal max disjoint eval days |
|---|---|---|---|
| `wait_time` (lb 14 + val 1 + hold 5) | 20d | ~40d | ~40 |
| `run_duration` (lb 30 + val 1 + hold 5) | 36d | ~24d | ~24 |

**These are nominal planning bounds, not guarantees.** Retention prunes
`queue_forecast_tasks` by `task_created` and cascades to `task_runs`
(`src/retention.js:41`), while training windows filter on `pending_at`
(`data_loader.py:240`). A task created well before it went pending can
therefore disappear earlier than the pending-time arithmetic predicts. The
dispatcher must derive the **actual contiguous eligible as-of range** from
extract and coverage availability at submission time and refuse a confirm whose
requested span exceeds it; the table above is for planning only.

`run_duration` confirms are structurally lower-powered, and its contract's
minimum `n_eff` must be set with that in mind rather than copied from
`wait_time`.

**Retention is not raised pre-emptively.** During Phase 3, estimate
target-specific power and minimum detectable effect from the historical
disjoint-day series. Keep `RETENTION_DAYS` at 60 unless `run_duration` provably
cannot reach its contractually required power. Moving to 75 days would add
roughly 4.5 GB before index and WAL overhead, and is the lever of last resort.

### 8.5 The evaluation artifact, built by trusted code

Independent re-derivation is impossible today: manifests carry only aggregates
and per-day rollups (`train.py:525-541`).

The correction that matters: `trainer/src/**` lives in `qf-research` and is
untrusted, so an artifact *emitted by the candidate run* proves nothing — two
implementations agreeing on a corrupted artifact still agree. The candidate run
therefore emits **only predictions**: `row_id`, `p50`, `p90_raw`.

`eval.parquet` is **constructed by the root-owned evaluator** in `qf-platform`,
which joins those predictions to the **frozen extract** (§7) and supplies
everything else itself: `actual`, `baseline_p90`, the guarded p90 (its own copy
of `compute_guarded_p90`), row membership, and every slice field (`day`,
`bucket`, `task_queue_id`, `priority`, `reason_resolved`). The candidate cannot
influence actuals, row membership, or bucketing — only the numbers it is meant
to be judged on.

The evaluator rejects a prediction set whose `row_id` multiset does not exactly
match the frozen extract's holdout rows.

The evaluator and its **complete dependency closure** live in `qf-platform`,
outside agent write access; the dispatcher refuses to execute an evaluator
resolved from anywhere else.

`verdict.py` computes the official verdict from `eval.parquet`. The copilot
computes its own using a deliberately separate implementation in
`qf-platform/eval/independent/`. A mismatch beyond floating-point tolerance is
an automatic `DEADLOCK`. Running `verdict.py` twice is not verification.

## 9. Feature availability and coverage

A start date alone does not detect collection gaps, partial population, or
drifting null rates.

- A daily job records, per feature column, the **non-null rate** and row count
  per UTC day into `feature_coverage`, exported to
  `research/state/coverage/<feature>.csv`.
- **Eligibility formula**, stated exactly to remove the earlier ambiguity. A
  feature is eligible for a cohort iff, over every UTC day in that cohort's
  **training window**: `day >= collection_started_at + min_history_days`, and
  `daily_coverage >= min_coverage` (default 0.98), and no day is missing from
  the coverage series. Partial windows do **not** silently shrink the training
  window — shrinking a window changes the comparison and would itself require a
  contract change. The feature is simply ineligible until the whole window
  qualifies, and the ledger carries the resulting `unblock_after` date.
- Violation is a **hard trainer error**, not a warning. A feature that is NULL
  across most of history teaches the model "NULL ⇒ old regime" and yields an
  impressive, worthless result.

## 10. The experiment queue

Priority classes, FIFO within class: production daily walk-forward > confirm
sweeps > screens. One heavy job at a time, enforced by the shared lock (§3.4).
No automatic retry of failed model code; infrastructure failures may be retried
explicitly, retaining the job hash and recording an attempt number.

## 11. Data acquisition

### 11.1 Channel

Agents hold no credential on `qf-service`, so they propose rather than submit.

```
agents (qf-research)                      human / trusted path
--------------------                      --------------------
propose feature needing new data
  |
  +-- commit proposals/<date>-<slug>.patch
  |     (migration + collector diff + tests, validated
  |      against a throwaway DB by a `test` job)
  +-- open an issue on qf-service
  |
  +-- human reviews, applies, merges  --->  deploy step (root-owned)
                                              1. diff policy: allowed paths,
                                                 additive-only migration
                                              2. apply migration (forecast_migrator)
                                              3. restart collector
                                              4. health gate (§11.2)
                                              5. auto-revert on failure
                                              6. record to deploys.jsonl
                                                    |
  leader reads deploys.jsonl next tick  <-----------+
```

### 11.2 Health gate

A deploy completes only after ~15 minutes of post-restart observation in which,
for each affected table: insert rate is within tolerance of its trailing 24-hour
baseline, **and** the new columns' non-null population rate meets the rate
declared in the PR, **and** collection lag (now − max observed timestamp) stays
within tolerance. Rate alone is insufficient — a change that writes NULLs or
wrong values at full speed passes a rate check.

On miss: revert the commit, restart the previous image, file an issue.

Deploys run only inside a configured window and never while a heavy training
job holds the lock.

### 11.3 Migration policy

Additive and forward-only, enforced by a CI linter: new columns nullable and
populated forward only; no `DROP`, `ALTER TYPE`, `UPDATE`, or `DELETE`; never
redefine an existing column's semantics (that manufactures a fake regime break
mid-history — the `claimed_tasks` metric already left a permanent hole in the
series on Jun 30 / Jul 1); `IF NOT EXISTS` guards; idempotent and fast.

## 12. Human interfaces

- **GitHub issues** — interactive. Escalations become issues on `qf-research`
  carrying both positions and the evidence; the human's reply is the tie-break.
  Service-change proposals become issues on `qf-service` pointing at a patch
  artifact (§11.1). Agents open no pull requests anywhere.
- **Dashboard research page** — at-a-glance. `dashboard-gen` gains a research
  view: queue, leaderboard, confirmed/refuted hypotheses, open decisions.
- **`RESEARCH_LOG.md` / `DECISIONS_PENDING.md`** — narrative, regenerated each
  tick.

## 13. Safety rails

Prerequisites, before *any* unattended execution:

- **SCRAM authentication on Postgres**, verified by attempting a write through
  the actual experiment connection path and being refused.
- **Defang `test/smoke.js`** — it defaults to the live DB port and performs an
  unconditional `DELETE FROM queue_forecast_task_runs` / `queue_forecast_tasks`
  with no `WHERE`. It must refuse to run without an explicit disposable
  `DATABASE_URL`, and refuse port 5433 outright.

Standing rails: shared heavy-training lock; sandboxed experiment containers
with no egress and no credentials; **no model promotion without a
human-approved PR**; live-predictor restarts remain human; usage budget; `research/PAUSE` kill switch; retention ≥55 days.

**Authentication is interactive SSO, not API keys** (the account sits behind
Mozilla SSO). Three consequences: the binding constraint is the account's rate
and usage limits rather than spend, so the budget mechanism backs off on
rate-limit responses instead of counting dollars; the one-time login must
happen **before** the egress allowlist is applied, since the OAuth flow reaches
the IdP and the vendors' auth domains; and token refresh must keep working
afterwards, so `phase0-setup.sh auth-check` runs periodically. An SSO token
that silently fails to refresh stops the loop with no other symptom.

### 13.1 Negative-control suite, by phase

Containment is asserted by tests that must **fail closed**. They are grouped by
the infrastructure each depends on; requiring all of them at Phase 0 was
impossible.

**Phase 0 — host controls.** As the `research` user, each must be refused:

1. Write to any forecasting table.
2. Invoke `docker`/`podman` or reach a container-runtime socket.
3. Read `.env`, Pulse/Taskcluster/GCP credentials, SSH keys, or the read-only
   deploy keys for the trusted checkouts.
4. Modify `/srv/qf-platform`, `/srv/queue-forecasting`, or any unit file.
5. Write into `trainer/data/models/`.
6. Reach a network endpoint outside the egress allowlist.

**Phase 1 — credential scoping.** Using the agent's own token:

7. Against `qf-platform` or `qf-service`: push, create or delete a branch or
   tag, open or merge a pull request, dispatch or edit a workflow, or change
   repository settings. Filing an **issue** on `qf-service` must succeed —
   this control asserts the boundary is mutation, not visibility. Reading
   `qf-service` contents must also succeed.

**Phase 2 — dispatcher integrity.**

8. Start a second concurrent heavy training job.
9. Submit a job whose `contract_hash` disagrees with the trusted checkout.
10. Cause a contract, evaluator, or linter to resolve from the research
    worktree instead of the trusted checkout.
11. Submit a prediction set whose `row_id` multiset does not match the frozen
    extract.

**Gate:** the full eleven must pass as a single run before Phase 4 begins, and
be re-run immediately before Phase 5.

## 14. Rollout

Value lands before autonomy does. **Phases 0–3 are worth building even if
autonomy is never enabled.**

**Phase 0 — host prerequisites and containment.** SCRAM; the three DB roles;
`smoke.js` defanged; `research` user, systemd slice, egress allowlist.
*Accept:* negative controls 1–6 fail closed.

**Phase 1 — three repositories and credential scoping.** `qf-research`,
`qf-platform`, `qf-service` split out of `tools/queue-forecasting/` with
history preserved via `git filter-repo`; trusted checkouts pulled by root with
read-only deploy keys; two agent tokens issued — `Contents: write` on
`qf-research`, and `Contents: read` + `Issues: write` on `qf-service` — and
none for `qf-platform`.
*Accept:* negative control 7 fails closed on every mutation path while reading
`qf-service` and filing an issue there both succeed; the agent can push to
`qf-research`; services keep running from `qf-service`; existing test suites
pass.

**Phase 2 — dispatcher, pinning, and the evaluation artifact.** Typed jobs,
worktree-at-SHA execution, SQLite live state with leases, the root-owned
evaluator and `eval.parquet`, `verdict.py`, the independent derivation,
contracts per target.
*Accept:* negative controls 8–11 fail closed; a known past result
(`wait_time_residual_throughput_filtered_baseline`) is reproduced end-to-end
through the dispatcher; the oracle's verdict matches the recorded numbers; and
the independent derivation agrees to tolerance. **At this point a human submits
experiments with one command and no SSH — the latency problem is solved with
zero autonomy.**

**Phase 3 — statistical validity.** Pre-registration bound to SQLite, the
disjoint-day decomposition, moving-block bootstrap inference, the
non-overlapping robustness check, BH-FDR, the post-registration adoption gate,
screen calibration.
*Accept:* replaying the existing 17-cohort history through the new machinery
reproduces the recorded conclusions, or explains precisely why one changes.

### Stop/go checkpoint

Operate Phases 0–3 as a human-triggered system for **four weeks** before
building any autonomy. Measure: confirms completed per week, hours per week
spent maintaining the machinery, how often a verdict was contested, and how
often the human was the binding constraint. Phases 4–6 then require a
**separate authorisation**, not a continuation. This checkpoint exists
specifically so sunk-cost momentum does not make the autonomy build automatic.

**Phase 4 — dry-run loop.** `tick.sh` and `consult.sh`, leader and copilot
conferring, **enqueue disabled**.
*Accept:* the full eleven-test negative-control suite passes as one run, and
the human reads several ticks of bus output and judges the reasoning
acceptable.

**Phase 5 — supervised autonomy.** Enqueue enabled; screening tier; digests;
issue escalation; dashboard page. Daily human review of the digest for the
first two weeks.
*Accept:* the negative-control suite is re-run and passes; one hypothesis
travels pre-registered → screened → confirmed or refuted with no human action,
and `features.yaml` gains an evidence-backed entry that survives the
future-cohort gate.

**Phase 6 — data acquisition.** Proposal artifacts, deploy path, migration
linter, health gate with column-population checks, coverage tracking.
*Accept:* one additive collector change is agent-proposed, human-applied,
deployed, health-gated, and appears in `deploys.jsonl` with coverage tracked.

**Phase 7 — group ETA.** Labels, an eval harness, and its own contract for
`max(wait + run)` over a task group. Scoped separately; not covered here.

## 15. Failure modes

| Failure | Detection | Response |
|---|---|---|
| Bus corruption | `bus doctor` at tick start | Refuse the tick; open an issue |
| Verdict derivations disagree | Independent recomputation from `eval.parquet` | Automatic `DEADLOCK`; escalate |
| Trainer OOM | Dispatcher resource watch | Infrastructure class; one retry; record high-water mark |
| Tick overruns the interval | Tick lock | Next tick exits immediately |
| Runner crash mid-job | SQLite lease expiry | Reclaim, mark failed, preserve logs |
| Comparison members disagree on extract | Dispatcher pin check | Refuse the job |
| A control relocated into `qf-research` | Dispatcher path-resolution check | Refuse the job; open an issue |
| Collector deploy degrades collection | Health gate (rate + population + lag) | Auto-revert, restart previous image, issue |
| Feature used with insufficient coverage | Trainer eligibility check | Hard error; ledger marks `BLOCKED_DATA` |
| Program-wide false discovery | FDR recomputation on each verdict | Retroactive downgrade to `INCONCLUSIVE` |
| Primary and non-overlapping estimates disagree in sign | Robustness check (§8.4.4) | Verdict forced to `INCONCLUSIVE` |
| Narrative projection rewritten or force-pushed | SQLite hash-chain mismatch | Ledger, bus, and job/result projection rebuilt from SQLite; `features.yaml` re-synthesised from recorded evidence; issue opened |
| Agent loops on a dead hypothesis | Daily copilot audit | `SHELVED` with reason |
| Runaway usage | Budget / rate-limit check | Tick exits early with backoff |
| SSO token stops refreshing | Periodic `auth-check` | Escalate; likely a missing auth domain in the allowlist |

## 16. Open decisions

1. Rootless Podman for experiment containers versus rootful Docker behind the
   dispatcher. The dispatcher boundary is required either way; rootless would
   additionally shrink the blast radius of a dispatcher bug.
2. Cron cadence (3 hours is the starting assumption).
3. Screen cohort set and subsample rate — must be fixed in the contract before
   the first screen and calibrated for both error directions.
4. Usage budget: the rate-limit backoff policy, and how a tick detects it.
5. Whether the deploy step polls GitHub or is webhook-triggered.
6. Whether the audit tick may re-open a `REFUTED` hypothesis, or only
   `SHELVED` ones.
7. Retention for `eval.parquet` across hundreds of runs — ~935k rows per
   cohort per run needs a size estimate before any retention is promised.
8. The minimum `n_eff` threshold per target contract, given that
   `run_duration` is capped near 24 disjoint evaluation days by retention
   (§8.4.6). Raising `RETENTION_DAYS` above 60 is the alternative lever and has
   a disk cost at ~300 MB/day.
9. Whether the block-length rule should be `ceil(n^(1/3))` or the ACF
   zero-crossing. Decide empirically against the existing 17-cohort history
   during Phase 3, then freeze it.
