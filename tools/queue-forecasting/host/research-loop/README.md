# `research-loop` — the part that invokes an agent

Everything mechanical was already built before this directory existed:
`experiment.py` resolves a config's inputs and runs it end to end, `qf` submits
typed jobs, the root-owned evaluator scores them, `results.sh` reads them back,
and both agent CLIs are installed and authenticated for the `research` user with
`api.anthropic.com` and `api.openai.com` on the egress allowlist. **The only
thing missing was that nothing ever invoked an agent.**

So this is deliberately small — sixteen files against the dispatcher's 21,000
lines — and it must stay that way. The ratio that got this project into trouble was
65,000 lines of platform against 2,700 lines of trainer. Anything here that is
not "invoke an agent" or "stop an agent that is wrong" belongs somewhere else, or
nowhere.

## Files

| file | what it is |
|---|---|
| `tick.sh` | one turn: lock, budget, context, leader, copilot, publish |
| `tick-prompt.md` | the leader's instructions — six actions, first match wins |
| `verify-prompt.md` | the copilot's instructions — six ways to reject a claim |
| `retry-prompt.md` | the leader's REDUCED instructions when a tick is a retry — one action, one target, four bans |
| `prereg.py` | the pre-registration format, written by `experiment.py`, read by `frontier.py` |
| `frontier.py` | the progress artifact: per-series frontier, claims, confirm gate |
| `agent-env.sh` | puts the nvm-installed CLIs and the proxy on PATH for a non-interactive shell |
| `usage.py` | extracts structured CLI usage and appends the central log |
| `install.sh` | turns the loop on and off |
| `pause-issue.sh` | files the pause issue (`open`) and reads it back (`check`): the alarm, and the handle a human releases the brake with |
| `pause-resume-allowlist.txt` | the GitHub logins whose *close* of a pause issue resumes the loop |
| `state-names.sh` | the three state-file basenames `tick.sh` and `pause-issue.sh` must agree on |
| `tick-failure-alarm.sh` | `OnFailure=`: files the pause issue for a tick that died before it could pause itself |
| `qf-tick.{service,timer}` | hourly, as `research` |
| `qf-tick-failure.service` | runs `tick-failure-alarm.sh`; reached only by `qf-tick.service`'s `OnFailure=`, never enabled |

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
result the loop already wrote up stops matching the leader's first action.
Without it that action matches forever and every tick re-narrates the same row
instead of advancing — and `broken` is a permanent state, so refutations had the
same problem. **Escalations are excluded**: an escalated entry was rejected, so
the run it describes is still unwritten.

**Only content committed at `HEAD` counts.** Not "tracked", which is what this
read used to mean and what an earlier version of this section said: the paths
came from `git ls-files` and the bytes came from the **working tree**, so a
tracked entry could be edited in place and change what counted as written up
with nothing committed. Staging it did not count either way and still does not.
The reader is now `git ls-tree -r HEAD` for the listing and `git cat-file
--batch` on the resulting **blob oids** for the bytes, in all three of the flat
namespaces it reads — `journal/`, `journal/escalations/` and `journal/waivers/`.

The reason is the uid. **The leader runs as `research` and `research` owns the
journal**, so anything the loop steers on that is readable from the working tree
is forgeable by the identity being steered: an untracked `journal/anything.md`
citing a run id, a one-word edit to a committed entry, a `git add` with no
commit. What counts as written up — and, since retirement and waivers read
through the same primitive, what counts as rejected and what counts as revived —
therefore has to be a thing that got committed and pushed, which is a thing a
human can see in `git log`.

If the question cannot be answered at all — no repository, no `git`, no `HEAD`,
a failed call, a listing that will not parse — **nothing counts**: every run
reads as unrecorded, no rejection is counted against any run, and no waiver
revives anything. Noisy and safe, in that order, and it is the same convention
everywhere in `frontier.py`. The opposite default would silently retire results
nobody ever committed.

One consequence of reading the whole journal root, and it is guarded rather
than left to discipline: `journal/escalation-kinds.md` (below) sits in that
root, and `_RUN_ID` is loose on purpose — so a run id typed into it, as a
trailing comment naming the run for your own benefit, would have made that run
read as **recorded**. `journaled_run_ids` now skips that file by name, so only
`escalation_targets` reads it. Keep to stamps and kinds anyway: the classifier
line is fully anchored, so a trailing comment does not fail loudly, it just
fails to classify. Waivers avoid the whole question by living in their own
directory, which is why they have one.

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

### The previous rejection goes back to the leader, and only to the leader

A rejected entry went into `journal/escalations/` and the leader never read it,
so every tick was a blind attempt: the same entry could be rewritten three ticks
running against an objection it had never seen. `consecutive-disagreements`
counts those rounds as a drifting leader, so an entry that was one sentence away
from being recordable could instead auto-`PAUSE` the loop.

The tick now extracts the fenced reason block from the newest
`journal/escalations/*.md` and puts it in the leader's context, capped at
`QF_TICK_MAX_FEEDBACK_BYTES` (4096) with an in-band truncation notice. It is
anchored on the `## NOT RECORDED` heading rather than on the file's first fence,
because the rejected entry's own `Evidence:` block is fenced too.

Two properties, both asserted in `test_tick.sh`:

- **It is labelled as feedback and not as evidence, in-band.** The quoted text
  is another agent's prose about a finished run; a figure appearing in it must
  be obtained again from the JSON, the tick facts or a pasted command. The
  copilot cannot see the block, so it will reject any number whose only source
  is it — which is the correct outcome, not a bug.
- **The copilot never receives it.** Showing a verifier its own previous verdict
  anchors it. The question is whether *this* entry is supported by *this* tick's
  numbers, and a copy of what it said last time is an argument, not a number.

This adds no invocation, no revise round and no change to the gate. It is one
file read.

### The copilot may not reject the leader's reason for choosing an experiment

