# Research loop: escaping the rejection deadlock

Design, 2026-09-07. Fixes the failure that cost 2026-09-04 through 2026-09-07.

## What happened

Three ticks on 2026-09-04 (09:13, 10:07, 11:06) each picked the same finished
run, `evaluate-20260904T090941Z-0a217f40da8f-6446`, wrote it up, and were
rejected. The third rejection hit `QF_TICK_MAX_DISAGREE` and wrote `PAUSE`.
Sixty subsequent ticks logged `PAUSE exists; stopping`. Nothing told anyone.

Both rejections were correct, and both were about sentences that were not the
finding:

- `20260904T101126Z` — the entry said `within_2x` was "unchanged to four
  decimals"; the supplied values round to 0.0817 and 0.0818. A rounding word.
- `20260904T111002Z` — a false superlative ("the first quantile config"), when
  `val7_nop90` had already reached `p90_miss_tail=0.286152780`; plus a title
  calling a −1.43pp MAE move an "improvement".

The central claim was never disputed in either round: `p90_miss_tail`
0.298876182 → 0.280830734 off a one-key config diff, with `p90_coverage` moving
*down* rather than up. That result is sitting in an escalation file, correct and
unrecorded.

### Three independent defects

1. **A livelock, not a pause.** `frontier.py:journaled_run_ids` counts only
   `.md` files directly in `journal/`; `escalations/` is excluded by design and
   for a good reason (a rejected claim must not retire the result it was about).
   The consequence was unintended: a run whose write-up keeps being rejected
   stays `unrecorded` forever, `tick-prompt.md` action 1 keeps matching it, and
   the loop re-narrates the same row at ~$1.10 per tick indefinitely. The
   auto-PAUSE did not cause the outage — it capped it at three ticks.

2. **Rewriting resamples the defect surface.** Round 2 fixed the four-decimals
   defect and introduced a new false sentence. Every hedge, superlative and
   contextual aside is another rejectable surface, and the leader adds more of
   them each round because the feedback it receives is a rejection.

3. **No alarm.** `PAUSE` is a file on a box. The escalation is pushed to
   `lotas/qf-research`. Neither reaches a human.

A fourth, latent: the deployed unit runs `QF_TICK_MAX_DISAGREE=5` while
`qf-tick.service:39` says `3`. `unit_matches` in `phase2-setup.sh` compares unit
*files*, so a `systemctl edit` drop-in is invisible to it.

## Non-goals

- No revise-round inside a tick. Ruled out previously and still ruled out.
- No weakening of the gate. Both Sep 4 rejections were right and must stay
  rejections.
- `QF_TICK_MAX_DISAGREE` is not raised. The committed value stays **3**; the
  live `5` is treated as undocumented drift to be reported and removed, not
  blessed.

---

## 1. An explicit handling state, and retirement

### 1.1 Committed content, not tracked paths

`journaled_run_ids` currently resolves tracked paths with `git ls-files` and
then reads those paths from the **working tree** (`frontier.py:227`). The leader
shares the uid, so it can edit an already-tracked entry and change what counts
as recorded without committing anything.

Introduce one primitive and route both readers through it:

```python
def _committed_journal_blobs(journal_dir, subdir=None):
    """(path, text) for every `.md` blob in HEAD under journal_dir[/subdir].

    `None` -- no repo, no git, no HEAD, or a failed call -- means "nothing is
    known", and every caller must read that as "nothing counts" rather than
    "everything counts".
    """
```

Implemented as `git -C <dir> ls-tree -z -r HEAD` — full entries, not
`--name-only` — and the **blob object ids** are fed to `git -C <dir> cat-file
--batch`, one pipe rather than a process per file. Object ids rather than path
expressions on purpose: `ls-tree` run with `-C <journal-dir>` prints paths
relative to that directory, while `cat-file`'s path syntax wants repository-root
paths (or a `HEAD:./<path>` form), so passing the paths through invites a
silently empty result. An oid needs no prefix and cannot be misresolved. `PENDING.md` cannot appear (it is never
committed). Depth is enforced on the returned paths, not by the pathspec: no
slash for the journal root, exactly `escalations/<name>` for escalations.

`journaled_run_ids` keeps its exact current semantics and its docstring, and
only changes where the bytes come from. Its "escalations are deliberately
excluded" paragraph gains a pointer to §1.3, so a future reader does not
mistake retirement for a reversal of that decision.

### 1.2 A machine-readable target

A rejected entry cites its target, its comparator, the probe behind it and the
evaluation of it. Hashing every `_RUN_ID` match therefore mis-identifies the
target four ways: adding a comparator looks like drift, one rejection could
retire several runs, a probe and its evaluation both count, and a rewrite that
switches from citing the probe to citing the evaluation looks like a new target.

