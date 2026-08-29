"""The prediction set's row identity. Phase 2c Task 21, and NC11.

WHAT NC11 SAYS AND WHAT IT HAS TO MEAN. The design's negative control 11 is
"submit a prediction set whose `row_id` multiset does not match the frozen
extract". Read literally -- equality with the whole extract -- it is wrong: an
extract covers the training window as well as the holdout, and a probe predicts
only for the holdout. So the checkable property is three-part, and the third part
is the one that matters:

  1. **Well-formed.** `row_id` is `f"{task_id}:{run_id}"` (2b-2's contract), so it
     is DERIVABLE and therefore checkable rather than declarable. A prediction
     set whose row_id disagrees with its own task_id/run_id is refused: those are
     the columns a join uses, and a row_id nobody checks is a label.
  2. **A subset, with no duplicates.** Every predicted row exists in the extract,
     and no row is predicted twice. A duplicate silently double-weights a row in
     every metric.
  3. **COMPLETE WITHIN EACH DAY IT CLAIMS.** For every holdout day the prediction
     set covers, it must predict EVERY primary-slice row the extract has on that
     day.

Part 3 exists because the first two leave a gaming vector wide open: a probe
could predict only the rows it does well on, inside days it chose, and score
beautifully on a prediction set that is a subset of a subset. Nothing in parts 1
or 2 notices. The holdout DAYS are derived from the predictions (the evaluator
cannot know them otherwise -- they live in the trainer's config, not the
extract), but coverage WITHIN a day is fully determined by the extract, so
completeness is checkable exactly where cherry-picking would happen.
"""
from __future__ import annotations

import numpy as np

MAX_MISSING_LISTED = 5


class RowSetError(ValueError):
    """A prediction set that must not be scored. The message names the cause."""


def row_ids(task_id, run_id):
    """The 2b-2 contract's derivation, in one place."""
    return np.char.add(np.char.add(np.asarray(task_id, dtype=str), ":"),
                       np.asarray(run_id).astype(str))


def check(*, pred_task_id, pred_run_id, pred_row_id,
          extract_task_id, extract_run_id, extract_days, extract_in_slice):
    """Returns the extract POSITION of each predicted row, or raises.

    Positions rather than days, so the join is a consequence of the check rather
    than a second lookup: the caller reads `extract_days[idx]`, `y_true[idx]` and
    anything else it needs off the same index, and there is no way to check one
    alignment and score another.

    `extract_in_slice` is the primary-slice mask over the extract rows, so
    completeness is measured against the population the contract judges -- not
    against every row in the window.
    """
    expected = row_ids(pred_task_id, pred_run_id)
    given = np.asarray(pred_row_id, dtype=str)
    if given.shape != expected.shape:
        raise RowSetError(
            f"the prediction set has {given.shape[0]} row_id values for"
            f" {expected.shape[0]} rows")
    bad = given != expected
    if bad.any():
        i = int(np.argmax(bad))
        raise RowSetError(
            f"{int(bad.sum())} row_id value(s) disagree with their own"
            f" task_id/run_id, first at row {i}: {given[i]!r} should be"
            f" {expected[i]!r}. row_id is derived, so a value that differs is"
            f" either a different join key or a label nobody checked.")

    # A dict from the extract's keys to (day, in_slice). Built once; the
    # predictions are looked up in it, so this is O(n) rather than a join.
    ex_keys = row_ids(extract_task_id, extract_run_id)
    days = np.asarray(extract_days, dtype=str)
    in_slice = np.asarray(extract_in_slice, dtype=bool)
    lookup = {}
    for position, (key, day, keep) in enumerate(
            zip(ex_keys.tolist(), days.tolist(), in_slice.tolist())):
        # An extract cannot contain a duplicate (task_id, run_id) -- it is the
        # table's key -- but if it did, silently keeping the last would make the
        # completeness count below wrong in a way nothing else would show.
        if key in lookup:
            raise RowSetError(
                f"the extract itself has {key} twice; refusing rather than"
                f" scoring against an ambiguous row set")
        lookup[key] = (day, keep, position)

    seen = {}
    pred_days = []
    index = []
    unknown = []
    for key in given.tolist():
        if key in seen:
            raise RowSetError(
                f"the prediction set has {key} more than once. A duplicate"
                f" double-weights that row in every metric.")
        seen[key] = True
        entry = lookup.get(key)
        if entry is None:
            unknown.append(key)
            pred_days.append("")
            index.append(0)       # a placeholder; the refusal below is certain
            continue
        pred_days.append(entry[0])
        index.append(entry[2])
    if unknown:
        raise RowSetError(
            f"{len(unknown)} predicted row(s) are not in the frozen extract,"
            f" e.g. {unknown[:MAX_MISSING_LISTED]}. A prediction for a row the"
            f" extract does not contain has no y_true to be scored against.")

    # PART 3. Every primary-slice extract row on a claimed day must be predicted.
    claimed = {d for d in pred_days if d}
    missing = [key for key, (day, keep, _pos) in lookup.items()
               if keep and day in claimed and key not in seen]
    if missing:
        raise RowSetError(
            f"the prediction set claims {len(claimed)} holdout day(s) but omits"
            f" {len(missing)} primary-slice row(s) inside them, e.g."
            f" {sorted(missing)[:MAX_MISSING_LISTED]}. Predicting a subset of a"
            f" day scores the rows the model chose rather than the day.")
    return np.asarray(index, dtype=np.int64)


