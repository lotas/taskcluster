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

- **Task 23 — the evaluator's `evaluate()`. DONE (2026-08-29)**, 170 evaluator
  tests. `evaluate.py` reads the staged prediction set and the frozen extract,
  joins on the derived `row_id`, applies `rows`' NC11 property, computes
  `metrics`' counts, decides with `verdict`, and publishes `eval.parquet` and
  `verdict.json` atomically into `<eval_dir>/<run_id>/out/`. `main()` injects it;
  the import lives inside `main` so `service.py` stays importable without the
  closure, which is what lets the startup gate be tested here at all.

  **`baseline.py` moved from `dispatcher/` to `shared/`.** Three things now read
  it -- `promote-baseline.sh` from the deployment domain, `qfd` when it pins a
  baseline to a probe, and the evaluator when it recomputes the hash before
  judging. Its old home meant a root script in the deployment domain importing
  from the dispatcher's tree, and the moment a second domain needed it that was
  the mistake `shared/extract_spec.py` exists to avoid. The move exposed two
  latent import-order bugs: `test_protocol.py` and `test_review_fixes.py` both
  relied on `test_runner.py`'s `sys.path` insert, so they passed under
  `discover` and failed run alone. Each now inserts its own.

  **Memory is independent of the extract's window, by construction.**
  `runs.parquet` covers months and the baseline NDJSON covers the same span; the
  prediction set is one holdout. Both large inputs are STREAMED against the
  small set of predicted keys — pass 1 finds the claimed days and the whole
  window's in-slice days, pass 2 returns every row on the claimed days. Two
  passes over a local file, deliberately, because the alternative is a
  row-count-shaped memory profile in the component that must not fall over.

  **The streaming reduction had quietly made the day-block check vacuous**, and
  a test is what said so. `check_day_block` was being handed the REDUCED row set,
  so it was asking whether the claimed days were a contiguous block of
  themselves — always true, with `is_tail` always true too. `available_days` now
  comes from pass 1, where it costs nothing. This is the same failure as the
  static scan matching its own prose: a check whose input is derived from its own
  subject.

  **The day set is the other half of the cherry-picking vector**, and it is split
  deliberately. `rows.check` closes "predict only the easy rows inside a day";
  the days themselves come from the predictions, because the holdout dates live
  in the trainer's config. Two properties are derivable from the extract:
  *contiguity is ENFORCED* (a holdout is a block at one end of a window; five
  days picked out of twenty is the same vector as picking rows inside a day), and
  *recency is RECORDED, not enforced* — the trainer legitimately drops a partial
  final day, and refusing that would fail valid runs for a reason unrelated to
  the model. `is_tail` and `expected_tail` go into the verdict document; the
  first live acceptance run decides whether it becomes a refusal.

  **Both sides are scored over ONE population.** A relative bar compares two
  ratios, and two ratios over different row sets are not comparable — so the
  scored set is the rows in the primary slice where `y_true` and the baseline's
  own p50 are both finite, and model and baseline are computed over exactly
  that. `eligible_n` matches by construction, and `baseline_missing_n` and
  `out_of_slice_n` record what was dropped **without overlapping**: the first
  version subtracted one from the other, which double-counts the rows that are
  hardest to reason about.

  **The frozen prediction contract is now ENFORCED, not just declared.**
  Design §4.6 froze the columns and types and `qfd` recorded them in a comment,
  because `qfd` is stdlib-only and cannot read Parquet. Nulls, NaNs, infinities,
  extra columns and missing columns are all refusals naming the field. The type
  rule is deliberately NOT exact Arrow equality: §4.6 says `int32` and `double`,
  and a candidate writing this file from pandas gets `int64` and `float64` by
  default — so the family is fixed and the width is not, with the value checks
  (non-null, finite, int32-representable) carrying what the widths were for.

  **NC11 is in the suite**, voiding on three named preconditions. Its three
  inline mutations — relabel a `row_id`, drop the widest-interval row from a day,
  add a row the extract does not contain — are **extracted from the suite by a
  unit test and run against the fixture**, so the suite's mutations and this
  module's refusals are pinned together. Without that they drift the worst way:
  a snippet that silently failed to mutate would leave the suite reporting a
  refusal it caused itself.

  Six controls red-green verified by reverting each: the contiguity check, the
  `available_days` vacuity fix, the one-population scoring, the prediction
  digest, the extract digest, and the idempotency identity.

  Two housekeeping findings, both the "reports success for no work" shape:
  `test_service.py` had `unittest.main()` two thirds of the way up with 160 lines
  of test classes after it, so running it directly ran none of them; and
  `test_metrics.py`'s parity skip used `__import__("importlib").util`, which
  resolves only when another module has already imported the submodule — so
  whether the parity test skipped or **errored at class-definition time**
  depended on import order.

  **The parity test was re-run against pandas**: 30 tests, no skips.