Three of the five rejections on 2026-09-02 were unanswerable. The leader said
"the queue's top unblocked entry asks for the `qctx_d` comparator"; the copilot
said that is in neither the JSON nor the tick facts nor a pasted command, which
was true — the queue is the leader's briefing and the copilot is deliberately
not given it. No rewrite could have satisfied that objection, so the entry could
only escalate, and three of those in a row is the auto-`PAUSE` threshold.

`verify-prompt.md` now scopes the gate to what it was for: a sentence that would
change what a reader believes about a **model, a feature or a bar** gets checked
against the sources; a sentence about which action this tick took is narrative,
and the human reading the escalation judges it. A *count* remains a figure — the
fifth rejection ("all four attempts failed identically", three pasted) stands.

This is the same shape as the tick-facts gap above: a rule that is correct as
written and that the leader cannot possibly satisfy is a broken gate, not a
strict one.

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
**failing** — which `p90_miss_tail` was under v1; under v2 the tail is a
reported metric and the gate to watch is `p90_coverage_guarded` — without accepting an arbitrary
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

## A qctx probe did not fit the probe timeout (sweep optimised 2026-09-01; ceiling raised 2026-09-03)

`spec.py` caps `timeout_s` at **5400** for every kind — 3600 until 2026-09-03,
see "The ceiling was raised too" below — and the cap is deliberately subordinate
to the dispatcher's hold deadline:

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

1. **Raise the chain.** The hold deadline was 7800s, so headroom existed, but it
   is a coordinated constant change in the trusted dispatcher. (Taken later, on
   2026-09-03, once route 2 turned out not to be enough on its own.)
2. **Compute the sweep once.** `[queue_context]` timings show heavy skew — 26 of
   529 queues took 907s of the 3019s — so caching across splits, or sweeping the
   union once, is the real fix. That is trainer work, and it is the rare case
   where platform work unblocks the science rather than displacing it.

### What was done, and what that does and does not establish

Route 2 was taken first, and `TIMEOUT_MAX` was left alone at the time. The per-row sweep
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

### The ceiling was raised too (2026-09-03), because the sweep fix was not enough

Route 2 bought a lot and did not buy enough. Measured on three probes against
the live 6.0M-row cohort:

| stage | 6082 (scored) | 6189 (TIMEOUT) | 6246 (TIMEOUT) |
|---|---|---|---|
| loads, pass 1 | 178s | 213s | 226s |
| qctx sweep 1 | 2515s | 2481s | 2631s |
| quantile fits | ~389s | ~833s | ~690s |
| loads, pass 2 | 79s | 86s | 68s |
| qctx sweep 2 | 230s | killed at start | killed at start |

The fixed cost is within 6% across all three. What varies is **the fit**:
dropping the residual model's `bl_wait_p90` more than doubled it, and adding a
categorical nearly did, because early stopping runs much longer when the model
has to work harder. Both failures died at wall ≈3614s needing ~3950s. So the
programme sat at 95–110% of the ceiling and which configs were runnable depended
on how hard their fit happened to be — not a property anyone can predict before
submitting.

`TIMEOUT_MAX` is now **5400**, and the chain moved with it:
`JOB_HOLD_DEADLINE_S` 7800 → 9600, `LOCK_WAIT_S` 9000 → 11400, giving
`5400+1800+900+120+600 = 8820 < 9600` and `9600+300 < 11400`. Five files change
together — `spec.py`, `qfd.py`'s defaults, `qf-dispatch.service`,
`phase2-setup.sh`, and the tests that pin the shipped figures, `test_spec.py:90`
existing precisely so this cannot be done quietly.

**And the client wait had to move with it.** `experiment.py --timeout` was 5400,
which was headroom over a 3600s job and became *exactly equal* to a 5400s one: a
probe running to its ceiling would outlive the `qf --wait` that submitted it, so
the host would finish the work and the evaluation would never be submitted — a
scored result lost to a client-side clock. It is now 9900, tracking the hold
deadline, and `test_experiment.py` pins it above `spec.TIMEOUT_MAX`.

None of this makes the sweep cheap. Sweep 1 runs at **2410 rows/s** and sweep 2
at **8703 rows/s** — same code, same host, differing only in reference-frame
size per queue (13.2k rows vs 3.8k). Chunking sweep 1 temporally should recover
most of that ~1800s. The ceiling is headroom to do that work in, not a
substitute for it.

## Deploying restarts the dispatcher, which kills in-flight probes

`mirror-refresh` runs `systemctl restart qf-dispatch`, and qfd's startup recovery
fails any job it finds LEASED or RUNNING with no live container
(`qfd.py:4900-4908`, `error_class: reclaimed_at_startup`). A 31-minute hazard run
was destroyed this way on 2026-09-01 — no OOM, no timeout, no exit code, just a
deploy landing mid-experiment.

`sudo -H -u research touch ~research/qf-research/PAUSE`, wait for the heavy lane to clear
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

## The pause-issue token

`pause-issue.sh` files the alarm and reads it back with a **fine-grained PAT on
`lotas/qf-research`, Issues: read and write, and nothing else** — specifically
**not Contents**, so the token cannot touch the journal, which keeps its own
`.git-credentials`. Stored mode **0600**, owned by `research`, at
`/home/research/.config/qf/pause-issue-token`; the path is pinned in
`qf-tick.service` (`QF_PAUSE_TOKEN_FILE`) because the default is invisible and a
token somewhere else fails at the one moment the alarm is needed.

Four distinct permissions are exercised — label create, issue create, issue
read, events read — and **only the first two fail loudly**. A too-narrow token
therefore presents as *a pause nobody can release*, not as an error: the issue
exists, closing it changes nothing, and the loop stays down. Check the scopes
before concluding the release path is broken.

`QF_PAUSE_ISSUE_REPO` is pinned in the same root-owned unit and never derived
from the checkout's `git remote`: that checkout is research-writable, so
deriving it would let the identity being watched choose who gets told.

