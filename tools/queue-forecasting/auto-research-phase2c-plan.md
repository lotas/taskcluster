# Phase 2c — evaluation integrity

Revision 2. Written after 2b-3's implementation, per the convention in
`auto-research-phase2b-plan.md` §2: the details worth writing are the ones the
previous group taught.

**What 2c delivers** (`auto-research-loop-design.md` §14): the trusted
evaluator, `eval.parquet`, `verdict.py`, contracts per target with a
`contract_hash`, and the independent derivation.

**What 2c is for.** At the end of 2b a probe reproduces a cohort and writes a
prediction set. Nothing yet decides whether that prediction set is *better*, and
nothing stops the candidate from being the thing that decides. Every number in
`trainer-phase2-decision.md` was produced by the same code that produced the
model -- which was correct while a human read every diff, and is exactly the
property that cannot survive autonomy.

**Acceptance** (design §14): negative controls **9 and 11** fail closed; a known
past result (`wait_time_residual_throughput_filtered_baseline`) is reproduced
end-to-end through the dispatcher; the oracle's verdict matches the recorded
numbers; and the independent derivation agrees to tolerance.

---

## 1. What already exists, and what it means for the cut

Three facts about the current code change the shape of this phase, and all three
make it smaller than it looks.

**The metrics are already raw counts, not ratios.** `trainer/src/evaluate.py`
emits `{"mae": {"eligible_n": N, "sum_abs_error": S}, "within_2x":
{"eligible_n": N, "hit_n": H}}` and `p90_coverage` as `{eligible_n, covered_n}`.
Nothing divides until aggregation. This is load-bearing for 2c and it is
already true: **the trusted evaluator can aggregate counts and compute every
ratio itself**, so a candidate cannot hand it a pre-divided number to check.
Had the trainer emitted ratios, the oracle would have had to either trust them
or re-derive them from scratch, and "re-derive from scratch" is the whole
trainer.

**`row_id` does not exist in the trainer.** It was introduced by 2b-2's
prediction contract (`task_id`, `run_id`, `row_id`, `p50`, `p90_raw`) and the
fixture derives it as `f"{task_id}:{run_id}"`. So the expected multiset is
**derivable from the frozen extract** rather than declared by the candidate,
which is what makes NC11 checkable at all.

**The go/no-go bars are already written down** (`trainer-spec.md` §"Go/no-go"):
wait -- MAE ≥15% better and within-2x ≥5pp better; run_duration -- MAE ≥5%
better; consistent on ≥3 of 5 holdout days; p90 coverage in [85%, 95%]. A
contract formalises *these*, it does not invent a new rule set. Where the bars
have since moved -- the 30m+ tail gate from the Bet 1/Bet 2 work
(`trainer-phase2-decision.md`: "<35% experimental, <30% broad") -- the contract
is where that lands, versioned, instead of living in prose.

---

## 2. Decisions

### D24 — the evaluator is the deployment domain's fourth hat, not a fifth domain

`auto-research-loop-design.md` calls it "the root-owned evaluator". Taken
literally that is a new privilege domain, and 2b-3 already settled the analogous
question the other way: *do not create another Docker-plus-credential domain.*

The evaluator needs numpy/pandas/pyarrow, so it cannot live in `qfd` (D6,
stdlib-only). It needs no database credential -- it reads the frozen extract and
a prediction set, both files. It needs no network. So it is the **narrowest**
of the trusted processes, and the right home is a trusted host process invoked
by `qfd` over the same request-channel shape as `qf-extract` (D16): socket
activated, `Accept=no`, its own unprivileged uid `qfeval`, no docker group, no
credential.

Consequences, stated so they are checkable:

- `qfeval` is in **no** privileged group -- not `docker`, not `qfheavy`, not
  `qfclient`, and unlike `qfextract` it holds no credential of any kind. Its
  entire authority is "read two directories, write one file".
- It reads `/var/lib/qf-extracts/<request_hash>/` and the run's
  `out/predictions.parquet` **read-only**, and writes `eval.parquet` into a
  directory `qfd` owns. It never writes to either store.
- The contracts live in the **trusted checkout**, not in `qf-research`. That is
  the whole of NC9: a contract the candidate can edit is not a contract.

### D28 — the evaluator reads immutable stores in place and the candidate's output from a staged inbox

