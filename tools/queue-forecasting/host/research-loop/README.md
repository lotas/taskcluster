# `research-loop` — the part that invokes an agent

Everything mechanical was already built before this directory existed:
`experiment.py` resolves a config's inputs and runs it end to end, `qf` submits
typed jobs, the root-owned evaluator scores them, `results.sh` reads them back,
and both agent CLIs are installed and authenticated for the `research` user with
`api.anthropic.com` and `api.openai.com` on the egress allowlist. **The only
thing missing was that nothing ever invoked an agent.**

So this is deliberately small — seven files against the dispatcher's 21,000 lines
— and it must stay that way. The ratio that got this project into trouble was
65,000 lines of platform against 2,700 lines of trainer. Anything here that is
not "invoke an agent" or "stop an agent that is wrong" belongs somewhere else, or
nowhere.

## Files

| file | what it is |
|---|---|
| `tick.sh` | one turn: lock, budget, context, leader, copilot, publish |
| `tick-prompt.md` | the leader's instructions — six actions, first match wins |
| `verify-prompt.md` | the copilot's instructions — six ways to reject a claim |
| `prereg.py` | the pre-registration format, written by `experiment.py`, read by `frontier.py` |
| `frontier.py` | the progress artifact: per-series frontier, claims, confirm gate |
| `agent-env.sh` | puts the nvm-installed CLIs and the proxy on PATH for a non-interactive shell |
| `usage.py` | extracts structured CLI usage and appends the central log |
| `install.sh` | turns the loop on and off |
| `qf-tick.{service,timer}` | hourly, as `research` |

## Token and cost log

Every leader and copilot invocation appends one UTC line to
`~research/.local/state/qf-tick/usage.log` (override with
`QF_TICK_USAGE_LOG`). For example:

```text
2026-09-01 14:20  claude   3.5M tokens  est ~$7.0123 [total=3500000 input=1200 cache_write=3000 cache_read=3490000 output=5800] [exit=0]
2026-09-01 14:22  codex    24.9K tokens  est n/a [total=24885 input=24763 cached=24448 output=122 reasoning=0] [rates=unset] [exit=0]
```

Claude's CLI supplies its own cost estimate. Codex supplies token counts; set
all three API-equivalent rates to add a Codex estimate:

```bash
QF_CODEX_INPUT_USD_PER_MTOK=...
QF_CODEX_CACHED_INPUT_USD_PER_MTOK=...
QF_CODEX_OUTPUT_USD_PER_MTOK=...
```

The detailed counts remain in the line so old entries can be recalculated after
a price or model change. This is accounting only: a missing or malformed usage
record warns but does not stop a tick. `rates=unset`, `rates=partial`, or
`rates=invalid` explains a missing Codex estimate. If non-JSON text appears in
Codex's JSONL stdout, valid events are still used and `skipped_lines=N` makes the
format drift visible.

## The three guards

**A lock.** One tick at a time, on its own fd, released by the kernel on exit —
so a killed tick releases it and a `trap` is not relied on. A tick that finds
work blocks for up to 90 minutes on a training run; overlapping ticks would
reason about results they had not seen.

**A budget.** Four **submitted probes** per UTC day, one extract, twelve ticks —
all pre-gates. Read from `qf list` rather than from a local counter (a counter
drifts the first time a human runs an experiment by hand) and counted on
SUBMISSION rather than on scoring: an OOM, a refusal, a crashed probe and a probe
still awaiting evaluation each cost real host time, so counting only scored
results would have let twelve ticks launch twelve expensive failures under a
four-run cap. An unreadable job list **fails closed** — an unenforced budget on
an unattended loop is the whole risk.

The extract cap is **mechanical, not advisory**. Telling the leader in its prompt
that the budget is spent is a request, and the premise here is that the leader is
untrusted — so when the cap is reached, `qf` is shadowed on the leader's `PATH`
by a shim that refuses `qf extract` (exit 3) and forwards everything else
untouched, including `qf probe --extract <hash>`. It checks `$1` only, which is
exact: `qf extract` is its own subcommand and `qf submit --kind` accepts just
`test`/`selftest`, so an extraction cannot be requested another way — and a
broader match would have blocked every probe.