## The two brakes, and what resets them

Two counters in `~research/.local/state/qf-tick/`, either of which pauses the
loop. They are separate because a drifting leader and a dead verifier are
different mornings, and one counter absorbing both meant a `codex` outage
reported itself as research drift.

| brake / state file | what it counts | knob (committed value) | what resets it |
|---|---|---|---|
| `consecutive-disagreements` | one **rejected target episode** per tick: a usable `DISAGREE` whose target is not a repeat of the last one. A repeat of the *same* suppressible target does **not** advance it — retirement bounds that instead. A target that is missing, malformed, a `probe-` id, ambiguous across two rows, already recorded, retired, or gone from the scoreboard is not suppressible and **always** advances it, as does `none` | `QF_TICK_MAX_DISAGREE` = **3** | an `AGREE`, or an allowlisted close of the pause issue |
| `consecutive-verifier-failures` | one tick that produced no **usable verdict** — exit 0 *and* an anchored final `VERDICT:` line, both halves required. `QF_TICK_COPILOT_TRIES` (3) is **not** part of this: the retry loop retries only on a non-zero exit and breaks at once if the raw output already contains `VERDICT:`, so *one* invocation is enough in the two ordinary cases — exit 0 with prose and no anchored line, and a non-zero exit that had already printed a verdict. Do not go looking in the journal for three attempts | `QF_TICK_MAX_VERIFIER_FAILS` = **3** | any usable verdict, `AGREE` or `DISAGREE` alike, or an allowlisted close |

A third knob is not a brake but decides when a run stops being offered:
`QF_FRONTIER_RETIRE_AFTER` = **2**, the number of counted rejection episodes
that retire a run. It is validated as an integer ≥ 1 and `frontier.py` **refuses
to build the report** on `0`, `-1` or a non-number rather than falling back —
`0` would retire every unrecorded row on sight.

`last-reject-target` is the third state file: the id the drift streak is
currently suppressed against, or the literal `none`. Cleared on `AGREE`,
on a resume, and whenever the stored target stops being suppressible. It is
never written for a verifier failure — an entry nobody judged is no evidence
about which run the leader is stuck on.

Neither counter is ever inferred. A counter that cannot be read or written stops
the tick or pauses it immediately, and the log says which file and why; a
threshold reached because the file was unreadable says so too, so a pause
reading `consecutive-disagreements at 3` cannot hide a broken state directory.

## When the loop pauses

An auto-PAUSE writes `~research/qf-research/PAUSE` and files an issue on
`lotas/qf-research` titled `PAUSED <stamp>: <brake> at <n>`. The brake name in
the title is the diagnosis, and it is there so the tab title alone tells you
which of three mornings this is:

- **`consecutive-disagreements`** — the leader kept being rejected on targets
  that were not a bounded repeat. Research drift. Read the escalations: the
  issue body quotes the reason block of the **three newest files** in
  `journal/escalations/` verbatim under "The last three rejections", so you do
  not have to clone anything to find out why the loop stopped. (Newest three
  *files* — if a verifier outage escalated in between, one of them is that.)
- **`consecutive-verifier-failures`** — three consecutive ticks produced no
  usable verdict. Three *ticks*, not three attempts within one: see the brake
  table for why a single invocation can be the whole failure.
  Infrastructure, not research: check the tinyproxy
  allowlist (`api.openai.com`), `codex login` as the research user, and whether
  `codex` is reachable through `agent-env.sh` at all. Nothing was judged, so no
  finding is in question.
- **`tick-unit-failure`** — the tick died before it could pause itself, and
  systemd's `OnFailure=` filed the alarm. A broken install: an unreadable prompt
  file, a missing workspace, an unset `HOME`, a malformed knob. Here `<n>` is
  **not a count** — it is `MONITOR_SERVICE_RESULT`, the word systemd used
  (`exit-code`, `timeout`, `signal`, or `unknown` on a systemd older than 250),
  and `timeout` versus `exit-code` is the difference between a tick that hung
  past `TimeoutStartSec=4h` holding the training mutex and a `die`. The reason
  itself is not in the issue: `journalctl -u qf-tick.service -n 200`.

**To release it: close the issue.** Comment first if you want to steer the next
tick. The next tick reads the *closing actor* from the issue-events REST API and
resumes only if that login is in `pause-resume-allowlist.txt` — the issue body
lists the authorised logins, precisely so a colleague does not click Close and
achieve nothing. A close by anyone else is logged and the loop stays paused, as
is an open issue, a repo that disagrees with the pinned `QF_PAUSE_ISSUE_REPO`, a
`PAUSE` whose stamp is not in the issue's body, and every API error.

**Closing is the only release gesture — a comment alone changes nothing.**
Comments from allowlisted logins become `human-directive.md`, handed to the
**leader only**, labelled an instruction and explicitly not evidence; the
verifier never sees it, so a figure you type into a comment still cannot be
cited and will be rejected if the leader tries. Comments from anyone else are
counted and mentioned, never promoted — the pause token can author comments too,
and an unfiltered fetch would let the loop write its own instructions.

On an authorised close the resume zeroes both counters, sets
`last-reject-target` to `none`, reads all three back, and removes `PAUSE`
**last**. Any failure before that leaves the brake on and retries next tick;
the reverse order is the one that must never happen, because `PAUSE` gone with a
streak still at its threshold re-pauses on the first rejection.

Two things worth knowing before you close one:

- **A `tick-unit-failure` you have not actually fixed comes straight back.** The
  resume removes `PAUSE`, the next tick dies in the same place, `OnFailure=`
  writes a fresh `PAUSE` with a fresh stamp, and that is a **new** issue. One
  new issue per failed fix attempt, not one per hour — the stamp is what bounds
  it.