`qfeval` cannot reach a run directory, and the gap is one step worse than a group
on `out/` would fix: the **base** run directory is `0750 qfd:qfclient`
(`Runner.OWNERSHIP`), so traversal itself needs `qfclient` -- and `qfeval` must
not be in `qfclient`. A directory carries one group, and that group is already
`qfclient` because it is what lets `research` reach `logs/` and `artifacts/`.

The two candidate remedies were a shared non-capability I/O group, or staging.
**Staging wins, and the deciding argument is that the two inputs have different
trust:**

- `/var/lib/qf-extracts/<request_hash>/` and `/var/lib/qf-baselines/<hash>/` are
  already `0755`, immutable, and content-hashed. `qfeval` reads them **in place**
  and verifies the digests it was given. Copying them would double 1.4 GiB to
  gain nothing -- the reason they are world-readable is precisely that a
  different uid has to read them.
- `out/predictions.parquet` is the one **untrusted** input: written by
  agent-authored code inside the sandbox. `qfd` stages it into
  `/var/lib/qf-eval/<run_id>/in/`, owned `qfd:qfeval` `0750`, and the evaluator's
  own output directory `/var/lib/qf-eval/<run_id>/out/` is `qfeval:qfeval` --
  the only thing it can write.

So the untrusted artifact is copied to somewhere the candidate cannot reach, and
the trusted immutable ones are read where they lie. The alternative -- an I/O
group on the base run directory -- would need either a second group on a
directory that can only have one, or POSIX ACLs. ACLs were rejected on evidence:
this phase already spent three attempts on a credential check that was wrong
because a DAC-based assertion cannot describe an ACL-protected file, and the
run-directory permission model is what NC10 and NC15's evidence rests on.