### 2c-2 review round (2026-08-29): five findings, all upheld

Reported against the Task 23 delivery above. Each was real; two of them
invalidated claims that section makes, and those claims are corrected in place
rather than left standing with a note.

- **P1 — the candidate could select an easier historical holdout.** Upheld, and
  my stated reasoning for recording rather than enforcing recency was **wrong on
  the facts**. I hedged against the trainer legitimately dropping a partial final
  day; `config.compute_windows` sets the holdout to
  `[as_of_date - holdout_days, as_of_date)`, `holdout_day_starts` walks it one
  calendar day at a time, and `load_config` REFUSES an `as_of_date` that is not
  UTC midnight -- as does `extract_spec._parse_boundary`. No partial day can
  arise. The hedge guarded a case that cannot happen and left the real vector
  open. `check_day_block` now takes a `required` list DERIVED from the extract's
  own `as_of_date` and the contract's `holdout_days`, and refuses anything else;
  a required day the extract has no in-slice rows on gets a separately worded
  refusal, because that is the extract's gap and not the candidate's doing.

- **P1 — the evaluation was disconnected from the probe's recorded artifact.**
  Upheld, and this is the worst of the five: `_evaluate_source` staged
  `<run>/out/predictions.parquet`, the candidate's own output directory, which
  has no recorded digest and is pruned after the handoff (D9). Bytes could change
  after a probe succeeded and still be judged as that probe -- **and the NC11
  clauses I had just written demonstrated exactly that, mutating `out/` after a
  SUCCEEDED run and getting a refusal.** A negative control that passes because
  of the defect it should find is worse than no control. The relay now reads
  `artifacts/predictions.parquet` and requires the digest `add_artifact` recorded
  when the run finished to equal both the current file and the staged copy, with
  a new `Store.artifact` accessor (`get` returns the `jobs` row only). NC11 is
  restructured: post-hoc mutation now tests THAT BINDING, a new clause asserts
  that mutating `out/` changes nothing, and the row-set property moves to fixture
  candidates -- which voids until the fixture branch carries them, since the
  property can no longer be reached by editing a file behind a finished run.

- **P1 — `extract_hash` was recorded without validation.** Upheld. It went into
  every verdict unverified, and the test fixture computed it with
  `json.dumps(..., sort_keys=True)` -- DEFAULT separators, so `", "` and `": "`,
  so different bytes and a different hash from production. The fixture was wrong
  and the suite was green, because the only code that could have noticed did not
  look. New `shared/extract_manifest.py` owns the canonical form and the
  verification; `extractor.py`'s inline `_canonical` + `sha256` is deleted in
  favour of it (the extractor's 184 tests passing unchanged is what shows the
  bytes are identical), the evaluator verifies before scoring, and the fixture
  uses the shared implementation. A red-green pass caught that the FIRST test for
  this exercised the wrong check -- editing `as_of_date` changes `request_hash`
  too, so removing the new verification left the suite green. The test now edits
  `settlement_lag_s`, which is deliberately outside `request_hash`.

- **P2 — idempotent reuse trusted its own outputs.** Upheld. Reuse returns
  somebody else's numbers as this run's answer, so it now recomputes the
  document's `eval_hash` and verifies `eval.parquet` against the
  `eval_sha256` the document pins. Deleting or editing the per-row file used to
  return `reused: true` -- a verdict with no evidence behind it, reported as a
  success.

