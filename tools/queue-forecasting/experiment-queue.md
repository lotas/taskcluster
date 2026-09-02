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

## RESOLVED 2026-08-31: qctx clears three of four bars, and the tail is the only blocker

Two corrections to the sections above, from the first two in-series scoreboards.

**Finding 2's 3.6x disagreement was my own cross-series comparison.** The
in-series reference scores **−4.038% MAE on the contract's unfiltered slice**
against **−4.1% on the trainer's filtered slice** — 0.06pp apart, not 2.6pp and
not 3.6x. The −6.7% quoted above came from a gen-1 run on extract `cd467b4b`.
Population mismatch is not a small effect, it is a non-effect. The walk-forward
gap is drift, and drift alone.

**The evaluator is deterministic too.** The same probe re-evaluated at 10:34 and
10:35 returned bit-identical metrics, as the trainer already had via
`predictions_sha256`.

### The two scored rows

| metric | baseline | reference | `..._baseline_qctx` | bar |
|---|---|---|---|---|
| MAE | 234.6s | 225.1s (−4.04%) | **171.6s (−26.9%)** | −15% |
| within_2x | 0.5933 | 0.6375 (+4.4pp) | **0.6884 (+9.5pp)** | +5pp |
| p90 coverage | 0.8915 | 0.8901 | 0.8821 | 0.85–0.95 |
| 30m+ p90 miss | 0.3989 | **0.2946 PASS** | **0.3108 FAIL** | < 0.30 |
| days passed | — | 0/3 | **4/3** | 3 |

qctx is **one metric from a full go, missing by 1.08pp**. Nothing before it
cleared the MAE bar at all; it clears by 11.9pp with consistency to spare.

**No leakage.** Checked before believing it. Every qctx window is
backward-closed: backlog is `pending_at <= T AND exit > T`, arrivals and starts
are `(T-w, T]`, and capacity is "the latest sample with `sampled_at <= T`"
(`queue_context.py:919`). Nothing in the feature set reads past T.

### Why the tail metric got worse while everything else got better

qctx made the model **sharper**, and sharpness costs tail coverage: p90 coverage
fell 0.8901 → 0.8821 and the 30m+ miss rose 0.2946 → 0.3108 while MAE fell 24%.
The reference passes that bar largely by **over-inflating its p90**; qctx stopped
needing to. So the tail miss is not qctx predicting the tail worse — it is qctx
no longer being rescued by a wide guardrail.

That is worth stating plainly because it defines what a legitimate fix is. The
1.08pp gap is almost certainly closable by widening the p90 guardrail in the
tail — coverage has room from 0.8821 to 0.95, and within_2x has +4.5pp of slack
above its bar to spend. But that means re-inflating exactly what qctx fixed, and
the program's goal (group ETA for `mach try`) needs sharp tails, not wide ones.
**Run it as a diagnostic to bound the gap; do not promote on it.**

### The run above tested features a prior ablation had already rejected

`wait_hazard_qctx_d_priority_flow.yaml:60` records a Bet 1 finding that was never
in `walk_forward_summary.csv`: qctx_b beat qctx_a, qctx_c was statistically
indistinguishable from qctx_b, and **capacity "appears to actively dilute the
model, not just sit inert."** The config scored above carries every feature that
ablation rejected. That finding is pre-freeze and therefore not in-series, which
is what entry 2 is for.

## The queue

Each entry is one variable. Run against the canonical trio in `AGENTS.md`.

**1. DONE — the reference and the first qctx result.** Reference
`probe-20260830T202842Z-4a2ae967d664-5418` (−4.04% contract, no-go); qctx
`probe-20260830T193901Z-22bcaf4f474a-5344` (−26.9%, no-go on the tail alone).
Everything below is judged against both rows.

**2. DONE — `wait_qctx_d_priority_flow`: capacity is a two-sided wash, not dilution.**
`probe-20260831T130111Z-51b862ebf4de-5568`. Dropping the 12 capacity/repo
features moved the tail the predicted direction and cost central accuracy:

| metric | reference | qctx (all) | qctx_d (no capacity) | bar |
|---|---|---|---|---|
| MAE | −4.04% | −26.86% | −25.85% | −15% |
| within_2x | +4.42pp | +9.51pp | +8.68pp | +5pp |
| p90 coverage | 0.8901 | 0.8821 | 0.8870 | 0.85–0.95 |
| 30m+ p90 miss | 0.2946 | 0.3108 | **0.3042 FAIL** | <0.30 |

**The pre-freeze ablation finding does not transfer.** Capacity is not diluting
the model; it trades ~1.0pp of MAE and ~0.8pp of within_2x for ~0.7pp of tail
coverage. Both effects are real and both are small — which is itself the useful
result: the qctx win is carried by the priority/flow features, and capacity is
close to a wash. The comment at `wait_hazard_qctx_d_priority_flow.yaml:60`
("capacity appears to actively dilute the model") should be read as pre-freeze
and superseded.

**The gap is now 0.42pp on one metric.** That is inside the range where entry 4
stops being academic.

**3. `wait_hazard_qctx_d_priority_flow` — RAN 2026-09-01 on `8734690f`; clears
the tail bar, fails the centre. Needs a comparator, not a rerun.** First attempt
refused:
`probe-20260831T135844Z-18c7eb6ed0db-5632`,
`ExtractError: the extract's train_start is 2026-08-07, but this cohort needs
train_start <= 2026-08-01`. The config's documented `validation_days: 7` (vs 1
everywhere else) pushes train_start back exactly 6 days:
`as_of − holdout(5) − validation(7) − lookback(14)`. **The canonical trio was
frozen against a config family with `validation_days: 1` and cannot hold this
cohort.** The guard is right — it refused rather than quietly training on 8 days
less data.

Do NOT fix this by cutting `validation_days` to 1. That number is measured (val
AUC on bins 3-4 went 0.711/0.574 → 0.915/0.909), and shrinking it to fit an
extract sabotages the experiment to make it runnable.

~~The fix needs no new extract: `c179c7f5b961` is already published, gen=2 (so it
has `task_created`), and spans 2026-07-21..2026-08-26.~~ **CORRECTED 2026-09-01
— that sentence is wrong, and the run it recommended cannot happen.**
`experiment.py plan` refuses `c179c7f5b961`: *"qctx_runs has no `task_created`,
and this config enables queue_context_features"*. The check at
`host/experiment.py:190` reads the extract's actual `qctx_runs` columns; it does
not infer them from the generation number, so "gen=2, therefore it has
`task_created`" was an inference the manifest does not support. The same reason
rules out `8e94d833d4c6`, and `cd467b4bd869` fails on both that and the window.

**`8734690f4cd8` is the only extract that can currently serve this config**
(published 2026-08-31; `plan` reports it as gen=1). Nothing else in the
published set clears both the `task_created` requirement and the
`train_start <= 2026-08-01` window.

**The reference run on it already exists — do not create another.** The
"NO scored run has used this extract yet" line quoted above was `plan`'s output
at 11:29Z on 2026-09-01 and is now stale. `probe-20260901T171159Z-61dd1b700db5-6004`
ran this very config on `8734690f` and scored at 17:58Z:

| metric | value | bar | |
|---|---|---|---|
| mae | 0.007302 | rel_improve 0.15 | FAIL |
| p90_coverage | 0.9409 | band 0.85–0.95 | pass |
| p90_miss_tail | **0.1631** | absolute 0.30 | **PASS** |
| within_2x | −0.1616 | abs_improve +0.05 | FAIL |

Verdict `no-go`, but it is the first config in the program to clear
`p90_miss_tail` — 59% below the baseline's 0.3989, and 46% better than qctx_d's
0.3042.

So this entry is no longer blocked, and the run it asked for has happened. **What
is missing is a comparator, not a reference.** `results.sh` reports exactly one
row on `extract=8734690f baseline=e51a3210 contract=f740716d`, so the hazard
result currently sits in a series of one and `plan` will keep calling anything
there "comparable to NOTHING". The next run on this extract should be **qctx_d**,
so the two configs can be read as a pair inside one series rather than across
extracts. (They already agree on baseline and contract and report identical
baseline values to 4 s.f., which makes the cross-extract reading sound enough to
reason from — but not a series, and the frontier is right not to treat it as
one.)