def check_day_block(claimed, available, *, required):
    """The DAY-set property, the other half of the cherry-picking vector.

    `check` closes "predict only the easy rows inside a day". This closes
    "choose the easy days", and it closes it by REQUIRING AN EXACT SET rather
    than a shape.

    WHAT THE FIRST VERSION DID, AND WHY IT WAS NOT ENOUGH. It enforced only that
    the claimed days were a CONTIGUOUS block of the days the extract had, and
    merely RECORDED whether that block was the most recent one -- reasoning that
    the trainer might legitimately drop a partial final day, so refusing an
    earlier block could fail valid runs. The reasoning was wrong on the facts:
    `config.compute_windows` sets the holdout to
    `[as_of_date - holdout_days, as_of_date)`, `holdout_day_starts` walks it one
    calendar day at a time, and `load_config` REFUSES an `as_of_date` that is not
    UTC midnight -- as does `extract_spec._parse_boundary`. No partial day can
    arise. So the hedge guarded a case that cannot happen while leaving the real
    vector open: a candidate could hold out an easier earlier block, be scored on
    it, and the verdict would record `is_tail: false` where nothing gated on it.
    A finding that does not gate is not a control.

    So `required` is DERIVED BY THE CALLER from the extract's own `as_of_date`
    and the contract's `holdout_days`, and anything else is refused.

    `available` -- the whole extract's in-slice days -- is kept for the
    DIAGNOSIS. A required day the extract has no in-slice rows on is not the
    candidate's doing, and that refusal must not read like the other one. It is
    still a parameter rather than derived here, for the reason the tests found
    earlier: the caller reduces the extract to the claimed days before this runs,
    so anything derived from what it holds would be compared against itself.
    """
    available = sorted(set(str(d) for d in available))
    claimed = sorted(set(str(d) for d in claimed))
    required = [str(d) for d in required]
    if not claimed:
        raise RowSetError(
            "the prediction set claims no holdout day, so there is nothing to"
            " judge. An empty day set scores zero rows rather than failing.")
    if not required:
        raise RowSetError(
            "no holdout days were derived, so there is no rule to hold the"
            " prediction set to. Refusing rather than accepting whatever it"
            " claims.")
    if claimed == sorted(required):
        return {
            "claimed": claimed,
            "required": sorted(required),
            "available_days": len(available),
        }

    # A REQUIRED DAY THE EXTRACT CANNOT SUPPLY, named separately: it is not the
    # candidate's doing, and sending somebody to look at the prediction set
    # would waste their time.
    unavailable = [d for d in required if d not in available]
    if unavailable:
        raise RowSetError(
            f"the contract's holdout is {sorted(required)} and the frozen"
            f" extract has no primary-slice rows on {unavailable}. That is a gap"
            f" in the EXTRACT rather than in the prediction set: those days fall"
            f" inside the window that was requested, so either the collector was"
            f" down or the contract's slice is empty there.")
    missing = [d for d in required if d not in claimed]
    extra = [d for d in claimed if d not in required]
    raise RowSetError(
        f"the prediction set covers {claimed}, and the contract's holdout is"
        f" {sorted(required)} -- derived from the extract's as_of_date and the"
        f" contract's holdout_days, which is exactly the trainer's own window."
        + (f" Missing: {missing}." if missing else "")
        + (f" Not in the holdout: {extra}." if extra else "")
        + " The days are not the candidate's to choose: holding out an easier"
        " earlier block and being scored on it is the same vector as predicting"
        " only the easy rows inside a day.")