`tick-prompt.md`'s entry template gains a required first field:

```markdown
**Target run:** <a single run id, or the literal `none`>
```

Parsed by exactly one regex anchored on that label, yielding at most one id.
**Line endings are normalized before matching, and disagreeing matches are
refused.** Both were found by review with reproductions: a `\r\n`-bodied
escalation matched nothing at all — contributing to neither the counts nor the
`unclassified` total, so it vanished with no operator-visible trace — and a
target line *quoted* inside an `Evidence:` block resolved ahead of the real
field, which would count a rejection against an innocent run while the rejected
one never retires. So all matches are collected: identical ones are one episode,
and disagreeing ones yield no target at all.
`none` is the honest answer for a tick that submitted nothing and wrote up
nothing — an action-6 waiting entry.

An entry with no parseable `**Target run:**` line is treated as `none`. It is
**not** rejected — that would be a new gate, and the gate is not this design's
business — but `none` is conservative in both directions: it retires nothing,
and it always advances the drift streak (§2).

Run ids appearing anywhere else in an entry, `Evidence:` included, are read as
prose. A comparator can never be retired incidentally.

**Evaluation ids only.** The target must be an `evaluate-` id, never a `probe-`
one. `frontier.py`'s `index` maps an id to a *list* of rows precisely because one
probe can be evaluated under two contracts, so a probe id does not identify a
row — and retiring on an ambiguous id would retire two series' worth of work
from one rejection. A well-formed target that does not resolve to exactly one
frontier row retires nothing and does not enter retry mode (§3.1); it is reported
as unresolvable, because that means either a stale target or an id that has
stopped appearing in the scoreboard, and both are worth seeing.

### 1.3 Retirement

`escalation_targets(journal_dir)` returns, per run id, the count of **distinct
committed escalation files** whose `**Target run:**` field is that id. Distinct
files, not regex matches: one escalation is one rejection episode however many
times it names the run.

**Only a real rejection counts.** `tick.sh` also writes an escalation when the
copilot never returned a verdict at all, and two `codex` outages must not retire
a run whose write-up nobody ever judged. So the block `tick.sh` appends after
`## NOT RECORDED` gains a machine-readable line:

```
Escalation kind: rejection | verifier-failure
```

`escalation_targets` counts `rejection` only. A file with no parseable kind line
is **`unknown`**, and `unknown` does not retire anything — the escalations
committed before this change include both real rejections and verifier outages,
and defaulting them to `rejection` would retire work nobody ever judged, which is
the precise harm §1.3 exists to prevent.

`unknown` is reported rather than swallowed. The report carries one line —
`UNCLASSIFIED ESCALATIONS: <n> (not counted toward retirement; see
journal/escalation-kinds.md)` — so a backlog of them is visible instead of
quietly disabling retirement for the runs they name.

Historical files are classified by a **committed migration record**,
`journal/escalation-kinds.md`: a human-authored table of `<stamp> <kind>`, read
through the same committed-content primitive. A stamp listed there takes the
listed kind; a stamp neither listed nor carrying a kind line stays `unknown`.
New escalations carry the line and never need the record.

Each frontier row gains:

```
handling: "recorded" | "unrecorded" | "retired"
rejections: <int>
escalation_latest: "escalations/<stamp>.md"
```

`rejections`, not `escalations`: the row field is a count, and `escalations`
already names the dict it is derived from, so one word for both shapes invites a
reader to take the count for a list.

- `recorded` — a committed journal entry cites the id (unchanged semantics).
- `retired` — not recorded, and `escalations >= QF_FRONTIER_RETIRE_AFTER`
  (default **2**, validated as an integer **>= 1**; `0` would retire every
  unrecorded row on sight, so an out-of-range or unparseable value is an error
  that refuses to build the frontier rather than a silent fallback). The
  threshold is **passed into `build` as an argument**, resolved from the
  environment once at the `main` boundary like every other piece of external
  state, and a bad value raises a module exception in the idiom of
  `prereg.PreregError` — never `SystemExit`, which `frontier.py` cannot use
  because the test suite imports it as a library.
- `unrecorded` — everything else.

`escalation_targets` therefore returns `({run id: {"rejections": n, "latest":
"escalations/<stamp>.md"}}, problems)`, where `problems` is a dict —
`{"unclassified": n, "unparseable_waivers": n}` — the latest qualifying path
included because §1.4's report line names it, and a bare count cannot produce
it.