**Counter state fails closed.** Both counters used `cat … || echo 0`, so an
unreadable or unwritable state directory silently reset them — the tick budget
never accumulated and, worse, the consecutive-disagreement counter never reached
its threshold, so the automatic `PAUSE` could not fire. A non-numeric or
unpersistable counter now stops the tick, and if the streak cannot be counted the
loop pauses immediately. If `PAUSE` itself cannot be written, that is reported as
CRITICAL with the manual command, rather than reported as a pause that did not
happen.

**A second opinion.** The leader writes to `journal/PENDING.md` and never
commits. The copilot reads that entry against the frontier JSON and must end
with `VERDICT: AGREE` for it to be recorded; anything else — a disagreement, a
missing verdict line, a crashed or absent `codex` — files it under
`journal/escalations/` marked NOT RECORDED. Three consecutive disagreements
write `PAUSE` and the loop stops itself.

The evidence handed to the copilot is **re-read after the leader exits**. The
pre-leader snapshot cannot contain the result of an experiment the leader just
ran, so verifying against it would force the copilot to reject every genuinely
new result or accept it blind.

Both halves of the refresh must succeed and the JSON must be non-empty; a
failure falls back to the old snapshot **explicitly labelled as stale**. And a
stale label is a reason to *defer* a finding, never to accept one: if the absent
figure is the entry's central result, `verify-prompt.md` requires `DISAGREE`,
because there is nothing to verify. Only incidental figures may be waved
through with a note.

### The journal is an input too

`frontier.py --journal <dir>` scrapes run ids out of recorded entries, so a
result the loop already wrote up stops matching the leader's first action. **Only
git-tracked files count** — the leader owns that directory, so dropping an
untracked `journal/anything.md` citing a run id would otherwise retire a result
nothing had verified or committed. If tracked-ness cannot be determined (no repo,
no git), nothing counts: every run reads as unrecorded, which is noisy and safe.
Without it that action matches forever and every tick re-narrates the same row
instead of advancing — and `broken` is a permanent state, so refutations had the
same problem. **Escalations are excluded**: an escalated entry was rejected, so
the run it describes is still unwritten.

### What the copilot can and cannot see

It receives the leader's entry and the frontier JSON, and nothing else — not the
leader's transcript, not the commands it ran. The first live tick (2026-09-01)
escalated a **correct** entry because of this: the leader had read `qf status`
pins and cited `predictions_sha256` values, RSS and wall-clock figures that no
frontier JSON contains, so the copilot applied its "a figure with no source"
rule exactly as written.

That was a prompt gap, not a copilot fault. `tick-prompt.md` now requires an
`Evidence:` block pasting the command and its relevant output for any figure the
JSON does not carry, and `verify-prompt.md` says a pasted output counts as a
source. Rejecting a sound entry for lack of a paste is a cost; recording an
unverifiable central result is a worse one, so the rule stays strict and the
leader carries the burden of showing its work.

### The facts the tick itself measured go to BOTH agents

The second escalation (`journal/escalations/20260901T113305Z.md`) rejected
*"this spends probe 3 of 4"* as a figure with no source — and was right on the
rule as written, because the copilot had never been shown it. But the tick
computed that number itself and printed it into the leader's context. A fact
handed to one agent and withheld from the other is not a claim the leader can
support, however honest it is.

So the counts are written once to `$CTX/tick-facts.md` — probes, extracts and
ticks used against their caps, plus the in-flight count — and that same file is
concatenated into both the leader's and the copilot's input. `verify-prompt.md`
names it as the third valid source alongside the JSON and a pasted `Evidence:`
block.

**This is not "show the copilot the leader's context", and the distinction is
the point.** The queue excerpt, the frontier prose, the doctor output and the
command list are *instructions*; handing them to the verifier would invite it to
check the entry against the leader's briefing instead of against the numbers.
Only figures the tick measured are shared, and `test_tick.sh` asserts both
halves — that the budget line reaches the copilot, and that the queue excerpt
and command list do not.

