# Host artifacts — auto-research loop, Phase 0

Applied to the experimental server. See `../auto-research-loop-design.md` §3
and §13 for why each control exists, and `../auto-research-phase0-plan.md` for
the task-by-task narrative.

## Running it

`phase0-setup.sh` implements plan Tasks 2–9. **You run it, not an agent** —
Phase 0 is inherently privileged work (creates a unix user, writes
`/etc/systemd` and `/etc/nftables.conf`, restarts the live stack), and the
point of scripting it is that no agent ever needs root on this host.

```bash
./phase0-setup.sh discover           # read-only; changes nothing
./phase0-setup.sh db-auth --check    # dry run of the SCRAM cutover
./phase0-setup.sh db-auth            # apply, verify, auto-rollback on failure
./phase0-setup.sh db-roles
./phase0-setup.sh research-user
./phase0-setup.sh egress
./phase0-setup.sh agent-cli
./phase0-setup.sh verify             # negative controls 1–6
```

Every subcommand is idempotent. Run `discover` first and read the output.

`db-app-cutover` (moving the services off the Postgres superuser) is
deliberately *not* part of `all`. Run it separately, once the stack has been
observed healthy.

## What the script will not do

1. **Log the agents in.** Authentication is interactive SSO, not API keys, so
   there is no key file. Do this once, as `research`, **before `egress`** — the
   OAuth flow reaches your SSO provider and the vendors' auth domains, which
   the allowlist does not permit:

   ```bash
   sudo -u research -i
   claude          # then /login
   codex login
   exit
   ```

   Then re-run `agent-cli`. Afterwards, `auth-check` is the standing probe:
   SSO tokens refresh against an auth endpoint, and if the allowlist blocks it
   the agents work for days and then stop silently.

   **That happened, on 2026-09-02.** `auth.openai.com` was not on the list, so
   `codex` refused to start — `Error loading configuration: Failed to load cloud
   config bundle (workspace-managed policies)` — and `codex login` failed at the
   token exchange against `https://auth.openai.com/oauth/token`. It cost a day
   of the research loop: a copilot that cannot start counts as a disagreement,
   so every entry escalated and nothing was recorded, while the leader ran
   normally and produced good work that was then discarded.

   Two things made it hard to read. `reqwest` reports a filtered `CONNECT` as
   *"error sending request for url"*, which looks like a network fault rather
   than a policy denial; and the config-bundle load happens **before** auth, so
   the first symptom is not an auth error. `LogLevel Connect` in
   `tinyproxy.conf` logs the refused hostname, which names the domain instead of
   guessing:

   ```bash
   sudo grep -iE 'denied|filter' /var/log/tinyproxy/tinyproxy.log | tail -20
   ```

   Two rules follow. **Add the host, never relax the filter** —
   `FilterDefaultDeny Yes` is the property worth keeping, and one domain is a
   decision while a widened filter is an unbounded one. And **add it to the
   heredoc in `phase0-setup.sh`, not just to `/etc`**: that block `tee`s over
   `allowlist.txt`, so a line appended during an incident is erased by the next
   `phase0-setup.sh egress` and the failure returns with no trace of the fix.

   On a VM there is no browser for the OAuth redirect to `localhost:1455`, so
   re-authentication is `codex login --device-auth` — and it must be a **login**
   shell, because the proxy variables live in `~/.profile` and a non-login shell
   bypasses them into a uid-scoped nftables refusal that surfaces as a ~5ms
   connection failure:

   ```bash
   sudo -H -u research bash -lc 'codex login --device-auth'
   sudo -H -u research bash -lc 'codex exec --skip-git-repo-check "reply with the single word: ready"'
   ```
2. **Fix `password_encryption=md5`.** It stops. Setting passwords in the wrong
   scheme and then flipping `pg_hba` locks the services out.
3. **Decide about unexpected tables.** Grants are derived from the live table
   list and printed before being applied — read them.
4. **Apply the `HTTPS_PROXY` fallback.** If a CLI cannot reach its API through
   tinyproxy, that is fail-closed, not a hole. Widening egress is a decision.

## Files

| File | Installed to | Purpose |
|---|---|---|
| `phase0-setup.sh` | run in place | Implements plan Tasks 2–9 |
| `qf-research.slice` | `/etc/systemd/system/` | Resource caps for the agent processes |
| `tinyproxy-allowlist.conf` | `/etc/tinyproxy/tinyproxy.conf` | Egress allowlist (domains in `/etc/tinyproxy/allowlist.txt`) |
| `nc-suite.sh` | run in place | Negative controls 1–6; must exit 0 |
| `nc-evidence-phase0.txt` | — | Baseline evidence from the first passing run |

Run order matters: `research-user` → `agent-cli` → **interactive login** →
`egress` → `auth-check` → `verify`. Logging in after the egress lock-down
will fail.

## Two checkouts: the deployment and the trusted mirror

The stack runs from a checkout inside the **deploy user's home** (`$DEPLOY_DIR`).
`research` cannot traverse that directory, and must not be able to: granting
traversal would expose everything in that home carrying `o+r`, which is what NC3
exists to prevent. A symlink does not help either — it is resolved with the
accessing process's credentials.

So `/srv/queue-forecasting` is a separate **root-owned mirror**, a shallow
single-branch clone of the public fork, and it is what `research` reads:

```bash
sudo git clone --depth 1 --single-branch --branch feat/queue-forecasting \
  https://github.com/lotas/taskcluster /srv/queue-forecasting
# refresh after deploying:
sudo git -C /srv/queue-forecasting fetch --depth 1 origin feat/queue-forecasting
sudo git -C /srv/queue-forecasting reset --hard FETCH_HEAD
```

It holds no secrets, by construction — a fresh clone has no `.env` and no
`trainer/data/`. **Refresh it after every deploy**, or an agent reading service
source reasons about code that is no longer running.

`nc-suite.sh` must still be pointed at the real deployment:

```bash
sudo DEPLOY_DIR=<the deploy checkout> SECRETS_DIR=$HOME/qf-secrets \
  /srv/queue-forecasting/tools/queue-forecasting/host/nc-suite.sh
```

Aiming `DEPLOY_DIR` at the mirror **VOIDs NC3 and NC5** instead of asserting
them: their canaries require `.env` and `trainer/data/models` to exist. The suite
itself comes from the mirror because a control must be root-owned; the paths it
probes are the deployment's.

Deliberately not in this repo: `pg_hba.conf` (inside the postgres volume,
backed up as `pg_hba.conf.pre-scram`), `/etc/nftables.conf`, `~/qf-secrets/*.pw`,
and `/home/research/.config/qf/agent-env`.

## nftables: match the uid positively, never negatively

`meta skuid != <uid> accept` as a leading rule is **wrong**. In nftables,
`meta skuid` on a packet with no owning socket — kernel-generated traffic, ICMP
errors, TCP resets, forwarded packets — does not match at all; the expression
fails rather than evaluating true. Those packets skip the accept, fall through
every later rule, and hit the reject. Observed effect: the collector started
timing out on all outbound requests.

Every rule in `inet qf` therefore matches `meta skuid <uid>` positively, so
anything else matches nothing and reaches the chain's accept policy untouched.

## Rollback

- Egress table only: `sudo nft delete table inet qf`

- SCRAM cutover: `./phase0-setup.sh rollback-db-auth`
- Service identity: `cp .env.pre-app .env && docker compose up -d`

