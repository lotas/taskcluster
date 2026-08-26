# Remote Experiment Runner Proposal

Status: superseded by `auto-research-loop-design.md` (design v3, 2026-08-24).
Retained as design history and as the origin of the trusted-dispatcher boundary
carried forward into that design; this proposal was never implemented.

## Summary

Add a small, constrained experiment service on the GCP VM so local agents can
submit forecasting experiments, monitor long-running training, retrieve
results, and iterate without committing, pushing, opening a general SSH shell,
or moving the training dataset across the network.

Training and database-heavy preprocessing stay on the VM. The laptop-to-VM
connection carries only source patches, job specifications, logs, metrics, and
selected artifacts.

The initial submission format is a patch bundle based on a known Git commit.
Local experiment branches and commits can be supported later, but they are not
required and the current restrictions on agent `git commit` and `git push` can
remain in place.

## Motivation

The current loop requires a person to:

1. Review agent changes on the laptop.
2. Commit and push them from the host.
3. Connect to the GCP VM.
4. Pull the changes.
5. Start a training or walk-forward command.
6. Wait for a run that may take minutes or hours.
7. Bring the result back to the local agents for the next iteration.

This is useful as a deployment boundary, but it adds manual coordination to
every exploratory iteration. It also encourages the production checkout,
training caches, model outputs, and experimental work to share state.

The forecasting repository already provides most of the execution building
blocks:

- containerized Node and Python components;
- config-driven training;
- resume-safe walk-forward evaluation;
- manifest and CSV result formats;
- memory limits and an existing cron lock;
- GCS backup support.

The missing piece is a safe control plane around those building blocks.

## Goals

- Execute all expensive preprocessing, SQL, training, and evaluation on the
  GCP VM next to Postgres.
- Let an agent submit the exact working-tree changes it wants to evaluate
  without committing or pushing them.
- Let agents queue, inspect, follow, cancel, fetch, and compare experiments.
- Preserve enough provenance to reproduce every result.
- Serialize memory-intensive jobs so experiments cannot freeze the VM.
- Prevent experimental code from writing to the forecasting database,
  accessing service credentials, modifying the production checkout, or
  replacing live models.
- Keep deployment and model promotion as explicit, separately authorized
  actions.
- Start with a small implementation that fits the current single-VM setup.

## Non-goals

- Giving agents a general-purpose VM shell.
- Giving agents GitHub commit, push, merge, or force-push credentials.
- Automatically deploying successful code or promoting models.
- Moving the full Postgres dataset to the laptop.
- Building a general distributed ML platform.
- Running unreviewed dependency, Dockerfile, Compose, or infrastructure changes.
- Replacing the existing training and walk-forward logic unless isolation
  requires a small refactor.

## Proposed agent experience

The local interface is an `exp` CLI available inside the agents' Docker
container.

```sh
# Submit selected tracked changes and explicitly named new files.
exp submit \
  --name qctx-capacity-a \
  --path trainer/src/queue_context.py \
  --path trainer/src/features.py \
  --include-new trainer/configs/wait_qctx_a_capacity.yaml \
  --walk-forward \
  --from 2026-07-01 \
  --to 2026-07-14 \
  --configs configs/wait_qctx_a_capacity.yaml

# The command returns immediately with an immutable run ID.
# qctx-capacity-a-20260806T142301Z-a21c9d4e

exp status qctx-capacity-a-20260806T142301Z-a21c9d4e
exp logs qctx-capacity-a-20260806T142301Z-a21c9d4e --follow
exp fetch qctx-capacity-a-20260806T142301Z-a21c9d4e
exp cancel qctx-capacity-a-20260806T142301Z-a21c9d4e

exp compare \
  production-baseline-20260801T010000Z-42c71be0 \
  qctx-capacity-a-20260806T142301Z-a21c9d4e
```

The CLI should also accept a patch prepared separately:

```sh
git diff --binary HEAD -- \
  tools/queue-forecasting/trainer/src/features.py \
  tools/queue-forecasting/trainer/configs/wait_qctx_a_capacity.yaml \
  > /tmp/qctx-capacity.patch

exp submit \
  --name qctx-capacity-a \
  --patch-file /tmp/qctx-capacity.patch \
  --include-new trainer/configs/wait_qctx_a_capacity.yaml \
  --spec /tmp/qctx-capacity-experiment.yaml
```

These examples assume the command runs from `tools/queue-forecasting`. The
client normalizes selected paths to repository-relative paths in the bundle.

Submitting a Git commit or branch can be added as an optional convenience:

```sh
exp submit --commit 0123456789abcdef --spec experiment.yaml
```

Patch submission remains a first-class path even if local agent branches are
allowed later.

## End-to-end flow

```text
Local agent container                         GCP VM
---------------------                         ------

edit and run local tests
        |
        |  source bundle:
        |  - base Git SHA
        |  - binary patch
        |  - explicit new files
        |  - job specification
        v
    exp submit  --------------------------->  experiment dispatcher
                                                     |
                                                     v
                                                durable queue
                                                     |
                                      one heavy job at a time
                                                     |
                                                     v
                                             isolated containers
                                              |             |
                                              v             v
                                        local Postgres   run artifacts
                                                              |
    exp status/logs/fetch  <----------------------------------+
```

No dataset is forwarded to the laptop for training. Even optional exploratory
queries execute on the VM; only bounded query results are returned.

## Submission bundle

Each submission is an immutable bundle containing:

```text
job.json
changes.patch
new-files/
new-files.json
bundle.json
```

`bundle.json` records at least:

- schema version;
- base Git commit SHA;
- SHA-256 of the patch;
- path, size, mode, and SHA-256 of every explicitly included new file;
- submitting identity;
- local creation timestamp;
- requested job type and arguments;
- requested resource class and timeout;
- a hash of the complete bundle.

The default client behavior should be conservative:

- include only explicitly selected tracked paths;
- do not automatically include untracked or ignored files;
- require `--include-new` for every new file;
- show the complete file list and sizes before upload;
- reject `.env`, credentials, `.git`, `trainer/data`, model artifacts, database
  dumps, device files, sockets, and paths outside the forecasting tool;
- reject absolute paths, `..` traversal, and escaping symlinks;
- impose per-file and total bundle-size limits;
- support `exp submit --dry-run` to inspect the bundle without transmitting it.

On the VM, the dispatcher:

1. Verifies the bundle schema, size limits, hashes, and paths.
2. Verifies that the base commit exists in a read-only repository mirror.
3. Creates a fresh per-run source directory from that base commit.
4. Runs `git apply --check` before applying the patch.
5. Adds the explicitly listed new files after validating their hashes and
   destinations.
6. Makes the resulting source tree read-only to the experiment containers.
7. Records the final source-tree hash in the run metadata.

The patch is never applied to the VM's production checkout. Local edits made
after submission cannot change a queued or running experiment.

If the base commit is unknown to the VM, the client fails with an actionable
message. A later `--include-base` source-archive mode could support unpublished
base commits, but is not required initially.

## Job specification

The wire format should be structured JSON, even if the human-facing file is
YAML. Do not transmit a shell command string.

Example:

```yaml
version: 1
name: qctx-capacity-a
kind: walk-forward
walk_forward:
  from: 2026-07-01
  to: 2026-07-14
  step_days: 1
  configs:
    - configs/wait_qctx_a_capacity.yaml
comparison:
  baseline_run_id: production-baseline-20260801T010000Z-42c71be0
resources:
  class: heavy
  timeout: 12h
artifacts:
  keep_models: true
  keep_caches: false
```

Initial job kinds:

- `single-training`: one config and one as-of date;
- `walk-forward`: a date range, stride, and config list;
- `test`: selected Python or Node test suites;
- `probe`: an explicitly selected Python or Node module executed inside the
  same experiment sandbox;
- `summarize`: regenerate summaries from an existing run.