- **P2 — the prediction ceiling did not support the memory claim.** Upheld.
  Measured at **536 MB per 1_000_000 rows** (`ru_maxrss` around a real
  `read_predictions`), so the 20_000_000 ceiling was about 10.7 GB on a unit with
  `MemoryMax=4G`, reached before validation finishes. Now 2_000_000 with the
  arithmetic beside the constant and a test pinning it to a stated budget and to
  the unit's own `MemoryMax`. A recorded 5-day holdout is ~162_000 eligible rows,
  so this is ~12x the real cohort. The module docstring stated the streaming
  property and read as though it covered the prediction set; it now says which
  mechanism bounds what.

**A systemic test-harness defect surfaced while fixing these.** Nine test files
carried `if __name__ == "__main__": unittest.main()` mid-file with test classes
below it, so running any of them directly executed only the classes above the
guard and reported OK -- `test_protocol.py` ran 1166 of 3260 lines' worth.
`discover` imports whole modules, so every suite was green and the gap was
invisible. All nine fixed, and every test file now reports the same count run
directly as under `discover`.

Four of the five fixes were red-green verified by reverting them; the fifth
(`extract_hash`) needed a new test first, as recorded above.

This completes **2c-2**.

- **Task 24 — the 2c install step. DONE 2026-08-29.** `host/phase2c-setup.sh`,
  `discover`/`install` in the shape 2b-1 established: the `qfeval` user and the
  group memberships it must NOT have, `/var/lib/qf-eval`, `uv sync --frozen` for
  the closure, the two units with `%%QFD_UID%%` substituted, and a round trip
  that is RUN rather than printed -- 2b-1 only printed those commands because
  running them would have put the database DSN in a process argument, and this
  domain holds no credential. 16 tests in a new
  `host/tests/test_phase2c_setup.sh`, 17 in `evaluator/tests/test_service.py`
  and 7 in `dispatcher/tests/test_runner.py`; 8 of the claims red-green verified
  by reverting them one at a time.

  **Writing it found a defect that would have made the first live evaluation
  fail, and it is the same shape as everything else this phase has found: a
  handover whose paperwork looked complete.** `_stage_predictions` chmodded the
  three per-run directories to `0750/0750/0770` and chowned their GROUP to
  `qfeval` -- then created `predictions.parquet` inside the inbox. A file created
  in a directory takes that DIRECTORY's group only if the setgid bit is set, and
  the chmod had just cleared it. So the staged prediction set would have been
  `0640 qfd:qfd` inside a directory the evaluator could traverse: **the one file
  the entire staging path exists to hand over would have been the only thing it
  could not read**, and `qfd` never chowns the file. Nothing in 866 dispatcher
  tests could see it, because every test runs as one uid.

  The fix is `2750/2750/2770` on the per-run directories and `2770 qfd:qfeval` on
  the staging root, so the group is inherited rather than assigned. That
  inheritance is load-bearing for a second reason: Linux permits
  `chown(-1, gid)` only for a member of `gid`, and `qfd` is deliberately not in
  `qfeval` -- so without it the dispatcher could not have given the file away at
  all. `_give_to_the_evaluator` therefore checks the STATE rather than the call
  (already-correct is a no-op) and REFUSES with `eval_staging_denied` naming
  `phase2c-setup.sh` when the group is wrong and it cannot fix it, instead of
  staging a file the evaluator will fail to open one privilege domain away.

  Three more, each an install-shaped trap rather than a coding error:

  - **`StateDirectory=qf-eval` could not work and is removed.** It creates the
    directory as the unit's own `User:Group` -- `qfeval:qfeval 0750` -- and the
    process that must create `<run_id>/in/` inside it is `qfd`. The evaluator
    would have started cleanly, its gate would have reported every store
    correct, and the first evaluation would have failed in the DISPATCHER on
    `mkdir`. Two uids meet in that directory, so neither unit can own it:
    `evaluator/qf-eval.conf` provisions it through systemd-tmpfiles, like
    `qf-locks`.
  - **`ReadWritePaths=/var/lib/qf-eval` in `qf-dispatch.service` is now
    `-`-prefixed.** A listed path that does not exist makes a unit fail to
    START, so as written, installing 2c had become a prerequisite for the
    dispatcher running at all -- the opposite of what the code says, where
    `qfeval_gid` is `None` on such a host and staging tolerates it. And because
    the namespace is built when the service starts, a directory provisioned
    afterwards is READ-ONLY inside the running dispatcher: install can succeed,
    `discover` can report `2770 qfd:qfeval`, and the first evaluation still
    fails on `mkdir` with EROFS. So the script measures writability from inside
    the running namespace with `nsenter` rather than printing "remember to
    restart qfd", with a weaker timestamp inference where `nsenter` is absent --
    and it says which of the two it did.
  - **`discover` printed "installed and matches the checkout" without having
    compared anything** when the extraction of `unit_matches` from
    phase2-setup.sh failed to load. That is the exact claim `test_unit_drift.sh`
    exists because somebody once believed, so an uncompared unit is now its own
    warning, and a test drives the real script against a temp `UNIT_DIR` to
    prove both that the comparison runs and that it detects an edit.

  **Nothing pruned the staging root, and now something does -- partly.**
  `qf-runs-prune` is scoped to `/var/lib/qf-runs` (its unit's `ReadWritePaths=`
  says so) and no timer touches `/var/lib/qf-eval` at all, so every evaluation
  left a full second copy of the prediction set there forever, on the filesystem
  whose last 20GiB the dispatcher's own admission floor reserves. The relay now
  removes the staged copy when it is done, on every path including a refusal:
  those bytes are also in the run's `artifacts/`, digest-recorded, and the
  verdict pins that digest, so it is the one thing here that can be deleted
  without losing anything.

  MEASURED at a real 162_000-row holdout: **staged predictions 4.9 MB,
  `eval.parquet` 12.8 MB** per evaluation. So the copy that now goes was 28% of
  it and the record that stays is the rest. **OPEN ITEM, not fixed here:** at ~20
  evaluations a day that is ~250 MB/day retained, ~7.7 GB a month. `verdict.json`
  is kilobytes and is the record; `eval.parquet` is the audit trail and is
  reproducible from the extract, the predictions and the contract only for as
  long as the predictions survive. A retention policy for
  `/var/lib/qf-eval/<run_id>/out/` is somebody's decision, and deleting evidence
  here to avoid asking for it would be the wrong answer to a full disk.

