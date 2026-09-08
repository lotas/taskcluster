## This tick is a RETRY, and the action is not open

Your last entry was not recorded. This tick is **action 1 on one named target**
and the other five actions are unavailable: do not submit an experiment, do not
pick a different row, do not write up anything else.

**Rewrite by shrinking, not by rewriting.** On 2026-09-04 a retry fixed the
defect it was told about and introduced a new one -- the round-2 entry added a
superlative that was false. Every hedge, aside and piece of context is another
sentence that can be wrong, and the finding is lost when any of them is. So the
entry is the pre-registered claim and the evidence for it, in exactly this
shape.

**This template REPLACES the one under "Your output" above**, the same way the
six actions are replaced by the one named here. Where the two disagree, this one
wins; where this one is silent, that one still applies.

**Every field below is sourced from the block pasted after this one.** It
carries the target's row under `target`, the pre-registered comparator (`vs`)
row under `comparator` -- or `null` there, which the next paragraph governs --
and the series' `holdout` window above both. You have not been given
`frontier.json` -- the copilot has -- so that block is your only source for
either row's figures, and it is at full precision. Do not take a figure from
your previous entry or from the rejection feedback: a number you remember is a
number you invented, and that is the rejection this retry exists to stop.

**Two cases, and the paste decides which -- not you.** If `comparator` is an
object, the entry states both operands and the signed delta. If `comparator` is
`null`, the block carries a `comparator_note` in its place and **there is no
delta**: you write the target's own value at full precision and the note's
reason there is nothing to compare it against. You cannot reach that case by
declaring the comparator missing -- `comparator` is either an object or `null`
in the block below, and that is the entire test.

**A null `comparator` is not a gap for you to fill.** Do not go looking for a
second operand: not with a command, not from `frontier.md`, not from memory. The
note itself ends by telling you to "obtain any second operand with a command and
paste it under `Evidence:`" -- **that half of the note is superseded by this
template, and following it is how a retry ends up asserting something stronger
than anything that was pre-registered.** Each reason the note can give is a
reason no delta was ever pre-registered:

- "this row pre-registered no `vs`, so there is no comparator row" -- a
  reference run, or a row whose pre-registration carried no `vs` at all. `--vs`
  and `--reference-run` are mutually exclusive, so a reference row has no
  comparator **by construction**: no command can produce one, and a delta
  requirement can never be satisfied for it.
- "`vs` is <id>, which resolves to 0 rows in this series" -- a `vs` was
  pre-registered but is not among this series' rows, which includes its being in
  **another** series. Fetching it would build a cross-series comparison, and the
  copilot rejects those outright.
- the same line with a count above 1 -- the id matches more than one row, so any
  figure fetched under it could be the wrong contract's.

So in the null case the entry makes no comparison at all, and that is a complete
entry rather than a weakened one: the target's value at full precision plus the
named reason is checkable by the copilot against the rows it already holds, and
there is nothing in it left to reject.

```markdown
# Retry: <config>@<config_digest>, <target run id>

**Target run:** <the target named above>

**Action taken:** 1 -- retry of <`target.escalation_latest`, or the escalation
named in the feedback block if that field is empty>

**Claim:** <`target.claim`, `target.bar` and `target.direction` verbatim, and
the figures from `target.metrics` and `target.passed` that they rest on. If
`comparator` is an object: signed deltas against `comparator.metrics` at the
full precision both are pasted with. If `comparator` is `null`: no delta at all
-- `target.metrics[target.bar]` at full precision, and `comparator_note`'s
reason there is nothing to compare it against. No config other than the
pre-registered `vs`, no metric other than `target.bar`.>

**Confidence and what would change it:** <one sentence: `target.tol`, and the
block's `holdout` window. Both are pasted whether or not `comparator` is null.>

**Not concluded:** <one sentence: what this cohort does not establish.>

**Evidence:** <pasted command output for any figure that is NOT in the block
below. Nothing else: a figure that IS in the block needs no command, because the
copilot has the same rows -- and a null `comparator` adds no figure to fetch, so
write `none` when the block covers everything you cite.>
```

Four bans, each of which has already cost a finding:

1. **No rounding-equivalence.** "Unchanged to four decimals" is a figure claim,
   and it was false: the values were 0.0817 and 0.0818. State **both operands
   and the signed delta** -- `target.metrics[bar]`, the same metric from
   `comparator.metrics`, and the difference -- so the arithmetic can be checked
   before you submit it and again by the copilot. That is the two-operand case,
   and the paste puts you in it whenever `comparator` is an object. When
   `comparator` is null there is no second operand and no delta to state, and
   you must not reach for a rounding word instead -- not "unchanged", not
   "flat", not "comparable". That would be the 2026-09-04 defect with the
   operands taken away: a figure claim with nothing behind it at all.
2. **No superlatives and no firsts.** Not "the first", not "the best", not "the
   only" -- unconditionally, whatever you can source. "The first quantile config
   to clear the tail bar" was rejected because `val7_nop90` had already reached
   0.286152780, and a shrunken entry does not need the claim to stand up.
3. **The title is exactly the line the template gives** -- the action, the
   config, its digest and the target run id -- and nothing more. It never says
   whether a metric got better or worse: a -1.43pp MAE move is not an
   "improvement".
4. **No comparison to any config except the pre-registered `vs` -- and no
   substitute when there is none.** This rests on the same 2026-09-04
   rejection: the comparator that false "first" implied was `val7_nop90`, which
   was not that row's pre-registered `vs`. The `vs` is the one comparison the
   row pre-registered, so it is the one a reader can check. **When `comparator`
   is null this ban permits exactly one thing: no comparison at all.** It does
   not license one you chose yourself -- not the series reference, not its best
   row, not the config you judge closest. A row with no `vs` has nothing to be
   judged against by construction, and silence is the only thing you can say
   about it that is not stronger than its pre-registration.

`target` carries `config_digest`, `vs`, `tol`, `claim`, `bar`, `direction`,
`escalation_latest`, `metrics` and `passed`; `comparator` carries the same
fields for the pre-registered reference, or is `null` with `comparator_note` in
its place; and `holdout` sits above both. So every field above has a citable
source. A null `comparator` costs you none of them: `tol`, `escalation_latest`
and the target's own metrics are on the target row, `holdout` sits above it, and
the only thing absent is the delta -- which is the one thing the null case does
not ask you for. There is no independent-cohort figure in the block and you must
not supply one: that count exists only for a config whose every bar passed,
which a rejected run's did not. Use the `holdout` window instead.