## Egress exceptions

`pypi.org` and `files.pythonhosted.org` were added 2026-08-24 for Phase 1. The
research agent creates and owns its own Python virtualenv, because the
alternative — root running `uv sync` inside an agent-writable worktree — would
let a one-line `[build-system]` addition to `pyproject.toml` execute
agent-authored code as root, and sdist dependencies build as root regardless.
Nothing root-owned now reads or executes anything from the worktree.

This gives up less than it appears to: `github.com` was already allowlisted, so
arbitrary code was already fetchable. Dependency review is enforced where it
matters, at the Phase 2 trusted image build, from a root-owned Dockerfile and
the human-promoted manifests in the trusted checkout.

NC6's denied-host probe moved from `pypi.org` to `huggingface.co` accordingly.

If a CLI is found not to honour `HTTPS_PROXY` and a direct nftables allowance is
added for it, record the endpoint, the reason, and the date here.

## Two invocation traps (both cost real debugging time)

**Never `sudo -i` with a command.** With `-i`, sudo joins its arguments into a
single string and hands that to the target user's login shell, which re-parses
it — quoting and newlines are destroyed. Use `sudo -H -u research bash -lc
"$cmd"`, which passes argv through untouched. Observed failure: `export
NVM_DIR="$HOME/.nvm"; ...` became a bare `export` that dumped the environment,
leaving `$NVM_DIR` empty and every later command broken. This also silently
weakens `nc-suite.sh`, where a command mangled into failure reads as "refused".

## Proxy environment lives in ~/.profile, not ~/.bashrc

Same non-interactive trap as PATH. Verified behaviour:

| invocation | reads `.profile` | reads `.bashrc` |
|---|---|---|
| `bash -lc` (sudo, run_research) | yes | no |
| `bash -c` (cron-like) | no | no |

So `.profile.d-proxy` is sourced from `~/.profile`, and `run_research` sources
it directly as well. **cron reads neither** — the Phase 4 tick must
`. /home/research/.profile.d-proxy` itself or the agents will bypass the proxy
and then be blocked by nftables.

Both upper- and lower-case variables are set: libcurl (curl, git) prefers the
lower-case names, most Node HTTP stacks read the upper-case ones.

## PATH gotcha for anything non-interactive (cron included)

Debian's `~/.bashrc` starts with

```sh
case $- in
    *i*) ;;
      *) return;;
esac
```

so it returns immediately for non-interactive shells — and that is where nvm's
initialisation lives. A `bash -lc` login shell is still non-interactive, so
`node`, `npm`, `claude`, and `codex` are all invisible to it.

Sourcing `nvm.sh` from the tick script is *not* a reliable fix either — it
defines `nvm` as a shell function, and whether that survives depends on the
invocation. `phase0-setup.sh` instead resolves the installed node's bin
directory and prepends it to `PATH`:

```sh
export NVM_DIR="$HOME/.nvm"
_nvmbin="$(ls -d "$NVM_DIR"/versions/node/*/bin 2>/dev/null | sort -V | tail -1)"
[ -n "$_nvmbin" ] && export PATH="$_nvmbin:$PATH"
```

**The Phase 4 cron tick must do the same**, or it fails with `command not
found` and no other clue.

Worth reconsidering at Phase 4: for a cron-driven loop, a system-wide
`/usr/bin/node` (apt/NodeSource) is more robust than a per-user version
manager. The only cost of switching is reinstalling the two CLIs; their
credentials live in `~/.claude` and `~/.codex` and would survive.

---

# Phase 2a — the trusted dispatcher

Plan: `auto-research-phase2a-plan.md`. Design: `auto-research-phase2-design.md`.

The deliverable is a root-owned dispatcher (`qfd`) that runs a pinned, sandboxed
job on request from the `research` user, with append-only hash-chained state.
No data plane, no contracts, no evaluator, no autonomy.

## Identity: one user, three groups

| Name | Kind | Why it exists |
|---|---|---|
| `qfd` | user | runs the dispatcher; in `docker`, `qfrun`, `qfclient`, `qfheavy` |
| `qfrun` | group, **gid 10001 fixed** | the in-container group the trusted image bakes in, so `out/` is handed over by group |
| `qfclient` | group | who may talk to the client socket and read `artifacts/`: `research`, the deploy user, `qfd` |
| `qfheavy` | group | who may hold the training mutex: `qfd` and the deploy user, **never `research`** |

`qfrun`'s gid is not negotiable — `trainer-env.Dockerfile` creates gid 10001
inside the image, and `phase2-setup.sh dispatch-user` dies if the host's group
already exists with a different gid.

`qfheavy` excludes `research` deliberately. `qfclient` contains `research`, so a
mutex group-writable by `qfclient` would let the untrusted agent hold the lock
indefinitely and stop nightly training — with no job involved, and therefore no
timeout to save it.

## Two state directories

- `/var/lib/qf-platform` — `qfd`-private (0750 `qfd:qfd`): `state.db` (SQLite,
  WAL), `mirror.git` (the bare trusted mirror), `build.lock`.
- `/var/lib/qf-runs` — `0750 qfd:qfclient` so clients can *traverse* to the run
  directories they are meant to read. `StateDirectory=` creates it `qfd:qfd`, so
  `phase2-setup.sh runs-dir` fixes the group; without that step every `qf logs`
  fails with a permission error and nothing says why.

Per run, and **every one of these is owned deliberately** — `qfd`'s primary
group is `qfd`, so anything left unchowned is invisible to both identities that
need it:

| Path | Owner | Mode | Who needs it |
|---|---|---|---|
| the run directory | `qfd:qfclient` | 0750 | clients must *traverse* it to reach `artifacts/` and `logs/` |
| `src/` | `qfd:qfrun` | 0750 | the container (uid/gid 10001) reads the worktree here |
| `out/` | `qfd:qfrun` | **2770** | the container writes here; setgid keeps the group |
| `artifacts/` | `qfd:qfclient` | 0750, files 0640 | the only thing a client reads |
| `logs/` | `qfd:qfclient` | 0750 | `qf logs` reads the file directly (D9) |

`Runner.OWNERSHIP` is the single declaration of that table, and a test asserts
every created directory appears in it. Chowning only `out/` and `artifacts/`
left `src/` untraversable by the container and `logs/` unreadable by every
`qfclient` member — a failure that shows up as a job that cannot see its own
source, and a `qf logs` that always denies permission.

`chown` clears the setgid bit on Linux, so each `chmod` runs *after* its
`chown`; otherwise `out/` loses 2770 and artifacts the sandbox creates drift out
of the `qfrun` group.

## Two sockets, and why

- `/run/qf-dispatch/client/sock` — 0660 `qfd:qfclient`. `ping`, `submit`,
  `status`, `list`, `cancel`, `verify-chain`, `trusted-paths`.
- `/run/qf-dispatch/admin/sock` — 0660 `qfd:qfheavy`. `force-release`, and
  nothing else.

The split is the access-control boundary, not ergonomics. `force-release` lets a
caller assert "I have verified nothing is running" and release the training
mutex; on the client socket, whose group contains `research`, that is an escape
hatch reachable by the untrusted agent. `SO_PEERCRED` records *who* called — it
does not authorise them, so the admin socket also refuses any peer uid outside
{root, deploy}.

