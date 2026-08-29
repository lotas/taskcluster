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
    """Returns the per-row day array for the predictions, or raises.

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
    for key, day, keep in zip(ex_keys.tolist(), days.tolist(),
                              in_slice.tolist()):
        # An extract cannot contain a duplicate (task_id, run_id) -- it is the
        # table's key -- but if it did, silently keeping the last would make the
        # completeness count below wrong in a way nothing else would show.
        if key in lookup:
            raise RowSetError(
                f"the extract itself has {key} twice; refusing rather than"
                f" scoring against an ambiguous row set")
        lookup[key] = (day, keep)

    seen = {}
    pred_days = []
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
            continue
        pred_days.append(entry[0])
    if unknown:
        raise RowSetError(
            f"{len(unknown)} predicted row(s) are not in the frozen extract,"
            f" e.g. {unknown[:MAX_MISSING_LISTED]}. A prediction for a row the"
            f" extract does not contain has no y_true to be scored against.")

    # PART 3. Every primary-slice extract row on a claimed day must be predicted.
    claimed = {d for d in pred_days if d}
    missing = [key for key, (day, keep) in lookup.items()
               if keep and day in claimed and key not in seen]
    if missing:
        raise RowSetError(
            f"the prediction set claims {len(claimed)} holdout day(s) but omits"
            f" {len(missing)} primary-slice row(s) inside them, e.g."
            f" {sorted(missing)[:MAX_MISSING_LISTED]}. Predicting a subset of a"
            f" day scores the rows the model chose rather than the day.")
    return np.asarray(pred_days, dtype=str)