- **`systemctl stop qf-tick.service` over a running probe can file one.** A
  clean stop ends `inactive (dead)` and alarms nothing, but a cgroup that does
  not die inside `TimeoutStopSec=90` is SIGKILLed, the result becomes `timeout`,
  the unit enters `failed` and the alarm fires. Recoverable: close the issue.
  `install.sh off` cannot do this — it stops the **timer**, and a timer cannot
  put the service into `failed`.

## When a run is retired

A run that was written up and **rejected twice** stops being offered. The report
carries one line per retired row:

```text
RETIRED: evaluate-… — 2 rejections, never written up, latest
escalations/20260904T111002Z.md. Not selectable; revive with a committed
waivers/<stamp>.md carrying `**Waiver:** evaluate-…`.
```

The `written up` column renders `retired` rather than `NO`, the row is dropped
from the "needs writing up" shortlist the leader actually picks from, the JSON
row carries `handling: "retired"`, and `tick-prompt.md` says a `retired` row is
not `NO` and must not be chosen. Every steering surface, because retirement is
only an escape if all of them agree — a retired row left in the shortlist is
the livelock again whatever the counter says, and that shortlist is the table
the leader picks from.

This is deliberate, and it has a cost that is also deliberate: **a correct
finding can be given up on.** Three ticks on 2026-09-04 each rewrote the same
finished run and were each rejected on incidental prose — a rounding word, then
a false superlative — while the central result (`p90_miss_tail` 0.298876182 →
0.280830734) was never disputed and is still sitting in an escalation file. The
loop cannot be made to record a finding the gate keeps refusing. It can only be
made to stop paying for the attempt.

To revive one, commit a waiver. Only the run id line is machine-read; the rest
is for whoever reads `git log`:

**As `research`, never as root.** The journal is research-owned and keeps its
own `.git-credentials`; the pause token deliberately has no Contents scope, so
a root `git push` has no credential at all and the commit leaves root-owned
objects in `research`'s `.git` — a repository the loop can no longer push to,
whose remedy is the `sudo chown -R` that `phase2-setup.sh` already has a die
message for. Every block below is wrapped for that reason.

```bash
sudo -H -u research bash -lc '
  set -e
  cd ~/qf-research
  mkdir -p journal/waivers
  printf "**Waiver:** %s\n\n%s\n" \
    evaluate-20260904T090941Z-0a217f40da8f-6446 \
    "Reopening: the tail result is real and both rejections were about prose." \
    > "journal/waivers/$(date -u +%Y%m%dT%H%M%SZ).md"
  git add journal/waivers
  git commit -m "waiver: reopen the 09-04 tail result"
  git push'
```

Four things it will not tolerate, each of which fails silently or nearly so:

- **The filename must start with a `YYYYMMDDTHHMMSSZ` stamp.** That prefix is
  the waiver's date, and the date is the whole mechanism. A file without one
  revives nothing and is reported as `UNPARSEABLE WAIVERS` (below).
- **The stamp must name a real instant, and must not run ahead of the
  journal.** `20261345T996000Z` has the right shape and sorts above every real
  2026 stamp; a typo'd year does the same. Either one would put the waiver's
  floor above every escalation that will ever exist, so the run stays
  `unrecorded`, the leader keeps being told to retry it, the drift streak never
  advances because the target never changes, and **retirement can never reach
  its threshold** — the 2026-09-04 livelock with both brakes disabled, from one
  filename. So a stamp is refused unless it parses as a real timestamp and sits
  within **7 days** of the journal's own newest committed stamp (entries and
  escalations, never other waivers — one mistyped waiver must not certify the
  next). No clock is read: two committed stamps are compared, so the answer is
  the same on any box, including one whose clock is wrong. A refused stamp
  revives nothing and is reported as `MISDATED WAIVERS` (below).
- **The id must be an `evaluate-` id**, matched whole. A `probe-` id, a
  comparator, or a typo waives nothing — one probe can be evaluated under two
  contracts, so a probe id does not identify a row.
- **One anchored line, spelled `**Waiver:** <id>`** — the label in bold with
  the colon inside it, the id after it, nothing else on the line. Prose
  mentioning the run id anywhere else in the file does nothing.
- **It must be committed** (and, in practice, pushed — the loop reads its own
  checkout, but a waiver only you have is a waiver nobody can see). An
  uncommitted file in `journal/waivers/` is invisible for the reason the section
  above gives: the leader shares this uid.

**A waiver starts a new episode; it does not decrement a total.** It moves a
floor: escalations committed **at or before** the waiver's stamp stop counting,
and escalations committed after it count in full. So rejections already on file
are spent, and **two fresh rejections retire the run again** — which is the
point. Subtraction would fail in the only case that matters: three rejections
against a threshold of two, minus one, still retires.

And plainly: **do not delete or move escalation files to un-retire a run.** They
are the record of what the gate refused, and both 2026-09-04 refusals were
correct. Thinning that trail to change the loop's behaviour destroys the only
evidence that the refusals were right — and it is the reason the waiver is a
separate committed file in its own directory rather than an edit to anything.

### `UNCLASSIFIED ESCALATIONS` and `UNPARSEABLE WAIVERS`

Two report lines, both naming their files rather than counting them, because
"classify them" and "rename them" are not actionable without knowing which:

```text
UNCLASSIFIED ESCALATIONS 4 escalation(s) in escalations/: 20260902T…Z.md, …
  -- no `Escalation kind:` line, so they are NOT counted toward retirement.
  Classify them in journal/escalation-kinds.md.
```

Every escalation the tick writes now carries `Escalation kind: rejection` or
`Escalation kind: verifier-failure`, and **only `rejection` retires anything**.
Files written before that line existed are `unknown`, and `unknown` counts
toward nothing — **deliberately, and it is not a bug to be tidied away**. That
backlog contains both real rejections and `codex` outages, and defaulting them
to `rejection` would retire runs nobody ever judged, which is the precise harm
retirement exists to prevent. The visible line is the compromise: retirement is
disabled for the runs they name, and you can see that it is.