- **NC11 clause (c) — the row-set fixtures. DONE 2026-08-29.**
  `host/nc-fixtures-phase2c.sh` writes five experiment scripts into a
  `qf-research` checkout, in the shape `nc-fixtures-phase2b.sh` established: it
  writes files and prints the git commands, never committing and never pushing,
  because the branch is the operator's to publish with the AGENT's credential.

  Each violates exactly one part of the property and would pass the others:
  `nc11_relabelled` (part 1, a row_id that disagrees with its own key),
  `nc11_ghost_row` (part 2, a row the extract does not contain),
  `nc11_cherry_picked` (part 3, complete days with the largest-`y_true` rows
  dropped inside them), `nc11_easy_days` (the day block, an earlier block
  complete within each day it claims) -- and `nc11_honest`, which must be
  ACCEPTED and is the reason the other four prove anything. Four of them come
  back as one class, `row_set_rejected`, which is all the job record carries; a
  contract disagreement, a wrong holdout length or an empty slice would produce
  the same class, so without an accepted honest set every refusal in the group
  could be measuring the same unrelated mistake.

  **THE FIXTURES ARE EXECUTED IN-REPO, and that is the part worth defending.**
  `evaluator/tests/test_nc11_fixtures.py` (16 tests) runs each generated script
  against the synthetic extract from `test_evaluate.py` and feeds its output to
  the real `evaluate()`, asserting not just the class but the REASON -- that the
  cherry-pick refusal says "omits" and not "not the candidate's to choose", that
  the ghost row's refusal is absence and not mislabelling. It also compares the
  fixtures' second implementation of `required_days` against the trusted one
  (they cannot import it: agent-authored code inside a sandbox), and proves the
  vacuity guards fire. Four claims red-green verified by breaking the fixtures:
  a cherry-pick that drops a whole day, a missing vacuity guard, an
  `easy_days` that claims every day, and an off-by-one in the derivation (which
  fails 28 tests).

  This cost three concessions in the fixtures -- `NC11_EXTRACT`, `NC11_OUT` and
  `NC11_HOLDOUT_DAYS` env overrides, none of which the sandbox sets -- and they
  are worth it: without them the refusal class each fixture produces would be a
  guess, and a fixture whose violation is subtly wrong yields the right class for
  the wrong reason, which reads as coverage and is not.

  **Writing it found two defects, and the first is in code I wrote a day
  earlier.** `qf list` takes `--state` and `--limit` and nothing else -- there is
  no `--kind`. Both NC11 loops passed `--kind probe`, so every invocation exited
  2 with "unrecognized arguments", read nothing, and voided with a message
  blaming the absence of a probe or of a fixture. **A filter the client rejects
  is not a filter, and the clause it disabled would have reported its subject
  missing on a host where the subject was right there.** The kind is now taken
  from the run id's own prefix, the experiment path is read from each probe's own
  spec through `qf --json status` (the only place it exists -- a listing has no
  path in it), and `test-nc-instrument.sh` now stubs `qf list` with argparse's
  ACTUAL refusal, so reintroducing the flag fails off-host: 33 clauses, 3
  red-green verified.

  The second came only from executing a fixture: relabelling EVERY row_id
  produces `evaluate_refused` -- "none of the predicted rows is in the frozen
  extract" -- which is a different property, correctly checked earlier, and not
  the row-set derivation at all. So the fixture now relabels one row in ten,
  which is both the realistic candidate bug and the only version that reaches
  the check it names. A fixture written from reasoning alone would have shipped
  asserting a class it never produced.

  **What is left is the operator's:** push the fixture branch, then one probe per
  script against the same extract. Until then clause (c) voids, naming the
  generator and the probe lines.

