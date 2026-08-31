# What to run next, and why

Written 2026-08-30, from `walk_forward_summary.csv` (105 wait_time rows, 17
cohorts) and a key-by-key diff of the configs that produced them. Nothing here
needs new platform work; every entry is a config or a feature change that the
existing `qf probe` → `qf evaluate` loop already runs.

## Finding 1: the experiment history is confounded, so three "tried" ideas are untried

Every wait config was compared against the promoted candidate
(`wait_time_residual_throughput_filtered_baseline`) on the cohorts they share.
Medians, challenger minus reference, on the trainer's primary slice:

| config | n | mae % | within-2x pp | 5-30m MAE s | 30m+ MAE s | 5-30m w2x | 30m+ w2x |
|---|---|---|---|---|---|---|---|
| `wait_time` | 16 | +1.45 | −12.52 | **−20.4** | **−76.4** | −0.0489 | −0.0263 |
| `wait_time_residual` | 13 | +4.67 | −2.68 | +3.3 | +308.2 | −0.0134 | −0.0508 |
| `..._throughput` | 16 | +3.59 | −0.20 | **−28.8** | +238.5 | −0.0050 | −0.0419 |
| `..._throughput_filtered` | 12 | +1.05 | −0.29 | −5.3 | +113.5 | −0.0031 | −0.0243 |
| `..._throughput_filtered_both` | 12 | **−0.37** | −0.48 | −10.5 | +41.6 | −0.0043 | −0.0163 |
| `..._residual_velocity` | 15 | +1.27 | −6.54 | −11.1 | **−154.7** | **−0.1176** | −0.0108 |

(`mae %` is model-vs-baseline error, so lower is better and a negative difference
means the challenger beat the reference. `within-2x` columns: higher is better.
MAE-in-seconds columns: lower is better. The reference's own medians are −24.3%
MAE and +6.6pp within-2x over 16 cohorts.)

Read naively: the candidate wins nearly everything, and the two configs that beat
it on the 30m+ tail pay 12.5pp and 6.5pp of within-2x for it.

**That reading does not hold.** A key-by-key diff shows those configs are not
feature tests:

- `wait_time_residual_velocity` differs from the candidate in **six keys and
  about nineteen features**. It adds `pool_kind`, `provider_type` and eight
  capacity numerics — and it also **loses Policy B** (`anomaly_filter` plus the
  filtered baseline history), **loses `bl_wait_p90`**, and **loses every
  throughput feature**. Policy B is the change that fixed the regime fragility in
  the first place. Attributing this config's within-2x regression to the pool
  columns is not something its numbers support.
- `wait_time` differs in five keys: no residual at all, and no Policy B. So
  "LGB-only is best in the tail" is entangled with the same thing.
- `..._filtered_both` is the only near-clean comparison (two keys: anomaly mode
  `both` vs `baseline`, and dropping `bl_wait_p90`) — and it lands within noise.

So the queue below is mostly not new ideas. It is the one-variable version of
experiments that were already run and dismissed.

## Finding 2: the walk-forward numbers and the contract numbers disagree by 3.6x

The candidate's walk-forward median is **−24.3% MAE / +6.6pp within-2x**. The
contract-scored run on the frozen extract gave **−6.7% / +4.4pp** — a no-go
against bars of 15% and 5pp.

Both are called the "primary slice" and both mean `reason_resolved = 'completed'`,
but they are **not the same population**: the trainer's primary slice is computed
on the FILTERED holdout (`started_at IS NOT NULL`, `queue_pending IS NOT NULL`,
`wait >= 0`), and the evaluator's is the extract's completed rows with **none of
those filters** — deliberately, because the completeness rule exists to stop a
candidate from choosing its own population. The cohorts also differ: walk-forward
ran Apr–May 2026, the contract run is Aug 2026, and there is a known regime break
between them.

This matters more than any single experiment: **if the 15% bar was set on
filtered-population numbers and is being applied to the unfiltered slice, it is a
bar the same model cannot reach** — and every no-go verdict is measuring the
population change, not the model.