**An unclassified escalation is counted only in the total, never per run.** A
per-id unknown count cannot be populated correctly: an id whose escalations are
*all* `unknown` has no record at all, since only a real rejection allocates one.
It would also imply something the file does not establish — that the run it
names was judged. So `unclassified` is global, and a record's presence in
the dict means exactly one thing: this id has had at least one rejection episode
counted against it.

**`problems` is a dict rather than a count because more than one kind of file
can be silently dropped.** A waiver whose filename carries no parseable stamp
revives nothing however many ids it names — and a waiver is the one file type
whose entire purpose is *operator-initiated* revival, so it is the one that most
needs to say when it did nothing. Every silently-ignored file gets a key here
and a line in the report; the dict is what stops the next such discovery from
changing the function's signature again.

**The values are lists of filenames, not counts.** "Rename them" and "classify
them" are not actionable without saying *which*, and the `RETIRED:` line three
lines above names its exact id and path — these must meet the same standard.
`health` still exposes ints, derived with `len()`, so the health shape is
unchanged; the report names the files, capped so a backlog cannot swamp it.

`health.unrecorded_runs` counts `handling == "unrecorded"` only. `row["recorded"]`
is kept as a derived boolean for any existing consumer, so this is additive.

### 1.4 Every steering surface, not just the counter

Retirement is only an escape if every place the loop steers on agrees.

- **JSON** — `handling` as above, plus the three pre-registration fields the
  retry template needs and the rows do not currently carry. `_series_out`
  exposes `config` (the label) but not the digest, `vs` or `tol`; they exist on
  the decoded `row["prereg"]` and are simply not projected. Each row gains:

  ```json
  { "config_digest": "...", "vs": "...", "tol": 0.0 }
  ```

  Without them the retry template of §3.2 asks the leader for figures with no
  citable source, which is the rejection it exists to avoid.
- **Report table** (`frontier.py:667`) — the `written up` column renders
  `yes` / `NO` / `retired`, replacing the two-valued `{'yes' if recorded}`.
- **The "needs writing up" shortlist table** — it filters on `if r["recorded"]:
  continue`, so a retired row would still be listed there as `needs writing up:
  yes`. **This is the surface the leader actually picks from**, so it matters
  more than the counter does: a retired row in this table is the livelock again
  whatever `unrecorded_runs` says. Filter on `handling != "unrecorded"`.
  (Missing from this document's first draft; found by reading `render()` while
  dispatching the work.)
- **Report body** — one line per retired row, so it is visible and never
  silent: `RETIRED: <id> — <n> rejections, never written up, latest
  escalations/<stamp>.md. Not selectable; see §1.5 to revive.`

  **The label must not contain the word "unrecorded".** An earlier draft said
  `RETIRED:`, which reuses the exact term the health line and action
  1 use for *the bucket the leader is supposed to act on* — so the report's own
  vocabulary argued against the instruction, and the prompt had to spend a
  paragraph overriding it. The fact that the run was never written up belongs in
  the prose, not in the label.
- **`tick-prompt.md` actions 1 and 2** — select on `written up: NO`, and state
  explicitly that a `retired` row is not `NO` and must not be picked.

### 1.5 Revival is a recorded waiver

A retired run is revived by a committed file in its own directory,
`journal/waivers/<stamp>.md`:

```markdown
**Waiver:** <evaluation run id>

<why this is being reopened>
```

**A waiver starts a new episode; it does not decrement a total.** Subtraction
does not revive anything in the case that matters: three rejections against a
threshold of two still leaves the row retired after one waiver. So
`escalation_targets` counts, per run id, only the qualifying escalations
committed **after the latest waiver for that id** — stamps compare
lexicographically, which is what the `YYYYMMDDTHHMMSSZ` naming is for. With no
waiver the window is all of history, which is today's behaviour.

The dedicated directory is what keeps this from leaking into
`journaled_run_ids`. A waiver in `journal/` would cite the run id and thereby
mark the run `recorded`, defeating itself, and avoiding that would mean
special-casing a line format inside the recorded-entry reader. `journal/`,
`journal/escalations/` and `journal/waivers/` are three flat namespaces with one
reader each, and depth is enforced on the returned paths in all three.

Moving or
deleting escalation files is explicitly **not** the mechanism: the escalation
trail is the audit record of what the gate refused, and thinning it to change
the loop's behaviour destroys the only evidence that the refusals were correct.

**Accepted cost.** The Sep 4 finding becomes permanently unrecorded rather than
permanently re-attempted, unless a human waives it. §3 is what makes that rare.

---

## 2. Two brakes, because there are two failures

Today one counter absorbs both a drifting leader and a dead verifier
(`tick.sh:800-807`, deliberately). Under same-target suppression that becomes
unsafe: a verifier outage on a resolvable target would stop advancing the streak
and the loop would burn a leader turn an hour forever.

