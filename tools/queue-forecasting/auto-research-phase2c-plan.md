# Phase 2c — evaluation integrity

Revision 1. Written after 2b-3's implementation, per the convention in
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
- **Task 18 — the first two contracts.** `wait_time.v1.json` and
  `run_duration.v1.json`, transcribing `trainer-spec.md`'s bars plus the tail
  gate, each pinning a `baseline_hash`. Transcription is checked by a test
  against the spec text, not by eye.
- **Task 19 — the `qfeval` domain.** User, socket unit, service unit, startup
  refusals for forbidden group membership, `ping`. Mechanically the
  `qf-extract` pattern with less authority -- and per 2b-1's lesson, `main()`
  **serves** refusals rather than exiting, because under socket activation
  exiting non-zero is a hang.
- **Task 20 — the `evaluate` kind, and NC9.** `args.contract` is a 64-hex hash
  resolved and verified against the trusted checkout; a job naming a contract
  that is not there, or whose body does not hash to its name, is refused before
  anything starts. NC9 asserts the refusal *and* that a valid contract still
  works, so a broken resolver cannot pass by refusing everything.

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
3. **What tolerance is "agrees to tolerance"?** Counts must agree exactly.
   `sum_abs_error` is a float sum over ~10^6 rows in a different order, so the
   bound has to come from a measurement, not a guess -- the same way
   `MAX_WINDOW_DAYS` came from 8 minutes over 36 days rather than from an
   opinion.