Classify them in `journal/escalation-kinds.md` — one `<stamp> <kind>` per line,
tab or spaces, nothing else on the line, committed:

```bash
sudo -H -u research bash -lc '
  set -e
  cd ~/qf-research
  cat >> journal/escalation-kinds.md <<EOF
20260904T101126Z rejection
20260904T111002Z rejection
20260902T113305Z verifier-failure
EOF
  git add journal/escalation-kinds.md
  git commit -m "classify the pre-kind-line escalations"
  git push'
```

**Classifying can retire the run you were about to revive, immediately.** Two
`rejection` lines against one run *is* the threshold, and the two stamps above
are the 2026-09-04 pair — the same run the section above argues is worth
reviving. That is not a bug in either half; it is retirement working on
evidence it could not previously read. So **re-read the `RETIRED:` lines after
you commit a classification**, and if one of them is a run you want back, the
undo is a waiver — committed after the classification, so its floor covers it.

The stamp is the escalation's filename without `.md`. This record **overrides**
a file's own `Escalation kind:` line when both exist, on purpose: it is there so
a human can correct a classification, not only supply a missing one. Remember
the trap from "The journal is an input too" — no run ids in this file, ever.

```text
UNPARSEABLE WAIVERS 1 file(s) in waivers/: reopen-the-tail-result.md
  -- no `<stamp>` filename prefix, so they revive NOTHING. Rename them
  `YYYYMMDDTHHMMSSZ.md` and commit.
```

```text
MISDATED WAIVERS 1 file(s) in waivers/: 20270908T000000Z.md
  -- stamped more than 7 day(s) ahead of the journal's newest committed
  stamp, so they revive NOTHING. Re-stamp and commit.
```

Same remedy as the line above — `git mv` it to a real stamp and commit. Do not
"fix" it by back-dating an escalation instead: the escalations are the record of
what the gate refused, and the waiver's date is the only thing that says which
of them a human has forgiven.

```bash
sudo -H -u research bash -lc '
  set -e
  cd ~/qf-research
  git mv journal/waivers/reopen-the-tail-result.md \
         "journal/waivers/$(date -u +%Y%m%dT%H%M%SZ).md"
  git commit -m "waiver: give it a stamp so it does something"
  git push'
```

## Knobs the unit pins, and the two places they can be overridden

`qf-tick.service` pins the `QF_*` knobs, and two separate checks exist because
the unit is not the only thing that sets them. Both **report**; neither refuses,
because a forgotten override must not block the deploy that ships its fix.

**A `systemctl edit` drop-in** is invisible to a file comparison — that is how
the box ran `QF_TICK_MAX_DISAGREE=5` against a committed `3` for weeks.
`mirror-refresh --check` now compares the *effective* environment and prints:

```text
qf-tick.service effective-environment drift: DRIFT QF_TICK_MAX_DISAGREE repo=3 live=5
```

Remedy: `systemctl cat qf-tick` to see the drop-in, `systemctl revert qf-tick`
to drop it. A declared knob's value is printed because it is a reviewed
threshold; an unexpected `QF_*` key is printed **by name only**, because its
value is unreviewed and could be anything.

**`~research/.profile` is the other one, and it wins.** `ExecStart` is
`/bin/bash -lc` deliberately — the proxy variables and the agent CLIs' paths
live in that profile, and without it git fails with a fake five-millisecond
connection error. But a login shell runs *after* systemd has set the unit's
environment, so `export QF_TICK_MAX_DISAGREE=5` in the profile silently beats
the unit. The same refresh now scans it:

```text
PROFILE-OVERRIDE   QF_TICK_MAX_DISAGREE ~/.profile unit=3 profile=5
PROFILE-DUPLICATE  QF_FRONTIER_RETIRE_AFTER ~/.profile value=2 (matches today)
PROFILE-UNEXPECTED QF_EXPERIMENTAL_THING ~/.profile (name only)
PROFILE-UNSCANNED  ~/.profile (1 source line(s) cannot be resolved literally)
```

A `DUPLICATE` is not an error today — it is a second source of truth for the
same value, and it will diverge the moment one side changes. `UNSCANNED` is the
honest half: this is a textual scan, so it follows `source` lines it can resolve
literally (three levels deep) and says so when it cannot. It does not see a
value set by `eval`, by command substitution, or through `$BASH_ENV`, and it
deliberately skips `/etc/profile*` — root-owned, the same trust domain as the
unit, whereas `~research` is writable by the identity being watched.

**So the tick also checks itself.** Because the scan runs at deploy and the tick
runs hourly, `tick.sh` compares its own effective `QF_*` against
`systemctl show -p Environment qf-tick.service` at startup. The delta between
what systemd configured and what the tick actually holds *is* the profile
injection, with no dot-file parsing at all. It logs, it never dies, and when the
loop pauses the delta is carried into the issue body — if the loop is about to
pause with `MAX_DISAGREE` effectively 5 while the unit says 3, that belongs
where a human is already looking.

Two kinds of expected noise, so you do not chase them:

- **A hand-run tick reports every declared knob as `MISSING`.** `install.sh
  once` and the tests do not have the unit's environment. That is correct, not
  a fault — and it is another reason to trip things with `systemctl start
  qf-tick.service` instead.
- **`UNVERIFIED` means "could not ask", not "clean".** No `systemctl`, a
  non-zero `systemctl`, a unit that is not `loaded`, or no `Environment`
  property all report it, and every escalation from that tick carries the line.
  A pass over a question nobody asked is the failure being fixed here.

## What a retry tick looks like, so you do not read it as a stall

When the previous tick's rejection has a live target, the next tick is a
**retry**, and it deliberately looks narrower than a normal tick. In
`journalctl -u qf-tick.service` it announces itself:

```text
[tick HH:MM:SS] this tick is a RETRY of evaluate-… (still unrecorded)
```

Three things are different, and none of them is the loop being stuck:

- **The action is closed to one named target.** The leader is told this tick is
  action 1 on that exact row and the other five actions are unavailable: no new
  experiment, no different row. So a retry tick submits nothing and spends no
  probe budget — it does still cost a leader turn and one of the twelve daily
  ticks, which is what retirement exists to bound.
- **The entry template is reduced**, not rewritten: `retry-prompt.md` replaces
  the six-action template with one fixed shape and four bans — no
  rounding-equivalence, no superlatives or firsts, a title that names only the
  action and the config, and no comparator other than the pre-registered `vs`.
  Each ban is traceable to a rejection that actually happened. The premise is
  that **rewriting resamples the defect surface**: on 2026-09-04 round 2 fixed
  the defect it was told about and introduced a new one, so a retry must shrink.
- **The target's frontier row is pasted into the context at full precision**,
  with its comparator, because the leader receives `frontier.md` and not
  `frontier.json` — the markdown carries none of `config_digest`, `vs`, `tol`
  or the row's own measured value. Without the paste the template would mandate
  fields whose only sources are a blank or a remembered number, which is the
  rejection it exists to prevent.

A retry that cannot be assembled whole degrades to an **ordinary** tick and says
so loudly (`WARNING could not paste the row for retry target …`). Half a retry
block — the closed action without the bans, or the template without the row — is
worse than none. And if you see the same run retried repeatedly, that is bounded:
two counted rejections retire it.

## Releasing the pause the box is in now

The live `PAUSE` dates from 2026-09-04 and predates all of this. Two routes.

**Preferred, once the loop is deployed and the token is installed:** do
nothing. "Deployed" is load-bearing, and it is three separate things:

1. **The new `research-loop/` is in the trusted checkout.** `tick.sh` warns and
   stops if `pause-issue.sh` is not there and executable, which is the state of
   any box whose `/srv` predates this change.
2. **`install.sh on` has been re-run since.** The units are what pin
   `QF_PAUSE_ISSUE_REPO` and `QF_PAUSE_TOKEN_FILE` and what wire
   `OnFailure=qf-tick-failure.service`; until they are reinstalled,
   `pause-issue.sh` exits 1 with the repo unset and files **nothing**.
3. **The timer is armed.**

```bash
# 1 — is the new code there at all:
ls -l /srv/queue-forecasting/tools/queue-forecasting/host/research-loop/pause-issue.sh
git -C /srv/queue-forecasting log --oneline -1

# 2 — are the INSTALLED units the checkout's, and is the environment drifted:
sudo /srv/queue-forecasting/tools/queue-forecasting/host/phase2-setup.sh \
  mirror-refresh --check
# then, idempotently, and read its output rather than its exit status:
sudo /srv/queue-forecasting/tools/queue-forecasting/host/research-loop/install.sh on

# 3 — armed, and paused or not:
sudo /srv/.../host/research-loop/install.sh status
```

`install.sh on` is where the preflights live — the token's existence and its
`0600`, the pinned `QF_PAUSE_ISSUE_REPO`, and the allowlist — checked against
**both** units, because `qf-tick-failure.service` restates those directives
rather than inheriting them and can therefore diverge from them. So **its
output is the verification**, and it is safe to re-run on a live box: it
reinstalls the units, reloads, and re-enables an already-enabled timer, and it
does not touch a running tick.

**`install.sh status` reports; `install.sh on` refuses.** `status` is read-only
and never dies: it reads the timer and `PAUSE`, compares both installed units
against the checkout, and runs the repo, token and allowlist preflights against
the **installed** units — so it does answer "can this box file and release a
pause". What it cannot answer is whether the checkout itself is current, which
is `mirror-refresh --check`'s question, and it will not fix anything it finds.
`on` is the authoritative verification because it reinstalls *before* it
checks.

Given all three, that `PAUSE`
carries no `issue:`/`stamp:` binding, so the next tick's `check` recovers its
stamp (from its own `auto-paused <stamp>:` line, else the file's mtime — never
from `date`, which would file a fresh duplicate hourly), **files the issue for
it**, and stays paused. Close that issue as an allowlisted login and the resume
path zeroes the counters for you, in the verified order. This is worth
preferring for a reason beyond convenience: it exercises the release path you
will actually depend on, on a pause where being wrong costs nothing.

**By hand**, if the token is not in place yet or you do not want to wait an
hour. Removing `PAUSE` alone is **not enough** — the drift counter is still at
its threshold, so the loop would re-pause on the first rejection:

```bash
sudo -H -u research bash -lc '
  set -e
  # THE SAME EXPRESSION tick.sh AND pause-issue.sh USE. Neither unit pins
  # QF_TICK_STATE or XDG_STATE_HOME, so an XDG_STATE_HOME set in
  # ~research/.profile moves the real directory and a hardcoded
  # ~/.local/state path would write three decoys beside a live streak.
  S="${QF_TICK_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/qf-tick}"
  echo "state: $S"
  mkdir -p "$S"
  printf "%s\n" 0    > "$S/consecutive-disagreements"
  printf "%s\n" 0    > "$S/consecutive-verifier-failures"
  printf "%s\n" none > "$S/last-reject-target"
  # THE READ-BACK GATES THE REMOVAL. Not printed for eyeballing: `set -e` plus
  # `test` means PAUSE is removed only if all three really hold those values.
  test "$(cat "$S/consecutive-disagreements")"     = 0
  test "$(cat "$S/consecutive-verifier-failures")" = 0
  test "$(cat "$S/last-reject-target")"            = none
  rm -f ~/qf-research/PAUSE
  echo "released"'
```

**Every line before the `rm` is a gate, and that is the whole point of the
order.** Without `set -e` and those three `test`s, a state directory that does
not exist yet — or one moved by an `XDG_STATE_HOME` in `~research/.profile` —
lets all three writes fail while `rm -f` runs anyway: `PAUSE` gone,
`consecutive-disagreements` still at 3, one tick, and a re-pause on its first
rejection. That is precisely the failure this ordering exists to prevent, and
an eyeballed `cat` does not prevent it. If it prints `released`, it worked; if
it prints nothing after `state:`, nothing was removed.