So there are two counters in `$STATE`, and either can pause the loop.

### 2.1 `consecutive-disagreements` — research drift

Advanced once per **rejected target episode**, and only when the copilot
returned a usable verdict (§2.2).

**Suppression is defined by one predicate, used in both places.** A target is
**suppressible** when all of:

- it is a canonical `evaluate-` id (not `none`, not `probe-`, not malformed);
- it resolves to **exactly one** row in the frontier built by *this* tick;
- and that row is `handling == "unrecorded"`.

Nothing else is suppressible. A target that is missing, unparseable, ambiguous
across rows, already `recorded`, `retired`, or simply stale — no longer on the
scoreboard at all — behaves exactly like `none`: it **increments**, and
`last-reject-target` is set to `none`.

This is the load-bearing sentence of §2. Suppression is only safe because
retirement bounds it (§1.3), and retirement only counts escalations against a
resolved unrecorded row. Suppressing on a target retirement cannot reach — an
ambiguous id, or one that has left the scoreboard — is unbounded, and rebuilds
the exact livelock of 2026-09-04 with the drift brake switched off.

| Condition | Streak | `last-reject-target` |
|---|---|---|
| `AGREE` | reset to 0 | `none` |
| `DISAGREE`, suppressible, ≠ stored | increment | store the target |
| `DISAGREE`, suppressible, == stored | unchanged — bounded by §1.3 | keep |
| `DISAGREE`, target `none` | increment | `none` |
| `DISAGREE`, not suppressible (missing, ambiguous, recorded, retired, stale) | increment | `none` |
| no usable verdict (§2.2) | unchanged | unchanged, and never stored |

The same predicate gates retry mode in §3.1, so the tick cannot suppress a
streak for a target it will not then instruct the leader to retry.

`last-reject-target` is written only for a rejection the copilot actually
delivered. An entry that failed to be verified was never judged, so the next
tick must be free to write it up normally rather than under §3's reduced
template — and a persistent outage is caught by §2.2, not by pretending the
leader was wrong.

Clearing `last-reject-target` on `AGREE` is the part missing today
(`tick.sh:770` resets only the number), and without it the next rejection of a
different target could read as a retry.

Threshold `QF_TICK_MAX_DISAGREE`, committed value **3**.

### 2.2 `consecutive-verifier-failures` — infrastructure

Advanced on every invocation that exhausted `QF_TICK_COPILOT_TRIES` without
producing a **usable verdict**, defined as:

```
usable verdict = exit status 0 AND a valid anchored final `VERDICT:` line
```

Both halves are required. `tick.sh` already distrusts a verdict from a process
that exited non-zero — a copilot that printed `AGREE` and then crashed once
published an entry as verified, and the anchored `grep -oE` plus the non-zero
check are what stopped it. So a non-zero exit is `verifier-failure` even when
the output happens to contain a verdict line, and an exit-0 run with prose and
no anchored line is too. Anything that is not a usable verdict advances only
this counter, retires nothing (§1.3), and stores no target (§2.1).

Reset
to 0 **only by a usable verdict**, `AGREE` or `DISAGREE` alike: a verifier that
answered under both halves of the definition is up, and whether it agreed is not
an infrastructure fact. The wording matters in one direction specifically — a
non-zero invocation whose output contains verdict text is `verifier-failure`, so
it advances this counter and must **not** reset it. Otherwise a copilot crashing
after printing a verdict would clear the infrastructure brake on every tick, and
the outage it exists to catch would never reach the threshold.

Threshold `QF_TICK_MAX_VERIFIER_FAILS`, default **3**. Preserves today's
behaviour — a verifier that is *down* rather than flaky pauses the loop — while
taking it off the drift counter, so the escalation and the pause reason say which
of the two happened.

### 2.3 Fail closed

Both counters go through the existing `counter` / `set_counter` contract: a value
that cannot be read or written is a stop, not a zero (`tick.sh:167`).

`last-reject-target` cannot use those helpers — `counter` rejects any value
containing a non-digit (`tick.sh:172`), so it would fail on every run id. It gets
its own pair, `target` / `set_target`, validating exactly two shapes: the literal
`none`, or one canonical run id matching `frontier.py`'s `_RUN_ID` anchored whole
(`^evaluate-[0-9A-Za-z]+-[0-9a-f]+-[0-9]+$`, per §"evaluation ids only" below).
Anything else — empty, multi-line, malformed, unreadable — is treated as a
*changed* target: the streak increments, because failing towards the pause is the
safe direction.

---

## 3. A retry shrinks; it is not rewritten

