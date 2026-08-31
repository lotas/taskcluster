# The research loop, for whoever is running it

THIS FILE BELONGS AT THE ROOT OF `qf-research`. It lives in the trusted repo
because the operator owns it: `first-probe.sh` copies it in beside
`research/experiments/run_cohort.py`. Editing it here is editing the instructions
every future experiment starts from; editing the copy is editing a scratch file
that the next bootstrap overwrites.

## What you are doing

Predicting how long a Taskcluster task will wait before it starts. A percentile
baseline is what you are beating, and a contract of four bars is what decides it.

**As of 2026-08-31 the situation is not "close the gap" any more.** Queue-context
features cleared the MAE bar by 11.9pp and the within-2x bar by 3.7pp on the
first try. **One bar is still failing, by 0.42pp**: the 30m+ p90 miss, at 0.3042
against a 0.30 ceiling. So the job is narrower and harder than a general search
for improvement -- it is a tail-coverage problem with three bars already banked,
and a change that trades any of them away for tail coverage has to trade at a
rate the contract survives.

**The ranked list of what to run and why is at**

```
/srv/queue-forecasting/tools/queue-forecasting/experiment-queue.md
```

**by absolute path**, because it is not copied into this repo -- bootstrap brings
only this file and `research/experiments/run_cohort.py`, and a bare filename
would send you looking in a checkout that does not have it.

Read it before proposing anything: it records which ideas were already tried and
-- more importantly -- which of those were tried in a CONFOUNDED form and are
therefore still open. Three of its six entries are one-variable versions of
experiments that were run, dismissed, and cannot actually be attributed.

### Where the error actually is

Measured on one cohort of 1.13M holdout rows, and it contradicts what this file
used to say:

| bucket | rows | share of rows | share of model error |
|---|---|---|---|
| <1m | 562,562 | 49.8% | 3.5% |
| 1-5m | 365,583 | 32.3% | 8.0% |
| 5-30m | 161,823 | 14.3% | 20.5% |
| **30m+** | **40,359** | **3.6%** | **68.0%** |

**68% of the error is in 3.6% of the rows.** `<1m` and `1-5m` cannot reach the
MAE bar even at zero error -- their whole contribution is smaller than the bar.
So MAE is a tail bar whatever it is called, and this file's old claim that
"5-30m is the bucket where the information is missing" came from the model's
relative edge over baseline being thinnest there, not from leverage.

The mirror image: **within-2x leverage is in `<1m`**, which holds half the rows
and has the worst within-2x of any bucket (49.5%). Nothing in the queue targets
it, because nobody has a hypothesis for it -- sub-minute waits are probably
scheduler and claim latency rather than queueing, and no feature in any config
describes that. If you have one, that is a real contribution.

## The canonical inputs

**Two published series, and which one you use is decided by arithmetic, not by
preference.** A frozen extract's window is a function of the config family it was
requested for, so a config with a wider train window than that family's cannot
run on it at all.

**Series A -- the default.** For configs with `validation_days: 1`, which is
every quantile config. All the scored results below are on this series.

```
EXTRACT   bd29b39ab6254a3cf5de6a7413c1476a6caa178a0685f88aaa7d489c9a2db91f
BASELINE  e51a321057ca884977edc357c3c2c254dcefb01ed700f9009f5d92b412ec9a27
CONTRACT  f740716d32b8ddef20bd2e42ede873fd0b59486f752c8d077293ebc440997173
```

**Series B -- wider window, for configs Series A cannot hold.** Its window starts
2026-07-21 and its `as_of` is 2026-08-26, one day earlier than Series A, so its
holdout is a DIFFERENT population.

```
EXTRACT   c179c7f5b961edc30fac1a494be4867f50129c2639a99b7015e77f9be6c47a12
BASELINE  e51a321057ca884977edc357c3c2c254dcefb01ed700f9009f5d92b412ec9a27
CONTRACT  f740716d32b8ddef20bd2e42ede873fd0b59486f752c8d077293ebc440997173
```

**Series B has no scored rows yet.** So a result there is comparable to nothing
until you also run the config you are comparing against on the same extract. Two
runs, not one. If you are not willing to spend the second run, you do not have an
experiment.

### Choosing between them