**The parent runtime directory is 0711**: traversable but not listable. Chowning
a socket does not grant traversal of its directory, so a tighter mode here makes
*both* sockets unreachable and every negative control that assumes a reachable
socket passes vacuously.

## The lock, and `PrivateTmp=no`

One provisioned inode: `/var/lib/qf-locks/heavy-training.lock`, 0660
`root:qfheavy`. Plus `/var/lib/qf-locks/intent.d`, **2770** — the setgid bit is
load-bearing, because without it a nightly marker's group comes from the deploy
user's primary group and its mode from that user's umask, so under `umask 077`
`qfd` cannot read the declaration and admits straight through it.

`PrivateTmp=no` is defence in depth *only*. The lock no longer lives in `/tmp`,
so a private `/tmp` would not break it; the setting stays off so that a future
reader who moves the lock back to `/tmp` does not silently get two private
inodes and no mutex. That mistake cost two host freezes in 2026-07.

The real requirements are checked at startup, and `qfd` refuses to start if any
fails: one inode shared with the deploy user's cron entry (hence the root-owned
marker `/etc/qf-dispatch/lock-migrated`), group-write permission on it (the
nightly script opens it with `exec 9>`, a *write* open), and a readable,
writable, setgid `intent.d`.

**Invariant, stated because it ties every timeout together:**

```
TIMEOUT_MAX + BUILD_TIMEOUT_S + BUILD_LOCK_WAIT_S + HANDOFF_TIMEOUT_S
  + SETUP_TEARDOWN_ALLOWANCE_S  <  JOB_HOLD_DEADLINE_S
JOB_HOLD_DEADLINE_S + KILL_CONFIRM_S  <  LOCK_WAIT_S
```

Shipped: `3600 + 1800 + 900 + 120 + 600 = 7020 < 7800`, and `7800 + 300 = 8100
< 9000`. These numbers move together or not at all. `qfd` refuses to start if
the chain inverts, `phase2-setup.sh discover` **fails** rather than warns, and
`Config.check_deadline_chain` is unit-tested — because the failure mode is a
silently skipped or starved nightly run.

**A phase start is a critical section, not a check.** Each `Hold` carries a
`guard` (an `RLock`) and `Hold.phase_gate(what)`. Starting the candidate or the
handoff happens *inside* the gate; revoking a hold (`Reaper.release_hold`,
`qfadmin force-release`) takes the *same* guard. That ordering is the only thing
that makes two properties true at once:

- **no container starts after the mutex is freed** — a revocation that arrives
  first sets `revoked` under the guard, so the phase refuses;
- **the mutex is never freed while a container is recorded live** — a phase that
  wins the race records its row under the guard, and `release_hold` then sees it
  and *vetoes*, leaving the hold registered for the next sweep.

The gate covers **decision, record AND start** for *both* phases. Covering only
decision-and-record was the same defect one level down: `docker.run` after the
guard was released let `force-release` close the descriptor and only then would
the handoff container start.

**`Popen` is not proof that a container exists**, which is why the containers
are run as `docker create` + `docker start --attach` rather than `docker run`.
Spawning the CLI says the local process started, not that the daemon bound the
name — and until it is bound, `docker inspect` answers *"No such object"*, which
every confirmation path here reads as a POSITIVE absence. A sweep landing in
that window would release the resource row *and* the training descriptor, and
only afterwards would the CLI create and start the container: live work, no
mutex. `docker create` is synchronous, so its exit status is the
acknowledgement, and it is the last thing the gate does. Afterwards the name is
bound, so a sweep is harmless either way — it sees the container and refuses to
release, or it removes it and `docker start` then has nothing to start.

Two consequences worth stating, because both are load-bearing:

- the container-state probe asks `{{.State.Status}}`, not `{{.State.Running}}`.
  A **created** container is not running, and the old probe answered `false` for
  it — "has not started yet" read as "has finished".
- the kill escalation ends in `docker rm -f`. A created container never dies, so
  `--rm` never fires and `docker stop` changes nothing; without a removal such a
  container could never be confirmed stopped and the job would hold admissions
  shut forever.

`create` is the only Docker call made while the guard is held, bounded by
`min(Runner.create_timeout_s, time left on the hold)`, and `wait` still happens
*outside* the gate — `subprocess.run` inside would have held the guard for the
whole handoff and stalled the reaper for up to `HANDOFF_TIMEOUT_S`.

**Because the create is synchronous, it spends the hold**, which has two
consequences that are easy to miss:

- expiry is re-checked between the create and the `docker start`. The create is
  bounded by what was left, so it can return exactly *on* the deadline, and
  starting then is work admitted past it.
- every wait budget is measured **after** the create, not before. The candidate
  would eventually be caught by its deadline watcher; the handoff has none, so
  an over-granted wait there simply runs past the hold. Under a second left is
  no grant at all: the container is already up, so it comes straight back down.

**A resource row may be released without confirmation only when Docker was never
asked about that name.** Once a create has been issued, an absence read back is
a *reading*, not a proof — a daemon can complete a submitted request after the
client that submitted it has died, so a non-zero exit covers both "the daemon
answered no" and "the connection broke after the request went out". Both failure
paths therefore retain the row. The probe is kept only to *classify*:
`container_start_failed` when the name reads absent, `start_unconfirmed`
otherwise.

**Retaining the row is necessary and not sufficient**, because confirmation
would then convert the *first* absence into a release — the same mistake one
layer down. `KILL_CONFIRM_S` is a maximum polling period; it does not require
absence to be *stable*. So the ambiguity is **persisted before the create is
issued** (`store.absence_settles_pin`, one pin per role), taken down only by
Docker's answer, and every path that would release a row on an
inspection consults it:

- `Runner._account_for` — the single place a row is released on an inspection,
  which is why the rule lives there and not in each of the two confirmation
  loops. For an unacknowledged name it keeps `docker rm -f`-ing as well as
  probing (a window that only samples cannot destroy what lands between two
  samples), and requires the absence to hold for `BUILD_SETTLE_S`. Any sighting
  pushes the instant forward, so what is tested is stability, not a lucky
  sample. It observes *before* it removes: `docker rm -f` exiting zero does not
  reliably distinguish "removed it" from "there was nothing there", and that is
  not a distinction to bet a mutex on.
- `Store.reclaim` — an unsettled absence joins the *unknowns*. That method holds
  a probe, not a Docker client, so it can only wait; it re-runs every reap
  interval and the instant is fixed, so it terminates.

`BUILD_SETTLE_S` (30s) does double duty here, and deliberately: this is the same
question as an abandoned `docker build` — daemon-side work whose client is gone
— so it takes the same knob and inherits the same documented residual. A daemon
that completed a create *more* than `BUILD_SETTLE_S` after its client died would
still slip through. It rests on documented behaviour, not on a proof, exactly as
design D10 already accepts for the build. With `BUILD_SETTLE_S` well under
`KILL_CONFIRM_S` the settle finishes inside a single confirmation call; if it
does not, the job sits in `CLEANUP_BLOCKED` — holding, not releasing — and a
later reaper pass finishes it.

The handoff child is spawned on `DEVNULL`, not pipes: nothing reads them, so a
handoff writing more than one pipe buffer would block on the write until its own
timeout killed it, and buffering instead would mean an unbounded stream from a
container whose `/bin/sh` comes out of the candidate's own image. Its exit code
is its diagnostic channel (2–5 map to `error_class`), which is why it has one.
Either way the client is **reaped** after the container is stopped, not
abandoned.

