# Auto-Research Loop Phase 2 — Design

Status: design v11, 2026-08-25 — ninth review round, plus a decision: **image
builds drop buildx and use the classic builder** (D10). Three revisions of
builder machinery kept failing on the driver's real behaviour, so the goal it
served — positively confirming build death — is deliberately given up, and the
weakening is recorded rather than glossed. Revision 10's note follows.

Status: design v10, 2026-08-25 — ninth review round. `BUILDING` reached the
transition table and none of the state-dependent queries; the `resources` table
was verified in prose and not in code; restart recharged a `BUILDING` job as its
builder rather than its reservation; and the socket directory was `0750 qfd:qfd`,
which made **both** sockets unreachable by every intended client — a control plane
that could not be contacted, unnoticed because every check assumed a reachable
socket. The builder's own lifecycle was the open decision, now closed in D10.
Revision 9's note follows.

Status: design v9, 2026-08-25 — eighth review round, and the first with a
**security regression of my own making**: revision 8's `qf force-release` escape
hatch sat on the `qfclient` socket, which `research` is a member of, so the
untrusted agent could assert "I verified nothing is running" and release the
mutex over live work. `SO_PEERCRED` records who called; it does not authorise
them. It moves to a root/deploy-only admin socket with a negative control.
Revision 8's dedicated buildx builder also brought three problems of its own —
a persistent builder cannot carry a per-run label, `docker-container` builds do
not load into the local image store by default, and the builder's own BuildKit
image was an unpinned addition to the trusted build boundary. Revision 8's note
follows.

Status: design v8, 2026-08-25 — seventh review round. The fail-closed rule for
unconfirmed kills was applied to containers but not to **builds**, whose
daemon-side work is invisible to `docker ps`; `kill_unconfirmed` was a *terminal*
state that kept holding a lock and a reservation, with no route back;
reconciliation ran forced cleanup before re-establishing the mutex; and the
handoff container carried no label, so the "everything stopped" inventory the
whole release path depends on could pass while it ran. Revision 7's note follows.