The same escalation's *other* objection was a genuine leader error: it said both
reclaimed probes ran "~31 minutes" when the pasted timestamps show 31.1 and
26.8. That rejection is correct and stays — `verify-prompt.md` now says outright
that a figure derived from a source must follow from it arithmetically. Closing
the evidence gap must not soften the arithmetic.

### Why the copilot checks the claim and not the arithmetic

The design (`auto-research-loop-design.md` §6) specifies an "independent
derivation" of the metrics. This does something narrower on purpose.

The metrics come from a root-owned evaluator that has been shown to reproduce
bit-identically across re-evaluations, and the trainer's `predictions_sha256`
reproduces too. A second LLM recomputing those numbers would not be a second
source of truth; it would be a worse one. What has actually gone wrong in this
project has never been the arithmetic — it has been the sentence about the
arithmetic. "Capacity appears to actively dilute the model" survived for weeks
off a confounded run; a cross-series comparison read as a 3.6× disagreement that
turned out to be a non-effect. So the copilot is pointed at exactly that, and
`verify-prompt.md` names the six failures it must reject.

## Pre-registration

`experiment.py run` gained `--bar`, `--dir`, `--vs` and takes `--note` as the
hypothesis. Together they are the pre-registration, and they live in the probe's
`note` — which the dispatcher writes into `spec_json` at submit time and the
store hash-chains. So the claim exists before the number does, and the agent
that wrote it cannot revise it. That is not a policy; it is the order the
operations happen in.

`QF_REQUIRE_PREREG` defaults to `1` **in `tick.sh` itself**, not only in the
systemd unit — so `install.sh once` and a hand-run tick enforce it too. Putting
it only in the unit would have left the supervised first run, the one an operator
is most likely to trust, silently exempt from the discipline the leader's prompt
says is in force. The unit still sets it explicitly, as documentation.

It is **not** required for a human running `experiment.py` directly: an operator
re-running a known config or debugging a mount is not making a claim, and a
pre-registration written to satisfy a flag is worse than none because the
frontier would count it.

`--vs` is **mandatory**, not merely recommended: without it a run trains for an
hour and produces a number the frontier can only mark `unjudgeable`. The declared
exception is `--reference-run`, for the genuine first run of a new series — it is
recorded in the note as `ref=1`, so it is a declaration rather than an inference
from a missing field, and such rows report as `reference` rather than as a kept
claim.

`--dir hold` claims a bar does not get **numerically worse** than `--vs`, within
a `--tol` that is itself pre-registered (default 0: strictly not worse). It is
judged on values rather than on pass/fail status, and that distinction is not
cosmetic: status-equality would call a tail miss going 0.304 → 0.900 "kept"
because both fail the 0.30 bar, and would call an improvement from fail to pass
"broken". Judging on values keeps the claim answerable on a bar that is currently
**failing** — which `p90_miss_tail` is — without accepting an arbitrary
regression.

Band metrics (`p90_coverage`) have a "worse" too: **distance to the nearest band
edge**, read from the contract's own `low`/`high`. Reading them as pass/fail alone
repeated the bug this direction was rewritten to fix — with both rows outside the
band, coverage collapsing 0.84 → 0.01 counted as "kept" because neither passed.
Without readable bounds, two failures are `unjudgeable`, never automatically kept.

`--dir improve` is judged against `--vs` and not against the contract, because
beating the reference is an improvement whether or not it clears the bar; that is
the discipline `experiment-queue.md` already runs on.

**No structured field can be forged.** The separator is refused in every
structured value (config path, `vs`, `cfgh`) — `decode` takes the *first*
occurrence of each key, so a pipe in an earlier field injects a value that beats
the genuine one, and `configs/a | cfgh=deadbeefdead` shadowed the real digest,
which is enough to make two different files confirm each other. No path, run id
or digest here contains a pipe, so one is refused rather than rewritten.

**The hypothesis cannot become a structured field.** `decode` splits at the
first ` | hyp=` and never key-parses the tail. That is a security property, not
a convenience: `encode` omits `tol` when zero and `vs` on a reference run, so
key-parsing the hypothesis meant `--note "safe | tol=5"` decoded to a tolerance
of 5 against a pre-registered 0 — the unrefutable-claim hole reached through the
one field the leader writes freely. A side benefit is that the hypothesis now
round-trips verbatim instead of needing to be sanitised.