Both phases also re-check expiry **after** their synchronous record, because the
record is a round-trip to the DB-owner thread and takes time; if the budget went
meanwhile, the row is marked released, since nothing started under it.

`if hold.revoked.is_set(): ...` followed by starting the phase is a
time-of-check/time-of-use race, and no number of extra checks closes it. The
same reasoning applies to the deadline: the candidate's expiry is re-checked
inside the gate immediately before `spawn`, and if the budget went while the
container was being recorded, the row is marked released — because nothing ever
started under it.

**New work may only appear in `PHASE_ACTIVE_STATES` — which is `RUNNING`, and
nothing else.** `add_resource` refuses anywhere else, and that refusal is
load-bearing rather than defensive. `reclaim` can move a run out from under a
phase that already holds its gate, and each exclusion in that set is a rule:
`LEASED` and `BUILDING` own no container of ours (the classic builder is not
recorded), and by `CLEANUP_BLOCKED` cleanup has already *begun*. Every mutation
is serialised through the DB-owner thread, so the two cannot interleave inside a
statement — but they can *arrive* in that order, and a row inserted afterwards is
the worst available shape:

- **`FAILED`** (reclaim settled a momentarily-empty inventory — the candidate
  exited and `--rm` took its container): the reservation and the lane are already
  freed, the phase then starts a real container, and a terminal job is invisible
  to `expired` (lease-active states only) and to `resolve_blocked`
  (`CLEANUP_BLOCKED` only), so nothing looks at it again.
- **`CLEANUP_BLOCKED`** (reclaim moved a RUNNING job with an empty inventory):
  `resolve_blocked` is already confirming this run's absence, so it finds the new
  row, sees the deterministic name not yet bound, releases it as gone and
  finishes the job.

Both end the same way: the descriptor veto in `release_hold` fires correctly and
too late, over live work. `not terminal` is therefore the wrong test.

Refusing the *record* is what makes that unreachable rather than unlikely:
containers are recorded before they can exist, so a refused record means no
container is ever created. The phase takes the revoked path, which already means
"do not start, collect nothing". The check and the insert are one transaction in
the single writer thread, which is the only place this ordering can be decided.

For the same reason, the ambiguity is pinned by `_record_container`, before the
row it protects — not at create time. From the moment a row exists, that name's
absence must not be read as proof: the container does not exist *yet*, and a
confirmation pass holds no guard, so it would release the row as gone while the
phase is still inside its gate on its way to creating it. A pin with no row is
harmless; a row with no pin is the window.

**And what it pins is not an instant.** `_record_container` writes
`ABSENCE_NOT_YET_ISSUED`, and it stays up until Docker *answers*. The
distinction is the difference between two claims:

- *a request is in flight and may complete late* — a bet on a window, which is the
  residual design D10 already accepts;
- *nothing has been asked yet* — not a bet at all. The phase holding the gate is
  still going to ask, so no amount of elapsed time makes the absence mean
  anything. A phase stalled past any window (with its renewer stalled too, so the
  lease lapses) must not become a settled absence and have its row released just
  before it issues the create.

**An answer is what starts the clock — not the asking.** Converting the sentinel
"immediately before the request goes out" is still *before* it: a thread
descheduled between those two statements leaves an instant expiring while
nothing has been asked, which is the same defect in a narrower window, and a
window is not a fix. So the answer moves the pin: cleared on exit 0, replaced by
`now + BUILD_SETTLE_S` on a non-zero exit or a timeout (the request *was*
issued, so a late create is the bounded residual). The sentinel is deliberately
not shaped like an instant, and `absence_believable` refuses it explicitly
rather than relying on string ordering to do it by accident.

**A sentinel is immune to elapsed time, so it needs an owner — and it has
three, one per way of losing the last one.** Its meaning is "a phase holds this run's gate and has not asked
yet", which is true while that phase exists and a lie the moment nothing can
ask any more. Left standing it refuses *every* absence for ever: cleanup could
never confirm, the job would sit in `CLEANUP_BLOCKED`, and the lock, the lane and
the reservation would stay held until an operator ran `force-release`. So every
way of losing the owner is covered, and all three convert to
`now + BUILD_SETTLE_S` via `Store.settle_unissued_creates`:

- **the phase abandons the create** — `Runner._unacked_create`, a context manager
  wrapping the record *and* the create in both phases. A **finalizer, not a
  longer list of `except` clauses**: `subprocess.run` raises `OSError` when a
  fork or exec never got as far as a Docker request, a DB call can raise, and the
  next exception this code learns about has not been written yet. Enumerating
  them means the next one is a stall. It converts the sentinel only, so an
  acknowledged create passes through cleared and an answered-ambiguous one keeps
  the instant its answer justified.
- **the conversion itself fails** — `Runner.retry_unsettled`, drained by the
  reaper each pass, before `resolve_blocked` (while the sentinel stands every
  absence is unbelievable, so a confirmation pass ahead of the conversion is a
  wasted pass). The finalizer must not raise — it runs in a `finally` with the
  real diagnosis in flight — but **swallowed is not abandoned**: nothing forces a
  restart, so a DB failure that clears a second later would otherwise leave the
  pin ownerless for the life of the daemon, and the pin is immune to time. The
  queue is in memory because the store is what just failed; the pin it refers to
  is durable, and a process that dies before a retry lands hands it to the owner
  below. A pair leaves the queue only on success — including the store reporting
  nothing left to convert, since another owner getting there first is the same
  outcome.
- **the process dies with the pin up** — `Recovery._settle_unissued`, before the
  first confirmation of each re-adopted run. A restart is the one moment when "no
  phase can issue this create" is true of every role at once.

Three details of the conversion are load-bearing:

- **an instant, not a clear.** A crash cannot distinguish "never asked" from
  "asked, and the answer died with the client", and in the second case the
  daemon may still bind the name. The ambiguity is kept, in the bounded form
  that repeated `docker rm -f` terminates, rather than resolved by assumption in
  the direction that frees a mutex.
- **the window starts at the restart.** The instant is a bet about a request that
  may be in flight *now*; the time qfd spent down is not time anything was
  watching.
- **an instant already running is left alone.** It was written by an answer, so
  its window is already elapsing; rewriting it on every pass would mean a crash
  loop, or a retry, never settles anything.

**A resolution does not overwrite a cause.** `resolve_blocked` used to set
`error_class=reclaimed_after_block`, which says how a job got unstuck and throws
away why it died (`hold_deadline_expired`, `kill_unconfirmed`, `mutex_lost`) —
the half triage actually needs. The cause now survives and the resolution is
recorded as an `unblocked_at` pin, which is what pins are for.

**Startup recovery hands back a hold only when something must keep asking.** A
recorded inventory means *forced cleanup*, not adoption, whatever state the job
was in — because nothing in a restarted process can resume one of these runs: the
`docker start --attach` client died with the old process, so the exit status is
gone, the logs are no longer pumped, the watchers are not sampling and the
handoff will never run. A hold handed back there would be driven by **nobody**,
and an undriven hold is not merely useless — the lease lapses, `reclaim` finds a
live container and renews it, and every later sweep does the same while the
mutex, the lane and the reservation stay held. `finish` still releases only on
confirmed shutdown, so a hold does come back when the job ends
`CLEANUP_BLOCKED`; the retained descriptor is the witness to which happened.