Status: design v7, 2026-08-25 — sixth review round. Three load-bearing gaps, all
about what a *release* actually proves: the outer deadline released the mutex
after an **unconfirmed** kill, the intent marker was published non-atomically
under a fixed name (so it could be read half-written, or deleted by another
invocation's trap), and the deadline itself did not survive a dispatcher restart,
so repeated restarts could hold the lock indefinitely. The deadline arithmetic
also left no room for setup or handoff. Revision 6's note follows.

Status: design v6, 2026-08-25 — fifth review round. The intent gate was itself a
reader/writer `flock`, so it inherited the very barging it was introduced to
stop; it becomes a **writer-visible marker**, which is not a lock and therefore
cannot be barged. `HOLD_CEILING_S` also turned out to be a formula rather than an
enforced limit, and the nightly wrapper **fails open when `flock` is missing**
(`scripts/daily_walk_forward.sh:218`) — a branch Task 7b was not touching.
Revision 5's note follows.

Status: design v5, 2026-08-25 — fourth review round, and the first one where two
claims were settled by experiment on this host rather than by reasoning:
**shared `flock`s do barge past a queued exclusive waiter** (a `LOCK_SH` was
granted while an `EX` waiter sat in the queue, and the waiter only entered after
every shared holder left), so a bounded wait derived from one job's runtime does
not prevent starvation; and **`flock` ownership is per open file description**
(two `open()`s each held `SH` independently, and closing one left the other's
lock standing, while a single shared descriptor lost the lock the moment its
first user closed it). D10a is rewritten around both. Revision 4's note follows.

Status: design v4, 2026-08-25 — third review round. Two blocking defects, both
consequences of trying to *observe* a mutex rather than *hold* one: admission
checked the training lock without taking it (a race), and the build reservation
was charged on top of a job that had not started (a deadlock at full size). The
mutex protocol is now shared/exclusive, which also deletes the external-memory
reservation it replaces, and **2a now changes the nightly script's one `flock`
line** — see D5. A rationale that rested on `fs.protected_symlinks` was also
wrong and has been replaced. Revisions 2 and 3 notes follow.

Status: design v3, 2026-08-25 — revised again after a second review round that
found six further defects, four of them blocking, all in cross-process
accounting: the lock inode, the lock's group, memory admission against work the
dispatcher did not start, and uid/gid mechanics in the handoff. Revision 2's
note follows.

Status: design v2, 2026-08-25 — revised after a review round that found
eleven defects, five of them blocking. The decomposition survived; resource
admission, scheduler and lease semantics, and artifact handoff did not. The
revised passages are D4, D5, D7, D9, D10, D12, D14, §4.2, §4.4, §4.5, §5, §7
and §8.
Parent: `auto-research-loop-design.md` §3.4, §4, §7, §8.2–§8.5, §13.1, §14.
Inherits: `auto-research-phase1-design.md` §4.1 (host layout: the trusted
mirror and the deploy checkout are different directories) and §6 (build
provenance: root-owned Dockerfile, human-promoted dependency manifests,
`uv sync --locked --no-install-project`, research source mounted only after the
image is built).
Amends the parent: §13.1 (control numbering), §14 (Phase 2 becomes four
sub-phases), §16.1 (closed), plus the rows in §9 of this document.

Phases 0 and 1 are complete: SCRAM and the three DB roles, the `research` user
under `qf-research.slice` with a uid-scoped egress allowlist, negative controls
1–7 failing closed with their canaries passing, `qf-research` extracted with
the subtree's history, and one agent token with `Contents: write` +
`Issues: write` on that repository and no credential of any kind on the
monorepo.

Phase 2 is what turns that containment into a usable machine. Its deliverable
is stated in the parent's §14 and is worth repeating, because it is the whole
justification for the work: **at the end of Phase 2 a human submits an
experiment with one command and no SSH, and the result is a verdict computed by
code the agent cannot influence.** No autonomy is added. Nothing here decides
what to run; it only makes running things cheap, pinned, and trustworthy.

---

## 1. Why Phase 2 has to be cut up

The parent lists Phase 2 as one step with five deliverables — typed jobs,
worktree-at-SHA execution, SQLite live state, the root-owned evaluator and
`eval.parquet`, `verdict.py` plus the independent derivation, and contracts per
target — and one acceptance bundle that includes reproducing a known historical
result end-to-end.

That is not one step, for a reason that only became visible once the trainer's
actual execution path was read rather than assumed. Today a single cohort is
produced by `scripts/run_training.sh`, which is **five** orchestration steps,
not one: resolve holdout days, resolve excluded dates, resolve the baseline
directory, ensure the aggregate baseline NDJSON exists, generate per-day
baseline JSONs with the **Node** predictor, then train. The trainer container
mounts `./trainer` read-write, receives `.env` wholesale, extracts from
Postgres itself, and writes models, caches and manifests back into the mounted
worktree.

Every one of those properties is something Phase 2 has to invert:

| Today | Phase 2 |
|---|---|
| `docker compose run` as the deploy user | typed job to a root-owned dispatcher over a unix socket |
| mutable working tree | `git worktree` at an exact SHA, mounted read-only |
| image built from `trainer/Dockerfile` in the same tree | image built from the **trusted checkout's** Dockerfile and human-promoted manifests |
| `.env` and full `DATABASE_URL` in the container | no credentials, `--network none` |
| trainer extracts from Postgres | trainer reads a **frozen extract** produced by trusted code |
| trainer computes its own metrics and writes its own manifest | trainer emits **predictions only**; trusted code builds `eval.parquet` and the verdict |
| writes into `trainer/data/models/` — what the live predictor reads | writes into `/var/lib/qf-runs/<run-id>/out/` |

Bundling all of that into one landing means the trust boundary and the data
plane and the evaluator all arrive together, unverified, and the first thing
that fails is indistinguishable from the second. So it is cut into four
sub-phases at the seams where a negative control can be made to fail closed.

---

## 2. The four sub-phases

| Sub-phase | Delivers | New job kinds | Controls asserted | Standalone value |
|---|---|---|---|---|
| **2a — the spine** | `qfd` dispatcher, unix-socket protocol, closed-world typed job specs, SQLite live state with a hash chain and leases, bare mirror + worktree-at-SHA, content-keyed trusted image build, the sandbox itself, run-directory layout and retention, the `qf` client | `test`, `selftest` | NC8 (no second heavy job), NC10 (trusted paths resolve only from the trusted checkout), NC12 (build provenance), NC13 (sandbox isolation), NC14 (the dispatcher's own token cannot write), NC15 (disk containment) | anyone can run the trainer's test suite at an exact published SHA in a sandbox, reproducibly, without SSH |
| **2b — the data plane** | trusted extractor over the six query shapes the trainer uses, `extract/MANIFEST.json` with per-file digests and a data watermark, trusted baseline-artifact production (the existing Node predictor), predictions-only trainer contract, mount layout for a read-only source with a writable `data/` | `extract`, `probe`, `query` | NC13 extended: a cohort trains to completion with no network and no credential | one cohort reproduces from a frozen, hash-recorded extract; the extract cache stops being a correctness hazard |
| **2c — evaluation** | `eval.parquet` built by the root-owned evaluator from predictions + frozen extract, `contract.wait_time.yaml` and `contract.run_duration.yaml`, `verdict.py`, the copilot's independent derivation in `eval/independent/`, the evaluator's own trusted image | `evaluate` | NC9 (`contract_hash` disagreement refused), NC11 (`row_id` multiset mismatch refused) | a verdict nobody can tilt: the candidate supplies `p50`/`p90_raw` and nothing else |
| **2d — ergonomics** | multi-cohort sweep composition, `screen` and `confirm` as first-class kinds sharing one frozen extract, `summarize`, the one-command submit path, the historical reproduction | `screen`, `confirm`, `summarize` | full suite 8–15 as a single run | the parent's Phase 2 acceptance: `wait_time_residual_throughput_filtered_baseline` reproduced end-to-end through the dispatcher |

Two properties of this cut are deliberate:

1. **Each sub-phase ends where a control can fail closed.** 2a's boundary is
   asserted without any data plane at all: a `test` job proves worktree
   pinning, image provenance, sandbox isolation, lane exclusion and the state
   machine, and a poisoned `pyproject.toml` in `qf-research` proves the build
   ignores it. Nothing about that acceptance depends on Postgres, contracts, or
   metric code.
2. **No sub-phase needs the next one to be useful.** 2a is a pinned test
   runner. 2b is a frozen-extract cohort runner. 2c is a verdict machine. 2d is
   the sweep. If the program stopped after 2b, the latency problem would
   already be materially better than SSH plus `docker compose run`.

Pre-registration, the disjoint-day decomposition and the moving-block bootstrap
stay in Phase 3, unchanged. 2c writes `verdict.py` against a contract; Phase 3
adds the statistical machinery *around* the verdict. The `pins` table in 2a is
shaped so `prereg_hash` needs no migration when it arrives.

---

## 3. Decisions

### D1 — Four sub-phases, in the order above

Settled. The alternative cuts (three, folding ergonomics into evaluation; two,
folding the spine into the data plane) were rejected because both land the
trust boundary together with something else, and the whole point of the boundary
is that it can be tested alone.

### D2 — Rootful Docker behind the dispatcher (parent §16.1, closed)

Experiment containers run under the host's existing rootful Docker, launched by
the dispatcher. Rootless Podman is not installed now.

What carries containment is NC2 — the `research` user has no container-runtime
access of any kind, which Phase 0 already asserts and `phase0-setup.sh`
actively refuses to break (it dies if `research` is in the `docker` group).
Rootless would additionally shrink the blast radius of a *dispatcher bug*,
which is a real but second-order gain, and it would cost subuid/subgid ranges,
cgroup delegation, and a second image-build path on a live host that is already
running 22 GB memory-capped training jobs against ~29.4 GB of physical RAM.
Revisit at Phase 5, when unattended execution actually changes the threat
model.

The sandbox flags are part of the boundary, not an implementation detail:

```
--network none --read-only --cap-drop ALL --security-opt no-new-privileges
--user 10001:10001 --pids-limit N --memory M --memory-swap M --cpus C
--tmpfs /tmp:rw,nosuid,nodev,size=... --oom-score-adj 500
-v <run>/src/trainer:/app/trainer:ro   -v <run>/out:/out:rw
```

No `--env-file`. No `-v /var/run/docker.sock`. No `DATABASE_URL`. Nothing from
`$DEPLOY_DIR`.

### D3 — Source at a SHA comes from GitHub, via a dispatcher-only read token

The dispatcher keeps a bare mirror at `/var/lib/qf-platform/qf-research.git`,
fetched over HTTPS with a second fine-grained token — `Contents: read` on
`qf-research`, owned by the dispatcher user, mode 0400, unreadable by
`research` — and materialises source with `git worktree add --detach <sha>`.

Rejected alternatives, with the reason each was rejected, because the reason is
the design:

- **`git fetch file:///home/research/qf-research`.** No credential, simplest
  plumbing — and it spawns `git upload-pack` against an **agent-writable
  repository**, whose local configuration is chosen by the agent. Whether any
  particular knob (`uploadpack.packObjectsHook` and friends) is honoured from
  repository-local config in the installed git version is exactly the sort of
  question a control must not depend on. A boundary that needs an empirical
  probe of git internals to be trustworthy is not a boundary; it is a bet.
- **`git bundle` produced by dropping to `research` via sudo.** Sound — the
  bundle is created by an unprivileged process and consumed as data, and the
  SHA is still the pin. Rejected on a weaker but real ground: it lets a job run
  a commit that exists only on the host, so a human reviewing a verdict has
  nothing to look at. Fetching from GitHub makes "what ran" a URL.

Two consequences follow and are load-bearing:

- **Only published commits can run.** The dispatcher refuses a SHA that is not
  reachable from a remote-tracking ref, and records the `source_ref` it was
  reachable from. This is a feature: it kills "it worked on the host" as a
  category.
- **The mirror is hardened against the content it carries.** `core.hooksPath`
  is set to `/dev/null` on the mirror, worktrees are added detached with no
  submodule initialisation, and the dispatcher never runs any command *inside*
  the worktree — it only mounts it.

### D4 — Candidate code never touches Postgres; it reads a trusted frozen extract

The sandbox has no egress and no credentials, so the trainer cannot extract for
itself. Trusted code — dispatcher-side, under the read-only
`forecast_experiment` role, `default_transaction_read_only=on` — extracts a
**wide superset** per window into Parquet, and the container reads it from a
read-only mount with `--network none`.

The superset is not a new inventory; it is the six query shapes
`trainer/src/data_loader.py` already issues:

| Extract file | Source today |
|---|---|
| `runs.parquet` | `_build_query` — `queue_forecast_task_runs` ⋈ `queue_forecast_tasks` over the window |
| `worker_counts.parquet` | `load_worker_counts` |
| `worker_pools.parquet` | `load_worker_pools` |
| `throughput_runs.parquet` | `load_task_runs_for_throughput` |
| `qctx_runs.parquet` | `load_task_runs_for_queue_context` |
| `anomalous_dates.json` | `load_anomalous_dates` |

**"Reuse the query shapes" must not be read as "run the trainer's loader".**
`_build_query` (`trainer/src/data_loader.py:199-247`) splices `c.filters`
straight into its `WHERE` clause and selects `c.target_column` — both chosen by
a config file that lives in `qf-research`. Trusted code executing SQL fragments
or a column name supplied by the research repo would defeat the entire claim
that a new table or column needs human promotion, and it would do so silently.
So the extractor takes a **closed-world typed extraction request** — target,
window, watermark, nothing else — validated the same way a job spec is (D12).
No filters. No config file. No candidate-chosen column.

Each file is written with a **fixed column inventory enumerated in trusted
code**: every column the union of those six queries can select, unfiltered, not
the per-config subset. So a candidate that wants a different derivation needs no
trusted change, and cannot ask for anything the inventory does not already name.

**All six datasets are read from one read-only `REPEATABLE READ` snapshot**, so
they cannot disagree about what existed. The manifest records the snapshot's start timestamp and
**`pg_current_snapshot()`** alongside the watermark — the snapshot, not merely
`txid_current()`, because a transaction id does not encode what that transaction
could see. Without it, two files in one "frozen" extract could straddle a
collector write and nothing in the record would show it. `extract/MANIFEST.json` records, per file, a
SHA-256, a row count, the window, and the **data watermark** (the maximum
`pending_at` and resolution timestamp included), which RECORDS the parent §7 hole
-- see D20 in §8a for why recording is not closing, and what closes it
where the trainer's content-hashed cache key omitted a watermark and the same
window re-extracted later silently picked up late-arriving rows.

The line this draws, stated so nobody has to rediscover it:

- **A new derivation is free.** Anything computable from those rows —
  every bet-1 queue-context feature, every bet-2 hazard label — is a change in
  `qf-research` alone.
- **A genuinely new table or column is a human change**, promoted into the
  trusted extractor. That is the correct place for the friction: a new column
  is a claim about availability at the prediction moment (parent §9), which is
  veto-class (§5.1 item 2).

The frozen extract is also, by construction, the artifact §8.5 needs: the
evaluator joins predictions to *these* rows for `actual`, `day`, `bucket`,
`task_queue_id`, `priority` and `reason_resolved`, and the row key is the
`(task_id, run_id)` pair the trainer already carries.

The rejected alternative — inject `forecast_experiment` into the sandbox and
allow Postgres-only networking — preserves maximum iteration freedom and was
rejected because it puts a live credential and a live socket inside untrusted
code, and leaves the frozen-extract guarantee to be built separately anyway.

### D5 — The dispatcher runs as a dedicated system user in the `docker` group

`qfd`: a system user, home `/var/lib/qf-platform`, member of `docker` and of
`qfrun` (gid 10001, the in-container uid), owner of `/var/lib/qf-platform` and
`/var/lib/qf-runs`. The unit is root-owned; the process is not root.

**Being in the `docker` group is root-equivalent, and this design says so
plainly rather than implying otherwise.** What the dedicated user buys is that
an ordinary bug — a bad path join, an unlinked tree, a runaway write — damages
`qfd`'s own directories instead of the filesystem, and that the process runs
under systemd hardening (`NoNewPrivileges`, `ProtectSystem=strict`,
`ProtectHome=yes`, `ReadWritePaths` limited to its two state directories). What
it does not buy is protection against a determined compromise of the dispatcher
itself. That residual is precisely the argument for rootless Podman in D2, and
it is why D2 is marked revisitable rather than closed forever.

`ProtectHome=yes` is deliberate and free: the dispatcher has no reason to read
`/home/research` — under D3 it fetches from GitHub — and no reason to read
`$DEPLOY_DIR`.

**The shared lock needs `PrivateTmp=no` *and* a provisioned inode.** Under
`PrivateTmp=yes` the dispatcher would lock a private inode, both sides would
"succeed", and two 22 GB trainers would run on a 29 GB host — the 2026-07
incident that froze the box twice. But a shared namespace is not shared
*permission*, and this is where the first revision was wrong:
`daily_walk_forward.sh:213` acquires the lock with `exec 9>"$LOCK_FILE"`, a
**write** open. `qfd` and the deploy user are different users, `/tmp` is 1777,
and whichever process creates the file first owns it at mode 0644 — after which
the other side's `exec 9>` fails with `EACCES`. In the nightly script that is a
redirection failure in a non-interactive shell, so **the nightly walk-forward
would simply die**, and the dispatcher would hold a lock nobody else can take.
A silent single-writer mutex that kills the incumbent is worse than no mutex.

So the lock is a provisioned resource, not an incidental file. Revision 2 got
the provisioning wrong in two further ways, both of which mattered:

- **`flock` is per inode, so "provision both paths" was not a fallback at all.**
  Creating `/var/lib/.../heavy-training.lock` *and*
  `/tmp/queue-forecasting-walk-forward.lock` as two regular files gives two
  independent locks; an un-migrated cron entry and the dispatcher would each
  take one and both would run. There is no such thing as a transitional period
  here: either both sides name the same inode or there is no mutex.
- **`qfclient` is the wrong group.** It contains `research`, so the untrusted
  agent could take the mutex directly and hold it for as long as it liked —
  stopping nightly training with none of the queue's limits or timeouts
  applying, because no job would be involved.

The corrected arrangement:

- **One inode, at `/var/lib/qf-locks/heavy-training.lock`**, provisioned by
  `systemd-tmpfiles` as `0660 root:qfheavy` inside a directory provisioned
  `0755 root:root`. A directory under `/var/lib/qf-platform` would not work:
  `StateDirectory=` creates that as `qfd:qfd` mode 0750, which the deploy user
  cannot traverse.
- **`qfheavy` is a dedicated group containing exactly `qfd` and the deploy
  user.** `research` is not in it and must never be added; the mutex is not a
  thing an agent participates in.
- **Cron migration is a prerequisite, not a follow-up.** The installer verifies
  that the deploy user's `daily_walk_forward.sh` entry carries
  `LOCK_FILE=/var/lib/qf-locks/heavy-training.lock` and writes a root-owned
  marker; **`qfd` refuses to start without that marker**, and refuses if its
  lock path is missing or not writable by a group it belongs to. A dispatcher
  that cannot join the mutex must not run jobs.
- **Nothing lives in `/tmp`, and the reason is not the one revision 3 gave.**
  That revision claimed `fs.protected_symlinks=1` would stop the deploy user
  following a root-owned symlink in `/tmp`. That is **wrong**: the kernel's rule
  permits the follow when the symlink's owner matches the *directory's* owner,
  and `/tmp` is root-owned, so a root-owned symlink there is followable. It was
  not confirmed by experiment either, which is how a plausible-sounding
  mechanism became a justification.
  The argument that does hold is about the **absent** name, not the symlink: any
  name in a 1777 directory is plantable while it does not exist. If `research`
  creates `/tmp/queue-forecasting-walk-forward.lock` first — before
  `systemd-tmpfiles` runs, after a boot or a `/tmp` clean — then the nightly
  `exec 9>` opens an **agent-owned** file, and the agent either breaks the
  nightly run (mode 0644, `EACCES`) or holds its mutex (mode 0666 plus a
  `flock`). The sticky bit stops deletion of someone else's file; it does not
  reserve a name. So the lock lives under `/var/lib`, and the legacy name is
  retired rather than aliased — which costs nothing, since migration is a
  start-up prerequisite anyway.
- **NC8 asserts the inode, not the intention:** both names, if two exist, must
  report the same device and inode. The "both creation orders" test earlier
  revisions specified is **gone**: the lock's directory is `0755 root:root`, so
  neither runtime user can unlink or recreate the file, and `qfd` refuses to
  start when it is missing. What is asserted instead is that neither can unlink
  or recreate it, both can write-open it, and a `flock` taken by one is seen by
  the other. Also asserted: `flock` actually works on that filesystem — it is a
  no-op or node-local on some, which would make every guarantee here vacuous.

### D6 — The dispatcher's dependency closure is the Python standard library

`qfd` and the `qf` client use `json`, `sqlite3`, `socket`, `subprocess`,
`hashlib`, `struct`, `pathlib` and nothing else. No third-party packages, no
virtualenv, no lockfile of its own.

This is a containment decision, not an aesthetic one. The dispatcher is the one
piece of code that both holds the boundary and runs outside a sandbox, so
"what is its dependency closure and who reviewed it" must have a one-word
answer. The evaluator in 2c genuinely needs `pandas` and `pyarrow`; it gets its
own trusted image, built by the same content-keyed path as the trainer
environment from its own human-promoted manifests, and it runs in the same
sandbox with `--network none`. Trusted code and unsandboxed code are different
categories, and only the dispatcher is in the second.

### D7 — One hash chain, in `events`; `jobs` is a projection of it

The parent's §4.1 puts the job queue, leases, state transitions, resource
high-water marks *and* the bus in SQLite, hash-chained. Two chains (one for
jobs, one for bus notes) would double the verification surface for no gain, so
there is one append-only `events` table with a single chain, and Phase 4's
`note` operation becomes an event row of kind `NOTE`.

`jobs` rows are a **materialised projection** of `events` maintained in the
same transaction. Verification is `verify-chain`, which recomputes the chain and
then replays events into a projection and compares it to `jobs`.

**The comparison covers every column of `jobs`, not just `state`.** Revision 2
widened it and still left three gaps: `lease_owner` and `lease_expires_at` were
outside the projection, lease *renewals* updated `jobs` without appending an
event at all, and the artifact projection carried only a digest while the table
also holds a path and a size. So: every column is projected, every mutation —
renewals included — appends an event carrying the fields it sets, and the set of
deliberately non-authoritative fields is **empty**. If a field is ever moved out
of the projection, it is named in the code with the reason, because an
unexplained omission reads as coverage. The first
revision claimed any edit to a projected row was detectable while replaying only
the state, which would have let an edit to `spec_json`, `source_sha`,
`image_digest`, `exit_code`, the timestamps, the resource high-water mark, a pin
or an artifact digest pass verification untouched — precisely the fields a
verdict would later be argued from. Every event payload therefore carries the
values it sets, and the replay reconstructs and compares all of them, plus the
`pins`, `artifacts` and **`resources`** projections — the last keyed by
`(role, container_id)` and covering `created_at` and `released_at`, since a
release record is a claim about reality and forced cleanup reads it.

**And the claim needs narrowing.** A hash chain with no external anchor detects
*edits*; it does not detect a wholesale recomputation by an actor who can write
the database file. `qfd` itself can do that, which is exactly the D5 residual
risk. If that threat matters, the answer is to anchor chain heads outside the
host — periodically emitting `(seq, hash)` somewhere append-only that `qfd`
cannot rewrite — and until that exists, "tamper-evident" means evident to a
reader with the chain, not evident against the dispatcher itself. Recorded as an
open decision (§8) rather than quietly implied.

### D8 — The Python environment lives outside the source mount, and the entrypoint is an interpreter path

The current `trainer/Dockerfile` runs `uv sync` with `WORKDIR /app/trainer`, so
the virtualenv lands at `/app/trainer/.venv` — the exact path the read-only
research mount will shadow. And the compose entrypoint is
`uv run python -m src.train`, which at runtime wants to re-resolve the
environment: it needs write access and, on any drift, network.

So the trusted image sets `UV_PROJECT_ENVIRONMENT=/opt/qfenv`, builds in
`/opt/qfbuild` from the promoted manifests alone, and the dispatcher's
entrypoint is an absolute interpreter path — `/opt/qfenv/bin/python -m …`,
never `uv run`. `PYTHONDONTWRITEBYTECODE=1` and `-p no:cacheprovider` keep a
read-only source mount from failing on bytecode and cache writes.

This is small and it is the kind of thing that costs an afternoon if it is
discovered during the host step instead of now.

### D9 — Artifacts are read from the filesystem; the socket is control-plane only

Run directories are `0750 qfd:qfclient`, with `qfclient` containing `research`
and the deploy user. Clients read **`artifacts/` and `logs/`** directly. The
socket carries `submit`, `status`, `list`, `cancel`, `verify-chain` and `ping` —
one JSON object per connection, newline-terminated, response one JSON object.

**`out/` is not the client-readable directory, and cannot be.** It is written by
untrusted code as uid 10001, which means its contents arrive with whatever
ownership and mode that code chooses: a hostile candidate can write
`predictions.parquet` mode 0600, and `qfd` — the unprivileged owner of the
*directory* but not of the *file* — can then neither read it nor chmod it. That
is a denial of service against the dispatcher's own result collection, and no
permission arrangement on the directory fixes it.

So every run ends with a **trusted handoff step**: a second container
invocation, same sandbox flags, `--network none`, running as the same uid 10001
— the only identity that can read a 0600 file its predecessor created — with an
entrypoint script read from the trusted checkout.

Getting the ownership right takes four steps, because uid 10001 cannot write
into a `0750 qfd:qfclient` directory and anything it *did* create would be owned
by 10001, which is the problem restated one level down:

1. **`qfd` pre-creates each allowlisted destination** in `artifacts/` as
   `qfd:qfclient` mode `0660`, empty.
2. **The handoff container runs as `10001:10001` with supplementary group
   `qfclient`** (`--group-add <qfclient gid>`) so it can write those files by
   group. The **candidate's** container never gets `--group-add`; only the
   trusted handoff does.
3. It copies content into the pre-created files — never creating new ones — and
   **refuses anything that is not a regular file**. A symlink would read outside
   the mount; a FIFO would block the copy forever, which is why the handoff
   also carries **its own timeout**.
4. **`qfd`, still the owner**, hashes each file, records size and digest, and
   drops the mode to `0640`.

**A failed handoff fails the job.** A candidate process that exited 0 but whose
artifacts trusted code could not collect must not read `SUCCEEDED` — the point of
the handoff is that the record describes something a later stage can use. So the
job becomes `FAILED` with an `error_class` naming the cause
(`handoff_bad_type` for a symlink or FIFO, `handoff_timeout`,
`handoff_missing_artifact`, `handoff_oversize`), while the candidate's own exit
code is preserved in `exit_code` and the handoff's is recorded separately in
`result.json`. Distinguishing "the experiment failed" from "the collection
failed" matters for triage; conflating them into success does not.

`artifacts/` is `0750 qfd:qfclient` and its files end up owned by `qfd`, which is
what NC15 asserts. `out/` stays `2770 qfd:qfrun` and is never in a client's path.
The duplication is real and is charged: a run's disk allowance is
`OUT_QUOTA + ARTIFACT_CAP`, not `OUT_QUOTA`.

Keeping bulk data off the protocol keeps the protocol small enough to audit,
and it means an agent inspecting a 900k-row prediction set is a file read, not
a socket stream the dispatcher has to babysit.

### D10 — Admission is a memory budget; the lane is derived, never requested

The first revision had two lanes and let the caller pick one. That was wrong in
a way that reproduced the exact failure NC8 exists to prevent: `test` jobs sat
in the light lane, which takes no lock and runs **two at a time**, while
`mem_limit` was independently allowed up to 22 GB. Two light `test` jobs at
22 GB, or one alongside the nightly trainer, is 44 GB of admitted memory on a
host with ~29.4 GB of RAM. The lane was carrying the memory decision without
being derived from memory.

So admission is a budget, and the lane falls out of it:

- **`LIGHT_MEM_CEILING` = 4 GB.** A job asking for no more than that is `light`;
  anything larger is `heavy`. **The caller does not choose**, and there is no
  `lane` field in a job spec at all (D12).
- **`ADMITTED_MEM_BUDGET` = 22 GB** caps the **sum** of `mem_limit` over
  everything admitted. A job that does not fit waits. The figure is the host's
  ~29.4 GB less the ~4.5 GB the live stack uses and a few GB of slack — the same
  arithmetic `docker-compose.yml` already documents for the trainer's own cap.
- **Work the dispatcher did not start is excluded by the lock, not by
  arithmetic.** Revision 3 charged an `EXTERNAL_HEAVY_RESERVATION` whenever the
  training lock was *observed* held. That is a time-of-check/time-of-use race:
  light jobs never take the lock, so a lock seen free at admission can be taken
  by the nightly job a millisecond later, and the overlap the reservation was
  invented to prevent happens anyway. Observation plus accounting cannot close
  it; only holding the lock can. Hence the protocol below, after which the
  reservation is unnecessary and is **removed** — no dispatcher job can be
  running while the nightly job holds the lock, by construction.
- **`light` runs at most two concurrently** against the same budget, so two 4 GB
  jobs fit and a third waits.
- **One reservation per job, sized `max(mem_limit, IMAGE_BUILD_MEM_MB)`, held
  from admission to terminal state.** Revision 3 charged the build *on top of*
  the job, which made a 22 GB job with a cold cache permanently unadmittable
  against a 22 GB budget. Revision 4 fixed the arithmetic by building first and
  admitting after — and thereby specified two contradictory orders in two
  paragraphs, since the scheduler was also said to admit before dequeuing.
  A single reservation removes the ordering question entirely: the container does
  not exist during the build, so the two phases never need separate charges, and
  `max()` covers whichever is larger. A build no longer runs uncapped in the
  daemon uncapped — `--memory` caps each classic build step (D10) — but its
  footprint is real either way, which is why the reservation must cover it.
- **The build itself is serialised by its own lock, with a re-check.** Two light
  workers can miss the same content key simultaneously; both holding `LOCK_SH`
  on the training lock would not stop them duplicating a build. So a build takes
  `LOCK_EX` on a dispatcher-private `build.lock`, **re-checks the cache under
  it**, and builds only on a confirmed miss — with `BUILD_TIMEOUT_S`, because an
  unbounded build is an unbounded lock hold (D10a).
- **Builds use the classic builder, capped, with intermediates reaped.**
  `DOCKER_BUILDKIT=0 docker build --memory <IMAGE_BUILD_MEM_MB>m --force-rm`.
  Classic honours the build-time resource flags that BuildKit ignores, so each
  `RUN` step runs in a container with our cap; the result lands in the local image
  store, so `ensure_image` can inspect the tag and hand the sandbox an id Docker
  can actually run; and there is no builder container, no BuildKit image inside
  the trusted boundary, and nothing extra to pin, provision or inventory.

  **This is a decision to accept weaker confirmation, and it is recorded as one.**
  Revisions 7–9 moved builds into a dedicated buildx builder specifically so build
  work could be *positively confirmed* dead, and that goal is given up here. Three
  attempts at it failed on the driver's actual behaviour: a persistent builder
  takes no per-run label, survives its build and restarts `unless-stopped`; an
  ephemeral one cannot be identified from `buildx create`, which returns a name
  and does not create the container until `--bootstrap`; and either way the
  builder's own `moby/buildkit` image joins the trusted build boundary and needs
  pinning, provisioning and its own negative control. That was roughly seven
  mechanisms to confirm the death of a build that runs for a few minutes, a
  handful of times a year, from a Dockerfile we wrote.

  What replaces it:
  - **Our own subprocess is confirmable.** The `docker build` client is a child of
    the runner, so its death is a `waitpid`, not an inference.
  - **The daemon cancels a build when its client disconnects**, and `--force-rm`
    removes intermediate containers on failure as well as success.
  - **A bounded settle window** (`BUILD_SETTLE_S` = 30) after the client dies,
    before the training descriptor is released.
  - **`--memory` caps each step**, so even an unnoticed surviving step cannot
    exhaust the host the way an uncapped one could.

  **The residual, stated plainly:** daemon-side build work is not enumerable, so
  "the build is gone" rests on the client's death plus documented cancellation
  rather than on inspecting a container. It is the one place in this design where
  a release is not backed by positive confirmation. It is bounded by the cap and
  by how rarely builds run, and it is the accepted price of not carrying the
  buildx machinery.

  **And it is measured, not assumed.** `BUILD_SETTLE_S` = 30 is a guess until
  discovery measures it, so Task 10 probes the real behaviour on the real host:
  that the classic builder is available at all, that `--memory` is honoured, that
  `--force-rm` removes intermediates on failure as well as success, and **how long
  daemon-side cancellation actually takes after the client disconnects**. The
  decision rule is pre-registered rather than left to judgement: **if measured
  cancellation exceeds the settle window even once, building moves out of `qfd`
  entirely** — images become a human-built, ID-pinned input and the dispatcher
  refuses a content-key miss — rather than the window being lengthened to cover
  the observation. Lengthening a guess until it stops failing is how a bound
  becomes a decoration.

### D10a — The mutex protocol: an intent gate, shared/exclusive holds, one descriptor per job

The lock is not an advisory signal to be sampled; it is the admission primitive,
and it has to be symmetric, starvation-free, and correctly owned. Revision 4 got
the first of those and neither of the other two.

**The holds.**

- **Light jobs hold `LOCK_SH` on the training lock for their whole lifetime.**
  Several coexist.
- **Heavy jobs hold `LOCK_EX`.** One at a time, never beside a light job.
- **Nightly training holds `LOCK_EX`.** Its `flock -n` becomes a bounded wait,
  since under this protocol `-n` would make it skip the night whenever any
  experiment held the shared lock.

**The intent gate, because shared locks barge.** A bounded wait alone does not
work, and this was confirmed on the host rather than argued: with a `LOCK_SH`
held and an `EX` waiter already queued, a *second* `LOCK_SH` was granted anyway,
and the exclusive waiter entered only after every shared holder had left. Two
light workers handing off can therefore hold continuous shared occupancy past any
timeout, even though no single job exceeds its own ceiling.

Revision 5 answered that with a second reader/writer `flock`, `intent.lock`, and
**that was the same mistake one layer up**: nightly queues `LOCK_EX` on the gate
while the dispatcher takes `LOCK_SH` on it, so a gate held momentarily by one
worker lets a second worker barge past the queued nightly exactly as before. A
smaller window is not a closed one, and "the window is short" is not a property
anyone can test.

So intent is **not a lock**. It is a writer-visible fact in the filesystem, and
the reader has to look at it rather than contend for it:

- **Nightly publishes a marker before it waits**, and publication has to be
  atomic and per-invocation. Revision 6 wrote a fixed `nightly.intent` in place,
  which is three bugs: `qfd` can read it half-written; two invocations overwrite
  each other and either `EXIT` trap deletes the other's declaration; and `qfd`
  can judge an old marker stale and then unlink the live one that replaced it.
  So: a **unique name per invocation** (`nightly.<pid>.<epoch>.intent`), written
  to a temporary file and **published by `rename`**, removed by a trap that only
  ever removes *its own* file. Any live marker in the directory is intent; there
  is no single well-known name to race over.
- **The dispatcher reads that marker before every admission** and refuses to
  admit while it is present. Nothing is contended, so nothing can barge: a file
  that exists is visible to every reader, in every order.
- **Staleness is handled explicitly, because a crashed nightly must not wedge the
  loop forever.** A marker whose PID is gone (`/proc/<pid>` absent) or whose
  deadline has passed is logged loudly, removed, and ignored. The trade is
  stated rather than hidden: during the stale window the dispatcher may admit
  work a dead nightly would have blocked, which costs a delayed nightly run at
  worst and never a memory overlap, since the training lock still governs
  execution.
- **An unreadable or malformed marker fails closed.** It is treated as live
  intent — no admission — and alarmed, because a marker that cannot be parsed
  cannot be *shown* to be stale. The escape hatch is mtime: older than
  `LOCK_WAIT_S` plus margin and it is removed with a loud log, so a corrupt file
  delays the loop rather than ending it.
- **`$INTENT_DIR` is `2770 root:qfheavy`** — setgid, which is load-bearing and was
  missing: without it a marker's group comes from the deploy user's primary group
  and its mode from that user's umask, so under `umask 077` `qfd` could not read
  the declaration at all and would admit straight through it. The writer also
  sets `0640` explicitly rather than trusting a umask. `research` is not in the
  group, so the agent can neither declare false intent nor suppress real
  intent.

**Every dispatcher lock acquisition is non-blocking.** A worker must never hold
one lock while waiting for another: that is how a momentary gate becomes a long
one. `LOCK_SH|LOCK_NB` and `LOCK_EX|LOCK_NB` throughout, and on any failure the
worker releases everything it holds and re-peeks.

**The hold ceiling is enforced, not calculated.** Revision 5 derived
`HOLD_CEILING_S` from a job's `timeout_s`, its build and a kill grace, and called
that the longest hold. It was not: the training descriptor is taken before
worktree setup and released after handoff, hashing and cleanup, and the formula
omitted the wait for `build.lock` (after one timed-out build, the next job could
wait 1800 s and then build for another 1800), the 120 s handoff timeout, and the
setup and teardown themselves. A ceiling that omits phases is an estimate wearing
a constant's clothes.

So there is **one hard outer deadline, enforced by the runner**:

- `JOB_HOLD_DEADLINE_S` = 7800, measured **from the moment the training lock is
  acquired to the moment its descriptor is closed** — covering worktree setup,
  every wait, the build, the run, the handoff, hashing and cleanup.
- **Expiry starts a forced cleanup; it does not release the lock.** This is the
  correction that matters most in this revision. A subprocess timeout on
  `docker kill` proves only that the CLI stopped waiting — not that the container
  died. Releasing the training descriptor on that basis lets the nightly job
  start against surviving work, which is the precise failure the mutex exists to
  prevent, reached through the mechanism meant to bound it. So on expiry the
  runner kills, then **polls Docker until it positively reports every container
  recorded for that run in `resources` — candidate and handoff — stopped**, plus,
  if a build was in flight, no live `docker build` client and `BUILD_SETTLE_S`
  elapsed, and only then closes the descriptor. If confirmation is not obtained within `KILL_CONFIRM_S` (300), the
  job moves to the non-terminal `CLEANUP_BLOCKED` state with
  `error_class=kill_unconfirmed`, **keeping** its descriptor and reservation, and
  the dispatcher alarms and stops admitting until a reaper obtains confirmation.
  Overrunning the deadline and sacrificing a nightly run is the safe direction;
  releasing a lock over live work is not.
- **The per-job timeout is subordinate to the outer deadline.** The container's
  effective timeout is `min(timeout_s, remaining hold budget)`, because otherwise
  a job at `TIMEOUT_MAX` that also needs a build cannot fit inside the deadline
  and would be cut short by arithmetic rather than by policy. Revision 6 set the
  deadline to exactly `TIMEOUT_MAX + BUILD_TIMEOUT_S`, leaving nothing for setup,
  the `build.lock` wait, the handoff or teardown.
- `TIMEOUT_MAX` = **3600 s** in 2a. Only `test` (1800) and `selftest` (300) exist
  yet, so nothing is given up, and it keeps the whole chain small: 3600 + 1800
  (build) + 900 (build-lock wait) + 120 (handoff) + 600 (setup, teardown and the 10 s container stop) = 7020 < 7800, with margin. **All four numbers move together or not at
  all**, and 2b must revisit them as a set once real cohort runtimes are known.
- `BUILD_TIMEOUT_S` = 1800 and `BUILD_LOCK_WAIT_S` = 900, and the build phase's
  deadline **includes the wait for `build.lock`**, so a queue of builds cannot
  add up behind it. Each attempt opens its own descriptor.
- `HANDOFF_TIMEOUT_S` = 120; the kill path is `docker stop -t 10` then
  `docker kill`, with a subprocess timeout on the Docker CLI calls themselves, so
  a hung daemon cannot extend the hold either.
- `LOCK_WAIT_S` = 9000 > `JOB_HOLD_DEADLINE_S` + `KILL_CONFIRM_S`, and
  **raising any of `TIMEOUT_MAX`, `BUILD_TIMEOUT_S` or `JOB_HOLD_DEADLINE_S`
  requires raising `LOCK_WAIT_S` in the same change.** Discovery fails, not
  warns, if the relation inverts. Note what the `KILL_CONFIRM_S` term admits: a
  nightly run can be delayed past `LOCK_WAIT_S` and skip its night if a kill
  cannot be confirmed. That is the deliberate trade above, and it is visible in
  the arithmetic rather than buried in a code path.

**One open file description per job.** `flock` ownership belongs to the open file
description, not to the process or the worker — also confirmed here: two
separate `open()`s each held `SH` independently and closing one left the other's
lock standing, whereas with a single shared descriptor the first close dropped
the lock while the second job was still notionally running. So every admission
does its own `open()`, keeps that exact descriptor in the job's runtime record,
and closes it only at terminal cleanup or reconciliation. A module-level
descriptor shared between workers is a correctness bug that presents as a rare
overlap, which is the worst kind.

The cost is a nightly run delayed by at most one in-flight hold. The benefit is
that "two 22 GB processes never coexist" depends on a lock rather than a check,
and the memory budget no longer has to model work it cannot see.

**And the nightly wrapper must fail closed on a missing `flock`.** Today
`scripts/daily_walk_forward.sh:218` warns and *trains anyway* when `flock` is not
on the path. Revision 5's change edited only the branch where `flock` exists, so
the entire mutex — every property argued for above — was still bypassable by a
`PATH` that lacked one binary. `flock` becomes a `require_command`, and the
script exits before training if it is absent. A mutex with a warn-and-continue
branch is advisory, whatever the rest of the design says.

This is where 2a touches running deployment code rather than only its
configuration, and §6 is amended accordingly. It is not optional: neither the
race, the starvation, nor the fail-open branch can be closed from the
dispatcher's side alone.

A job that cannot be admitted stays `QUEUED`. Refusal is for invalid jobs, not
for contention.

NC8 still needs a heavy job before any heavy *kind* exists, and now gets one the
honest way: a `test` job requesting more than the light ceiling **is** heavy.
The control therefore exercises the real admission rule rather than a flag that
existed for the test's benefit.

### D11 — Image identity is a content key over its trusted inputs

The image is tagged `qf-trainer-env:<key>` where `<key>` is the first 16 hex of
`sha256(base_image_digest ‖ Dockerfile ‖ pyproject.toml ‖ uv.lock)`, all four
read from the trusted checkout. The build context is a temporary directory the
dispatcher constructs containing **exactly those three files** — asserted, not
assumed — so no file from `qf-research` can participate in dependency
resolution or in any build step. The base image is pinned by digest.
`uv sync --locked --no-install-project`: `--locked` asserts lock and manifest
agree instead of trusting the lock the way `--frozen` does, and
`--no-install-project` keeps a future `[build-system]` from executing even if a
research manifest were somehow reached.

Honest limit: the image is **not bit-reproducible**, because the build runs
`apt-get install libgomp1` unpinned. The recorded identity is therefore the
built image's config digest (`image_digest`), pinned per job, and the content
key is what decides whether a rebuild is needed. Making the apt layer
reproducible is a real improvement and is not on Phase 2's critical path.

### D12 — Job specs are closed-world typed JSON, and no field ever reaches a shell

Unknown keys are rejected. Every field is typed and range-checked. Arguments
become `argv` elements, never a command string. **There is no `lane` field**:
the lane is derived from `mem_limit` by the dispatcher (D10), because a
caller-selected lane is a caller-selected concurrency limit. Defaults come from the kind, and
the **effective** spec — after defaults are applied — is what gets hashed and
stored, so the record is complete rather than merely faithful to what was typed.

`test` selection is deliberately narrow: `paths` (relative, inside `trainer/`,
no `..`), a single optional `-k` expression, and `pytest_args` restricted to a
fixed allowlist of flags. Not because argv injection is a shell risk here, but
because `--pdb` on an unattended runner is a wedged 22 GB slot.

### D13 — Retention: run directories 90 days, pruned by a timer

Per parent §4.3. In 2a there is nothing to keep beyond that window — no
`eval.parquet` exists yet — so the pruner removes whole run directories and
leaves the SQLite record, which is small and permanent. Parent §16.7 (long-term
`eval.parquet` retention, ~935k rows per cohort per run) stays open and is 2c's
to answer with a measured size.

### D14 — Control numbering

The parent's §13.1 lists 8–11 for Phase 2. Two of those (9: `contract_hash`
disagreement; 11: `row_id` multiset mismatch) name artifacts that do not exist
until 2c, and three controls the sub-phases need do not exist in the list at
all.
So:

| Control | Assertion | Lands |
|---|---|---|
| NC8 | A second concurrent heavy job cannot start, including against a lock held by `daily_walk_forward.sh` | 2a |
| NC10 | Every trusted path resolves inside the trusted checkout; a job cannot redirect one into the research worktree | 2a, extended in 2c |
| NC12 | **New.** Build provenance: a poisoned `pyproject.toml` committed to `qf-research` provably does not affect the built image | 2a |
| NC13 | **New.** Sandbox isolation, asserted from *inside* the dispatcher-built container: no network, no container-runtime socket, read-only source, no `DATABASE_URL`, no `.env`, cannot write `trainer/data/models` | 2a, extended in 2b |
| NC14 | **New.** The dispatcher's read-only token authenticates (canary) and cannot push, create a ref, or open a pull request on `qf-research` | 2a |
| NC15 | **New.** Disk containment: a job that writes without bound is killed at its output quota and at its log cap, and a full runs filesystem stops admission instead of scheduling into it | 2a |
| NC16 | **Implemented in 2a and undocumented until 2026-08-27.** `docker create` then `start` relays the container's exit status; no container survives a terminal run; every resource row is released | 2a |
| NC17 | **New.** The database credential is unreachable from both `qfd` and `research`, each refusal measured against a positive canary proving `qfextract` can | 2b-1 |
| NC18 | **New.** A request naming anything outside D17 is refused; a published extract is IMMUTABLE -- re-requesting a `request_hash` is served byte-identically and a second extraction under an existing `request_hash` is refused rather than overwritten; bumping `generation` yields a separate artifact | 2b-1 |
| NC9 | A job whose `contract_hash` disagrees with the trusted checkout is refused | 2c |
| NC11 | A prediction set whose `row_id` multiset does not match the frozen extract is refused | 2c |

NC10 in 2a covers the paths that exist in 2a — the Dockerfile, the two
dependency manifests, the dispatcher's own module path, and the in-container
self-test script. In 2c it grows to contracts, evaluators and linters. Scoping
it to what exists is not a weakening: the *rule* being tested is "trusted paths
resolve from the trusted root", and it is tested on every trusted path there
is.

The parent's gate — all controls passing as a single run before Phase 4 — is
unchanged and becomes 2d's acceptance.

---

## 4. Phase 2a in detail

### 4.1 Components

```
$TRUSTED = /srv/queue-forecasting            (root-owned mirror, phase1 §4.1)
$TRUSTED/tools/queue-forecasting/host/dispatcher/
  qfd.py                 daemon: socket server, scheduler, runner supervision
  spec.py                closed-world job spec validation + canonical hashing
  store.py               SQLite schema, hash chain, atomic dequeue-with-lease
  source.py              mirror fetch, reachability check, worktree add/remove
  image.py               content key, build-context construction, docker build
  sandbox.py             docker argv construction (the D2 flag set)
  qf                     client CLI (stdlib python, runs as the caller)
  trainer-env.Dockerfile trusted Dockerfile (D8, D11)
  env/pyproject.toml     human-promoted manifests (parent §3.4 step 3)
  env/uv.lock
  nc13-inside.sh         in-container assertions for NC13
  handoff-inside.sh      trusted artifact normalisation, runs as uid 10001 (D9)
  qf-dispatch.service    the unit (D5; PrivateTmp=no)
  qf-runs-prune.service  + .timer (D13)
  qf-locks.conf          systemd-tmpfiles: the shared lock inodes (D5)
  tests/                 stdlib unittest, no network, no privileges

/var/lib/qf-platform/    qfd-owned: state.db (WAL), qf-research.git (bare mirror)
/var/lib/qf-locks/       0755 root:root — outside qf-platform, which is 0750 qfd:qfd
/var/lib/qf-locks/heavy-training.lock   0660 root:qfheavy (D5)
/var/lib/qf-locks/intent.d/              2770 root:qfheavy (setgid) — nightly's
                         intent markers; NOT a lock, so they cannot be barged (D10a)
/var/lib/qf-locks/intent.d/nightly.<pid>.<epoch>.intent   0640, published by
                         rename, removed by its own invocation's trap
/var/lib/qf-platform/build.lock          qfd-private: one build at a time (D10)
/var/lib/qf-runs/        0750 qfd:qfclient — chgrp'd at startup; StateDirectory
                         creates it qfd:qfd, which clients cannot traverse
/var/lib/qf-runs/<id>/   qfd-owned 0750 qfd:qfclient
  spec.json  src/           run inputs
  out/                      2770 qfd:qfrun — untrusted writes, never client-read
  artifacts/                0750 qfd:qfclient, files 0640 — the handoff output
  logs/{stdout,stderr}      capped by the dispatcher (§4.6)
  result.json
/run/qf-dispatch/        0711 qfd:qfd — traversable, NOT listable
/run/qf-dispatch/client/ 0750 qfd:qfclient   → sock 0660 qfd:qfclient
/run/qf-dispatch/admin/  0750 qfd:qfheavy    → sock 0660 qfd:qfheavy
                         (`research` is in qfclient, not qfheavy)
/etc/qf-dispatch/github-token   0400 qfd:qfd
```

The dispatcher's code is **executed from the trusted checkout**, not copied
elsewhere. Updating it is a human `git fetch` on the mirror plus
`systemctl restart qf-dispatch` — the same trust path as every other control.

### 4.2 State

```sql
jobs(run_id PK, kind, lane, state, spec_json, spec_hash, source_sha, source_ref,
     image_digest, submitted_by_uid, submitted_at, started_at, finished_at,
     hold_started_at, hold_deadline_at, attempts, lease_owner, lease_expires_at,
     container_id, exit_code, error_class, wall_s, rss_high_water_kb)
resources(run_id, role, container_id, created_at, released_at)
                                      -- candidate | handoff. No builder role:
                                      -- the classic builder creates no container
                                      -- of ours to track (D10).*
                                      -- `jobs.container_id` is candidate-only,
                                      -- so forced cleanup and restart recovery
                                      -- inventory THIS, not a label query (D10)
pins(run_id, key, value)              -- contract_hash, extract_hash, prereg_hash…
artifacts(run_id, name, path, sha256, bytes)
events(seq PK AUTOINCREMENT, at, run_id, kind, payload_json, prev_hash, hash)
schema_meta(key, value)
```

`lane` is stored because it is derived at submission from `mem_limit` (D10), not
because a caller supplied it.

States: `QUEUED → LEASED → RUNNING → SUCCEEDED | FAILED | TIMEOUT | CANCELLED`,
plus `REFUSED` as a terminal state reached only from submission, plus **`BUILDING`** (`LEASED → BUILDING → RUNNING`), because an unconfirmed
build abandonment has to reach a terminal or blocked state and `LEASED` had no
such edge —
revision 8 specified a transition the state table forbade. And plus
**`CLEANUP_BLOCKED`** — non-terminal, reachable from `BUILDING` or `RUNNING`, and the state
of a job whose workload could not be *confirmed* dead. It is deliberately not
terminal: it still holds a training-lock descriptor and a memory reservation, and
admission is defined to last until a terminal state, so calling it `FAILED` (as
revision 7 did) both broke that rule and left no answer to "what if Docker
confirms death a second after we gave up". A reaper re-polls; confirmation moves
it to `FAILED` and releases everything; while any job sits in it, nothing is
admitted; and `qf force-release` is the operator escape — **on a separate
root/deploy-only admin socket**, not the client socket. Revision 8 put it on the
`qfclient` socket, whose members include `research`: the untrusted agent could
have claimed to have verified shutdown and released the mutex over live work.
Recording the caller's uid is audit, not authorisation, and the two must not be
confused again. The admin socket is `/run/qf-dispatch/admin.sock`, mode `0660
qfd:qfheavy` (`research` is not in `qfheavy`), and NC8 asserts that `research` is
refused. Transitions are
table-driven and an illegal one is a programming error, not a logged warning.

**Admission happens before the dequeue, so there is no `LEASED → QUEUED`.** The
first revision had a failed `flock` return a `LEASED` job to `QUEUED`, which the
state table forbids — a contradiction that would have surfaced as a crash under
ordinary contention. The scheduler instead **peeks** (a read-only look at the
lane's head), acquires the admission it needs (memory budget, and the training
lock for `heavy`), and only then dequeues atomically. If the dequeue loses a
race it releases the admission and re-peeks. No defer state, no illegal
transition, and the lock is never held while idle.

**Leases are absolute, renewed, and never the sole basis for reclaiming.**
`lease_expires_at` holds an absolute UTC instant (`now + lease_s`), not a
duration — the first revision's dequeue stored the duration itself and returned
the pre-update row, so every lease was both malformed and stale. The runner
renews at one third of the lease interval with an ownership-checked update
(`WHERE run_id = ? AND lease_owner = ?`), and a job may last one hour
(`TIMEOUT_MAX` = 3600 s in 2a — one hour, not the "four hours" earlier
revisions wrote), so
without renewal any sane lease would expire mid-run. Reclamation additionally
requires a **Docker state check**: a `RUNNING` job whose container is alive is
re-adopted and its lease extended, never reclaimed. Expiry alone reaps live
work.

**State-dependent queries read from named state sets, never from an inline
list.** `ADMITTED_STATES` and `LEASE_ACTIVE_STATES` are defined once and used by
lane occupancy, lease renewal and reclamation. Adding `BUILDING` to the
transition table and to nothing else — revision 9 — made a building job vacate its
lane, fail to renew its lease, and disappear from reclamation: three bugs from one
omission, none of which changes any transition. A test asserts the sets cover
every key of the transition table, so a new state cannot be added without landing
in them.

**One thread owns the database.** `sqlite3.connect()` binds a connection to its
creating thread, so a scheduler thread plus socket handling sharing one
connection raises `ProgrammingError` — and one blocking thread per lane cannot
provide two light workers anyway. So: a single **DB-owner thread** serving
requests over a queue (which makes "single writer" literal and serialises the
hash chain for free), a socket-accept thread, and a worker pool of three (two
light, one heavy). The concurrency tests drive real threads against a real
SQLite file, not the fake runner.

`pins` exists in 2a with no rows for most keys. That is the point: 2b adds
`extract_hash` and `data_watermark`, 2c adds `contract_hash` and
`evaluator_hash`, Phase 3 adds `prereg_hash`, and none of them is a migration.

Crash recovery has two layers. The lease is the backstop; the primary mechanism
is **reconciliation against Docker**: every container is labelled
`qf.run_id=<id>`, so on startup the dispatcher lists containers by label and
either re-adopts a live run or marks a dead one failed with its logs preserved.
A timeout-guessed lease alone would either reap live jobs or strand dead ones.

**Reconciliation starts from the database, not from `docker ps`.** Revision 8
reconstructed state only from live labelled containers, which loses any job whose
containers have since stopped — most importantly a `CLEANUP_BLOCKED` one. If
confirmation arrives while `qfd` is down, nothing is discovered, the persisted job
stays `CLEANUP_BLOCKED`, and the no-admissions rule stops the loop **forever**. So
startup first enumerates every **non-terminal job in SQLite** (`LEASED`,
`BUILDING`, `RUNNING`, `CLEANUP_BLOCKED`), re-charges its admission and
re-acquires its lock, and only then asks Docker about liveness — resuming the
reaper for anything still alive, and moving anything confirmed stopped to `FAILED`
with its resources released.

**Reconciliation must also rebuild the resource state, because both halves of it
are process-local.** Admitted memory is a counter in the dead process, and
`flock` is released when its file descriptor closes — while the containers keep
running. So, on startup and **before any worker starts**, in this order:

1. **Enumerate every non-terminal job from SQLite** — `LEASED`, `BUILDING`,
   `RUNNING`, `CLEANUP_BLOCKED`. Not from `docker ps`: a job whose containers have
   already stopped is invisible there, and a `CLEANUP_BLOCKED` one that died
   during the outage would never be found, leaving the no-admissions rule to stall
   the loop permanently.
2. **Re-acquire each job's lane-appropriate lock — `LOCK_EX` heavy, `LOCK_SH`
   light — on its own fresh descriptor, before any cleanup runs.** Both lanes:
   revision 4 covered heavy only, leaving an orphaned light container with no
   `LOCK_SH` while nightly could take `LOCK_EX`. A nightly incumbent blocking
   acquisition sends the job straight to the `mutex_lost` kill-and-confirm path.
3. **Re-charge the job's original logical reservation** —
   `max(mem_limit, IMAGE_BUILD_MEM_MB)` from its stored spec, taking a live
   container's larger cap if one exists. Charging the live container's own cap
   undercharges a `BUILDING` job to its builder's 2 GB.
4. **Restore the remaining hold deadline from the database, not the clock.**
   `hold_started_at`/`hold_deadline_at` are written at dequeue, carried in the
   event payload, and projected like every other column; revision 6 kept the
   deadline in the dead process's memory, so repeated restarts could extend one
   hold without limit. An already-expired deadline runs forced cleanup at once.
5. **Resolve `CLEANUP_BLOCKED` and `BUILDING` jobs — and here an empty inventory
   is not a confirmation.** Where a job has recorded containers, "all confirmed
   stopped" is a real check. Where it has none, that sentence is *vacuously true*,
   and acting on it would release the lock and reservation of a
   `BUILDING` job — which under the classic builder owns no container of ours at
   all — the instant a restart discovered it. So a `BUILDING` job **keeps** its
   reconstructed lock and reservation and goes through the same
   cancellation-settle procedure as any abandoned build (D10: no live `docker
   build` client, then `BUILD_SETTLE_S`) before it may become `FAILED`. Anything
   still alive resumes the reaper. This invariant — *confirmation over an empty
   set is not confirmation* — applies equally to `reclaim` and to forced cleanup,
   and is stated once because a vacuous truth over an empty set is the easiest
   kind of bug to write and the hardest to see.
6. Only then start the worker pool. `ExecStopPost` stops labelled containers on a
   clean shutdown, so an ordinary restart leaves nothing to re-adopt.

The residual is an **unclean** death (SIGKILL, kernel OOM, panic): between it
and the restart the mutex is unheld, and a nightly job starting in that window
overlaps a live 22 GB container until step 2 kills it. Bounded by `RestartSec`
and by the nightly job's own start time; recorded in §7 rather than papered
over.

### 4.3 The job spec

```json
{
  "schema": 1,
  "kind": "test",
  "source_sha": "3f1c…40 hex…",
  "args": { "paths": ["tests"], "k": null, "pytest_args": ["-q"] },
  "timeout_s": 1800,
  "mem_limit": "4g",
  "cpus": 4.0,
  "note": "baseline check before H-0031"
}
```

No `lane`: it is derived from `mem_limit` (D10). `4g` is at the light ceiling, so
this job is light; the same spec at `8g` is heavy and takes the training lock.

`selftest` takes `"args": {}` and is the NC13 vehicle: its entrypoint is
`nc13-inside.sh`, mounted read-only **from the trusted checkout**, so the
assertions run in the real dispatcher-built sandbox rather than in a
hand-rolled `docker run` that merely resembles it. A negative control that
tests a copy of the flags is worthless the first time the two drift.

### 4.4 The sandbox, and the details that are easy to get wrong

**Every container carries `qf.run_id` and `qf.role`.** `candidate` and
`handoff`. The forced-release path is a label query — "is everything for this run
stopped?" — so a container missing a label is a container the inventory cannot
see, and revision 7 labelled only the candidate. A handoff running under an
unlabelled name would have let "all stopped" return true while it wrote.

**Ownership across the boundary.** The container runs as `10001:10001` and must
write `out/`, but `qfd` is unprivileged and cannot `chown` to another uid. So
`qfd` is a member of group `qfrun` (gid 10001) and creates `out/` as
`qfd:qfrun`, mode `2770`. The container writes by group. It does **not** follow
that the dispatcher can read what lands there — see D9; the files' modes belong
to untrusted code, which is why the handoff container exists. Running the
container as `qfd`'s own uid instead would be simpler and strictly worse: a
container escape would land on the uid that is in the `docker` group.

**The heavy lock's inode and its permissions.** See D5: a shared namespace is
not shared permission, and the incumbent is the process that gets killed if this
is got wrong. NC8 asserts the property rather than the setting, in **both
creation orders**, as the real users.

### 4.5 Disk is a containment boundary too

`--read-only` and `--network none` bound what untrusted code can reach; they do
nothing about how much it can write. Left unbounded, a `test` job has up to four
hours to fill `/var/lib/qf-runs` — or, through its stdout, whichever filesystem
holds the log files and Docker's own logging storage. That is a host-availability
escape from a sandbox that is otherwise tight, and the 90-day pruner is no help
against something acute. Four measures, in order of how much they buy:

1. **Capped log capture.** The dispatcher writes stdout and stderr through a
   bounded writer: at `LOG_CAP` (16 MiB per stream) it stops writing, records
   `error_class=log_overflow`, and kills the container. Docker's own log driver
   is set to `none` for experiment containers, since the dispatcher already has
   the streams — otherwise a capped file still lets the daemon's log store grow.
2. **A per-run output quota.** The runner samples `out/` size every two seconds
   and kills the job at `OUT_QUOTA` (kind-specific; 1 GiB for `test`). Sampling
   is racy against a very fast writer and is honestly a bound rather than a
   guarantee.
3. **Filesystem-enforced quota where available.** If `/var/lib/qf-runs` is on
   XFS with project quotas, or a filesystem that can enforce a per-directory
   limit, that replaces measure 2 with a real one. Task 10's discovery step
   reports what is available rather than assuming.
4. **Free-space admission.** A job is not admitted unless free space on the runs
   filesystem exceeds `DISK_FLOOR` plus its full allowance — which is
   `OUT_QUOTA + ARTIFACT_CAP`, since the handoff duplicates output into
   `artifacts/` before `out/` is pruned (D9). A full disk stops scheduling
   instead of corrupting the store.

This gets its own negative control (NC15), because "the sandbox cannot hurt the
host" is exactly the kind of claim that should not rest on a code reading.

### 4.6 Interfaces frozen now for later sub-phases

Stated here so 2b and 2c are implementations rather than redesigns.

**Row identity, frozen once.** The parent §8.5 says a candidate emits
`row_id, p50, p90_raw`; the first revision of this document said `task_id,
run_id` while NC11 still compared `row_id` multisets. Both cannot be the
comparison key. Frozen:

- The identity is the ordered pair `(task_id, run_id)`, which is what
  `queue_forecast_task_runs` and the baseline NDJSON join on today.
- `row_id` is a **derived canonical serialisation**, `f"{task_id}:{run_id}"`,
  UTF-8, and it is what NC11's multiset comparison uses. It is written into both
  the extract's eval rows and the prediction file so the comparison needs no
  reconstruction.
- **Parquet types are part of the contract:** `task_id` `string` non-null,
  `run_id` `int32` non-null, `row_id` `string` non-null, `p50` and `p90_raw`
  `double` non-null and finite. A null or a NaN is a refusal.
- **Duplicates are a refusal, not a dedup.** A prediction set with a repeated
  `row_id` is rejected; silently keeping one of them is how a candidate would
  drop rows it scores badly on.

**Predictions.** `out/predictions.parquet`, columns `task_id`, `run_id`,
`row_id`, `p50`, `p90_raw`, subject to the types above. Nothing else is read
from a candidate run for scoring.

**Extract.** `extract/*.parquet` plus `extract/MANIFEST.json` carrying, per
file, `sha256`, `rows`, `window`, `watermark`, and — once per extract — the
`REPEATABLE READ` snapshot's start timestamp and transaction id (D4); mounted
read-only at `/extract`. `extract_hash` is the digest of the canonicalised
manifest and every member of a comparison must share it. The extraction request
itself is closed-world and typed: target, window, watermark, and nothing a
research config could influence.
- **Writable `data/`.** The research worktree is mounted read-only at
  `/app/trainer`, with a writable run-private directory mounted over
  `/app/trainer/data`, because `CACHE_DIR` and the model output path are
  computed relative to the module today. This nested mount is the reason 2b
  needs no path refactor inside `qf-research`.
- **Pins.** New pin keys, never new columns.

---

## 5. Acceptance for 2a

1. `python3 -m unittest discover` under `host/dispatcher/tests` passes with no
   network and no privileges, and the suite includes the specific cases listed
   in the plan's Tasks 1–6.
2. `qf submit --kind test --sha <published sha>` as `research`, with no SSH and
   no `docker` access, runs the trainer's pytest suite in the sandbox and
   returns its exit code; `qf status` shows the terminal state; the run
   directory holds captured stdout/stderr and a `result.json`.
3. The SHA requirement is real: a SHA that exists locally but is not reachable
   from a remote-tracking ref is refused, and the refusal names why.
4. `qf verify-chain` recomputes the event chain, replays it into a projection,
   and reports agreement with `jobs`. A row edited directly in `jobs` makes it
   report disagreement.
5. **NC8:** with the training lock held by an unrelated process, a heavy job
   stays `QUEUED` and starts once it is released; two heavy jobs never run
   concurrently; any two names for the lock resolve to one device and inode;
   neither runtime user can unlink or recreate the inode while both can
   write-open and mutually `flock` it; `research` cannot open it at all; and a
   22 GB heavy job and a 4 GB light job never overlap. The protocol is then
   asserted against both failures found by experiment (D10a):
   *(a)* while a light job holds `LOCK_SH`, a stand-in nightly `flock -w` waits
   and then proceeds rather than exiting;
   *(b)* **the gate cannot be barged**, which is what revision 5's version could
   not show. With one light job already running and the nightly marker then
   placed, **repeated** admission attempts must all be refused while the nightly
   waiter is queued — the test must try to barge, not merely observe that
   admissions stopped once nightly already held something. Tested with two light
   jobs overlapping and handing off, the case a bounded wait alone fails. A
   **stale** marker (dead PID or expired deadline) must be logged, removed and
   ignored; and `research` must be unable to create or delete a marker at all;
   *(c)* with two light jobs running, one finishing does **not** release the
   other's `LOCK_SH`, so nightly stays blocked until the second finishes — the
   per-descriptor property, which a shared descriptor would fail while passing
   (a) and (b);
   *(d)* `flock` genuinely works on that filesystem, since on some it is a no-op
   and every guarantee above would be vacuous;
   *(e)* **orphan recovery, in two separate runs — one light, one heavy.** A
   correct dispatcher can never have a light and a heavy orphan alive together (a
   heavy job holds `LOCK_EX`), and one descriptor cannot hold `SH` and `EX` at
   once, so they are tested apart: `SIGKILL` with a light orphan → `LOCK_SH`
   re-acquired; `SIGKILL` with a heavy orphan → `LOCK_EX` re-acquired; each
   repeated with a stand-in nightly holding the lock across the restart, asserting
   the orphan is killed with `error_class=mutex_lost`;
   *(f)* **the nightly wrapper refuses to run without `flock`** — with `flock`
   off its `PATH` it exits non-zero *before* training, rather than warning and
   proceeding as `daily_walk_forward.sh:218` does today;
   *(g)* a job that overruns `JOB_HOLD_DEADLINE_S` is killed **and the descriptor
   is closed only after Docker confirms every container for that run is stopped**.
   The test includes a **timed-out `docker kill`**: with confirmation withheld,
   the lock must **remain held** and the job recorded `kill_unconfirmed`, rather
   than the descriptor being closed on the strength of a CLI that merely stopped
   waiting. The recovery half is asserted too: confirmation arriving *after* the
   300-second failure moves the job out of `CLEANUP_BLOCKED` to `FAILED`, releases
   the reservation and resumes admissions with no operator action; and the whole
   clause is repeated with the deadline expiring **during the handoff**, which
   only passes if that container is labelled;
   *(g2)* **an unconfirmed builder shutdown** takes the same path: the job enters
   `CLEANUP_BLOCKED` from `BUILDING`, keeps its lock and reservation, and the
   reaper releases both once the builder container is confirmed gone;
   *(g3)* **restart while `CLEANUP_BLOCKED`**, both ways: with the workload still
   live (reaping resumes) and with it confirmed stopped while `qfd` was down (the
   job reaches `FAILED`, resources release, admissions resume). The second is what
   revision 8 would have stalled on forever, since nothing live existed for
   `docker ps` to find;
   *(g4)* **`research` is refused `force-release`** on the admin socket, and the
   client socket does not carry the operation at all. Both sockets also get
   **positive canaries**, because a refusal proves nothing if nothing can connect:
   `research` must reach the client socket and the deploy user must reach the
   admin socket. Revision 9's `0750 qfd:qfd` parent directory made both
   unreachable and no check would have noticed;
   *(g6)* a **`BUILDING`** job occupies its lane, renews its lease, and is
   reclaimed only after every recorded resource is confirmed stopped; and a
   restart during `BUILDING` recharges the job's **full** reservation, not its
   builder's cap;
   *(g5)* after a build, `docker image inspect` returns **the exact image id**
   handed to the sandbox — the classic builder lands the tag locally, and this
   check is what would catch a future switch to a driver that does not;
   *(g5b)* an **abandoned build** leaves no `docker build` client alive, and the
   descriptor is released only after `BUILD_SETTLE_S` — the weaker-confirmation
   path D10 accepts knowingly;
   *(h)* the intent marker's concurrency properties: two nightly invocations at
   once do not delete each other's declaration; a half-written or unreadable
   marker fails **closed** (no admission) and is alarmed; and a stale-marker
   removal racing a fresh declaration does not remove the new one.
6. **NC10:** a job cannot cause the Dockerfile, either dependency manifest, the
   dispatcher module path, or `nc13-inside.sh` to resolve outside
   `$TRUSTED`; each attempt is refused and named.
7. **NC12:** a scratch branch of `qf-research` carrying a poisoned
   `trainer/pyproject.toml` (a `[build-system]` table and a bogus dependency)
   produces a `test` job whose image content key is **byte-identical** to the
   pre-poison key, whose build context provably contained exactly three files,
   and in whose environment the bogus dependency is absent. A **22 GB job with a
   cold image cache** also completes, because one reservation of
   `max(mem_limit, IMAGE_BUILD_MEM_MB)` covers both phases rather than summing
   them (D10) — a regression test for a deadlock that only appears at full size.
8. **NC13:** `qf submit --kind selftest` passes every in-container assertion:
   no DNS, no outbound TCP, no container-runtime socket, `/app/trainer` not
   writable, `DATABASE_URL` unset, no readable `.env`, `trainer/data/models`
   unreachable. Each assertion has a canary proving the attempt was possible,
   and a VOID is a failure.
9. **NC14:** the dispatcher's token authenticates against `qf-research`
   (canary) and **each** of a git smart-HTTP push to a disposable ref, a REST
   ref creation, and a pull-request creation is refused, scored with Phase 1's
   `nc7-lib.sh` so a 422 or a 5xx cannot certify containment. A read credential
   whose scope is never asserted is just a credential.
10. **NC15:** a job that writes an unbounded stream to stdout is killed at the
    log cap with `error_class=log_overflow`; a job that fills `out/` is killed
    at its quota; and with free space below `DISK_FLOOR` a new job is not
    admitted. The host filesystem survives all three.
11. `qf verify-chain` detects an edit to **every** column of `jobs` —
    `spec_json`, `source_sha`, `image_digest`, `exit_code`, a timestamp, the
    resource high-water mark, `lease_owner`, `lease_expires_at` — and to a
    `pins` row and to an artifact's path, size or digest.
12. Restart recovery, against a **deliberately orphaned** container — `SIGKILL`
    the daemon, or start it with an orphan already present, since `ExecStopPost`
    means an ordinary restart leaves nothing to re-adopt — and in **both lanes**,
    because revision 4 re-acquired a lock only for heavy orphans. Startup
    re-charges admission from each container's own `HostConfig.Memory` and
    re-acquires that container's lock in its own mode on a fresh descriptor; any
    container whose lock cannot be re-acquired is killed with
    `error_class=mutex_lost`. **And the hold deadline survives:** restarting with
    an orphan whose `hold_deadline_at` is nearly or already past must run the
    forced-cleanup path rather than granting a fresh budget — otherwise repeated
    restarts extend the hold without limit.
13. A build that fails and a build that exceeds `BUILD_TIMEOUT_S` both leave the
    job `FAILED` with `image_build_failed` / `image_build_timeout`, and two light
    workers missing the same content key produce **one** build, not two.
14. Phase 0 and Phase 1 suites (`nc-suite.sh`, `nc7-suite.sh`) still exit 0 with
    `failed=0`, and their evidence files are refreshed.
15. **Fault gate A — kill `qfd` during a long build.** With a deliberately slow
    classic build in flight, kill the dispatcher three ways: subprocess timeout,
    direct `SIGKILL`, and `systemctl stop`. In each case the `docker build` client
    must be gone, daemon-side work must cancel within `BUILD_SETTLE_S`, and the
    restart must **not** release the `BUILDING` job's lock or reservation merely
    because it has no `resources` row — it must run the cancellation-settle
    procedure first. Any measured cancellation beyond the window triggers the D10
    decision rule.
16. **Fault gate B — crash after each startup phase.** Fault-inject a crash after
    each of the five reconciliation phases (enumerate, lock, recharge, deadline,
    resolve-blocked) and restart. On every restart, assert one of exactly two
    outcomes: resources remain **held**, or verified cleanup **completes**. Never
    an intermediate release. This is the gate that would have caught the
    empty-inventory defect, and it is where the residual uncertainty in this
    design now lives.
17. The live stack is undisturbed: collector, live-predictor and dashboard
    container uptimes span the whole phase, and the nightly walk-forward
    completes normally on the day 2a lands.
18. Evidence in `host/nc-evidence-phase2a.txt`, containing no token and no URL
    userinfo — checked by the suite, per Phase 1 §7.2.

---

## 6. Explicitly deferred from 2a

Contracts and `contract_hash`; the extractor and any Postgres access from the
dispatcher; baseline artifacts; the predictions-only trainer change; the
evaluator, `eval.parquet`, `verdict.py`, and the independent derivation;
`screen`/`confirm`/`probe`/`query`/`summarize`; pre-registration; the bus
`note` operation; multi-cohort sweep composition; anything touching
`trainer/data/models/` or the live predictor.

The frozen trainer in `$DEPLOY_DIR` is **not modified by 2a**. Its nightly
wrapper is: `daily_walk_forward.sh`'s locking becomes an intent-then-wait
sequence (D10a), because the mutex cannot be made correct or starvation-free
from the dispatcher's side alone. Revisions 1–3 listed that script as untouched,
which was true only while the mutex was broken. Everything else about the
nightly path — the sweep, the configs, the manifests — is unchanged.

---

## 7. Residual risks

1. **The dispatcher is in the `docker` group, which is root-equivalent** (D5).
   Bounded by systemd hardening and a small stdlib-only closure; not eliminated.
   This is the standing argument for revisiting D2 at Phase 5.
2. **A second credential exists.** `Contents: read` on one private repository,
   mode 0400, owned by a system user, never on a command line or in evidence.
   Its scope is asserted by NC14 rather than assumed. It still needs a
   rotation owner, and it is a real widening of the credential inventory that
   Phase 1 deliberately narrowed to one.
3. **The image is not bit-reproducible** (D11). Job-level pinning records what
   ran; it does not let a third party rebuild it byte-for-byte.
4. **The mirror can lag.** Phase 1 §4.1 already carries this for controls; 2a
   adds that the *dispatcher's own code* comes from the mirror, so a human who
   pulls the mirror without restarting the unit runs the old dispatcher. The
   installer prints the running version's commit, and `qf ping` returns it.
5. **Symlinks in the research worktree.** They resolve inside the container's
   mount namespace, so a symlink to `/etc/passwd` reads the container's file,
   not the host's. Harmless today; it stops being harmless the moment anything
   host-side reads paths out of the worktree, which is precisely what NC10
   forbids.
6. **The hash chain is unanchored** (D7). It detects edits by anyone who
   cannot recompute it; `qfd` can recompute it. External anchoring of chain
   heads is the fix and is an open decision, not a shipped property.
7. **A nightly run can be delayed by up to `JOB_HOLD_DEADLINE_S`** (D10a). The
   intent marker bounds the delay to the in-flight holds rather than an unbounded
   chain, but on a day when a long heavy job is running the nightly sweep starts
   late. The levers are `TIMEOUT_MAX` and the deadline itself, both deliberately
   low in 2a.
8. **An unconfirmable kill holds the mutex past its deadline** (D10a). The
   dispatcher stops admitting and alarms, and a nightly run can skip its night.
   Chosen deliberately: a lock released over live work is the failure this design
   exists to prevent, and a late sweep is recoverable. The related residual is
   the builder container, which is why builds moved out of the daemon (D10): a
   per-build `qf-build-<run_id>` builder can be inspected by container id and
   confirmed stopped, whereas in-daemon BuildKit work cannot, and "bounded by
   `BUILD_TIMEOUT_S`" said nothing once the job went terminal and released its
   reservation. The residual that remains: the builder is a managed container
   created by buildx, so its identity comes from `buildx create`'s output rather
   than from a label we control, and that output has to be captured reliably or the
   inventory has a hole.
9. **A crashed nightly leaves a stale intent marker** (D10a). It is reclaimed on
   PID-liveness or deadline expiry, so the loop recovers by itself — but during
   that window the dispatcher may admit work a live nightly would have blocked.
   That costs a delayed nightly run, never a memory overlap, because the training
   lock still governs execution.
10. **An unclean dispatcher death briefly leaves the mutex unheld** (§4.2). The
   startup check closes it by killing its own re-adopted heavy container, so the
   overlap is bounded rather than absent. Making heavy containers die with the
   dispatcher would remove it and is not something Docker offers directly.
11. **Two of the four disk measures are bounds, not guarantees** (§4.5). The
   `out/` sampler races a fast writer, and filesystem-enforced quotas depend on
   what `/var/lib/qf-runs` actually sits on. Task 10 reports it; if the answer
   is "no enforceable quota", the residual is a job that can transiently exceed
   its allowance before being killed.
12. **The trainer's own tests may not be sandbox-clean.** Any test that expects
   a writable source tree or a live `DATABASE_URL` will fail in the sandbox.
   Discovering that is part of 2a's value, and the fix belongs in
   `qf-research`, not in the sandbox.

---

## 8. Open decisions carried forward

Unchanged from the parent unless noted.

| # | Decision | Owner |
|---|---|---|
| §16.1 | Container runtime | **Closed by D2** (rootful Docker; revisit at Phase 5) |
| §16.2 | Cron cadence | Phase 4 — no scheduler exists in Phase 2 |
| §16.3 | Screen cohort set and subsample rate | 2d, fixed in the contract before the first screen |
| §16.4 | Usage-budget backoff policy | Phase 4 |
| §16.5 | Deploy step: poll or webhook | Phase 6 |
| §16.6 | May the audit tick re-open `REFUTED`? | Phase 4 |
| §16.7 | `eval.parquet` retention | 2c, with a measured size |
| §16.8 | Minimum `n_eff` per target | Phase 3 |
| §16.9 | Block-length rule | Phase 3 |
| new | Base-image digest pin, and who refreshes it | 2a install step; recorded in `env/`, refreshed with the manifests |
| new | Read-token rotation owner and cadence | 2a install step |
| new | External anchoring of hash-chain heads, if tamper-evidence against `qfd` itself is required (D7) | deferred; decide before Phase 5 grants autonomy |
| new | Whether `/var/lib/qf-runs` can carry filesystem-enforced per-run quotas, or the sampler is the only bound (§4.5) | 2a Task 10 discovery |
| new | Raising `TIMEOUT_MAX` above 3600 s for 2b/2d, which requires moving `BUILD_TIMEOUT_S`, `JOB_HOLD_DEADLINE_S` and `LOCK_WAIT_S` in the same change (D10a) | 2b, once real cohort runtimes are known |
| new | Whether blocking all dispatcher work during the nightly window (D10) is too blunt once experiment volume rises; the alternative is a ~2 GB light ceiling | revisit after four weeks of Phase 0–3 operation |

---

## 8a. Decisions settled for 2b (2026-08-27)

Recorded here because they **amend D4's shape** rather than extend it; the detail
and the task breakdown live in `auto-research-phase2b-plan.md` (D15-D22).

| # | Decision | Why it is not just an elaboration of D4 |
|---|---|---|
| D15 | The extractor is a **third privilege domain** -- a dedicated `qfextract` user and systemd unit, outside both `qfd` and the sandbox. `qfd` may *request* an extraction and never holds the database credential. | D4 said "trusted code, dispatcher-side", which reads as "inside `qfd`". Two constraints forbid that: Parquet needs `pyarrow` and D6 pins `qfd` to the standard library; and `qfd` is in the `docker` group (D5), so anything `qfd` hands a container it can read back -- a containerised extractor launched by `qfd` therefore *cannot* satisfy "`qfd` never holds the credential". Stated honestly: docker-group membership is root-equivalent, so this is a least-privilege boundary, not a barrier against a compromised `qfd`. What it buys is that no `qfd` **defect** can disclose the DSN, and it becomes a real barrier if D2 is revisited at Phase 5. |
| D16 | The request channel is a **socket-activated unit**, and the request is validated **twice** -- by `qfd` for legibility, and by the extractor because a caller is a caller. | D4 specified the request's shape and not its delivery. A spool directory would need its own atomicity, absence-settling and liveness stories; 2a spent sixteen review rounds establishing that an absence is evidence only once something was asked and an answer came back. |
| D17 | `lookback_days` is a **field of the typed request**, bounded `1..120`; the anomaly flag subset is **not** a field, and the whole `queue_forecast_daily_health` row set is emitted instead. | `load_task_runs_for_queue_context` derives its floor from `c.lookback_days` today, so a trusted query consults the research repo for a window bound -- admissible under D4's "window" but only if made explicit. `load_anomalous_dates` builds its `WHERE` with an f-string over a config value; emitting the rows deletes that f-string rather than making it safe. |
| D18-D22 | Fixed column inventory; one `REPEATABLE READ` snapshot with `pg_current_snapshot()`; extract identity/reuse/retention; the sandbox read path; trusted baseline artifacts. | Elaborations of D4 and §4.6, detailed in the 2b plan. |
| D20 | **Extracts are immutable**, reused by `request_hash`; `as_of_date` must be a completed UTC boundary past a settlement lag; the watermark is **provenance, not a cache-validity oracle**; late data requires a new `generation`. | This is a correction, not an elaboration. D4 introduced the watermark to close the parent §7 hole where a re-extracted window silently picked up late rows -- but the collector's one-minute enrichment backfill can update a row *inside* an already-extracted window without moving `max(pending_at)` or `max(resolved_at)`. So an unchanged watermark does not prove identical input, and reuse keyed on it would serve a stale extract while reporting a hit. Reproducibility has to rest on immutability. |
| D23 | `max_parallel_workers_per_gather = 0` for the extractor, one extraction at a time, a measured `temp_file_limit`, and a free-space admission check. | `temp_file_limit` (20GB, set by Phase 0) is a **per-process** bound and parallel workers are separate processes, so the server's four-workers-per-gather default lets one query spill roughly five times the limit -- `work_mem = 512MB` multiplies identically. Setting the worker count to zero is what makes the existing limit a limit. |

**A grant-surface finding that D4 should not be read around.** Phase 0 grants
`SELECT` on **every table in schema `public`** (`phase0-setup.sh:509`), derived
from the live database rather than a named list. D4's rule that "a genuinely new
table or column is a human change, promoted into the trusted extractor" is
therefore enforced **only** by the enumerated inventory in trusted code (D18);
the grant does not constrain it. A dedicated `forecast_extract` role limited to
the five tables the extractor reads would make the grant an independent second
boundary -- an open decision, since narrowing `forecast_experiment` itself would
break the nightly trainer that shares it.

Two corrections to this document fall out of the 2b reading:

- **`runs.parquet` must select `started_at`.** D4's inventory derives from
  `_build_query`, which omits it, while `load_task_runs_for_queue_context`
  selects it and bet 2's censoring filters on it. The union rule means the
  widest superset wins.
- **`anomalous_dates.json` becomes `daily_health.parquet`.** A set of dates is
  the *result* of a config-dependent filter; the rows are the fact. The name
  changes so nothing reads a narrowed artifact expecting the old one.

---

## 9. Amendments to the parent design

`auto-research-loop-design.md` must be edited so a future session does not read
Phase 2 as one step. Grep it for `qf-platform` and `qf-service` first, per
Phase 1 §10's warning that the amendment tables are not self-verifying.

| Location | Stale content | Amendment |
|---|---|---|
| §3.4, job kinds | one flat list including `screen`, `confirm`, `probe`, `test`, `summarize`, `query` | annotate which sub-phase introduces each (2a: `test`, `selftest`; 2b: `extract`, `probe`, `query`; 2c: `evaluate`; 2d: `screen`, `confirm`, `summarize`) |
| §3.4 step 2 | "materialises the source with `git worktree add` at an exact SHA" — silent on where the objects come from | add D3: a dispatcher-owned bare mirror fetched from GitHub with a dispatcher-only `Contents: read` token; the SHA must be reachable from a remote-tracking ref; local `file://` fetch from the agent's clone is rejected and why |
| §3.4 step 3 | "no service credentials" but the trainer extracts from Postgres today | add D4: candidate code never holds a DB credential; a trusted extractor produces the frozen extract and the container runs `--network none` |
| §3.5 | three DB roles, no statement of who uses `forecast_experiment` | state that only the **trusted extractor** connects as `forecast_experiment`, from the dispatcher side, never from inside a sandbox |
| §4.1 | "It also holds the bus itself" with hash-chaining described separately from job state | one chain in `events`; `jobs` is a projection of it, and `verify-chain` compares the replay to the projection (D7) |
| §4.3 | run artifacts retained 90 days | unchanged; add that in 2a there is nothing longer-lived to keep, and §16.7 is 2c's |
| §7 | pins list | add `source_ref`; note `pins` is a key/value table so later pins need no migration |
| §13.1 Phase 2 | controls 8–11 | six controls in 2a (8, 10, 12, 13, 14, 15) and two in 2c (9, 11), per D14; NC12, NC13, NC14 and NC15 are new and must be written into the list, not left implicit in an acceptance bullet |
| §14 Phase 2 | one phase, five deliverables, one acceptance bundle | four sub-phases per §2 of this document, with 2d carrying the historical-reproduction acceptance and the full suite as one run |
| §3.4 step 4 | "Holds the **shared** heavy-training lock … the same one `daily_walk_forward.sh` uses", named by path, with no permission or inode model | the lock is one provisioned `0660 root:qfheavy` inode at `/var/lib/qf-locks/heavy-training.lock`, with cron migration as a start-up prerequisite: `flock` is per inode so two provisioned paths are two mutexes, `daily_walk_forward.sh` acquires it with a **write** open so a foreign-owned file kills the nightly run, and `qfclient` cannot be the group because `research` is in it (D5) |
| §15 | "Trainer OOM → dispatcher resource watch" implies per-container caps suffice | add that admission is an aggregate memory budget across all admitted work including image builds (D10), and that disk is a separate boundary with its own control (§4.5, NC15) |
| §16.1 | open | closed by D2, with the revisit condition stated |
| §16 | no entry for base-image pinning or token rotation | add the two new rows from §8 |

`host/README.md` gains a Phase 2a section: the new user and group, the two
state directories, the socket, the token and who rotates it, `PrivateTmp=no`
and why, and the fact that updating the dispatcher means restarting the unit.