### 3.1 Decided before the leader runs

The shrink instruction has to be in the leader's context, so the retry has to be
known at startup rather than derived from `PENDING.md` afterwards. After the
frontier is built, a tick is a **retry** when all of:

- `last-reject-target` is **suppressible** in the sense of §2.1 — a canonical
  evaluation id, resolving to exactly one row in the frontier just built, and
  that row `handling == "unrecorded"`. This is the same predicate that suppressed
  the streak, deliberately: the two must never disagree about which target is
  live;
- and the tick then *tells the leader to retry that exact target*, rather than
  hoping it picks it.

On a retry tick the action is not open: the context says "this tick is action 1
on `<id>`; the other five actions are unavailable." That removes the guesswork
about what the leader will choose, and makes the conditional block exactly as
true as the instruction beside it.

If the stored target is now recorded or retired, `last-reject-target` is cleared
and the tick proceeds normally.

**The two callers of the predicate see different snapshots, and that is
deliberate.** The retry decision runs during context assembly, so it can only
resolve against the pre-leader frontier; the streak block runs after the leader
acted and resolves against the refreshed one (§2.1). A target that is `absent`
before the leader runs and `unrecorded` after it — a run this tick submitted and
scored — is therefore cleared by the retry decision and gets no retry
instruction, while the streak block still recognises it as one episode. Both
errors fall the safe way: no retry instruction is issued on a stale reading, and
no streak suppression is lost. Nothing needs to reconcile them, but a reader who
notices the asymmetry should find it written down rather than assume a bug.

### 3.2 One exact reduced template

Saying "the claim and nothing else" contradicts `tick-prompt.md:156`, which
mandates five fields. So the retry gets its own template with the same fields
and constrained content, and the constraint is what shrinks it:

```markdown
# Retry: <config>@<digest>, <target run id>

**Target run:** <id>

**Action taken:** 1 — retry of escalations/<stamp>.md

**Claim:** <the pre-registered `claim`, `bar` and `direction` verbatim from the
row pasted below, and the JSON figures they rest on. Signed deltas at the precision the JSON
supplies. No other config, no other metric.>

**Confidence and what would change it:** <one sentence: the pre-registered
`tol`, and this series' `holdout` window.>

**Not concluded:** <one sentence: what one cohort does not establish.>

**Evidence:** <pasted command output for any figure not in the JSON. Nothing
else.>
```

**THE LEADER DOES NOT RECEIVE THE FRONTIER JSON.** It receives `frontier.md`,
and that markdown carries none of `config_digest`, `vs`, `tol`,
`escalation_latest`, or the row's own measured value — only a `%.4g` cell for the
winning config. `frontier.json` goes to the copilot alone. Verified by rendering
a real row: all four absent. So a template saying "verbatim from the JSON" is
unsatisfiable, and the leader's only options are a blank mandated field or a
remembered number — the exact rejection the retry exists to prevent, firing on
the first production retry, unattended.

**So the retry injection pastes the target row's JSON object into the context.**
Not a widened markdown table (every tick would pay for a retry-only need) and
not a command to re-run (a tool call the leader can get wrong, for data the tick
already holds). `tick.sh` has `frontier.json` in `$CTX` at injection time, so it
emits the one row the retry is about, at full precision, immediately after the
target id. The template cites **the row pasted below** — a source the leader can
read and the copilot can check, since the same values reach it in the JSON it
receives.

And four bans, each traceable to a rejection that has actually happened:

1. **No rounding-equivalence.** "Unchanged to four decimals" is a figure claim,
   and it was false at 0.0817 vs 0.0818. State the signed delta.
2. **No superlatives or firsts** unless the JSON rows establishing the
   comparison are pasted. `val7_nop90` already had 0.286152780.
3. **The title names the action and the config only.** It never characterises a
   metric direction; a −1.43pp MAE move is not an "improvement".
4. **No comparison to any config other than the pre-registered `vs`.**

**No independent-cohort count.** It was in an earlier draft and is removed,
because `configs{}` — the only place `independent_cohorts` exists — is built
solely from rows where every bar passed (`frontier.py:457`). A retry is about a
run that was rejected, and the Sep 4 target had missed the MAE bar, so the figure
is structurally absent for exactly the runs this template governs. The series
`holdout` window is present for every row and carries the same meaning here: one
cohort, named.

The previous rejection reason is already fed back (`tick.sh:415-470`,
leader-only, non-evidence, capped). That stays exactly as it is.

---

## 4. The pause issue: alarm, and the release handle

`PAUSE` remains the local brake. A GitHub issue on `lotas/qf-research` is how a
human learns about it and how a human releases it.