Whether that container is `running` or merely `created` — the crash window
between `docker create` and `docker start` — makes no difference here, which is
why recovery does not need to tell them apart: the kill escalation ends in
`docker rm -f`, so both become a positive absence and both confirm.

**The persisted hold deadline outranks every probe outcome.** In `Store.reclaim`
the deadline is checked *above* the probe branches, not inside one: past
`hold_deadline_at`, a job with a recorded inventory goes to `CLEANUP_BLOCKED`
(`error_class=hold_deadline_expired`) whether Docker said *alive*, *unknown*, or
*absent-but-not-yet-settled*. A lapsed lease means nothing is driving the run, and
renewing it for ever and re-asking about it for ever are the same stall.

The unknown case is the one that makes the placement matter. Leaving such a job
`RUNNING` was a stall **with no escape**: `resolve_blocked` lists
`CLEANUP_BLOCKED` only, so the automatic path never saw it, and `force-release`
refuses anything that is not `CLEANUP_BLOCKED`, so the operator could not act
either. `CLEANUP_BLOCKED` is the state that keeps the lock, the lane and the
reservation held *and* is visible to both paths — fail-closed and reachable, which
is the combination that matters. The reaper's `resolve_blocked` has a Docker
client (unlike `reclaim`) and kills and confirms in the same sweep when the daemon
answers; when it does not, the two-pass `force-release` is the way out.

One exemption: a *settled* absence still releases and fails cleanly, because it
has a better answer available than "blocked".

`force-release` **revokes first, then re-verifies, and may take two passes.**
The long flag is an assertion about the past: the operator checked, then typed,
and the request may then have waited on the phase guard — which is long enough
for a phase that had already won the gate to create and start a container. So
revocation comes first (nothing further can start), and the recorded inventory
is then re-read under the guard and put to Docker:

- **positive "live"** — refused, every time. Evidence beats an assertion, and no
  amount of re-asserting launders it. The run stays `CLEANUP_BLOCKED` with its
  descriptor held, and ordinary confirmation resolves it without anyone's help.
- **unknown** — this is what the flag exists to override, but it cannot be
  overridden against an inventory that could still have grown, because Docker's
  silence is exactly when nobody can see that it did. So the **first** call
  freezes (that is the revoke) and refuses with the names to check; the
  **second** answers from an inventory nothing could have changed. With no
  registered hold there is no phase gate to win, so the inventory is already
  frozen and one call is enough.
- **positively absent** — released; no assertion was needed.

The event still records who said so.

**Every phase is bounded by an instant, not a duration.** The hold deadline is
persisted (`hold_deadline_at`), and each phase derives its own budget by
subtracting the clock:

- source work gets ONE absolute deadline — `min(hold deadline, now +
  SETUP_TEARDOWN_ALLOWANCE_S)` — passed as an argument to `resolve` and
  `add_worktree`, so every git command inside them takes
  `min(per-command ceiling, time left)`. A per-command ceiling alone is not a
  bound: `resolve` runs several commands, and five of them each honouring 300s
  is 1500s of held mutex. The deadline is never instance state, because two
  light workers share one `Source` and would overwrite each other's budget.
- every Docker call in the build phase takes `min(cap, time left)`. Watch for
  falsy zero here: `timeout or 60` silently promoted a correctly-computed budget
  of 0 back to a full minute.
- the candidate, the build and the handoff each REFUSE a spent budget rather
  than flooring it to one second. A one-second grant past an expired deadline is
  the same overrun, just smaller.

`LOCK_WAIT_S` must exceed the deadline *plus* the kill-confirmation window
because the dispatcher **holds the lock past its deadline** rather than release
it over a kill it could not confirm. So the nightly run can still skip a night.
That is deliberate: a skipped nightly run is recoverable, a released lock over
live work is not.

## The token, and who rotates it

`/etc/qf-dispatch/github-token`, 0400 `qfd:qfd`, a `Contents: read`-only fine
grained token for `lotas/qf-research`. Installed by `phase2-setup.sh token
<file>`, which verifies read works **and write does not** before accepting it.

It never appears in argv, in a URL, or in a log: `source.py` hands git a
credential helper that reads the file at the moment git asks for the password.

**Rotation has no owner yet** — see open decision 11 in
`auto-research-loop-design.md` §16. NC14 proves the token cannot write; nothing
yet proves it is current.

## Updating the dispatcher requires a restart

`qfd` **executes from the trusted checkout**, so new code is not picked up by a
`git pull` alone:

```sh
sudo ./host/phase2-setup.sh mirror-refresh   # fetch + hard reset + restart
```

Two consequences worth remembering: predictions and events already written are
immutable (the chain would stop verifying otherwise), and a restart re-runs
startup reconciliation, which re-acquires the training lock for every
non-terminal job **before** any cleanup.

## Refreshing the promoted `env/` manifests

`host/dispatcher/env/pyproject.toml` and `env/uv.lock` are **human-promoted
copies** of the trainer's manifests. A pinned lock still lets an sdist run build
code, so the pins have to be ones a human reviewed.

Refreshing them is a **reviewed act with a diff, never a sync**:

```sh
diff -u host/dispatcher/env/pyproject.toml trainer/pyproject.toml
diff -u host/dispatcher/env/uv.lock        trainer/uv.lock
# read the diff, then, deliberately:
cp trainer/pyproject.toml trainer/uv.lock host/dispatcher/env/
```

`test_image.py::TestPromotedManifestsMatchTheTrainer` fails when they diverge.
That test is a **tripwire saying a review is due** — it does not copy anything.
Changing either file changes the image content key, so the next job rebuilds.

## Order of operations (the parts that are not interchangeable)

```sh
sudo ./host/phase2-setup.sh discover        # fails, not warns, on a bad invariant
sudo ./host/phase2-setup.sh dispatch-user
sudo ./host/phase2-setup.sh locks
sudo ./host/phase2-setup.sh cron-lock-path  # writes the migration marker
sudo ./host/phase2-setup.sh builder-probe   # measures cancellation timing
sudo ./host/phase2-setup.sh runs-dir
sudo ./host/phase2-setup.sh pin-base        # prints a FROM line to commit
sudo ./host/phase2-setup.sh token /path/to/token
sudo ./host/phase2-setup.sh install
sudo ./host/phase2-setup.sh verify
```

`locks` and `cron-lock-path` come **before** `install`: `qfd` refuses to start
without the migration marker, and an unmigrated nightly script (`flock -n`) plus
a dispatcher holding `LOCK_SH` means silently skipped nightly runs.

`builder-probe` **fails** if daemon-side build cancellation exceeds
`QFD_BUILD_SETTLE_S`. The documented response is to move building out of `qfd`
(design D10) — *not* to raise the window.

The later phases' domains have their own install steps, each `discover` before
`install` and each idempotent:

```sh
sudo ./host/phase2b-setup.sh discover       # the extractor: qfextract, the DSN
sudo ./host/phase2b-setup.sh install
sudo ./host/phase2c-setup.sh discover       # the evaluator: qfeval, the staging root
sudo ./host/phase2c-setup.sh install
sudo systemctl restart qf-dispatch          # see below
```

