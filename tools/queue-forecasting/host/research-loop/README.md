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
| `install.sh` | turns the loop on and off |
| `qf-tick.{service,timer}` | hourly, as `research` |

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