`run_cohort.py` computes your cohort's train_start in the first second and
refuses if the extract cannot cover it:

```
train_start = as_of - holdout_days - validation_days - lookback_days
```

Use Series A. If that check refuses, use Series B and run the comparison config
there too. If Series B also refuses, **stop and report it** -- do not go looking
through `qf extracts` for something that fits, and above all do not shrink the
config's window keys to make the run start. A window key is part of what is being
tested (`wait_hazard_qctx_d_priority_flow` carries `validation_days: 7` for a
measured reason: it lifted validation AUC on the far bins from 0.711/0.574 to
0.915/0.909), and editing it to fit an extract changes the experiment into a
different one that happens to be runnable.

Copy the hashes verbatim. `qf extracts`, `qf baselines` and `qf contracts` are
for CONFIRMING they exist and reading their windows. They are not a menu, and
"something newer exists" is not permission -- a run against an extract nobody
else used is a number that belongs to no series.

### The scored results so far

Series A, all on the contract's unfiltered `completed` slice. Read the queue for
what each one means; these are here so you know what you are beating.

| config | mae | within_2x | p90 cov | 30m+ miss | days |
|---|---|---|---|---|---|
| bar | −0.15 | +0.05 | 0.85–0.95 | <0.30 | 3 |
| `..._filtered_baseline` (reference) | 0.0404 ✗ | 0.0442 ✗ | 0.8901 | 0.2946 ✓ | 0/3 |
| `..._filtered_baseline_qctx` | **0.2686** ✓ | **0.0951** ✓ | 0.8821 | 0.3108 ✗ | 4/3 |
| `wait_qctx_d_priority_flow` | 0.2585 ✓ | 0.0868 ✓ | 0.8870 | **0.3042** ✗ | — |

All three are no-go. The last two fail on the tail alone.

**Read the third row before proposing a feature.** qctx_d is qctx minus the 8
capacity numerics and 4 other columns, and it moved the tail 0.66pp closer while
costing 1.0pp of MAE and 0.8pp of within-2x. Two lessons in one row: the tail and
the central metrics trade against each other here, and a pre-freeze ablation
finding that capacity "actively dilutes the model" did NOT reproduce in-series.
Off-series findings are hypotheses, including the ones written in config
comments.

## The loop

One experiment is one commit.

```sh
# 1. change ONE thing: a feature in trainer/src/, or a config in trainer/configs/
#    If you add a config, point CONFIG in research/experiments/run_cohort.py at it.

# 2. commit and push -- a probe runs a COMMIT, never a working tree
git add -A && git commit -m "what changed and why you expect it to help" && git push

# 3. the series from "The canonical inputs" -- Series A unless it refuses you
EXTRACT=bd29b39ab6254a3cf5de6a7413c1476a6caa178a0685f88aaa7d489c9a2db91f
BASELINE=e51a321057ca884977edc357c3c2c254dcefb01ed700f9009f5d92b412ec9a27
CONTRACT=f740716d32b8ddef20bd2e42ede873fd0b59486f752c8d077293ebc440997173

# 4. train the cohort (~10-20 min). 20g because this peaks near 17.4GB;
#    the host ceiling is 22g.
qf probe --sha "$(git rev-parse HEAD)" \
    --path research/experiments/run_cohort.py \
    --extract "$EXTRACT" --baseline "$BASELINE" --mem 20g --wait

# 5. score it against the contract
qf evaluate --run <probe run id> --contract "$CONTRACT" --wait
```

`qf extracts`, `qf baselines` and `qf contracts` are for CONFIRMING those three
exist, and for reading what the extract's window is. They are not a menu.

Step 5 prints a `scoreboard:` block. That is the result of the experiment --
the verdict word on its own tells you nothing about how close you came.

## Reading a result

```sh
# EVERY experiment so far, one row each, oldest first. Read this before
# proposing the next change: a hypothesis somebody already tested is not one.
/srv/queue-forecasting/tools/queue-forecasting/host/results.sh
/srv/queue-forecasting/tools/queue-forecasting/host/results.sh --json

qf status <evaluate run id> --json     # one result: pins.scoreboard
qf logs <probe run id>                 # the trainer's own output
qf logs <probe run id> --stream stderr # why it failed, when it failed
```