**The dispatcher restart after 2c is not optional, and it is not a courtesy
reload.** `qf-dispatch.service` lists `/var/lib/qf-eval` in `ReadWritePaths=`,
and that namespace is built when the service STARTS — so a staging root
provisioned afterwards is read-only inside the running dispatcher and every
evaluation fails on `mkdir` with EROFS, while `phase2c-setup.sh discover` reports
the directory as correct. It reports the *namespace* separately, measured with
`nsenter` inside the running process, and says so when it could only infer.

The NC fixtures are separate tooling and are never run by the setup steps:
`nc-fixtures-phase2b.sh` and `nc-fixtures-phase2c.sh` write experiment scripts
into a `qf-research` checkout and print the git commands. They never commit and
never push — the branch is published with the AGENT's credential, and the
dispatcher's token is read-only.

`phase2c-setup.sh` does not create the contracts. `instantiate-contract.sh` pins
a template to a promoted `baseline_hash`, which means `promote-baseline.sh` comes
first — and until at least one contract resolves, NC9 and NC11 report `void`.

## Reading experiments back

`results.sh` (with `results.py`) joins `qf list --json` and `qf status --json`
into one row per SCORED run: when, verdict, each metric's `measured` value with a
`!` where it missed its bar, the config, and the note. Oldest first, so it reads
as a history.

Deliberately not a `qf` subcommand: the data is already reachable by a client, so
the missing piece was the join, and a join is not a new privilege — an op in
`qfd` would mean a service restart to read numbers nobody's permissions blocked.

The column to look at first is `extract`. Rows against different extracts are not
better and worse results, they are results from different series, and two of them
in one table is how a regime change gets read as a model improvement. The script
prints the prefix on every row and warns when more than one input set is in view.

The config name comes from the run's `note`, which `first-probe.sh` writes as
`cfg=<config> | <EXPERIMENT_NOTE>`. Nothing else in a run record says which config
trained — it is a constant inside the commit — so without the note, telling two
experiments apart means `git show` per row.

## Two instruments that do NOT detect a leaked file handle

Both were tried here and both reported success over a live leak:

- **`-W error::ResourceWarning`.** A ResourceWarning raised while a file object
  is being *finalised* becomes an "Exception ignored" **unraisable** exception,
  not a test failure. 354 warning-strict tests passed while two log handles
  leaked on every gated refusal path.
- **Scanning `/proc/self/fd`.** Under CPython refcounting the writer becomes
  unreachable as the exception unwinds the frame, so the fd is already gone by
  the time the assertion runs. The leak is still real — a retained traceback,
  which both `unittest` and `log.exception` keep, extends the window
  arbitrarily — but this check cannot see it.

What works: hold a reference to every writer (`TrackingWriter` in
`tests/test_review_fixes.py`) and assert each was **closed**. Removing the
refcounting rescue turns "was it closed" into a question with a deterministic
answer.

## Running the tests

The dispatcher's own suite needs no host, no privileges, no Docker and no
network:

```sh
cd tools/queue-forecasting/host/dispatcher
PYTHONPATH=. python3 -m unittest discover -s tests
```

`PYTHONPATH=.` is required: the tests import `spec`, `store` and friends as
top-level modules, and `unittest discover` only puts the *start* directory on
`sys.path`.

The evaluator's suite needs `numpy` and `pyarrow`, so it runs under its own
interpreter rather than the system one:

```sh
cd tools/queue-forecasting/host/evaluator
PYTHONPATH=. env/.venv/bin/python -m unittest discover -s tests
```

`tests/test_results.sh` covers `results.py` and needs neither. Nor does
`tests/test_experiment_sh.sh`, which stubs `sudo` to assert that
`experiment.sh` executes the DEPLOYED `/srv` copy rather than the one beside
it -- the research user cannot traverse an operator's home, so a file whose own
mode is 0755 is still unreadable to it, and `[ -r ]` evaluated as the caller
reports that as fine.

**Two things the unit suite cannot answer**, because they are properties of the
Docker CLI rather than of this code, so `nc-suite-phase2.sh` NC16 asks a real
daemon on the host:

- `docker start --attach` must relay the *container's* exit status. If it did
  not, every failing candidate would read as `SUCCEEDED` — a silent regression,
  which is the only kind worth building a gate for.
- `--rm` is now set at create time, so removal must still happen; a surviving
  name would collide with the next incarnation of it.
- **What the CLI *says* about a container that is gone.** This one bit us for
  real on 2026-08-26 (Docker 29.7.2), which is why the third bullet exists.

## Do not run the suites while the nightly holds the mutex

`fault-gates-phase2.sh` and `nc-suite-phase2.sh` both submit jobs, and a job
cannot be admitted into the light lane while anything holds the training lock
EXCLUSIVELY. The nightly walk-forward does exactly that, for as long as it runs.

This is not a theoretical caution. A gate run on 2026-08-27 produced eleven
failures from it: three iterations voided with "the job never reached BUILDING
(state QUEUED)", and the nine after that voided with "no run id" because each
voided iteration left its job QUEUED until `research` hit
`QFD_QUEUED_CAP_PER_UID`. Every indicator an operator would think to check read
healthy — `stall: None`, `admitted_mem_mb: 0`, both lanes idle — because
`may_admit` covers the cleanup stall and the intent gate but *not* the mutex,
which is taken per lane inside `Runner.try_one`.

Three fixes came out of it, and the first is the one to remember:

- **`qf ping` now answers it.** `admit`, `mutex` (`free` / `held_exclusive` /
  `unknown`) and `queued`. A queue that is not moving is the likeliest question
  to ask that endpoint and it could not answer any part of it; the reasons
  existed only as INFO lines repeated once per poll in journald.
- **The gate has a preflight** that refuses to start when the dispatcher is not
  admitting, when the mutex is held exclusively, or when the queue is already at
  half the per-uid cap. Sixteen iterations against a queue that cannot move is
  worse than one refusal naming the cause.
- **A voided iteration cancels its own job**, instead of leaving it to fill the
  cap and convert one upstream condition into a second, unrelated failure mode.

`unknown` from the mutex probe means "cannot tell", not "free" — the lock file
was missing or unreadable, which `check_startup` already refuses to start over.

## The `nc12-poisoned-manifest` branch is a FIXTURE, not litter

`qf-research` carries a permanent branch called `nc12-poisoned-manifest`, and
`host/nc12-sha.txt` pins the exact commit the suite runs. **Do not delete either
as stale.** Without them NC12 and every hostile-job clause of NC15 report VOID —
by design, so that a missing fixture is visible rather than silently dropping two
negative controls.

The branch deliberately contains a `trainer/pyproject.toml` that cannot be built:
a `build-backend` that does not exist and two requirements that do not exist.
That is the control. NC12 asserts the trainer image is built from the promoted
manifests in `dispatcher/env/` and never from the research tree, so poisoning the
research manifest must change nothing — the image content key stays
byte-identical and neither package reaches the image. It fails loudly rather than
harmlessly on purpose: a backend that resolved to something benign would let the
real regression pass unnoticed.

It also carries six fixtures under `research/experiments/`, each a pytest module
with one `test_*` function (the suite runs them through the ordinary `test`
path, and pytest collects an explicitly named file but still needs a matching
function name). Regenerate them with `./nc-fixtures-phase2.sh <qf-research
checkout>`, which is idempotent and never commits or pushes.

