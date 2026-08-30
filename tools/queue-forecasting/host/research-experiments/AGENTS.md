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
qf status <evaluate run id> --json     # pins.scoreboard, as JSON
qf logs <probe run id>                 # the trainer's own output
qf logs <probe run id> --stream stderr
qf list --limit 50 --json              # every run; pair with `qf status` for history
```

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

## What a good experiment looks like

State the hypothesis before you run it, in the commit message: what you changed,
what you expect to move, and roughly how much. Then one variable, so the number
attributes to something. A run that changes three things and improves is a run
that has to be repeated three more times before anyone knows why.
