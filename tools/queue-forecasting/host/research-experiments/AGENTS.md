# The research loop, for whoever is running it

THIS FILE BELONGS AT THE ROOT OF `qf-research`. It lives in the trusted repo
because the operator owns it: `first-probe.sh` copies it in beside
`research/experiments/run_cohort.py`. Editing it here is editing the instructions
every future experiment starts from; editing the copy is editing a scratch file
that the next bootstrap overwrites.

## What you are doing

Predicting how long a Taskcluster task will wait before it starts. There is a
production candidate already, and it is not good enough: it improves mean
absolute error by ~7% over the percentile baseline where the bar is 15%. Your job
is to find the change that closes that gap, one variable at a time.

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

The two places the current model is weakest, from fourteen walk-forward cohorts:

- **5-30 minute waits.** Short waits are easy (the queue is empty) and long ones
  are structural (the pool is saturated). The middle is where the information is
  missing, and it is the bucket that has resisted every feature so far.
- **The 30m+ tail.** Guarded p90 misses ~25% of the time against a 30% bar, so
  it passes -- but a group ETA is a max over tasks, and a max is all tail.

## The canonical inputs

Three hashes. **Copy them verbatim into every probe and every evaluation. Do not
substitute your own, and do not take "the newest" from a listing.**

```
EXTRACT   bd29b39ab6254a3cf5de6a7413c1476a6caa178a0685f88aaa7d489c9a2db91f
BASELINE  e51a321057ca884977edc357c3c2c254dcefb01ed700f9009f5d92b412ec9a27
CONTRACT  f740716d32b8ddef20bd2e42ede873fd0b59486f752c8d077293ebc440997173
```

A hash makes ONE run reproducible. Only the same three hashes across runs make
two results comparable -- and comparing results is the entire job, so a run
against a different extract is not a better experiment, it is a number that
belongs to no series. If a listing shows something newer, that is not permission:
changing the trio is an operator decision that starts a new series, and the old
numbers do not carry across it.

**The reference point below is NOT yet in this series, and that matters.** It was
produced on the gen-1 extract of the same window (`cd467b4bd869...`), which is a
different snapshot of the same days and therefore a different hash. Treat it as
the magnitude to expect, not as the number to beat, until the promoted config has
been re-run against the canonical extract above -- that run is the in-series
reference, and it is the operator's to make.

From the promoted config `wait_time_residual_throughput_filtered_baseline.yaml`:

```
FAIL  mae            measured=0.067  bar=relative_improvement:0.15
FAIL  within_2x      measured=0.044  bar=absolute_improvement:0.05
PASS  p90_coverage   measured=0.887  bar=band:0.85..0.95
PASS  p90_miss_tail  measured=0.247  bar=absolute:0.3       [30m+]
verdict: no-go
```

Beat the in-series reference, with the same three hashes, and the improvement is
real. Beat only the number above and you may have beaten a snapshot.

## The loop

One experiment is one commit.

```sh
# 1. change ONE thing: a feature in trainer/src/, or a config in trainer/configs/
#    If you add a config, point CONFIG in research/experiments/run_cohort.py at it.

# 2. commit and push -- a probe runs a COMMIT, never a working tree
git add -A && git commit -m "what changed and why you expect it to help" && git push

# 3. the canonical trio, verbatim from the block above -- not from a listing
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
compared against the bar) and `bar` (the rule). `measured=0.067
bar=relative_improvement:0.15` means you got 6.7% where 15% was needed.

`days_passed=N/M` is the consistency rule: a result has to hold on M of the
holdout days, not just in aggregate. A change that wins overall and loses on two
days is a change that has found a day, not a signal.

## Things that will refuse you, and why

- **No database.** A probe has no `DATABASE_URL` and no network. Training reads
  the frozen extract at `/extract`. If you find yourself wanting a query, the
  column you want has to be added to the extract by a human first.
- **A config the extract cannot serve is refused in the first second**, not
  twenty minutes in: a target mismatch, and a config enabling
  `queue_context_features` against an extract with no `task_created`.
- **The extract is immutable.** You cannot widen its window or add a column. A
  column that is not in it has to be added by a human, to trusted code, and
  re-extracted -- which produces a new hash and therefore a new series.
- **You cannot change the contract or the baseline.** Both are named by hash from
  the trusted checkout. A bar you could move is not a bar.
- **A probe runs a pushed commit.** Uncommitted work is invisible to it.
- **~22g of memory, one training job at a time.** An exit code of 137 with an
  empty log is the kernel killing the container: that is memory, not a bug in
  your change.
- **Every prediction row is scored.** You cannot drop the rows you do badly on;
  a prediction set that does not cover the holdout slice is rejected whole.

## The discrete-hazard configs

`model_type: discrete_hazard` (Bet 2) trains one booster per wait bin instead of
one model per quantile, and it is scored through the same contract: p50 and p90
come out of the survival curve. It is a TAIL specialist -- it roughly halves the
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