Three corrections to the plan's NC15 text came out of writing them, and the code
is right in each case:

- The plan says **two** scripts; there are **six**. NC15's canary asserts that a
  well-behaved job's artifact lands at 0640 `qfd:qfclient` — but an ordinary
  pytest run writes nothing to `/out`, so the canary voided on a working handoff
  and every refusal it guards proved nothing. `artifact_good.py` is that canary.
  The other two additions are the symlink and FIFO cases, which Task 13's own
  text does ask for.
- The plan names `predictions.parquet`. The 2a allowlist is **`result.json` and
  nothing else** (`Runner._artifact_allowlist`; 2b widens it with typed
  contracts). A fixture writing `predictions.parquet` would produce no artifact
  at all, and the run would read as a handoff failure for the wrong reason.
- The plan expects the FIFO case to terminate **at** `QFD_HANDOFF_TIMEOUT_S`. It
  should not: `handoff-inside.sh` tests the file type before it reads, so a FIFO
  is refused as `handoff_bad_type` in milliseconds. The timeout is the backstop
  for a world where that guard is gone. The suite therefore asserts the class
  *and* that the elapsed time is well inside the timeout — otherwise "it
  terminated" cannot distinguish the guard doing its job from the backstop
  catching a hang.

## Findings for `qf-research` (Task 12 output)

Recorded here rather than fixed in the platform, per the plan: a trainer test
that fails inside the sandbox is a finding, and the fix belongs in
`qf-research`, not in a looser sandbox.

First full `--kind test` run: **220 passed, 5 failed, 1 skipped** in 197s.

All five failures are one bug, and it is a real one rather than a sandbox
artefact. `trainer/tests/test_ablation_configs.py` and
`trainer/tests/test_config_qctx.py` call `load_config(Path("configs/..."))` with
a **relative** path, so they resolve against the process CWD. That makes them
pass only when pytest is invoked from whichever directory happens to sit above
`configs/`; run from the repository root -- which is what the dispatcher mounts
and what `pytest trainer/tests` implies -- every one of them raises
`FileNotFoundError`.

The fix is to anchor the paths to the test file rather than to the caller's CWD:

```python
CONFIGS = Path(__file__).resolve().parents[1] / "configs"
c = load_config(CONFIGS / "wait_qctx_a_capacity.yaml")
```

That is correct wherever `configs/` actually lives and from whatever directory
pytest is invoked, which is the property the current code lacks. Nothing about
the sandbox is involved: there is no network call, no write, and no missing
credential in any of the five.

**One platform change did come out of this run**, and it is not a workaround:
`spec.DEFAULT_TEST_PATHS` was `["tests"]`, which does not exist in the only
repository this dispatcher can run -- the worktree ROOT is the mount point and
the suite is a level down. It is now `["trainer/tests"]`. A default that is wrong
for the sole consumer is worse than no default, because the failure it produces
(pytest exit 4) looks like a broken experiment rather than a missing argument.

## A probe must ask a question whose ANSWER is positive

`is_running` used to establish absence by finding the string `No such object` in
`docker inspect`'s stderr. Docker 29 words it differently, and the consequence
was total: every run's `--rm` container had already been removed by the time
cleanup looked, the removal read as **unknown**, and unknown is deliberately
immune to time — so the confirmation loop polled for `KILL_CONFIRM_S`, gave up,
and left the job `CLEANUP_BLOCKED` with admissions shut. A restart correctly
re-adopted the orphaned cleanup and hit the same wall. Every job froze the loop
on its way *out*, with nothing wrong with the job.

Two things were wrong, and they are separable:

1. **The evidence was a sentence.** A daemon's error prose is the weakest thing
   in the system to bet a mutex on, and broadening the match is not the fix:
   a loose `no such` test would match `stat /var/run/docker.sock: no such file or
   directory` and turn "the daemon is unreachable" into "the container is
   positively gone". Absence now comes from `Docker._exists`, where a **zero
   exit from `docker ps -a` is a complete enumeration** — a name absent from it
   is absent from the daemon. A non-zero exit stays unknown, because "the list I
   could not obtain did not contain it" is exactly the unsafe inference.
2. **The unknown did not say why.** 150 log lines over 300 seconds read
   `state unknown; not treating as stopped` without once printing the exit
   status or the stderr, so the diagnosis needed the probe reproduced by hand.
   `is_running` now reports the reason once per (container, reason) — once, not
   once per poll, or the one fact an operator needs is buried by the polling.

The recovery path itself behaved correctly throughout, which is worth recording
because it is the part that was designed for this: `qfadmin force-release`
refused on the first call (revoking the hold and freezing the inventory),
released on the second, and the reservation came back by itself because
`admitted_mem_mb` is derived from job state rather than tracked separately.

## The NC suite's `pass=49 fail=24` run was not a result (2026-08-27)

A full run of `nc-suite-phase2.sh` on a healthy host reported 24 failures, almost
all of them `TIMEOUT_WAITING` or an empty state, while the *same clause's*
filesystem assertion passed: NC13 "ran to its summary", the log cap held at
16MiB, the 0600 artifact "was normalised to 0640", and nothing was copied for the
symlink and FIFO fixtures. **The jobs ran and behaved correctly.** What failed was
the suite's ability to read their state:

```bash
state_of() { as "$RESEARCH_USER" "qf status $1 --json" 2>/dev/null \
  | python3 -c '...print(json.load(sys.stdin)["job"]["state"])' 2>/dev/null; }
```

Two discarded error streams, and one empty-string outcome covering *no socket*,
*refused*, *unparseable payload*, *no such job* and *the job has no state*.
`require_state_for` proves it fired on the very first poll: `(left QUEUED for
after 0s)`.

**The root cause was the invocation, not the host.** `--json` is defined on the
top-level parser, so `qf status <run_id> --json` was never valid:

```
$ sudo -H -u research qf status <rid> --json
usage: qf [-h] [--json] {ping,submit,status,list,cancel,verify-chain,trusted-paths,logs} ...
qf: error: unrecognized arguments: --json
```

argparse exits 2 and prints that to stderr, which the helper discarded. It had
never worked. `ping`, `submit`, `verify-chain` and `qf logs` were fine because
none of them pass `--json`; NC10's `qf trusted-paths --json` failed identically.
An hour went into suspecting the daemon -- serialization in `_reply`, `pins_for`
dispatch, `cleanup_stall` blocking, response size -- because the one line that
said what was wrong was thrown away at the call site.

Two fixes, deliberately both: the client now accepts `--json` on either side of
the subcommand (`default=argparse.SUPPRESS` on the subparser flag, or store_true
would overwrite `qf --json status x` back to False), and the callers use the
global form, which also works against a client that predates the fix.

**The 24 failures were the harmless half.** These three lines printed `ok`:

```
ok  (refusal) it starts once the lock is released
ok  (exclusion) two heavy jobs are never both RUNNING
ok  (budget) a 22g heavy and a 4g light never run concurrently
```

The first is `wait_state ... || [ "$(state_of ...)" != "QUEUED" ]`, and an empty
string is not "QUEUED". The other two are `while ...; do if [ "$(state_of A)" =
RUNNING ] && [ "$(state_of B)" = RUNNING ]`, and an empty string is never
RUNNING. **NC8's mutual-exclusion and memory-budget properties — the two things
NC8 exists to prove — passed having observed nothing at all.** A vacuous mutex
reads exactly like a working one, which is the first paragraph of this suite's own
header; the code had never implemented it.