The three basenames are defined once in `state-names.sh` and shared by
`tick.sh` and `pause-issue.sh`; if you are typing them from memory, read that
file — a rename done by halves makes the resume path's own read-back
self-confirming.

## Verifying this on the host

Everything below was written against a container with no network, no `gh` and no
running systemd, so none of it is proven. It is all cheap to check on the box,
and each item names the failure it is checking for.

**Use the unit, not `install.sh once`.** The unit is sandboxed
(`ProtectSystem=strict`, `PrivateTmp=no`) and a hand-run tick is not, so a hand
run cannot prove a sandbox fix — that is exactly how the 2026-09-02 `EROFS` on
`/tmp/claude-<uid>/…` hid for a day: the hand run created the directory and the
sandboxed ticks quietly reused it until `/tmp` was cleaned.

```bash
sudo systemctl start qf-tick.service
journalctl -u qf-tick.service -n 100 --no-pager   # no EROFS, and a stated reason
```

**1. The host's `gh` accepts the flags these scripts use.** A rejected flag is
an alarm that never files and a pause that cannot be released. Run as the
research user, with only the pause token in scope, exactly as `gh_()` does:

```bash
sudo -H -u research bash -lc '
  export GH_TOKEN="$(cat ~/.config/qf/pause-issue-token)" GH_PROMPT_DISABLED=1
  gh label list --repo lotas/qf-research --search qf-pause --limit 50 \
    --json name --jq ".[].name"
  gh issue list --repo lotas/qf-research --state all --search 20260904T101126Z \
    --limit 50 --json number,body --jq ".[] | .number"'
```