A **malformed structured field invalidates the whole registration** rather than
being coerced to something safe. Coercion was a live hole: `abs()` turned an
injected `tol=-10` into a permissive `+10`, and `tol=inf` or `tol=nan` makes every
numeric `hold` unrefutable. Non-finite and negative tolerances are refused at
encode, and an unreadable one at decode sets `tol_error` and drops `registered`
to false — counted in the report as `malformed_preregs`, which is a different
signal from a legacy hand-typed note and is surfaced as its own warning.

A **content digest** of the config file goes into the note automatically. Without
it a confirmation identifies a config by PATH, and the agent owns the checkout
that path points into — so it could edit `configs/x.yaml` between two cohorts and
have two different models confirmed under one name, which is precisely what the
second cohort exists to rule out. A row with no digest can never reach
CONFIRMED; it says so in `blocked_by`.

## `measured` is not the metric

The scoreboard's `measured` field is **not** always the underlying quantity, and
conflating the two produced the worst defect in this directory so far —
found by the loop's own first tick, in the loop's own code.

`verdict.py:48-72` is the authority:

| bar `kind` | `measured` holds | ordering |
|---|---|---|
| `relative_improvement` | improvement fraction, `(baseline-value)/baseline` | **higher** |
| `absolute_improvement` | improvement in points | **higher** |
| `absolute` | the raw metric | by the metric's `direction` |
| `band` | the raw metric | in / out only |

For the improvement kinds the metric's own `direction` has **already been
applied** inside the computation. `frontier.py` originally hardcoded
`RANK = {"mae": "lower", ...}`, reading the contract's `direction:
lower_is_better` as a statement about the scoreboard value. It is a statement
about MAE the quantity. So every mae comparison was inverted: the frontier named
the config that *failed* the bar as the series best, `--dir improve` on a real
26.9% improvement reported `broken`, and a 22.8-point regression registered as
`hold` reported `kept`.

The map is gone. `metric_ranks(contract)` derives ordering from `bar.kind`, and a
series whose contract cannot be read is reported **unordered** with every claim
on it `unjudgeable` — rather than guessing a direction, which is what made this
possible. The test fixtures now carry real `measured` values from the first live
tick; the old ones put absolute seconds in `mae`, which is the `value` field, and
that agreement with the wrong rank is why 43 tests passed over it.

**Pre-registrations are immutable**, so any `--bar mae` note written before this
fix reads inverted permanently. Those runs are not recoverable as mae claims.

## The confirm gate

`frontier.py` never reports `CONFIRMED` from one cohort. A config that clears
every bar once is `PROMISING`. Confirmation needs, all of:

- a second cohort whose **holdout window does not overlap**, computed from the
  extracts' `as_of_date` and the contract's `holdout_days`;
- the **same config digest** in both;
- the **same baseline and the same contract** in both.

**The cohort is the only input a confirmation may change.** Grouping by config
alone would let one config clear contract A on one extract and contract B on
another and call that two independent cohorts — but a different contract is a
different question, and a different baseline is a different thing to have
beaten, so neither pair is a repeat of the other. The group key is
`(config, digest, baseline, contract)` and appears in the report name.

### What the digest does not cover

The digest is over **YAML bytes only**. The same config under changed trainer
code is not necessarily the same model, and nothing here detects that.

**So freeze trainer code between a PROMISING run and the cohort meant to confirm
it.** That is a procedural rule, deliberately not a mechanism — a second lineage
system is exactly the platform work this directory exists to avoid. The leader's
prompt states the rule, and if you want to check it by hand the probe's
`source_sha` pin is in `qf status <probe-id> --json`.

This matters more than it sounds. The experiment queue's entry 3 proposes
re-running on extract `c179c7f5b961` (as_of 2026-08-26) as a check on the
current trio (as_of 2026-08-27) — those share four of five holdout days, and the
gate correctly reports them as **one** cohort. `test_frontier.py` asserts exactly
that case.

This is deliberately cheaper than the design's Phase 3 (moving-block bootstrap,
BH-FDR, disjoint-day decomposition), which is not built. It buys the one property
an unattended loop cannot do without — a win has to repeat on data it was not
selected on — and buys it with arithmetic rather than a framework.

## The tick does not hold across an in-flight experiment

`tick.sh`'s design assumed the leader blocks on `experiment.py run` for the whole
training. It does not: the leader is an agent with its own tool timeouts, so it
submits a probe, returns, and the tick exits while the dispatcher trains for half
an hour. The run is not lost — the dispatcher owns it — but "one action per tick"
is weaker than it reads.

So the context now reports jobs in a non-terminal state (`store.py:27`, and
`BUILDING` counts — its omission from a state set has caused three silent bugs
here already) and tells the leader that actions 4 and 5 are unavailable while one
is in flight. The daily probe budget was the only thing preventing a second
submission before.

Related: **waiting is action 6, and action 6 still writes an entry.** Two ticks
were lost printing "the probe is training, I'll write it up when it lands" to the
leader's own output and writing no file — so both recorded nothing and the
reasoning went to a log nobody keeps. The prompt now says the final message is
not the entry, and that a tool timeout on `experiment.py run` is expected rather
than a failure to be retried.

### The idle gate

Invoking the leader is the expensive part of a tick. Measured 2026-09-01: two
consecutive ticks whose only possible conclusion was "the probe is still
training" cost **$1.48 and $1.26** — 2.4M tokens, ~94% of it cache reads — and
recorded nothing.

So the tick asks *before* spending anything: is a job in flight (actions 4 and 5
unavailable), **and** is nothing unrecorded (1 and 2), **and** is nothing
PROMISING (3)? Then only "wait" remains, its content is already known, and the
leader is not invoked. A skipped tick also rolls its own counter back — it cost
nothing, so it should not consume the daily tick budget.

It fails **open**: an unreadable frontier runs the leader, because skipping on a
reporting glitch would turn it into a silently stalled loop. `QF_TICK_ALWAYS_LEAD=1`
overrides.

## A qctx probe did not fit the probe timeout (optimisation landed 2026-09-01; verification pending)

`spec.py` caps `timeout_s` at **3600** for every kind, and the cap is
deliberately subordinate to the dispatcher's hold deadline:

```
TIMEOUT_MAX + BUILD_TIMEOUT_S + BUILD_LOCK_WAIT_S + HANDOFF_TIMEOUT_S
  + setup/teardown  <  JOB_HOLD_DEADLINE_S  <  LOCK_WAIT_S