**Trust model, stated up front.** This is visibility and operator convenience,
not enforcement. `research` can already `rm ~/qf-research/PAUSE`, and the design
already treats `qf-research` as untrusted narrative whose authority lives in the
dispatcher's hash chain and the root-owned evaluator
(`auto-research-loop-design.md:114`, `tick.sh:846`). A token that can close an
issue therefore grants nothing new, and unlike `rm PAUSE` a self-release leaves
an attributable, timestamped GitHub event. The allowlist below is
normal-workflow policy, not a security boundary. Real enforcement is §4.5.

### 4.1 `pause-issue.sh open`

Called by `tick.sh` immediately after `PAUSE` is written.

- Repo comes from `QF_PAUSE_ISSUE_REPO`, set in the **root-owned unit**. It is
  *not* derived from `git remote get-url origin`, which is research-writable.
  Unset means do not attempt, and say so.
- Preflight the `qf-pause` label; create it if absent, and continue without it
  if creation fails — a label is not worth losing the alarm over. **"Already
  exists" is success, not failure:** `gh label create` exits non-zero on an
  existing label, which is the state on every run after the first, so treating
  that exit as a failure files essentially every issue *without* the label —
  the opposite of the intent. Determine existence with a read, create only when
  absent, and fall back to filing unlabelled only when the label's state cannot
  be determined at all.
- Title `PAUSED <stamp>: <brake> at <n>`, where `<brake>` is
  `consecutive-disagreements` or `consecutive-verifier-failures`, so the title
  alone distinguishes drift from an outage.
- Body: the pause reason; the escalation path; the last three rejection reasons
  verbatim; and the release instruction — *comment first if you want to instruct
  the leader, then close this issue.*
- Idempotent on the stamp, searching **open and closed** issues and **not**
  filtering by label. Both matter: if creation succeeds and the process dies
  before `PAUSE` is rewritten, the retry runs after a human may already have
  closed the issue, and an open-only search would file a duplicate against a
  pause that was already handled. Label creation is allowed to fail (below), so
  the label cannot be part of the key either. The key is the exact stamp string
  in the body.
- On success, `PAUSE` is rewritten to bind the authorization to one issue:

  ```
  auto-paused <stamp>: <n> consecutive <brake>
  see <escalation path>
  issue: <owner/repo>#<number>
  stamp: <stamp>
  ```

### 4.2 `pause-issue.sh check`

Called by `tick.sh` **before** the PAUSE early-exit.

| State | Action |
|---|---|
| `PAUSE` absent | normal tick |
| `PAUSE` has no `issue:`/`stamp:` pair | stay paused; retry `open` (covers a PAUSE whose issue creation failed) |
| `issue:` repo ≠ `QF_PAUSE_ISSUE_REPO` | stay paused; log the mismatch |
| issue open | stay paused |
| issue closed, closing actor on the allowlist | **resume** (§4.3) |
| issue closed, actor not on the allowlist | stay paused; log the rejected actor |
| any API error, or an unparseable answer | **stay paused**; log loudly |

Requiring repo, number *and* stamp to agree means a bare mutable number is not
by itself an authorization — and the stamp must be **compared against the
issue's body**, not merely read and tested non-empty. `PAUSE` is
research-writable, so an `issue:` line that is trusted on its own is a
self-release: point it at any long-closed issue in the repo and the next tick
resumes. (The first draft of the reference implementation made exactly that
mistake.)

**Recovering a missing stamp must not invent one.** A legacy or truncated
`PAUSE` has no `stamp:` line, and the obvious repair — take `date` now — files a
fresh duplicate issue every hour for as long as the file stays unbound. Recover
it from the `auto-paused <stamp>:` line, else from the file's mtime, and persist
it so the idempotency key is stable.

**Every value read out of `PAUSE` is untrusted input.** The stamp reaches a jq
program, a search query and an issue title; the issue number reaches an API
path. Shape-validate both before use — a `PAUSE` reading `issue: repo#../../x`
otherwise reaches `gh api` as written.

The closing actor comes from the REST issue-events API,
`gh api repos/{owner}/{repo}/issues/{n}/events`, taking the `actor.login` of the
latest `closed` event. `gh issue view --json` exposes `closedAt` and
`stateReason` but **no** `closedBy` field, so it cannot answer this question.
A closed issue with no `closed` event is an API answer that does not parse:
stay paused.

The allowlist is `research-loop/pause-resume-allowlist.txt`, committed, one
GitHub login per line, `#` comments allowed.

### 4.3 Resume, and the human directive

On an authorized close:

- Reset state in a fixed order, because four filesystem objects cannot be
  changed atomically and the invariant that matters is **never unpaused with
  stale counters** — not "all four or none":

  1. fetch and persist the human directive;
  2. set `consecutive-disagreements` to 0, `consecutive-verifier-failures` to 0,
     and `last-reject-target` to `none`;
  3. read all three back and verify they hold those values;
  4. remove `PAUSE` last, and only if step 3 passed.

  Any failure before step 4 leaves `PAUSE` in place, so the loop stays paused
  with whatever counters it had — safe, and retried on the next tick. A crash
  between 3 and 4 leaves zeroed counters under a live `PAUSE`, which is exactly
  the state the box is in today and is harmless: the next `check` re-resumes.
  The reverse order is the one that must never happen — `PAUSE` gone with the
  streak still at its threshold would pause again on the first rejection.
- Log the closing actor, the issue and the stamp, and carry them into the tick's
  log line so a self-release is loud.
- Collect the issue's comments, keeping only those authored by an allowlisted
  login — the research token can author comments too, so an unfiltered fetch
  would let the loop write its own instructions. Each promoted comment keeps its
  author and timestamp. Non-allowlisted comments are counted and mentioned, not
  promoted.
- Write them to `$CTX/human-directive.md`, capped **once** at
  `QF_TICK_MAX_FEEDBACK_BYTES`, handed to the **leader only**, labelled
  non-evidence. **The instruction block goes above the promoted comments**, so
  the only thing truncation can eat is comment text — an earlier draft appended
  the citation ban after the comments and capped in two places, and a
  cap-length comment cut the ban away exactly when the most human prose had
  reached the leader. Ordering, not arithmetic: budgeting the cap against the
  boilerplate's length is a constant somebody will change — the same contract as the escalation feedback. The copilot never
  sees it, so a figure typed into a comment still cannot be cited.

`CTX` creation and its `trap 'rm -rf "$CTX"' EXIT` (`tick.sh:204-205`) move
above the PAUSE check, so the directive has somewhere to live. The trap already
cleans up the early-exit paths.

### 4.4 Token

A new fine-grained PAT, `lotas/qf-research` only, **Issues: read and write, and
nothing else** — notably not Contents, so it cannot touch the journal; the push
keeps `.git-credentials`. Stored at
`~research/.config/qf/pause-issue-token`, mode 0600, read into `GH_TOKEN` for
each `gh` invocation so the existing `gh` login is left alone. Issues: write
necessarily permits closing and editing issues; that is the acknowledged
property above, not an oversight.

### 4.5 If real enforcement is wanted later

A file in the trusted mirror is not sufficient on its own: while `PAUSE` is
research-writable, an old authorization can be replayed by rewriting `PAUSE`. A
real version needs root-owned pause state plus a human authorization bound to a
fresh per-pause nonce, most likely through a narrow privileged helper. Out of
scope here, and deliberately so.

---

## 4b. The silent `die`, and the alarm that covers it

`tick.sh` has ~25 `die` sites and a `die` writes no `PAUSE`. So a broken install
— an unreadable prompt file, a missing workspace, an unset `HOME` — leaves the
loop retrying hourly forever with **nothing raising an alarm**: the 2.5-day
silence of 2026-09-04 by a different route, and the last hole this design would
otherwise leave open.

`OnFailure=` on `qf-tick.service` runs a one-shot that writes the same four-line
`PAUSE` `pause_now` writes (only if none exists) and calls the same
`pause-issue.sh open`. One notifier, one release path, one thing for a human to
learn.

**What bounds it to one issue is the stamp, not the exit code.** A paused tick
exits zero, so `OnFailure` is not re-reached — but the `die` sites *above* the
PAUSE check are exactly the broken-install faults this covers, and those exit
non-zero every hour with `PAUSE` already on disk. So the unit may fire hourly;
`pause-issue.sh`'s idempotency search over `--state all`, plus never rewriting an
existing `PAUSE`, is what keeps that to a single issue.

**A deliberate stop can fire it.** `systemd.unit(5)` triggers on the unit
entering `failed`. A clean `systemctl stop` ends `inactive (dead)`/`success` —
but if the stop job outruns `TimeoutStopSec=90` systemd SIGKILLs and the result
becomes `timeout`, which *is* `failed`. Stopping the service over a
multi-hour probe can therefore file a pause issue. Recoverable, and documented
where the operator meets the instruction. `install.sh off` is safe for a
stronger reason: it stops the **timer**, which cannot put the service into
`failed` at all.

**The hole it cannot close:** with no workspace there is no `PAUSE` to bind an
issue to, so the alarm exits non-zero naming the cause and the timer must be
disabled by hand.

## 5. Close the drift that hid the threshold