- **A command the client rejects is not a command, and it happened twice in two
  days.** The `--kind` above is one; the other was in the 2c generator's own
  instructions, which told the operator to run `qf submit --kind probe ...
  --extract <hash>` -- three impossibilities in one line, since `submit --kind`
  accepts only `test|selftest`, `submit` has no `--extract`, and a probe has its
  own subcommand. Neither is catchable by running the suite on a healthy host:
  argparse exits 2, the helper discards stderr, and the clause reports its
  subject missing.

  So `test_protocol.py` now PARSES the client's own argparse definitions and
  checks every `qf` invocation written in every host script against them --
  subcommand exists, flag exists on that subcommand, `submit --kind` gets a kind
  it accepts. It anchors on command position so prose is not scanned, cuts each
  line at `#` so an instruction in a comment is not read as a flag, and carries a
  canary that fails if the parse found nothing. `README.md` is deliberately NOT
  scanned: it documents invocations that were wrong, as lessons. Three
  red-green verifications: reintroducing `--kind`, restoring the bad `submit`
  line, and inventing a subcommand each fail it.

- **NC9 ran live for the first time (2026-08-29) and reported 15 pass, 1 fail --
  and the failure was the INSTRUMENT.** `NC9 (d) a trusted contract was refused
  at submit: evaluate-20260829T192144Z-f58141c0d68e-4448`. That string is a
  perfectly good run id; the dispatcher HAD accepted the contract and minted it.
  `is_run_id`'s first clause was `case "$1" in *[0-9]-[0-9]*)`, written to mean
  "there is a numeric seq" and actually meaning "some digit is immediately
  followed by a hyphen and another digit" -- which in a real id is satisfied only
  when the `sha[:12]` segment before the seq HAPPENS TO END IN A DIGIT. Six
  commits in sixteen end in a letter, and on those the helper rejects every id
  the dispatcher mints. All three fixtures in the harness ended in a digit
  (`9d54e39271d7`, `abcdef012345`, `000000000000`), so the check passed there for
  the same reason it failed here.

  Now the seq is tested as the seq -- the last hyphen-separated segment, all
  digits -- and the harness carries the real failing id plus one per kind ending
  in a letter (42 clauses, red-green verified: reverting the clause fails five).
  The direction of the bug is worth recording: `is_run_id` gates POSITIVE claims,
  so it produced false FAILURES rather than false passes, in seven clauses across
  the suite. NC9 (d)'s three follow-on assertions -- the job fails on the RUN not
  the contract, it reaches FAILED, and the contract is pinned regardless -- never
  ran, so NC9 is not yet a clean result.