**2. `gh issue create` prints only the URL on stdout.** The issue number is
parsed as `${url##*/}`, so a banner *before* the URL survives (the last `/` is
still the URL's) but anything printed *after* it does not — and a non-numeric
answer binds nothing, leaving a pause with no release handle until the next tick
re-files. Create one scratch issue and look at raw stdout:

```bash
sudo -H -u research bash -lc '
  export GH_TOKEN="$(cat ~/.config/qf/pause-issue-token)"
  gh issue create --repo lotas/qf-research --title "scratch: stdout shape" \
    --body "delete me" | cat -A | tail -3'
```

**3. `gh api --paginate --jq` emits one line per match, not per page.** The
closing actor is `awk 'NF { last = $0 }'` over that stream because `gh` applies
`--jq` per page; a page-spanning filter would emit one answer per page and
authorise nobody. Against an issue that has been closed, reopened and closed:

```bash
# Against an issue that has been closed, reopened and closed again:
sudo -H -u research bash -lc '
  export GH_TOKEN="$(cat ~/.config/qf/pause-issue-token)"
  gh api repos/lotas/qf-research/issues/<n>/events --paginate \
    --jq ".[] | select(.event == \"closed\") | (.actor.login // \"\")"' \
  | tee /dev/stderr | wc -l
```

Expected: **one line per `closed` event** — two for the issue described, and
the last line is the login that authorised anything. The failure shape is a
count that tracks the number of *pages* instead, or a single line containing
several logins; either means `--jq` is being applied per page and
`awk 'NF { last = $0 }'` would authorise the wrong login or nobody.

**4. GitHub's search index finds a just-created issue by a body substring.** The
idempotency key is the stamp in the body, searched across `--state all`. If the
index lags, a create that succeeded but did not get bound files a **duplicate**
next tick. Create the scratch issue from item 2 with a unique stamp in the body,
then immediately `gh issue list --state all --search <that stamp>` and see
whether it comes back. If it does not, expect duplicates and say so in the
issue body rather than trusting the key.

**5. `gh api` exits non-zero on HTTP 4xx/5xx.** Every "stay paused" branch in
`check` is a `|| return 1` on a `gh api` exit status; if a 403 exits zero with
an error body, an API failure reads as an answer. Check with a path that must
fail:

```bash
sudo -H -u research bash -lc '
  export GH_TOKEN="$(cat ~/.config/qf/pause-issue-token)"
  gh api repos/lotas/qf-research/issues/999999999; echo "exit=$?"'
```

**6. `gh label create` really exits non-zero on an existing label.** The
preflight determines existence with a *read* precisely so this does not matter,
but `cmd_open`'s comment asserts it, and an assertion nobody checked is how
that bug arrived in the first place:

```bash
sudo -H -u research bash -lc '
  export GH_TOKEN="$(cat ~/.config/qf/pause-issue-token)"
  gh label create qf-pause --repo lotas/qf-research \
    --description "auto-paused research loop" --color B60205; echo "first=$?"
  gh label create qf-pause --repo lotas/qf-research \
    --description "auto-paused research loop" --color B60205; echo "second=$?"'
```

Expected: `second=` **non-zero** (the label already exists after the first, or
after any pause the loop has ever filed). If it is `0`, the comment is wrong
and should be corrected — the code is right either way.

**7. `MONITOR_SERVICE_RESULT` is delivered.** It is set for `OnFailure=` units
since systemd **250**; older systemd gives `unknown`, which is honest but tells
you less. `systemctl --version | head -1`, and then read the title of the issue
item 8 files.

**8. `OnFailure=` actually fires, end to end.** The cheapest reversible way to
trip a `die` **above** the PAUSE check is a malformed knob, which
`tick.sh` validates before it touches any state. Do this with `PAUSE` absent:

`systemctl edit` is interactive, so write the drop-in directly — the whole
block pastes:

```bash
sudo mkdir -p /etc/systemd/system/qf-tick.service.d
sudo tee /etc/systemd/system/qf-tick.service.d/zz-trip.conf >/dev/null <<'EOF'
[Service]
Environment=QF_TICK_MAX_TICKS=bogus
EOF
sudo systemctl daemon-reload
sudo systemctl start qf-tick.service                   # expected: FAILS
journalctl -u qf-tick.service -n 20 --no-pager         # "MAX_TICKS must be a
                                                       #  non-negative integer"
journalctl -u qf-tick-failure.service -n 40 --no-pager
sudo rm -f /etc/systemd/system/qf-tick.service.d/zz-trip.conf
sudo systemctl daemon-reload
```

(`sudo systemctl revert qf-tick.service` also removes it — and everything else
in that directory, which is the point of item 10 but not of this one.)

Then check all four consequences: the journal shows **no** `Failed to enqueue
OnFailure job` (an `OnFailure=` naming a unit nothing installed is a hole that
looks closed); `qf-tick-failure.service` ran to completion under
`ProtectSystem=strict` with `gh` reaching the proxy rather than dying on
nftables — the failure to watch for is `Failed to connect to github.com port 443
after 5 ms`, which reads as a credential problem and is a missing
`~/.profile`; a `PAUSE` exists carrying `tick-unit-failure`; and an issue was
filed titled `PAUSED <stamp>: tick-unit-failure at exit-code`.

Reverting the drop-in does **not** unpause the box — that `PAUSE` is real and
its issue is the release handle, which is exactly what item 9 then exercises.

**9. The full release path, on that same issue.** Comment on it, close it as an
allowlisted login, and start a tick:

```bash
sudo systemctl start qf-tick.service
journalctl -u qf-tick.service -n 40 --no-pager
```

Expect `RESUMED: lotas/qf-research#<n> closed by <login> (stamp …); counters
zeroed`, then `resumed by an allowlisted close; continuing this tick`, then the
tick proceeding with your comment in the leader's context and not the copilot's.
Worth also closing one as a **non**-allowlisted login once and seeing the loop
stay paused with the actor logged — that is the half of the gate that is easy to
believe without evidence.

Two housekeeping notes. The resumed tick is a **real** tick: it may pick action
4 or 5 and submit a probe, so do this when the daily budget can afford one, or
`sudo -H -u research touch ~research/qf-research/PAUSE` again once you have read the resume line.
And close (or delete) the scratch issues from items 2 and 4 — they carry no
stamp any `PAUSE` names, so they cannot release anything, but an open
`qf-pause`-shaped issue in that repo is a thing somebody will read at 3am.

**10. The threshold drop-in is reported, and revert clears it.** A `systemctl
edit` drop-in raising `QF_TICK_MAX_DISAGREE` to 5 ran live for weeks, unreviewed
and invisible to a check that compares unit *files*. The comparison is now over
the **effective** environment and runs on every deploy:

```bash
sudo /srv/queue-forecasting/tools/queue-forecasting/host/phase2-setup.sh \
  mirror-refresh --check
```

Expect `qf-tick.service effective-environment drift: DRIFT QF_TICK_MAX_DISAGREE
repo=3 live=5`. It is **reported, not refused** — a drop-in is a deliberate
human act, and dying here would let one forgotten drop-in block the deploy that
ships its fix. The committed value stays **3**; that drop-in is drift to remove,
not tuning to bless:

```bash
sudo systemctl revert qf-tick.service && sudo systemctl daemon-reload
sudo /srv/.../host/phase2-setup.sh mirror-refresh --check   # drift line gone
```

A key present only in the drop-in is reported **by name with no value** (its
value is unreviewed by definition and this output goes to a deploy log); a
declared key is reported with both sides, because reading "repo=3 live=5" is the
entire point.

## Running it

```bash
# See what the leader would see, invoke nothing:
sudo -H -u research bash -lc '/srv/.../host/research-loop/tick.sh --dry-run'

# One turn, in the foreground:
sudo /srv/.../host/research-loop/install.sh once

# Turn the schedule on / off:
sudo /srv/.../host/research-loop/install.sh on
sudo /srv/.../host/research-loop/install.sh off

# Stop the next tick without touching the timer. AS `research`, like every
# other write into that tree: a root-owned file in a directory the loop owns is
# the shape that already cost this project a `chown -R` inside `.git`.
sudo -H -u research touch ~research/qf-research/PAUSE

# What is installed, when it next fires, whether it is paused:
sudo /srv/.../host/research-loop/install.sh status

# Read a pause without waiting for a tick (does not resume unless it may):
sudo -H -u research bash -lc \
  'QF_PAUSE_ISSUE_REPO=lotas/qf-research \
   /srv/.../host/research-loop/pause-issue.sh check ~/qf-research/PAUSE'
```

**A hand-written `PAUSE` now files an issue too**, and it will look odd: the
next tick finds a file with no `issue:`/`stamp:` binding, dates it from the
file's mtime and files the alarm, so the title comes out as
`PAUSED <stamp>:  at ` — the brake and the count are parsed out of an
`auto-paused` line a `touch` never wrote. Harmless, and it is the right
trade (a pause nobody was told about is the failure this whole mechanism
exists for). There is no way to make it quiet — a `PAUSE` carrying
`auto-paused` and `stamp:` but no `issue:` still hits the unbound branch and
still files an issue. Either remove the file again rather than closing the
issue, or make the issue it files readable:

```bash
sudo -H -u research bash -lc '
  S="$(date -u +%Y%m%dT%H%M%SZ)"
  printf "auto-paused %s: operator-hold at 1\nsee (held by hand)\nstamp: %s\n" \
    "$S" "$S" > ~/qf-research/PAUSE'
```

As `research`, like every other write into that workspace. A root-written
`PAUSE` happens to work — `pause-issue.sh` replaces the file rather than
editing it in place, and the directory is research-owned — but it leaves a
root-owned file in a tree the loop owns, and that is the shape that has already
cost this project a `chown -R` in `.git`.

Tests: `host/tests/test_prereg.py`, `test_frontier.py`, `test_tick.sh`,
`test_pause_issue.sh`, `test_tick_failure_alarm.sh`, `test_unit_drift.sh`. The tick
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