```

*"those numbers move together or not at all"*, and `phase2-setup.sh discover`
fails if the chain inverts.

Measured on `wait_hazard_qctx_d_priority_flow` (2026-09-01, extract
`8734690f4cd8`, 6.02M rows): queue-context features took **3019.7s** for the
training split; the model then trained and reported `30m+ p90 miss 13.3% guarded
(bar 34.49%)`; then a **second** qctx sweep began for the 1.94M-row prediction
pass, and the job hit TIMEOUT. Feature work alone is ~4000s against a 3600s
ceiling, because the sweep is recomputed per split rather than once.

That was the state until 2026-09-01. Two ways out existed, neither to be chosen
quietly:

1. **Raise the chain.** The observed hold deadline was 7800s, so headroom exists,
   but it is a coordinated constant change in the trusted dispatcher.
2. **Compute the sweep once.** `[queue_context]` timings show heavy skew — 26 of
   529 queues took 907s of the 3019s — so caching across splits, or sweeping the
   union once, is the real fix. That is trainer work, and it is the rare case
   where platform work unblocks the science rather than displacing it.

### What was done, and what that does and does not establish

Route 2 was taken; `TIMEOUT_MAX` was deliberately left alone. The per-row sweep
issued ~150 scalar `np.searchsorted` calls per target row; it now issues every
search once per (queue, rank) over the target vector, chunked at
`SWEEP_CHUNK = 250_000` targets to keep peak memory independent of queue skew
(`379e372`, `fc88650`).

**The timeout above is not evidence about the current trainer.** That run,
`probe-20260901T112900Z-a78cdab1a997-5837`, was submitted at 11:29Z; the sweep
change was committed at 14:06Z, two and a half hours later. It ran the old code
and could not have run anything else.

What is established: two qctx-enabled probes submitted *after* both commits —
`probe-20260901T152934Z-fe8755f5c4f2-5941` (15:29Z, `wait_qctx_d_priority_flow`)
and `probe-20260901T171159Z-61dd1b700db5-6004` (17:11Z,
`wait_hazard_qctx_d_priority_flow`) — both reached the scoreboard with full
metrics, so both finished inside the 3600s ceiling. Under the old cost, feature
work alone was ~4000s, so completion is not something the pre-fix code could
have done.

What is **not** established from the repo alone: that those two runs executed the
fixed trainer rather than merely a faster-than-usual old one. `experiment.py run`
does not sync the trainer — `experiment.py sync` is a separate step after
`mirror-refresh` — so the deploy is the unverified link, not the completion.
Settle it against the **commit**, never the working tree:

```sh
qf status <probe-id> --json | grep source_sha
git -C ~/qf-research show <sha>:trainer/src/queue_context.py | grep SWEEP_CHUNK
```

Until that is checked, state the status as "two post-fix qctx probes completed
and scored", which is observed, rather than "qctx now completes", which is an
inference about code identity.

## Deploying restarts the dispatcher, which kills in-flight probes

`mirror-refresh` runs `systemctl restart qf-dispatch`, and qfd's startup recovery
fails any job it finds LEASED or RUNNING with no live container
(`qfd.py:4900-4908`, `error_class: reclaimed_at_startup`). A 31-minute hazard run
was destroyed this way on 2026-09-01 — no OOM, no timeout, no exit code, just a
deploy landing mid-experiment.

`touch ~research/qf-research/PAUSE`, wait for the heavy lane to clear
(`qf list` shows no non-terminal probe), then deploy.

## The prompt goes on stdin

Both CLIs are fed their prompt on **stdin**, assembled into a file under the
tick's context directory. It was originally a single argv argument, which has two
problems:

- **`MAX_ARG_STRLEN` is 131072 bytes** on Linux — 32 pages, not tunable. The
  assembled leader prompt is ~27KB today, but the frontier grows with scored
  history at roughly 240 bytes per run across its two tables. Measured
  projection: ~75KB at 200 runs, over the cliff at ~430. At four runs a day that
  is about three months out — an `E2BIG` that nothing in the loop explains.
- **Argv is world-readable in `ps`.** The entire prompt, queue excerpt included,
  was visible to every account on the host. That is how this was noticed.

`claude -p` reads the prompt from stdin when no positional is given (verified
against the installed CLI). `codex exec` is passed an explicit `-`, because its
documented behaviour is that a prompt argument *plus* piped stdin wraps the stdin
in a `<stdin>` block rather than using it as the prompt — so passing both would
silently reshape the request.

Stdin removes the hard limit, which changes the failure mode from a crash to a
quietly over-long prompt. So the tick **reports** the assembled size every run,
warns above 100KB, and bounds the queue excerpt by bytes
(`QF_TICK_MAX_QUEUE_BYTES`, default 24KiB). When that excerpt is trimmed the
leader is told so explicitly, because the queue's ranked list lives at the *end*
of the file and a silent cut would make a real entry look nonexistent. The
frontier is never truncated: dropping rows from it is a research decision, not a
plumbing one, so it warns instead.

## `which claude` is not the question

The first `install.sh once` on the host aborted with ``no `claude` on PATH`` while
`which claude` as the research user printed
`/home/research/.nvm/versions/node/v24.19.0/bin/claude`. Both were correct.

nvm appends its init to `~/.bashrc`, and Debian's `~/.bashrc` opens with
`case $- in *i*) ;; *) return;; esac`. A **login** shell is not necessarily an
**interactive** one: `bash -lc` runs `~/.profile`, which sources `~/.bashrc`,
which returns immediately. The CLIs are installed, on disk, and unreachable — and
that reads as "not installed". `phase0-setup.sh:147` already had `NVM_PRELUDE`
for its own invocations; nothing in the loop went through it.

`agent-env.sh` is that one definition, sourced by `tick.sh` (so the timer,
`install.sh once` and a hand-run tick all agree) and by `install.sh on`'s
preflight (so it tests the shell the tick will actually use). It resolves the
newest installed node by directory listing with `sort -V` — `nvm` is a shell
function that does not exist in a script, and a hardcoded version breaks on the
next `nvm install`.

Three details in it are load-bearing, all found by review rather than by use:

- **It selects version directories that actually contain a CLI**, not the newest
  by name. `nvm install 24` does not migrate global packages, so `v24/bin`
  routinely exists without `claude` while `v22/bin` still has it — and picking
  newest-by-name then hides an installed CLI, which is the very failure this file
  was written to fix. The two CLIs may also live under different node versions;
  both are found.
- **An inherited `NVM_DIR` is honoured only if it is under `$HOME`.** A relocated
  nvm is set that way in the user's own profile; a value pointing into another
  home (via `sudo -E` or an `env_keep` entry) is not a hint but a wrong answer.
- **`$HOME` is validated and normalised first**, because both checks are built
  from it. An empty `HOME` turned the under-`$HOME` pattern into `/*`, trusting
  *every* absolute `NVM_DIR` including another account's; a trailing slash
  produced `/home/research//*`, which rejects a legitimate relocation. An
  unusable `HOME` now leaves `PATH` untouched and says so.

To tell the two shells apart when diagnosing:

```bash
sudo -H -u research bash -lc 'command -v claude'   # what the tick sees
sudo -H -u research bash -ic 'command -v claude'   # what you see over ssh
```

## Running it

```bash
# See what the leader would see, invoke nothing:
sudo -H -u research bash -lc '/srv/.../host/research-loop/tick.sh --dry-run'

# One turn, in the foreground:
sudo /srv/.../host/research-loop/install.sh once

# Turn the schedule on / off:
sudo /srv/.../host/research-loop/install.sh on
sudo /srv/.../host/research-loop/install.sh off

# Stop the next tick without touching the timer:
touch ~research/qf-research/PAUSE
```

Tests: `host/tests/test_prereg.py`, `test_frontier.py`, `test_tick.sh`. The tick
test stubs both CLIs and asserts the guards — including that a rejected claim is
not recorded, which is the failure that would otherwise be silent until a wrong
finding was in the journal being cited by the next tick.

## The copilot gate is not an enforcement boundary

Say this plainly, because it would be easy to read it as one. The leader runs as
`research` and so does `tick.sh`, so the leader can reach everything the tick
can: `PAUSE`, the tick counters, the disagreement streak, and the journal
working tree. The gate is **advisory**.

What the tick does buy is visibility. The index is **reset** first, then exactly
one path is added (`git add -- <the verified file>`, never `git add -A journal`),
and the staged count is asserted to be 1 before committing — because the leader
shares this uid and can `git add` a rewrite of an older entry itself, which plain
`git diff` does not report and `git commit` would have included. Unauthorised
edits to other tracked entries are reported and **restored**, read NUL-delimited
so a filename containing a space is restored rather than word-split and missed.

`experiment.py`'s own `commit_and_push` **excludes `journal/`** for the same
reason: `tick.sh` writes `PENDING.md` into that workspace before the leader runs,
and the leader's action may be `experiment.py run` — so a blanket `git add -A`
committed and pushed the unverified entry before the copilot ever saw it.

What is actually authoritative lives where the research identity cannot write it:
the pre-registration is hash-chained in the dispatcher's SQLite store, the
metrics come from the root-owned evaluator, and the job history is the
dispatcher's. Treat the journal as narrative projection with exactly the
authority design §4.2 gives it — which is none.

## What is NOT here, and why

- **A dashboard.** `frontier.py` prints markdown; read it.
- **Retries.** A failed tick is a NOOP and the next one starts clean. A retry
  loop around an agent turn spends tokens to reach the same conclusion.
- **A queue writer.** `experiment-queue.md` is in the monorepo, which the
  research identity holds no credential for. The agent reads it and writes its
  own journal. Folding the two together is an operator's job, on purpose:
  it is the one place a human still reads everything.
- **Phase 3 statistics.** Deferred, not rejected. Revisit when the loop is
  producing confirms fast enough that multiple comparisons actually bite.