- **The fixture probes must pin the promoted baseline**, and the generated
  instructions did not say so. None of the five scripts reads `/baseline`, but the
  evaluator refuses a judged run that recorded no `baseline_hash` (a bar stated
  against one baseline and measured against another is not the bar that was
  agreed) -- so five probes submitted without `--baseline` would ALL be refused
  for that reason, canary included, and clause (c) would void with nothing to say
  about row sets. The generator's probe line now carries it with the reason
  attached, and a test asserts it on the joined command rather than on the
  instruction block: the first version of that test searched the whole block,
  where `--baseline` also appears in a `qf baselines` comment, so deleting the
  flag from the probe line left it green.

- **The pipeline's own baseline directory is not promotable, by design, and the
  refusal now says what to do.** `ensure_baseline_ndjson.sh` writes a coverage
  sidecar (`baseline_predictions.ndjson.meta.json`) beside the aggregate, and
  `trainer/data/baseline_filtered/` keeps per-day files from every earlier cohort
  of that policy. `baseline.describe` is closed-world, so it refuses both -- which
  is right twice over: the sidecar is not part of the identity, and those older
  days would otherwise be recorded as part of THIS baseline while the declared
  `exclude_dates` describe only the latest regeneration. `promote-baseline.sh`'s
  own header example pointed straight at that directory, so the documented
  command could not have worked. It now shows the staging recipe, and on a refusal
  the script lists the offending files and prints the fix. Three new clauses in
  `test_promote_baseline.sh` (23 total), red-green verified.

- **The suite takes group names now**, because the operator is about to run NC9
  and NC11 several times while bringing a host up and the whole suite submits real
  jobs against real deadlines. `sudo ./nc-suite-phase2.sh nc9 nc11` runs those
  two; no argument runs all twelve. **A partial run labels itself** in the totals
  line and in the evidence file: `pass=12 fail=0` from two groups is
  indistinguishable from a full clean run once it is in a file.

  That refactor silently emptied three tests -- `nc9`, `nc17`/`nc18` and `nc19`
  each asserted their wiring by matching a bare call line in `main`, which no
  longer exists -- so the wiring assertion is now one shared helper checking BOTH
  hand-written lists (the default set and the validating `case`), and the harness
  enumerates every `ncN()` definition against both. Two mistakes of mine in
  writing those checks, both the same species as everything else here: a `sed`
  range whose end pattern matched its own start line, so it ran on to the NEXT
  `)` -- the validating `case` -- and reported the group present after I had
  deleted it from the default set; and a `split()` that left `groups=(nc8` and
  `nc19)` as tokens, so the first and last group in the list would each have read
  as absent. Both found by red-green, which is the only reason either is written
  down as a fixed thing rather than a live one.

- **The unit-drift list had not grown with the phases.** `assert_units_current`
  is what stops `mirror-refresh` restarting the daemon into configuration from an
  older commit, and it works off a hand-written list of five units -- the
  evaluator's two were not in it, so a refresh would have restarted `qf-eval`
  under a stale unit silently, which is the exact failure that function exists to
  make loud. Added, plus a test in `test_unit_drift.sh` that ENUMERATES every
  `.service`/`.socket`/`.timer` in `host/*/` and requires each to be named in the
  list, so the next phase cannot forget either. `phase2c-setup.sh discover` also
  compares the installed `qf-eval.conf` against the checkout now: tmpfiles is
  applied at every boot, so a stale copy does not merely describe the wrong
  staging root, it restores it.

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