Arguments are parsed and validated as arrays or typed fields. They are never
concatenated into a host shell command.

`probe` allows feature exploration without adding every diagnostic to the
runner itself. It may execute arbitrary patched Python or Node code, but only
inside the constrained experiment container. Dependency and image changes are
not honored by a patch submission; those require a separate reviewed update to
the trusted experiment image.

## Run state and filesystem layout

Suggested VM layout:

```text
/var/lib/queue-forecast-experiments/
  mirror/                     # read-only Git mirror maintained by the runner
  queue/
  runs/
    <run-id>/
      bundle/                 # immutable submitted bundle
      source/                 # reconstructed source tree, read-only at runtime
      data/                   # run-private caches and intermediate data
      artifacts/              # manifests, models, summaries, diagnostics
      logs/
        runner.log
        training.log
      job.json
      metadata.json
      status.json
```

Run states:

```text
QUEUED -> PREPARING -> RUNNING -> SUCCEEDED
                            |-> FAILED
                            |-> CANCELLED
                            `-> TIMED_OUT
```

State files are updated atomically. A queued or running job survives a client
disconnect. The runner recovers queue state after its own restart and marks a
job `FAILED` with an infrastructure reason if it cannot safely resume it.

The run ID contains a readable name, UTC submission timestamp, and a short
bundle hash. The full hash remains in `metadata.json`.

## Execution model

The dispatcher and queue implementation are trusted code installed outside the
submitted source tree. Patched host-side scripts must never be executed
directly by the dispatcher.

The trusted runner translates each typed job into container invocations. The
submitted source tree is mounted read-only into those containers. Existing
host-side orchestration may need to be moved into a trusted runner module or a
purpose-built experiment image so a patched `run_training.sh` is not executed
on the host and no experiment container needs the Docker socket.

The experiment image contains the pinned Node and Python dependencies required
by the forecasting tool. A submitted patch may change application code,
configs, and experiment modules, but not the image, dependency lockfiles,
Dockerfiles, Compose files, runner code, or host configuration. Experiments
requiring new dependencies go through the normal reviewed host workflow first.

### Container boundary

Experiment containers should have:

- a non-root runtime user;
- a read-only root filesystem where practical;
- the submitted source mounted read-only;
- only a run-private data/artifact directory mounted read-write;
- no Docker or container-runtime socket;
- no host home directory or production checkout mounts;
- all Linux capabilities dropped and `no-new-privileges` enabled;
- CPU, memory, PID, temporary-storage, and wall-clock limits;
- no runtime access to `.env`, Pulse credentials, Taskcluster credentials,
  GCP credentials, SSH keys, or GitHub credentials;
- network access only to the experiment database endpoint unless a reviewed
  job type explicitly needs something else;
- outbound internet disabled by default.

Arbitrary model code is still arbitrary code. The security boundary is the
container, network policy, database role, mounted files, and job limits—not the
job-kind parser alone.

### Output isolation

Experiment output must not use the live `trainer/data/models` tree. The trainer
currently derives its model directory from its source location, so introduce a
configuration such as:

```text
FORECAST_TRAINER_DATA_DIR=/run/data
FORECAST_MODEL_OUTPUT_DIR=/run/artifacts/models
```

All baseline caches, Parquet caches, manifests, models, summaries, and probe
outputs then live under the run directory or an explicitly mounted shared
read-only cache.

The live predictor continues reading only the production model directory. A
separate, human-authorized promotion command copies a reviewed artifact into
that directory.

## Queueing and resource protection

Only one memory-intensive forecasting job should run at a time on the current
VM. The trainer is capped at 22 GB on a roughly 29 GB host, and previous
concurrent trainers exhausted memory and froze the VM.

The experiment runner and `daily_walk_forward.sh` must share one global
training lock. It is not sufficient for agent jobs to use a new lock while cron
continues using a different one.

**Done as of Phase 2a** (`auto-research-phase2a-plan.md` Task 7b): the shared
mutex is one provisioned `0660 root:qfheavy` inode at
`/var/lib/qf-locks/heavy-training.lock`. The old
`/tmp/queue-forecasting-walk-forward.lock` name is **retired, not aliased** —
any name in a 1777 directory is plantable while it does not exist, so the
untrusted user could create it before `systemd-tmpfiles` ran and own the
nightly run's lock. `flock` is per inode, so migrating cron is a dispatcher
**start-up prerequisite** rather than a follow-up: two provisioned paths are two
mutexes and both sides would run. Holds are shared/exclusive, with a separate
intent-marker gate, because shared holders barge past a queued exclusive waiter
(design D10a).

Recommended queue policy:

- FIFO within a priority class;
- production daily training has higher priority than exploratory jobs;
- at most one heavy job running;
- optionally allow one lightweight test or query job concurrently if measured
  safe;
- configurable maximum queued jobs per submitting identity;
- maximum wall time and artifact size;
- no automatic retry of failed model code;
- explicit retry for infrastructure failures, retaining the original bundle
  hash and recording a new attempt number.

Cancellation should stop all containers associated with the run, preserve
existing logs and partial diagnostics, and mark the run `CANCELLED`.

## Database access

Heavy SQL stays on the VM. Training containers connect over a private Docker
network to local Postgres; database rows do not cross the laptop-to-VM control
channel.

### Authentication must be fixed first

The existing Compose setup uses `POSTGRES_HOST_AUTH_METHOD=trust`. An SSH or IAP
TCP tunnel to the published loopback port would therefore allow a tunnel holder
to claim any database identity, including `postgres`. Supplying a read-only
username is not a security boundary while host authentication is `trust`.

Before autonomous experiments:

- enable password authentication using SCRAM or an equivalent authenticated
  local arrangement;
- update the existing volume's `pg_hba.conf`; changing only the Compose
  initialization environment does not rewrite an initialized database volume;
- retain the loopback-only published host port;
- confirm that application services still use distinct intended roles;
- remove unnecessary schema creation privileges from `PUBLIC`;
- test write denial using the actual experiment connection path.

### Separate experiment roles

Use at least two non-superuser roles:

1. `forecast_experiment`: explicit `CONNECT`, schema `USAGE`, and `SELECT` only
   on the forecasting tables required by training. It may have a long enough
   statement timeout for known training queries.
2. `forecast_query`: the same or narrower table access, but a short statement
   timeout and stricter interactive limits.

Useful per-role controls include:

- `default_transaction_read_only=on`;
- explicit table grants rather than ownership or broad write roles;
- connection limits;
- `statement_timeout`;
- `idle_in_transaction_session_timeout`;
- `lock_timeout`;
- `temp_file_limit`;
- conservative session-level parallel-query and work-memory settings.

Read-only access prevents mutation; it does not prevent an expensive query from
affecting availability. Queueing, timeouts, connection limits, and query
resource settings remain necessary.

The experiment container receives only the experiment database credential. It
does not receive the current shared `.env` file.

### Optional bounded query interface

Agents will sometimes need to inspect feature distributions before writing a
training patch. Provide:

```sh
exp query investigation.sql
```

The runner executes the query on the VM using `forecast_query` and returns a
bounded result. Controls should include:

- one statement or transaction in read-only mode;
- a short timeout;
- maximum returned rows and bytes;
- connection and temporary-file limits;
- logs containing query hash, duration, row count, and failure reason;
- cancellation when the client disconnects or explicitly requests it.

This avoids using a database tunnel for routine agent work. If a direct tunnel
is added later, it must use authenticated Postgres roles and should still be
limited to a dedicated read-only endpoint.

## Control-plane transport

The simplest initial transport is SSH with a dedicated experiment-runner key.
This is not general SSH access.

The corresponding VM account should use an OpenSSH forced command and
restrictions equivalent to:

- no interactive shell;
- no PTY;
- no TCP forwarding;
- no SSH agent forwarding;
- no X11 forwarding;
- no user startup commands;
- a fixed dispatcher that accepts only versioned runner operations.

Allowed operations:

```text
submit
status
list
logs
fetch
cancel
query
```

The restricted key can be mounted read-only into the local agent container. If
it is leaked, its capability is limited to the experiment API, subject to queue
and resource quotas. It cannot be reused for GitHub or an interactive VM
session and can be revoked independently.

If the VM has no public SSH endpoint, the control connection can be carried
through [GCP IAP TCP forwarding](https://docs.cloud.google.com/iap/docs/using-tcp-forwarding).
Human administrative access should use
[OS Login](https://docs.cloud.google.com/compute/docs/oslogin) where practical.
Do not mount a broad human `gcloud` configuration or general VM SSH key into the
agent container solely to support experiments.

An authenticated HTTPS API could replace the SSH dispatcher later. It is not
necessary for the first single-user version.

## Logs, results, and comparison

Every run should retain:

- submitted bundle and job specification;
- base commit and patch/bundle hashes;
- environment and dependency versions;
- resolved training, validation, and holdout windows;
- resolved config contents and config hashes;
- start/end time, exit status, timeout/cancellation details;
- stdout and stderr;
- resource high-water marks;
- model manifests;
- walk-forward summary CSV;
- requested model artifacts;
- cache hit/miss metadata;
- infrastructure versus model-code failure classification.

`exp fetch` downloads only the standard small result set by default. Large
models, caches, or diagnostics require explicit flags. Results are written to a
local ignored directory such as:

```text
tools/queue-forecasting/trainer/data/experiments/<run-id>/
```

The existing GCS bucket can provide durable VM-loss protection for run
manifests and selected artifacts. Agents do not need direct bucket credentials;
the VM runner can upload and the dispatcher can fetch authorized results.

`exp compare` should compare identical overlapping cohorts and clearly report
missing or non-comparable cells. Initially it can wrap the existing manifest
and walk-forward summarization formats. It should emphasize the repository's
existing decision metrics, including aggregate calibration and the target tail
slices, rather than introduce a new experiment-tracking platform immediately.

## Permissions and approval boundaries

| Capability | Local agents | Experiment runner | Human/admin |
|---|---:|---:|---:|
| Edit forecasting code and configs locally | yes | no | yes |
| Create a source patch | yes | no | yes |
| Commit or push Git changes | no initially | no | yes |
| Submit/status/log/fetch/cancel experiments | yes | executes | yes |
| Execute bounded read-only SQL | requests | yes | yes |
| Write to forecasting Postgres | no | no | yes, when operationally required |
| Open a general VM shell | no | no | yes |
| Change dependencies or experiment image | proposes patch | no | reviews/applies |
| Restart production services | no | no | yes |
| Replace or promote live models | no | no | yes |

The important boundary is promotion, not experimentation. Agents may run many
isolated candidates, but no score automatically changes live behavior.

## Failure handling

The runner should distinguish:

- invalid bundle or patch;
- unknown base commit;
- invalid job specification;
- queue rejection or quota exceeded;
- source preparation failure;
- dependency/image incompatibility;
- database timeout or connectivity failure;
- container OOM;
- wall-clock timeout;
- cancellation;
- training or evaluation failure;
- artifact upload/fetch failure.

Status and logs must remain available for failed jobs. A failed job must never
leave the global training lock held indefinitely or a production directory
partially modified.

## Alternatives considered

### Continue commit/push/pull/manual run

This retains strong human control but keeps the person in every exploratory
iteration and makes long-running jobs hard for agents to monitor autonomously.

### Give agents GitHub push rights

Push access does not solve job execution, isolation, queueing, result retrieval,
or database safety. It also expands authority beyond what experimentation
requires.

### Give agents a normal SSH account

A shell makes the VM, Docker daemon, production checkout, credentials, database,
and live model directory part of the agent's effective authority. A constrained
job protocol exposes the needed operations with a much smaller blast radius.

### Tunnel Postgres to the laptop

This is useful for small interactive queries but inefficient for training and
unsafe with the current `trust` authentication. Server-side execution keeps
large reads local and returns only bounded results.

### Self-hosted GitHub Actions runner

This provides a queue and log UI, but it requires a GitHub submission path and
still needs the same container, database, secret, and production-output
isolation. A repository workflow or patched action running on a privileged
self-hosted runner is not by itself a safe boundary.

### GCP Batch or a full experiment platform

These become attractive when training data is available from a shared snapshot
or object store and experiments need multiple ephemeral machines. Today the
pipeline is coupled to VM-local Postgres and already emits manifests and CSVs,
so a small local runner provides most of the benefit with less migration.

## Implementation plan

### Phase 1: Isolated local execution on the VM

1. Make trainer data, cache, model, and summary output roots configurable.
2. Create authenticated read-only experiment and query database roles.
3. Remove shared `.env` use from experiment containers.
4. Add an experiment-specific container definition with read-only source,
   run-private output, resource limits, and restricted networking.
5. Refactor enough orchestration into a trusted entrypoint that patched host
   shell scripts and the Docker socket are not required.
6. Verify a representative single training run and walk-forward sweep using
   isolated outputs.

### Phase 2: Queue and remote CLI

1. Add patch-bundle construction and dry-run inspection to the local `exp` CLI.
2. Add the forced-command SSH dispatcher.
3. Add durable queue, state machine, global locking, cancellation, and restart
   recovery.
4. Add log streaming and default result fetching.
5. Integrate cron and agent jobs with the same global heavy-training lock.

### Phase 3: Comparison and bounded exploration

1. Add `exp compare` around existing manifests and summaries.
2. Add the bounded `exp query` interface.
3. Upload standard manifests and selected artifacts to GCS.
4. Add retention for local run data and GCS artifacts.
5. Add queue quotas and lightweight/heavy job classification after observing
   actual usage.

### Later options

- submit local experiment commits or branches in addition to patches;
- use IAP as the only network path to the VM;
- add a small web dashboard;
- create a read replica or snapshot-based analytics database if experiment SQL
  materially affects live collection/prediction;
- move portable jobs to GCP Batch once data access is decoupled from the VM;
- introduce MLflow or another tracking service only if manifests and summaries
  stop being sufficient.

## Acceptance criteria

The first version is successful when:

- an agent with no commit or push rights can submit selected local changes;
- the returned run ID identifies an immutable base commit and patch hash;
- training and SQL execute on the VM against local data;
- editing the laptop worktree after submission cannot affect the run;
- disconnecting the laptop does not stop the job;
- two heavy submissions are serialized;
- cron and agent training cannot overlap accidentally;
- agents can retrieve status, logs, metrics, and selected artifacts;
- experiment code cannot write to Postgres;
- experiment code cannot read service credentials or access the Docker socket;
- experiment output cannot overwrite production models or caches;
- cancellation and timeouts release resources and preserve diagnostics;
- no experiment can deploy code, restart services, or promote a model.

## Open decisions

1. Should `probe` initially permit any Python/Node module in the submitted
   source, or only modules under a dedicated `trainer/experiments/` directory?
   The recommendation is a dedicated directory plus the same container sandbox.
2. Should daily production training always jump ahead of queued exploratory
   work, or should it only wait for the currently running experiment?
3. How long should raw models and large intermediate caches be retained?
4. Is direct IAP available from the local agent container, or should a
   restricted SSH endpoint be used first?
5. Which base commits will the VM mirror accept: the main upstream repository,
   the personal fork, or both?
6. Which result slices and thresholds should `exp compare` treat as promotion
   gates versus informational metrics?

## Recommended first cut

Implement patch submission, a single FIFO heavy-job queue, isolated run output,
read-only database credentials, status/log/fetch/cancel operations, and the
existing walk-forward summary format. Keep branches, direct database tunnels,
web UI, distributed execution, and automatic promotion out of the first cut.

That removes the repetitive commit/push/SSH/pull/run loop while preserving a
small and explicit authority boundary.