`results.sh` prints an `extract` column and warns when the table holds rows from
more than one input set. Rows from different extracts are not comparable to each
other -- if your row's extract prefix differs from the reference row's, you have
measured something else.

Each metric in the scoreboard carries `value` (what the model scored),
`baseline` (what the percentile model scored), `measured` (the quantity actually
compared against the bar) and `bar` (the rule). The live example is
`measured=0.3042 bar=absolute:0.3` -- 0.3042 where 0.30 or lower was needed, so
the whole verdict turns on 0.42pp of one metric.

`days_passed=N/M` is the consistency rule: a result has to hold on M of the
holdout days, not just in aggregate. A change that wins overall and loses on two
days is a change that has found a day, not a signal.

## Things that will refuse you, and why

- **No database.** A probe has no `DATABASE_URL` and no network. Training reads
  the frozen extract at `/extract`. If you find yourself wanting a query, the
  column you want has to be added to the extract by a human first.
- **A config the extract cannot serve is refused in the first second**, not
  twenty minutes in. Three checks, all from the manifest: a target mismatch, a
  config enabling `queue_context_features` against an extract with no
  `task_created`, and a config whose train window reaches earlier than the
  extract's. The third one names the remedy, because the remedy is not yours to
  apply -- see "Choosing between them".
- **The extract is immutable.** You cannot widen its window or add a column. A
  column that is not in it has to be added by a human, to trusted code, and
  re-extracted -- which produces a new hash and therefore a new series.
- **You cannot change the contract or the baseline.** Both are named by hash from
  the trusted checkout. A bar you could move is not a bar.
- **A probe runs a pushed commit.** Uncommitted work is invisible to it.
- **~22g of memory, one training job at a time.** The host ceiling is 22528m and
  `qf probe` refuses a larger `--mem` outright, before starting anything. An exit
  code of 137 with an empty log is the kernel killing the container: that is
  memory, not a bug in your change. Series B is ~71% more rows than Series A
  (8.6M vs 5.0M), so a config that fits in Series A may not fit in Series B --
  and 20g is already close. For a hazard config the lever is the last FINITE bin
  edge; for a quantile config there is no lever, so report it.
- **Every prediction row is scored.** You cannot drop the rows you do badly on;
  a prediction set that does not cover the holdout slice is rejected whole.

## The discrete-hazard configs

`model_type: discrete_hazard` (Bet 2) trains one booster per wait bin instead of
one model per quantile, and it is scored through the same contract: p50 and p90
come out of the survival curve. **It needs Series B**: `validation_days: 7` puts
its train_start 6 days earlier than Series A's window reaches. It is a TAIL specialist -- it roughly halves the
30m+ p90 miss and gives up overall within-2x -- so read those two metrics
together rather than the verdict alone.

Its one operational difference: a p90 the survival curve has not reached by the
last FINITE bin edge is placed by an exponential tail, and if that tail's rate is
degenerate -- no observed starts in the terminal bin's risk set, so the MLE has
no rate -- the p90 is undefined. The contract's columns are non-null, so the run
refuses right after training rather than after the prediction pass.

**The terminal edge must stay `.inf`.** `hazard_labels.bin_edges_seconds` refuses
a finite one: the terminal bin is open by construction and the model gives it a
finite SHAPE through the tail rate, not a finite edge. What to change is the last
finite boundary -- move it earlier (480 -> 240, say) so the terminal risk set
contains observed starts.

## What a good experiment looks like

State the hypothesis before you run it, in the commit message: what you changed,
what you expect to move, and roughly how much. Then one variable, so the number
attributes to something. A run that changes three things and improves is a run
that has to be repeated three more times before anyone knows why.

THIS IS NOT A STYLE PREFERENCE. `wait_time_residual_velocity` was run over
fifteen cohorts, scored worse, and was dropped -- and it differed from the
candidate in six keys and about nineteen features at once, including the one
change (Policy B) that had fixed the model's regime fragility. Fifteen cohorts of
compute produced a number nobody can attribute to anything. Diff your config
against the one you are comparing to, and if the diff is more than one idea,
split it.

The config name is recorded on every run, so `results.sh` can tell your rows
apart. If you add a config, give it a name whose ENDING says what is different --
the table elides long names from the left, and two configs that differ only in a
prefix print as the same experiment.