The cost is one copy of the predictions per evaluation, which is the prediction
set only (2b-2's frozen five columns), not the extract.

### D25 — the contract is a file in the trusted checkout, and its hash is content

Same shape as `baseline_hash` (2b-3) and for the same reason: an identity that
is a content key can be *verified* rather than trusted.

**JSON, not YAML**, and that follows from D6 rather than from taste: `qfd`
resolves and verifies a submitted contract hash, `qfd` is stdlib-only, and there
is no YAML parser in the standard library. Hashing the raw bytes instead would
avoid the parse and give up the property that matters -- a byte-level key changes
when a trailing newline does, so it could not distinguish a reformatted contract
from an altered one. The trainer's experiment configs stay YAML; a contract is a
trusted artifact read by trusted code, not a file anyone edits by hand often.

`contract_hash = sha256(canonical(contract))`, and the evaluator **recomputes it
from the file it actually read** before using it. A submitted `contract_hash`
that disagrees with the trusted checkout is refused -- that is NC9. Note the
direction: the job *names* a contract hash and the trusted side *resolves and
verifies* it, exactly as a probe names a `baseline_hash`. The candidate never
supplies the contract body.

A contract pins, per target:

- the **primary slice** predicate (`reason_resolved = 'completed'`,
  pending-at-anchored) -- so "the same cohort as the baseline" stops being prose
- the **metric set** and the bucket edges
- the **bars**, including the tail gate
- the **consistency requirement** (≥3 of 5 days)
- the **p90 coverage band**
- the **`baseline_hash`** it is judged against

That last one is new relative to `trainer-spec.md`, and it is the point of doing
this after 2b-3: "MAE improves by ≥15% over baseline" is not a claim until the
baseline is named. Two runs citing the same contract hash are comparable by
construction; two citing different ones are not, and the record says so.

### D26 — the independent derivation is a different route to the same number, not a second implementation

The design says "the independent derivation agrees to tolerance", and the honest
question is what independence can mean when one person writes both sides. A
second implementation written by the same author, from the same understanding, on
the same day, is a copy with different variable names -- it agrees precisely
where a misunderstanding would make both wrong. The 2b-3 fixture's second
`canonical` is defensible because it is genuinely constrained (byte-for-byte
JSON) and pinned by a test; a metric aggregation is not that.

So independence here means **a different route**, and specifically:

- the trusted evaluator computes each metric from **per-row** predictions and
  the extract's own `y_true`, in one pass, with no intermediate per-day objects
- the trainer's existing path computes them **per day, then aggregates counts**
- they must agree exactly on the counts (`eligible_n`, `hit_n`, `covered_n`,
  and `sum_abs_error` to floating tolerance)

That is a real cross-check: it catches slice disagreement, day-boundary
disagreement, join errors and double-counting -- the failures that actually
happen -- without pretending to catch a shared misunderstanding of what MAE is.
**Stated as a limitation in the plan rather than implied away**, because a
"verified independently" label that means less than a reader assumes is worse
than no label.

### D27 — the oracle emits a verdict, never a decision to act

`verdict.py` writes `{contract_hash, baseline_hash, extract_hash, per_metric:
{value, bar, passed}, consistency: {days_passed, days_required}, verdict:
"go"|"no-go", refusals: [...]}`. It does not promote a model, touch
`trainer/data/models/`, or write to the live predictor -- those are explicitly
out of 2b/2c scope (`auto-research-phase2b-plan.md` §8) and stay out.

A verdict is also **recorded, not returned**: it lands in `eval.parquet` /
`verdict.json` under the run directory and in the job's pins, so a failed
evaluation still says what it was judging by. Same rule as 2b-3's `baseline:
none` pin -- provenance that exists only on the happy path is provenance a
reader cannot rely on.

---

## 3. The 2c cut

| Group | Delivers | Controls | Standalone value |
|---|---|---|---|
| **2c-1** | contracts in the trusted checkout, `contract_hash`, the `evaluate` kind, the `qfeval` domain and its request channel | NC9 | a prediction set is judged by a rule the candidate cannot edit |
| **2c-2** | `eval.parquet`, per-row evaluation against the extract's own `y_true`, the row_id multiset check | NC11 | the oracle's numbers, from the frozen data, for one cohort |
| **2c-3** | `verdict.py`, the bars, the consistency rule, the independent-derivation cross-check | — | a go/no-go the record can defend; the 2c acceptance run |

Each ends where a control can fail closed, and none needs the next to be useful.

---

## 4. Tasks — 2c-1

- **Task 17 — `contract.py` in `host/shared/`. DONE (2026-08-29), 35 tests.**
  Closed-world contract validation and `contract_hash`, stdlib-only (both `qfd`
  and the evaluator import it, and `qfd` is stdlib-only per D6 -- so it lives in
  `shared/`, where 2b-1 put `extract_spec.py` for the same reason). Same shape as
  `baseline.py`: the identity a content key covering everything that affects the
  judgement, `load()` rehashing rather than trusting a declared hash, and every
  refusal naming the field.

  Beyond the outline, it refuses **rules nothing could satisfy** --
  `days_required` above `holdout_days`, an empty band, an empty metric set, a
  repeated slice value -- because each of those fails every run for a reason
  that has nothing to do with the model, which is the worst kind of bug in a
  judge: the output looks like a finding. It also refuses a `band` bar paired
  with a one-sided direction, since a coverage metric with a one-sided bar reads
  as though it checks calibration and does not (a model that never misses its p90
  is not calibrated, it is inflated).

  **The hostile-shape sweep found two defects in my own code on its first run**,
  which is the argument for writing it: `_NAME_RE.match(raw.get("name") or "")`
  raises `TypeError` when `name` is `5` (`5 or ""` is `5`), escaping the typed
  refusal -- the identical shape to `extract_spec`'s P1. And `_need_number`
  accepted **non-finite** bars: every comparison against NaN is False, so a NaN
  bar fails every run while looking like a threshold, and an infinite one decides
  every run by its sign. Neither announces itself.
- **Task 18 — the first two contracts. DONE (2026-08-29)**, 12 transcription
  tests + 19 shell clauses. `contracts/wait_time.v1.json.in` and
  `run_duration.v1.json.in`, transcribing `trainer-spec.md`'s bars plus the tail
  gate. Transcription is checked **against the spec text**, not by eye: a bar
  that quietly disagrees with the document everyone cites is a rule nobody
  agreed to, wearing the authority of a committed contract. The tests also pin
  the tail bucket name against `evaluate.WAIT_BUCKETS`, since `contract.py`
  deliberately does not own that vocabulary.

  **They ship as `.json.in` TEMPLATES, and that is a finding rather than a
  convenience.** A contract must name its `baseline_hash` (D25), which is the
  content key of a directory that has to be promoted first -- so these cannot be
  written complete until the baseline behind the reference result
  (`wait_time_residual_throughput_filtered_baseline`) is promoted on the host. A
  contract shipped with a plausible-looking placeholder hash would validate and
  judge against nothing, so the placeholder is `@BASELINE_HASH@`, which
  `contract.validate` refuses because it is not 64 hex. The incompleteness is
  enforced by the validator and visible in `ls`.

  `instantiate-contract.sh` pins one: it verifies the named baseline is promoted
  **and that its manifest still hashes to its own name** (the same check
  `_probe_baseline` makes), substitutes, validates, writes the declared
  `contract_hash` into the file, and publishes by rename. Re-instantiating an
  existing contract is refused naming versioning -- bump v1 to v2, because
  repointing a contract makes every result that cited its hash unreadable. The
  output is **committed**: the control NC9 asserts is that the file is in the
  trusted checkout, and a file that exists only on the host is a rule with no
  provenance.

  Two transcription decisions worth naming. `within_2x` is
  `absolute_improvement` 0.05, because "5pp" is percentage points and reading it
  as 5% relative would be a materially looser bar. The tail gate takes the
  **broad** figure (`<30%`) rather than the experimental one (`<35%`), because a
  contract is what a result must clear to be believed, not what an exploratory
  run hopes for. `run_duration` gets no within-2x bar and no bucket metrics --
  the spec states neither, and inventing them here would be a rule nobody agreed
  to.

  **Blocked on an operator step:** promoting the reference baseline, so the two
  contracts can be instantiated and committed.
- **Task 19 — the `qfeval` domain.** User, socket unit, service unit, startup
  refusals, `ping`. Mechanically the `qf-extract` pattern with less authority --
  and per 2b-1's lesson, `main()` **serves** refusals rather than exiting,
  because under socket activation exiting non-zero is a hang.

  "Root-owned evaluator" means root-owned **code, environment, units and
  policy**, not a process running as root. Six properties, each enforced rather
  than documented, and each with a stated way it is checked:

  1. **The socket peer is `qfd` and nothing else.** `SO_PEERCRED`, compared
     against a uid resolved at start-up -- not group membership on the socket,
     which is how `research` came to be able to reach a channel in revision 8.

     *Revision 2 correction: an earlier draft of this line said the socket would
     be `0600 root:qfeval`, "so there is no group to be added to". That is
     wrong, and building to it would have produced a channel `qfd` could not
     open: `0600` grants the group nothing, and `qfd` is not root. The socket is
     `0660 root:qfd`, exactly as `qf-extract.socket` is -- the group is the
     DISPATCHER's, whose only member is the dispatcher, and owning a socket is
     not membership of a group. The DAC layer keeps `research` from reaching the
     channel at all; `SO_PEERCRED` is the control on top of it.*
  2. **No Docker, no credential, no network, not `qfheavy`, not `qfclient`.**
     `Config.check_startup` refuses on membership, naming
     `SupplementaryGroups=`, the same shape as `qfextract`'s -- with `qfclient`
     in the forbidden list for a second reason here: it is the group that would
     have let it read a run directory, which D28 exists to avoid needing.
     `PrivateNetwork=yes`, which `qfextract` cannot have and this can.
  3. **Root-owned code, contracts and a locked environment.** The service runs
     from the trusted checkout with `uv.lock` frozen -- a refusal, not a
     warning, per 2b-1 -- and the contracts directory is root-owned and
     read-only to it.
  4. **IDs and hashes over the protocol, never caller-selected paths.** The
     request carries `run_id`, `contract_hash`, `request_hash`, `baseline_hash`
     and the staged input's digest. Every path is derived from the evaluator's
     own trusted roots, exactly as `_probe_extract` derives a mount from a
     64-hex hash. A path on the wire would make the peer check the only control.
  5. **Read-only inputs, digests verified.** The extract and baseline are
     opened read-only and their manifests rehashed; the staged predictions are
     digested and compared against the value `qfd` sent. A digest that is
     carried and not checked is provenance that looks complete.
  6. **One writable directory, atomic publication.** Only
     `/var/lib/qf-eval/<run_id>/out/`. `eval.parquet` and `verdict.json` are
     written to a temporary name and renamed -- the same single-act discipline as
     D20 and the baseline promoter, so a reader never sees a partial verdict.
  **Delivered 2026-08-29**, 57 tests. `evaluator/service.py`,
  `evaluator/request.py`, both units, and `env/` with a committed lock. All six
  properties above are asserted, and four were red-green verified by reverting
  them: narrowing `FORBIDDEN_GROUPS`, removing the writable-store refusal,
  removing the peer check, and exiting instead of serving on a failed gate each
  break tests.

  Three things the writing changed:

  - `FORBIDDEN_GROUPS` is `docker, qfheavy, qfclient, qfrun, qfextract` -- wider
    than `qfextract`'s three. `qfclient` is refused with its reason attached
    (it is the access D28 exists to avoid needing), because a refusal that does
    not say so reads as boilerplate and gets relaxed by whoever next needs a
    group.
  - A **writable input store is a startup refusal**, not a warning, and `ping`
    reports each store's state so a mode that changes under a running service is
    visible. The gate has a canary test: a correct configuration must produce
    ZERO problems, or every refusal above it could be passing because the gate
    refuses everything.
  - `ping` reports `can_evaluate: false` while 2c-1 has no implementation. A
    stub that returned a plausible empty verdict would be the worst possible
    placeholder, so the absence is answered rather than filled.

  Two defects of mine, both the same shape as ones this programme has already
  paid for. The socket was drafted `0600 root:qfeval` -- which grants the group
  nothing, so `qfd` could not have opened the channel at all; it is `0660
  root:qfd`, as `qf-extract.socket` is. And `ExecStart` named
  `/opt/qfevalenv/bin/python`, a path no install step creates: 2b-1's P1 exactly,
  where the unit named an interpreter lacking the imports and the tests hid it
  with `sys.path`. Now three tests pin the interpreter, the directory the setup
  script syncs, and the manifest that declares the closure to each other.

  The closure is deliberately **one task ahead of the code**: `service.py` is
  stdlib-only today, so `/usr/bin/python3` would run it and then fail the moment
  2c-2 imports pyarrow. It declares pyarrow and numpy and **not pandas** -- the
  trusted evaluator's per-row single pass is what makes it an independent route
  rather than the trainer's path twice (D26), and leaving pandas out keeps this
  environment off the trainer's dependency bump cycle.

- **Task 20 — the `evaluate` kind, and NC9. DONE (2026-08-29)**, 46 tests.
  `args.contract` is a 64-hex hash resolved and verified against the trusted
  checkout; a job naming a contract that is not there, or whose body does not
  hash to its name, is refused before anything starts. NC9 asserts the refusal
  *and* that a valid contract still works, so a broken resolver cannot pass by
  refusing everything.

  Shape as built:

  - **`args` is exactly `{run, contract}`.** No baseline, no bar, no metric, no
    extract. A caller that could pass a threshold could pass its own threshold,
    and NC9 (c) probes six policy field names **over the socket rather than
    through the client**, because argparse would refuse an unknown flag and the
    clause would be testing argparse.
  - **Every identity comes from the JUDGED RUN's pins**, never from the evaluate
    job's args. A caller that could name the extract could claim to have
    evaluated cohort A's predictions against cohort B's data, and the verdict
    would look exactly like a real one.
  - **`RELAYED_KINDS`** replaces three separate `kind == "extract"` branches --
    which lock is taken, which relay runs, whether a run directory is prepared.
    Three copies is how the fourth gets forgotten. `evaluate` takes an
    `ExtractSlot`, never the training mutex: scoring a finished experiment has
    no business making the nightly wait.
  - **`source_sha` is `sha256(run, contract)`** with
    `EVALUATE_SOURCE_REF = "evaluate-request (not a commit)"`. The column is
    `NOT NULL` and its role is "the immutable identity of what this job ran";
    hashing the pair rather than the contract alone means judging two runs by one
    rule is two pieces of work rather than a duplicate.
  - **An `error_class` arriving from another domain is constrained**, not
    trusted: it becomes a column operators grep and the suite asserts on, so a
    reply cannot put a sentence or an empty string where consumers expect a
    token. `contract_not_trusted` IS carried through, because flattening the
    NC9 outcome into "refused" would make the control's own signal invisible.
  - **Provenance is pinned before the relay**, so a failed evaluation still says
    what it was judging and by what rule -- the same rule as a probe's
    `baseline: none`.
  - **A reply that says `ok` is not a verdict.** Checked, because the extract
    relay's P1 was `{"ok": true, "manifest": {}}` recorded as a success with a
    test enshrining it as "does not crash".

  Four controls red-green verified by reverting them: the verdict check, the
  submit-time contract resolution, the error-class guard, and the requirement
  that the judged run SUCCEEDED.

  Also `qf contracts` and `qf evaluate`, the third user of the one prefix
  resolver. Its empty listing names the likeliest cause -- a `.json.in` template
  carries no pinned baseline, so it judges against nothing -- because that is the
  expected state until a baseline is promoted, and "no contracts" alone would
  send somebody hunting for a resolver bug.

  **NC9 voids rather than fails until a contract is instantiated**, which is the
  honest state: the group is gated on a contract resolving, and the void names
  `instantiate-contract.sh` as the remedy.

This completes **2c-1**.

## 4b. Tasks — 2c-2 (partial, 2026-08-29)

- **Task 21 — `metrics.py` and `rows.py`. DONE**, 22 tests. The metric
  definitions transcribed from `trainer/src/evaluate.py`, computed in ONE PASS,
  with the per-day split derived from the same pass. Counts only: nothing
  divides, so the ratio is computed once by the verdict from summed counts, which
  is what lets a trusted process recompute every number from the parts.

  **The parity test was RUN, not skipped.** It imports the trainer's own
  `per_row_metrics` and compares on random data with NaNs and zeros. pandas is
  outside the evaluator's closure by design, so the test skips there -- and a
  skipped parity test is a canary that does not gate, so it was executed once
  against a scratch venv with pandas and it passes. To re-run:
  `uv venv /tmp/parity-venv && uv pip install --python /tmp/parity-venv/bin/python
  pandas numpy`, then `PYTHONPATH=. /tmp/parity-venv/bin/python -m unittest
  discover -s tests`. Three definition perturbations were verified to break it:
  loosening `within_2x`'s zero exclusion, moving a bucket edge, and turning
  coverage's `<=` into `<`.

  **NC11's literal reading is wrong, and `rows.py` says why.** "The row_id
  multiset matches the frozen extract" cannot mean equality: an extract covers
  the training window as well as the holdout. The checkable property is
  well-formed (row_id is derived, so it is checkable rather than declarable),
  subset-without-duplicates, and -- the part that matters -- **complete within
  each day it claims**. The first two leave a gaming vector wide open: a probe
  could predict only the rows it does well on, inside days it chose, and score
  beautifully on a subset of a subset. Holdout DAYS have to be derived from the
  predictions (they live in the trainer's config, not the extract), but coverage
  WITHIN a day is fully determined by the extract, so completeness is checkable
  exactly where cherry-picking would happen.

- **Task 22 — `verdict.py`. DONE**, 18 tests. Every ratio computed here, from
  counts. A metric with no eligible rows is a REFUSAL, not a pass: "there were no
  rows to check" is not evidence that a bar was met. A metric the contract names
  and this cannot compute is refused BY NAME rather than skipped, because a
  skipped metric makes a contract look stricter than the judgement it produced.
  Consistency counts days where every per-day metric passes -- not one metric --
  since "consistent across 3 of 5 days" is a statement about the result. Bucket
  metrics are aggregate-only: a tail gate per day would fail on days with three
  tail rows.

- **Task 23 — the evaluator's `evaluate()`: read the parquet, join, write
  `eval.parquet` and `verdict.json` atomically. NOT STARTED.**

**A ninth static-scan-matched-its-own-documentation instance produced a real
fix.** `verdict.py`'s docstring says "nothing here writes to
`trainer/data/models/`" -- which is exactly the string the test scanning for it
matched. `code_only()` (the sixth instance's fix) strips `#` lines and cannot see
a docstring, because a docstring is an expression rather than a line prefix. So
`host/shared/srcscan.py` now TOKENISES, stripping comments and string literals
while preserving line offsets, and a file that fails to tokenise comes back
unchanged rather than empty -- an empty result would make every assertion built on
it pass.

Two fixture bugs of the "more convenient than production" kind, both found by the
tests failing: the synthetic result drew `lognormal(4, 1)` so the `30m+` bucket
was EMPTY and every bucket metric refused for want of rows; and the
inconsistent-day perturbation multiplied a `sum_abs_error` of 0.0 by 1000, which
is 0.0, so the test passed while testing nothing.

## 5. Open questions for 2c-2 and 2c-3

Deliberately not answered here -- 2c-1 will teach them, the way 2b-1 taught 2b-2:

1. **Does `evaluate` run as its own job kind, or as a phase of `probe`?** A
   separate kind means a prediction set can be re-judged under a new contract
   without re-running the model, which is worth having; it also means two jobs
   per experiment. Leaning separate, deciding in 2c-2.
2. **Where does `y_true` come from for a holdout day that has not settled?** The
   extract carries a settlement lag (D17) and the holdout window is inside it by
   construction, but "by construction" is exactly the kind of claim this project
   keeps measuring instead of asserting.
3. **What tolerance is "agrees to tolerance"?** SETTLED for the two-route
   comparison: counts agree EXACTLY, and `sum_abs_error` to
   `abs(value) * 1e-9 + 1e-9` -- a relative bound, because the two routes sum the
   same floats in a different order and nothing else should differ. Still open
   for the comparison against the RECORDED numbers, which needs real data.