`unit_matches` compares unit files, so the live `QF_TICK_MAX_DISAGREE=5`
drop-in is invisible. Extend the check to compare the **effective** environment:
`systemctl show -p Environment <unit>` against the repo unit's `Environment=`
lines, comparing the **exact set** of effective `QF_*` keys rather than only
those the repo unit declares. Restricting it to declared keys would miss the
case a drop-in is most likely to produce: an entirely new knob that exists only
live. A key the repo declares is reported with its value — those are thresholds
and budgets, not secrets. An unexpected key is reported by **name only**, since
its value is by definition unreviewed and could be anything. Nothing outside
`QF_*` is read, compared or printed.

The committed value stays **3**. This check will flag the live `5` on the next
run, which is the point: the disposition of that drop-in is a reviewed decision,
not something a deploy silently inherits.

---

## Testing

- `tests/test_frontier.py` — committed-content reads (a working-tree edit to a
  tracked entry changes nothing; a `None` from git counts nothing; blob oids
  resolve from a journal subdirectory, which is the `ls-tree`/`cat-file` path
  trap); `Target run` parsing including a missing line, a malformed line, a
  `probe-` id and an id resolving to two rows — the last two retire nothing and
  do not enter retry mode; retirement at the threshold and not below it;
  distinct-file counting, so one escalation naming a run three times counts once;
  a comparator in `Evidence:` is never retired; `unknown` kind never retires, is
  counted in the report, and is overridden by `journal/escalation-kinds.md`;
  waiver windowing — three rejections then a waiver leaves the row selectable,
  and a rejection committed after the waiver retires it again on its own count;
  `QF_FRONTIER_RETIRE_AFTER` of `0`, `-1` and `x` all refuse to build;
  `config_digest`/`vs`/`tol` present on every row; `handling` tri-state in the
  JSON and `retired` in the table and body.
- `tests/test_tick.sh` — same-target rejection does not advance the drift
  streak, distinct-target does, `none` does; the usable-verdict definition in
  both directions (exit 0 with prose and no anchored line, and a non-zero exit
  whose output *does* contain `VERDICT: AGREE`, are both `verifier-failure` and
  store no target, and neither resets the verifier counter); a usable `DISAGREE`
  does reset it; `AGREE` clears the target; each counter can pause independently
  and the pause title names which; the suppressible predicate rejects each
  non-suppressible shape in turn — missing, malformed, `probe-`, ambiguous across
  two rows, already recorded, retired, and stale (absent from the scoreboard) —
  each incrementing and storing `none`; `target`/`set_target` accept `none` and
  one canonical evaluation id and reject a digit-free value, a multi-line file
  and a `probe-` id, with an unreadable target counting as changed; retry mode
  and streak suppression agree on the same target in every one of those cases; the reduced template is injected on a retry and not on
  a first write-up; the resume ordering — a failure at each of steps 1-3 leaves
  `PAUSE` in place, and `PAUSE` is never removed before the counters verify.
- `tests/test_pause_issue.sh` (new) — `gh` stubbed on PATH, one case per row of
  §4.2, plus: label preflight failure still opens the issue; idempotency finds an
  already-**closed** issue carrying the stamp and does not duplicate it, with the
  label absent; a closed issue with no `closed` event stays paused; the actor is
  taken from the latest `closed` event when there are several; comment filtering
  drops a comment authored by the token's own login; `PAUSE` rewriting binds
  repo, number and stamp, and a `PAUSE` whose repo disagrees with
  `QF_PAUSE_ISSUE_REPO` stays paused.
- `tests/test_unit_drift.sh` — the effective-environment comparison, including
  the placeholder-substitution case the existing test already guards; a declared
  key whose value drifted is reported with both values; a key present only in the
  drop-in is reported by name with no value; and non-`QF_*` keys are neither
  compared nor printed.

## What this does and does not fix

| Sep 4 failure | After |
|---|---|
| The same run re-selected every tick | Retired after 2 rejections, visibly, and excluded from actions 1 and 2 |
| A good finding lost to a rounding word | Retry 2 is the pre-registered claim under four explicit bans |
| Rewriting introduces new defects | The retry template is fixed and reduced; no new prose is invited |
| A verifier outage pausing as "drift" | Separate counter, and the pause title says which |
| 2.5 days before a human knew | An issue at the moment `PAUSE` is written |
| No release short of shelling into the box | Close the issue; comment first to steer the next tick |
| Threshold drift 3 vs 5 | Reported by the effective-environment check |

Not fixed, and deliberately: the loop cannot be made to record a finding the
gate keeps refusing. It can only be made to stop paying for the attempt, and to
tell someone.