Memory, now measured rather than predicted (the earlier "8.6M runs vs 5.0M
(+71%)" warning described `c179c7f5b961`, which is ruled out above and is not
the extract in play). On `8734690f`'s 6.02M rows this config was OOM-killed at
20g — `probe-20260831T155907Z-1ecbb864be31-5709`, exit 137, `rss_high_water_kb`
20,636,960 after 54.7 min — and completed at 22g. `--mem 22g` is therefore
required and is also the ceiling (`spec.py:43 MEM_CEILING_MB`);
`experiment.py`'s auto-retry fires only on a *refusal*, and 20g is under the
ceiling so it is never refused — the flag is necessary, not decorative, and it
is a TOP-LEVEL flag placed before `run`.

*Provenance: this correction was derived by the research loop's leader on
2026-09-01 and verified here against `host/experiment.py:190`. The entry it came
from was escalated over unrelated defects and remains NOT RECORDED
(`journal/escalations/20260901T113305Z.md`); nothing about that escalation is
reversed by repeating a check that stands on its own.*

**4. The guardrail-width diagnostic (see above).** Not a promotion candidate.
Bounds how much of the 1.08pp is information versus inflation, which tells us
whether entries 2 and 3 are solving a real problem or a calibration one.

**5. `..._baseline_pool` — the pool class alone (config written).**
`pool_kind` and `provider_type` on top of the promoted config, nothing removed.
Per Finding 1 these two columns have never been tested on their own. The
hypothesis is specific: Azure/Windows queues wait very differently from GCP ones,
and today the model can only learn that through `task_queue_id`, whose cardinality
prevents generalising from a busy Azure queue to a quiet one. **Demoted by the
qctx result**: it is a variant of the promoted config, which is now 23pp of MAE
behind qctx, so a win here is a win on a superseded base. Rebase it on entry 2's
winner before running it.

**6. `bl_wait_p90` — is it earning its place?**
Every config that dropped it (`wait_time`, velocity, `filtered_both`) also changed
other things, so its contribution is unmeasured. A one-line delta, and if it
contributes nothing the residual model is simpler by a feature. Same rebase note
as entry 5.

**7. `normalized_name` — task identity, never used by a WAIT config.**
Both `run_duration.yaml` and `run_duration_residual.yaml` carry it as a
categorical; no wait config does. That precedent is the useful part: the
cardinality is evidently trainable here at the shared `min_data_in_leaf: 100`, so
the wait version is a one-line addition with a known starting point rather than a
tuning exercise. Same rebase note.

**8. Re-run qctx_a / _b / _c in-series.** Only if entry 2 disagrees with the
pre-freeze ablation. Three probes to re-derive a finding we already have on
paper; worth it only if the paper turns out to be wrong.

## What would change this ordering

- **Entry 2 clearing all four bars.** Then the question stops being "what
  feature" and becomes promotion: a walk-forward sweep across cohorts to check
  the result is not one week of data, then serving. The tail-modelling program
  (Bet 2) becomes optional rather than load-bearing.
- **A tail result that does not transfer.** The 68%-of-error decomposition is
  from one cohort. If entry 3's tail gain evaporates on a second, the leverage
  argument is about this week rather than about the problem.
- **A `<1m` within-2x idea.** Nothing in the queue targets the bucket holding
  half the rows with the worst within-2x (49.5%). Sub-minute waits are probably
  dominated by scheduler and claim latency rather than queueing, and no feature
  in any config describes that. A gap, not an entry — it needs a hypothesis
  first. Note qctx already bought +9.5pp of within_2x, so some of this may
  already be paid.
- **Evidence about the bar itself.** Population is ruled out; drift is not
  quantified. Re-running one April cohort through the current pipeline would say
  whether −24.3% was the model or the month. Less urgent now that a config
  clears 15% in-series by 11.9pp.

Every entry is judged against entry 1 rather than against the bar, so a config
that beats the reference is an improvement whether or not the reference passes.