It costs nothing to settle. Both numbers exist for the SAME run: the trainer
prints its own `Delta MAE: X%` on the filtered slice, and the evaluator's
scoreboard reports the contract's on the unfiltered one.

```sh
qf logs <probe run id> | grep -E "Delta MAE|per-bucket|^(<1m|1-5m|5-30m|30m\+)"
```

If those two disagree sharply on one cohort, the population mismatch is the
cause. If they agree, the population mismatch is RULED OUT — and that is all it
rules out. The remaining walk-forward/contract gap could still be regime drift,
or code and data-snapshot drift between April and August, and a 15% bar may
simply no longer be attainable on this data. **Bar calibration is a separate
question from population, and this check does not answer it.** Do not touch the
contract either way until the reference result exists.

The same command also prints per-bucket ROW COUNTS, which is what is needed to
know how much of the aggregate each bucket can actually move — the summary CSV
has bucket MAEs but no weights, so no decomposition is possible from it alone.

## RESOLVED 2026-08-30: Finding 2 is drift, and the MAE bar is a TAIL bar

The in-series reference ran (`probe-20260830T202842Z-4a2ae967d664-5418`). Both
numbers, same run:

| population | delta MAE | source |
|---|---|---|
| filtered (trainer's own) | **−4.1%** | `qf logs` |
| unfiltered contract slice | **−6.7%** | scoreboard |

They agree in sign and are 2.6pp apart, not 3.6x. **The population mismatch is
ruled out** — and that is all it rules out. The walk-forward median of −24.3%
against today's −4.1% is therefore regime drift or code/data-snapshot drift
between April and August. Whether 15% is still the right bar is a separate
question and is NOT answered by this.

Two other things fell out of that run.

**Reproducibility, confirmed by accident.** The reference's
`predictions_sha256` is `c200360e10c1...` — byte-identical to the 2026-08-30
14:46 run on the gen-1 extract. The promoted config reads `runs`,
`throughput_runs` and `daily_health`, and `qctx_runs` is the only file that
changed between gen 1 and gen 2, so the training inputs really were identical
and produced an identical model. That is the first hard evidence the canonical
row sort made the pipeline deterministic end to end. It also means
`results.sh`'s cross-series warning is conservative: it compares extract hashes,
not the files each config actually reads.

**The bucket decomposition, which the summary CSV could not give.** Per-bucket
row counts from that run:

| bucket | rows | row % | % of model error | model vs baseline | aggregate MAE if this bucket improves 20% |
|---|---|---|---|---|---|
| <1m | 562,562 | 49.8% | 3.5% | −24.9% | −4.75% |
| 1-5m | 365,583 | 32.3% | 8.0% | −15.9% | −5.61% |
| 5-30m | 161,823 | 14.3% | 20.5% | −12.2% | −8.02% |
| **30m+** | **40,359** | **3.6%** | **68.0%** | **+1.9%** | **−17.13%** |

(Reconstructed aggregate: −4.08% MAE and +4.35pp within-2x, against the reported
−4.1% and +4.4pp. The decomposition is sound.)

**68% of the error is in 3.6% of the rows, and that is the only bucket where the
model LOSES to the baseline.** What each bucket would have to do alone to reach
the 15% bar:

- `<1m` and `1-5m`: **impossible.** Reduce their error to zero and the rest
  still exceeds the budget.
- `5-30m`: cut its MAE by **55%** (323s -> 144s, against a 368s baseline).
- `30m+`: cut its MAE by **16.7%** (4284s -> 3567s, against a 4205s baseline).

So the aggregate MAE bar is reachable through the tail and effectively not
reachable any other way. A relative percent of tail improvement is worth about
3.3x the same percent in 5-30m.

**The within-2x bar is the opposite.** It needs +0.6pp more, and the leverage is
in the biggest bucket, not the tail:

- `<1m`: **+1.2pp in-bucket** clears it — on half the rows, at 49.5% today
  (baseline 45.4%), the worst within-2x of any bucket.
- `5-30m`: +4.2pp needed. `30m+`: +16.8pp needed.

**This contradicts the framing the program has been run on.** "5-30m is the weak
bucket" came from the model's relative edge over baseline being thinnest there
among the short buckets — true, and it holds only 20.5% of the error. The two
bars are closed in two different places: MAE in the tail, within-2x in `<1m`.

## The queue

Each entry is one variable. Run against the canonical trio in `AGENTS.md`.

**1. DONE — the promoted config is the in-series reference.**
`probe-20260830T202842Z-4a2ae967d664-5418`, −4.1% filtered / −6.7% contract,
verdict no-go. Everything below is judged against this row.

**2. The hazard config — the only entry with the leverage to move the MAE bar.**
`wait_hazard_qctx_d_priority_flow`, promoted from entry 5 by the decomposition
above. Memory of Bet 2's walk-forward: it roughly HALVES the 30m+ p90 miss
(18.97% vs 34.59%) while costing 12.6pp of overall within-2x. Read against the
table above, that trade is no longer obviously bad: the tail is 68% of the error,
a 16.7% tail cut clears the MAE bar on its own, and within-2x needs only +0.6pp
which `<1m` can supply separately. Its last finite bin edge is 480 minutes, so
watch for the terminal-risk-set refusal described in `AGENTS.md`.

**3. `..._baseline_qctx` — queue-context features (Bet 1's first measurement).**
`walk_forward_summary.csv` has **zero** rows for any qctx config: they were built
and never measured across cohorts. Adds ~30 numerics describing the queue a task
lands in (pending counts by priority, arrivals, per-capacity ratios). Aimed at
the 5-30m bucket, which is where the candidate's error concentrates and where
every previous feature has failed. Needs the gen-2 extract for `task_created`.

**4. `..._baseline_pool` — the pool class alone (NEW, config written).**
`pool_kind` and `provider_type` on top of the candidate, nothing removed. Per
Finding 1, these two columns have never been tested on their own. The hypothesis
is specific: Azure/Windows queues wait very differently from GCP ones, and today
the model can only learn that through `task_queue_id`, whose cardinality prevents
it from generalising from a busy Azure queue to a quiet one. A pool class is the
same fact at a usable cardinality. `test_ablation_configs.py` asserts the delta is
exactly two categoricals plus the join that supplies them. Costs a
`worker_counts` load the candidate does not do, so expect a peak above 17.4GB.

**5. `bl_wait_p90` — is it earning its place?**
Every config that dropped it (`wait_time`, velocity, `filtered_both`) also
changed other things, so its contribution is unmeasured. Removing it from the
candidate is a one-line delta, and if it contributes nothing then the residual
model is simpler by a feature. A negative result here is worth having.

**6. `normalized_name` — task identity, never used by a WAIT config.**
Both `run_duration.yaml` and `run_duration_residual.yaml` carry it as a
categorical; no wait config does. That precedent is the useful part: the
cardinality is evidently trainable in this codebase at the shared
`min_data_in_leaf: 100`, so the wait version is a one-line addition with a known
starting point rather than a tuning exercise. A specific test suite's queueing
behaviour is plausibly the strongest per-task signal available, and no wait
experiment has looked.

## What would change this ordering

Entry 1 is done and it already reordered the queue once. What would move things
again:

- **A tail result that does not transfer.** The 68%-of-error figure is from one
  cohort. If entry 2's tail gain evaporates on a second cohort, the leverage
  argument is about this week's data rather than about the problem, and 5-30m
  work goes back to the top.
- **A `<1m` within-2x idea.** Nothing in the queue targets the bucket that holds
  half the rows and has the worst within-2x (49.5%). Sub-minute waits are
  probably dominated by scheduler and claim latency rather than by queueing, and
  no feature in any config describes that. This is a gap in the queue, not an
  entry in it — it needs a hypothesis first.
- **Evidence about the bar itself.** Population is ruled out; drift is not
  quantified. Re-running one April cohort through the current pipeline would say
  whether −24.3% was the model or the month. That is the honest way to decide
  whether 15% is still the right number, and it is not something a verdict can
  tell us.

Every entry is judged against entry 1 rather than against the bar, so a config
that beats the reference is an improvement whether or not the reference passes.