Fixed in four places:

1. `state_of`/`field_of` return `UNREADABLE` and record *why* in `$BLIND_FILE`.
   The counter is a FILE because these run inside `$(...)`, and a subshell's
   increment to a shell variable is discarded on exit.
2. `never_concurrent` replaces both hand-rolled concurrency loops and requires
   **positive** evidence: each job observed RUNNING *separately*. Never observed
   is VOID, not a pass. It is correct even against the old blind helper, so the
   two fixes are independent.
3. `preflight_instrument` submits a probe job and reads its state back through
   the same helper the clauses use, and **exits 2** if that round trip fails.
   `qf ping` answering proves the socket, sudo, the login shell and qfclient
   membership; it does *not* prove `qf status <run_id>` answers, which is what
   every state assertion is built on. Thirty seconds instead of forty minutes.
4. A run that was blind even once prints `THE INSTRUMENT WAS BLIND n TIME(S);
   THESE TOTALS DO NOT STAND`, writes `VOID RUN:` into the evidence file, and
   exits nonzero **even if every clause passed**.

`refuse_as`, `canary_as` and NC10's canary now print the captured reason on the
unexpected outcome. Every VOID in that run arrived with no reason attached.

Guarded by `test-nc-instrument.sh` (extracts the instrument, drives it against a
stubbed `qf`, 10 assertions) and by
`TestTheNcSuiteCanTellBlindnessFromAnAnswer` in `dispatcher/tests/test_protocol.py`,
which runs that harness and statically rejects the vacuous comparison. The suite
cannot test this itself: reaching the bad path needs a dispatcher that will not
answer, and on a healthy host that path never runs.

### Why `fault-gates-phase2.sh` 32/0 is unaffected

The gates never call `qf status`. They read `state.db` directly with `sqlite3`,
and their blind path is *loud*: an empty state matches neither arm of

```bash
case "$state" in
  LEASED|BUILDING|RUNNING|CLEANUP_BLOCKED) [ -n "$holders" ] && outcome_held=1 ;;
  SUCCEEDED|FAILED|TIMEOUT|CANCELLED)      [ "$live_after" -eq 0 ] && outcome_clean=1 ;;
esac
```

so both flags stay 0 and the clause falls through to `bad`. That is the shape to
copy: enumerate the states that PERMIT a pass, and let everything else fail.

### `standin_nightly`: unreturnable and unwaitable (2026-08-27)

Four of NC8's protocol FAILs on a correct host came from one helper:

```bash
standin_nightly() {                                    # the original
  ( exec 9>"$LOCK"; flock -w "$1" 9 && sleep 60 ) &
  echo $!
}
```

read as `sp="$(standin_nightly 300)"`. Two bugs:

1. **The call did not return.** Command substitution reads its pipe to EOF, and
   the backgrounded subshell inherits that pipe as stdout — so `$(...)` blocked
   until the stand-in had waited for the lock, slept its 60s and exited. Measured
   at **67s** in a local reproduction. By the time `sp` was assigned the process
   was already dead, so `kill -0` reported `(a) the stand-in nightly exited
   instead of waiting`.
2. **`wait` could not reap it.** Forked inside a substitution subshell it was a
   *grandchild*, and `wait` on a non-child returns **127** immediately without
   waiting: `(a) it never acquired the lock`, `(b) nightly never entered`.

Whether clause (a) reported "waits rather than exiting" or "exited instead of
waiting" was a **race**: at that instant the process is a zombie, and `kill -0`
succeeds for a zombie until init reaps it. Two hosts disagreed for that reason.

Now the helper sets `STANDIN_PID` (a direct child, so `kill -0` and `wait` both
work) and reports acquisition through a marker file, so a clause can time the
*wait* rather than the wait plus the hold, and can distinguish "still waiting"
from "acquired" from "timed out".

**Clause (c) had an independent defect.** It asserted only `kill -0` — but a
stand-in that had *wrongly* acquired the mutex (which is exactly the bug: one
light job's exit releasing another's `LOCK_SH`) is also alive, holding it, for
its whole hold window. The clause would have printed `ok` for the failure it
exists to detect. It now asserts alive **and not acquired**.

Covered by six clauses in `test-nc-instrument.sh` (real `flock`, real background
processes, no dispatcher) and `TestTheStandInNightlyIsWaitableAndDoesNotBlockItsCaller`.

## Task 14 second run: 78/8, and one real dispatcher defect (2026-08-27)

Four of the eight were `standin_nightly` (above), fixed after that run started.
The other four:

### `log_overflow` never fired: the pump stopped draining

NC15's log-flooding job was reported `TIMEOUT_WAITING` with a NULL
`error_class`, while `no log file exceeded 16MiB (largest 16777262B)` passed —
exactly `cap + len(MARKER)`, so the cap worked perfectly. The watcher was fine
too; its disk-flood twin killed correctly with `out_quota_exceeded` on the same
1800s window.

The pump was the defect:

```python
for chunk in iter(lambda: stream.read(65536), b""):
    if writer.write(chunk) == 0 and writer.overflowed:
        break                      # <-- stops READING, not just writing
```

`docker start --attach` streams the container's output *into that pipe*. A full
pipe with no reader blocks the CLI in `write()`, so `proc.wait(timeout=budget)`
cannot return however promptly `watch_disk` kills the container. The job sat in
`proc.wait` for its whole 1800s timeout — which is precisely the window NC15
allows, so it read as a timeout rather than as the wedge it was. The disk flood
was immune because it writes to `/out` and leaves its pipe drained.

**Killing a process does not help when what it is blocked on is a pipe nobody is
reading.** The pump now never breaks: `write` returns 0 immediately once
`overflowed`, so the file stays bounded while the stream is drained to EOF.

Reproduced before fixing, in `TestTheLogPumpKeepsDrainingAfterTheCap`: 4 MiB
through a 4 KiB cap over real pipes, asserting the producer is not still blocked.
That test took 80s (blocked) before the fix and 0.005s after.

### `(g4) deploy reaches the admin socket`: a $PATH report in disguise

`qfadmin: command not found` — it lives in `/usr/local/sbin`, which is not on a
non-root user's PATH. The canary now uses `$QFADMIN`, pinned by test to the path
`phase2-setup.sh` installs. The two refusal clauses beside it connect to the
socket with `python3` rather than invoking a binary, which is what kept *them*
honest: a missing binary exits 127, and `refuse_as` would have scored that as a
refusal it had not earned.

This VOID was only actionable because `canary_as` had just started printing the
captured reason. The identical failure in the previous run said only
`VOID (g4) deploy reaches the admin socket`.

### `NC16 it is classified nonzero_exit`

Stale expectation. The probe names a path that does not exist — pytest's usage
error, exit 4 — and exit 4 now routes to `bad_invocation`, because "the
experiment ran and failed" and "the experiment never ran" send an operator to
different places. The clause was asserting the previous behaviour of the thing it
tests. Now pinned to the classifier in both directions.

### What went right

`NC15 artifact_symlink was refused by the type guard, not the timeout (1s <
120s)` and the FIFO at `0s`. Those clauses were written to distinguish the
file-type guard from the `HANDOFF_TIMEOUT_S` backstop, and they show the guard
doing the work — the strongest result in the run.
