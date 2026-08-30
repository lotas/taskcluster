"""The evaluation itself. Phase 2c Task 23, completing 2c-2.

WHAT THIS DOES: reads one untrusted prediction set and two immutable stores,
verifies every identity by recomputing it, joins the predictions to the frozen
extract's own `y_true`, applies `rows`' NC11 property, computes `metrics`'
counts, hands them to `verdict`, and publishes `eval.parquet` and `verdict.json`
atomically. It emits a verdict and never an action (D27).

EVERY PATH IS DERIVED, NEVER RECEIVED. The request carries identifiers; the roots
come from this service's own config. `qfd` is in the `docker` group, which is
root-equivalent (D5), so the narrowest domain in the system does not take
instructions from the widest one about where to read.

EVERY IDENTITY IS RECOMPUTED, NEVER TRUSTED:

  * the contract is re-loaded and re-hashed, and its digest must be the one the
    request named (`contract.load` refuses a file whose declared hash disagrees
    with its body);
  * the extract directory's name must be what `extract_spec.request_hash` makes
    of the request in its own manifest;
  * `runs.parquet`'s bytes must digest to what that manifest says;
  * the baseline directory's name must be what `baseline.baseline_hash` makes of
    its own manifest, and the NDJSON's bytes must digest to what that manifest
    says;
  * the staged prediction file's bytes must digest to what the request declared.

A content key that is read rather than recomputed records nothing.

MEMORY. Two different mechanisms, and the distinction matters because the first
version's docstring stated the first and read as though it covered both.

THE TWO LARGE INPUTS ARE STREAMED, so memory is independent of the extract's
window. `MemoryMax=4G` on the unit, while `runs.parquet` covers months and the
baseline NDJSON covers the same span -- neither is ever materialised:

  Pass 1  stream `runs.parquet` row group by row group, keeping only rows whose
          `(task_id, run_id)` is predicted. That yields the claimed holdout days.
  Pass 2  stream it again, keeping every row whose day is claimed. That is the
          population `rows.check` needs to prove completeness, and it is the same
          size class as the prediction set rather than the size of the window.

Two passes over a local file, deliberately, because the alternative is a
row-count-shaped memory profile in the one component that must not fall over.
Pass 2's row set is a strict superset of pass 1's, so nothing is proved against a
smaller population than the one the property is about.

THE PREDICTION SET IS READ WHOLE, and what bounds it is `MAX_PREDICTION_ROWS`
alone -- checked from Parquet metadata before any allocation. That ceiling is
therefore load-bearing rather than a sanity check, and it is MEASURED: see the
arithmetic beside the constant. The first version set it at 20_000_000, roughly
10.7 GB on a 4 GiB unit, so the streaming above was true and the sentence a
reader took from it was not.

THE BASELINE AND THE MODEL ARE SCORED OVER ONE POPULATION, BY CONSTRUCTION. A
relative bar ("MAE improves by >=15%") compares two ratios, and two ratios over
different row sets are not comparable -- so the scored set is the rows where
`y_true` and the baseline's own p50 are both finite, and BOTH sides are computed
over exactly that. `baseline_missing_n` records what that dropped. The
alternative -- letting each side use whatever rows it happened to have -- is the
kind of comparison that looks like a result and is not one.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sys

import numpy as np
import pyarrow
import pyarrow.compute
import pyarrow.parquet

import metrics as metrics_mod
import rows as rows_mod
import verdict as verdict_mod

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "shared"))
import baseline as baseline_mod                                # noqa: E402
import contract as contract_mod                                # noqa: E402
import extract_manifest as extract_manifest_mod                # noqa: E402
import extract_spec as extract_spec_mod                        # noqa: E402

SCHEMA = 1

PREDICTIONS_NAME = "predictions.parquet"
EVAL_NAME = "eval.parquet"
VERDICT_NAME = "verdict.json"
MANIFEST_NAME = "MANIFEST.json"
RUNS_DATASET = "runs"

# The frozen prediction contract (design §4.6), recorded in `qfd` as
# `Runner.PREDICTION_COLUMNS` and ENFORCED here -- `qfd` is stdlib-only (D6) and
# cannot read Parquet, so it collects the file and this validates it. A contract
# declared in one place and enforced in none is a comment.
PREDICTION_COLUMNS = ("task_id", "run_id", "row_id", "p50", "p90_raw")

# `bl_*` as `predictor.js --export-baseline-predictions` writes them and
# `data_loader.load_baseline_predictions` reads them. Keyed by the contract's
# target, because a wait contract judged against duration baselines would
# produce numbers rather than an error.
BASELINE_COLUMNS = {
    "wait_time": ("bl_wait_p50", "bl_wait_p90"),
    "run_duration": ("bl_duration_p50", "bl_duration_p90"),
}

# Ceilings, checked from Parquet METADATA before any read, so a hostile or
# mistaken file is refused rather than allocated.
#
# MEASURED, NOT ASSERTED, and the first value was neither. `read_predictions`
# holds the Arrow table plus Python `str` objects and numpy `<U` arrays for two
# string columns, which measures at **536 MB per 1_000_000 rows** with 22-char
# task ids (`resource.ru_maxrss` around a real call). The first ceiling was
# 20_000_000 -- about 10.7 GB, on a unit with `MemoryMax=4G`, reached BEFORE
# validation finishes. A ceiling above the memory limit is not a ceiling.
#
# The arithmetic, stated so a future bump has to redo it rather than guess:
#   2_000_000 rows x 536 MB/1M  ~=  1.07 GB   of a 4 GiB budget
#   leaving ~3 GB for the extract scan (streamed) and the baseline map.
# A recorded 5-day holdout is ~162_000 eligible rows, so this is ~12x the real
# cohort. A legitimate cohort that outgrows it gets a refusal naming the budget,
# which is the right way to find out.
MAX_PREDICTION_MB_PER_1M_ROWS = 536      # measured; see the test that pins it
MAX_PREDICTION_BUDGET_MB = 1_100
MAX_PREDICTION_ROWS = 2_000_000

# The baseline is STREAMED line by line and only the predicted rows are kept, so
# its ceiling bounds the work rather than the memory.
MAX_BASELINE_ROWS = 100_000_000

# Row groups are written at 10_000 rows (`parquet_writer.ROW_GROUP_ROWS`); this
# is the read batch, sized so the per-batch Python work amortises without the
# batch itself becoming the memory profile.
SCAN_BATCH_ROWS = 65_536


class EvaluateError(ValueError):
    """This evaluation must not produce a verdict. The message names the cause.

    A `ValueError`, so `service.SAFE_ERRORS` relays the text to `qfd` verbatim:
    every message here is written to be read by whoever has to fix the job.

    It carries an `error_class` because the DISPATCHER records one, and a
    refusal that flattens every cause into `refused` makes a negative control's
    own signal invisible -- the same reason `contract_not_trusted` is carried
    through rather than folded in. `qfd` constrains the value it receives
    (`_ERROR_CLASS_RE`), so these are tokens rather than prose.
    """

    def __init__(self, message, *, error_class="evaluate_refused"):
        super().__init__(message)
        self.error_class = error_class


def _err(msg, *, error_class="evaluate_refused"):
    raise EvaluateError(msg, error_class=error_class)


def file_digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_json(path, what):
    try:
        with open(path) as fh:
            return json.load(fh)
    except OSError as e:
        _err(f"cannot read the {what}: {e}")
    except ValueError as e:
        _err(f"the {what} at {path} is not JSON: {e}")


# --- the contract ---------------------------------------------------------
def load_contract(cfg, req, contract_name):
    """The rule, re-loaded and re-hashed. Refuses on any disagreement."""
    path = os.path.join(cfg.contracts_dir, contract_name)
    try:
        contract, digest = contract_mod.load(path)
    except contract_mod.ContractError as e:
        _err(f"the contract {contract_name} no longer validates: {e}")
    if digest != req["contract"]:
        # The resolution happened in `available_contracts`, so this is the file
        # changing between the listing and the read. Refused rather than
        # tolerated: the alternative is judging by a rule nobody named.
        _err(f"{contract_name} now hashes to {digest[:12]}, not the"
             f" {req['contract'][:12]} this request named: the contract file"
             f" changed between being resolved and being read")
    if contract["target"] not in BASELINE_COLUMNS:
        _err(f"the contract judges target {contract['target']!r}, which this"
             f" evaluator has no baseline columns for. Known:"
             f" {sorted(BASELINE_COLUMNS)}")
    declared = req.get("baseline_hash")
    if declared is None:
        # `request.py` leaves this to here, where the contract is in hand: a
        # contract ALWAYS names a baseline (`contract._REQUIRED`), so a request
        # with none is judging a relative bar against nothing.
        _err(f"the contract {contract['name']} is stated against baseline"
             f" {contract['baseline_hash'][:12]}, and this request carries no"
             f" baseline_hash. The judged run recorded no baseline, so its"
             f" predictions were not made in the arrangement this contract"
             f" describes.")
    if declared != contract["baseline_hash"]:
        _err(f"the contract {contract['name']} is stated against baseline"
             f" {contract['baseline_hash'][:12]} but the judged run used"
             f" {declared[:12]}. A bar relative to one baseline, measured"
             f" against another, is not the bar that was agreed.")
    return contract


# --- the extract ----------------------------------------------------------
def open_extract(cfg, req, contract):
    """`(directory, manifest, runs path)` for the frozen extract, verified."""
    directory = os.path.join(cfg.extracts_dir, req["request_hash"])
    manifest_path = os.path.join(directory, MANIFEST_NAME)
    if not os.path.isfile(manifest_path):
        _err(f"no extract {req['request_hash'][:12]} in the store: the judged"
             f" run pins an extract this host has not published. An extract is"
             f" immutable and reused by request_hash (D20), so this is a"
             f" different host or a pruned store, not a stale name.")
    manifest = _read_json(manifest_path, "extract manifest")
    if not isinstance(manifest, dict):
        _err("the extract manifest is not an object")

    request = manifest.get("request")
    if not isinstance(request, dict):
        _err("the extract manifest carries no request, so there is nothing to"
             " recompute its identity from")
    try:
        recomputed = extract_spec_mod.request_hash(request)
    except (TypeError, ValueError) as e:
        _err(f"the extract manifest's request cannot be canonicalised: {e}")
    if recomputed != req["request_hash"]:
        _err(f"the extract directory is named {req['request_hash'][:12]} but"
             f" the request in its manifest hashes to {recomputed[:12]}. The"
             f" name is a content key; one that does not match its content"
             f" records nothing.")
    if request.get("target") != contract["target"]:
        # The extract carries BOTH duration columns, so this is not about a
        # missing column -- it is about the pin. `request_hash` covers the
        # target, so a wait contract judged against an extract requested for
        # `run_duration` means the judged run and the contract disagree about
        # what was being modelled, and the numbers would come out looking fine.
        _err(f"the contract judges {contract['target']!r} but the extract the"
             f" run pins was requested for {request.get('target')!r}. The"
             f" request hash covers the target, so these are two different"
             f" cohorts and comparing them would produce numbers rather than an"
             f" error.",
             error_class="extract_target_mismatch")

    # THE MANIFEST'S OWN CONTENT KEY. Recomputed, like every other identity
    # here. It was recorded into the verdict unverified, which a review caught --
    # and the test fixture had been computing it with default `json.dumps`
    # separators, so it was wrong and nothing noticed, because the only code that
    # could have noticed did not look.
    try:
        extract_manifest_mod.verify(manifest)
    except extract_manifest_mod.ExtractManifestError as e:
        _err(str(e), error_class="extract_manifest_invalid")

    files = manifest.get("files")
    if not isinstance(files, dict) or RUNS_DATASET not in files:
        _err(f"the extract manifest names no {RUNS_DATASET!r} dataset, so it"
             f" carries no per-run rows to score against")
    entry = files[RUNS_DATASET]
    name = entry.get("file")
    if not isinstance(name, str) or "/" in name or name in ("", ".", ".."):
        _err(f"the extract manifest's {RUNS_DATASET} file name {name!r} is not"
             f" a plain file name")
    runs_path = os.path.join(directory, name)
    if not os.path.isfile(runs_path):
        _err(f"the extract manifest names {name}, which is not in"
             f" {req['request_hash'][:12]}")
    digest = file_digest(runs_path)
    if digest != entry.get("sha256"):
        _err(f"{name} digests to {digest[:12]} but its manifest says"
             f" {str(entry.get('sha256'))[:12]}: the frozen extract is not the"
             f" one that was published, and nothing computed from it is"
             f" attributable to the data anybody agreed on")
    return directory, manifest, runs_path


def _target_column(contract):
    return extract_spec_mod.TARGET_COLUMNS[contract["target"]]


def required_days(as_of_date, holdout_days):
    """The holdout dates, DERIVED. `[as_of_date - holdout_days, as_of_date)`.

    THIS IS EXACTLY THE TRAINER'S OWN WINDOW, not an approximation of it:
    `config.compute_windows` sets `hold_start = as_of_date - holdout_days` and
    `hold_end = as_of_date`, and `holdout_day_starts` walks one calendar day at a
    time between them. `load_config` refuses an `as_of_date` that is not UTC
    midnight, and so does `extract_spec._parse_boundary`, so there are no partial
    days at either end.

    WHY THIS IS ENFORCED AND `is_tail` WAS NOT ENOUGH. The first version checked
    only that the claimed days were a CONTIGUOUS block of the days the extract
    had, and merely RECORDED whether that block was the most recent one -- on the
    reasoning that the trainer might legitimately drop a partial final day. It
    cannot: there is no mechanism in `compute_windows` that would produce one,
    and the UTC-midnight refusal is what rules it out. So the hedge protected
    against a case that does not exist while leaving day-level cherry-picking
    open: a candidate could hold out an easier earlier block and be scored on it.
    Derived and required, the vector closes.
    """
    day = datetime.datetime.strptime(as_of_date, "%Y-%m-%dT%H:%M:%SZ").date()
    return [(day - datetime.timedelta(days=n)).isoformat()
            for n in range(holdout_days, 0, -1)]


# --- the predictions ------------------------------------------------------
def _prediction_type_problem(field, arrow_type):
    """`None` if the column can carry the frozen type, else why not.

    NOT EXACT ARROW-TYPE EQUALITY, and the reason is worth stating. Design §4.6
    freezes `run_id` as `int32` and the two predictions as `double`; a candidate
    writing this file from pandas gets `int64` and `float64` by default, so
    exact equality would be a contract that the tool everybody uses cannot
    satisfy. What the frozen types are actually about is nullability, NaN and
    losing information -- all three of which are checked on the VALUES below.
    So the width is allowed to vary and the family is not: a string where a
    number belongs is a different file, not a different writer.
    """
    if field in ("task_id", "row_id"):
        if not (pyarrow.types.is_string(arrow_type)
                or pyarrow.types.is_large_string(arrow_type)):
            return f"must be a string column, got {arrow_type}"
        return None
    if field == "run_id":
        if not pyarrow.types.is_integer(arrow_type):
            return f"must be an integer column, got {arrow_type}"
        return None
    if not (pyarrow.types.is_floating(arrow_type)
            or pyarrow.types.is_integer(arrow_type)):
        return f"must be a numeric column, got {arrow_type}"
    return None


def read_predictions(path, *, declared_sha256):
    """The one untrusted input, validated against the frozen contract.

    The digest is checked FIRST, before the file is parsed: what is validated has
    to be what was described, and a Parquet reader is a lot of code to point at
    bytes nobody has vouched for.
    """
    digest = file_digest(path)
    if digest != declared_sha256:
        _err(f"the staged prediction set digests to {digest[:12]} but the"
             f" request declared {declared_sha256[:12]}. The dispatcher takes"
             f" that digest from the staged bytes, so a disagreement means the"
             f" file changed after it was staged.")
    try:
        pf = pyarrow.parquet.ParquetFile(path)
    except Exception as e:                                     # noqa: BLE001
        # pyarrow raises its own exception types for a malformed file; they are
        # not `ValueError`, and an unreadable candidate artifact is a refusal
        # rather than an internal fault.
        _err(f"the prediction set is not readable Parquet: {e}")
    if pf.metadata.num_rows > MAX_PREDICTION_ROWS:
        _err(f"the prediction set declares {pf.metadata.num_rows} rows, past"
             f" the {MAX_PREDICTION_ROWS} ceiling. Checked from the metadata,"
             f" so the refusal costs no allocation.")
    if pf.metadata.num_rows == 0:
        _err("the prediction set has no rows: an empty set joins to nothing,"
             " which scores zero rows rather than failing")

    names = list(pf.schema_arrow.names)
    missing = [c for c in PREDICTION_COLUMNS if c not in names]
    if missing:
        _err(f"the prediction set is missing column(s) {missing}. The frozen"
             f" contract is {list(PREDICTION_COLUMNS)}.")
    extra = [c for c in names if c not in PREDICTION_COLUMNS]
    if extra:
        # CLOSED-WORLD, like the artifact allowlist and the extract's file set.
        # An ignored column is how a column that matters comes to be ignored.
        _err(f"the prediction set carries column(s) {extra}, which the frozen"
             f" contract does not include. Refused rather than ignored: the"
             f" columns are {list(PREDICTION_COLUMNS)} and nothing else is read"
             f" from a candidate run for scoring.")
    if len(set(names)) != len(names):
        _err("the prediction set repeats a column name")
    for field in PREDICTION_COLUMNS:
        problem = _prediction_type_problem(
            field, pf.schema_arrow.field(field).type)
        if problem is not None:
            _err(f"the prediction set's {field} column {problem}")

    table = pf.read(columns=list(PREDICTION_COLUMNS))
    for field in PREDICTION_COLUMNS:
        nulls = table.column(field).null_count
        if nulls:
            _err(f"the prediction set has {nulls} null value(s) in {field}."
                 f" Every column of the frozen contract is non-null: a null"
                 f" join key drops a row silently and a null prediction scores"
                 f" as an absence rather than as a miss.")

    run_id = table.column("run_id").to_numpy(zero_copy_only=False)
    if run_id.size:
        lo, hi = int(run_id.min()), int(run_id.max())
        if lo < -(2 ** 31) or hi > 2 ** 31 - 1:
            _err(f"the prediction set's run_id range [{lo}, {hi}] does not fit"
                 f" the frozen int32: a value that does not round-trip is a"
                 f" different join key")
    out = {
        "task_id": np.asarray(table.column("task_id").to_pylist(), dtype=str),
        "run_id": run_id.astype(np.int64),
        "row_id": np.asarray(table.column("row_id").to_pylist(), dtype=str),
    }
    for field in ("p50", "p90_raw"):
        values = table.column(field).to_numpy(zero_copy_only=False) \
                      .astype(float)
        bad = ~np.isfinite(values)
        if bad.any():
            i = int(np.argmax(bad))
            _err(f"the prediction set has {int(bad.sum())} non-finite"
                 f" {field} value(s), first at row {i}: {values[i]!r}. A NaN is"
                 f" excluded from every metric, so a model that emitted them"
                 f" would be scored on the rows it was confident about.")
        out[field] = values
    return out


# --- the extract's rows ---------------------------------------------------
def _batch_keys(batch):
    task_id = np.asarray(batch.column("task_id").to_pylist(), dtype=str)
    run_id = batch.column("run_id").to_numpy(zero_copy_only=False)
    return rows_mod.row_ids(task_id, run_id)


def _batch_days(batch):
    return np.asarray(
        pyarrow.compute.strftime(batch.column("pending_at"), format="%Y-%m-%d",
                                 locale="C").to_pylist(), dtype=str)


def scan_runs(runs_path, *, target_column, slice_values, wanted_keys):
    """Stream `runs.parquet` twice; return the rows the property is about.

    Pass 1 finds the days the prediction set claims AND the whole window's
    in-slice days. Pass 2 returns EVERY row on the claimed days -- which is what
    completeness is measured against, and is the same size class as the
    prediction set rather than the size of the window.

    `available_days` COMES FROM PASS 1 BECAUSE PASS 2 CANNOT PROVIDE IT. The
    returned row set is reduced to the claimed days, so `check_day_block` given
    that set would be asking whether the claimed days are a contiguous block of
    themselves -- vacuously true, with `is_tail` always true too. The first
    version did exactly that, and the test that pinned `available_days` to the
    whole window is what said so. Collected here, where the file is already
    being streamed and it costs nothing.

    `pending_at` is `timestamp('us', tz='UTC')` in the extract's schema, so the
    day is a UTC calendar day, matching the contract's `anchor: pending_at` and
    the `--pending-eval-date` the baseline runs used.
    """
    needed = ["task_id", "run_id", "pending_at", "reason_resolved",
              target_column]
    pf = pyarrow.parquet.ParquetFile(runs_path)
    available = set(pf.schema_arrow.names)
    absent = [c for c in needed if c not in available]
    if absent:
        _err(f"the frozen extract's runs dataset has no column(s) {absent},"
             f" so it cannot be scored for this contract's target")

    wanted = set(wanted_keys.tolist())
    slice_array = np.asarray(list(slice_values), dtype=str)
    claimed = set()
    available = set()
    for batch in pf.iter_batches(batch_size=SCAN_BATCH_ROWS, columns=needed):
        keys = _batch_keys(batch)
        days = _batch_days(batch)
        # A Python set membership per row. The alternative -- one vectorised
        # `isin` against a sorted key array -- is faster and needs the whole
        # extract's keys materialised to be worth it, which is the memory
        # profile this function exists to avoid.
        hit = np.array([k in wanted for k in keys.tolist()], dtype=bool)
        if hit.any():
            claimed.update(days[hit].tolist())
        reason = np.asarray(
            [("" if v is None else v)
             for v in batch.column("reason_resolved").to_pylist()], dtype=str)
        y_true = batch.column(target_column).to_numpy(zero_copy_only=False) \
                      .astype(float)
        in_slice = np.isin(reason, slice_array) & np.isfinite(y_true)
        if in_slice.any():
            available.update(days[in_slice].tolist())
    if not claimed:
        _err("none of the predicted rows is in the frozen extract. The"
             " predictions were made against different data, or against a"
             " different extract than the run pinned.")

    parts = {name: [] for name in ("row_id", "task_id", "run_id", "day",
                                   "y_true", "reason_resolved")}
    pf = pyarrow.parquet.ParquetFile(runs_path)
    for batch in pf.iter_batches(batch_size=SCAN_BATCH_ROWS, columns=needed):
        days = _batch_days(batch)
        sel = np.isin(days, np.asarray(sorted(claimed), dtype=str))
        if not sel.any():
            continue
        task_id = np.asarray(batch.column("task_id").to_pylist(), dtype=str)
        run_id = batch.column("run_id").to_numpy(zero_copy_only=False)
        reason = np.asarray(
            [("" if v is None else v)
             for v in batch.column("reason_resolved").to_pylist()], dtype=str)
        y_true = batch.column(target_column).to_numpy(zero_copy_only=False) \
                      .astype(float)
        parts["row_id"].append(rows_mod.row_ids(task_id, run_id)[sel])
        parts["task_id"].append(task_id[sel])
        parts["run_id"].append(run_id.astype(np.int64)[sel])
        parts["day"].append(days[sel])
        parts["y_true"].append(y_true[sel])
        parts["reason_resolved"].append(reason[sel])
    if not parts["row_id"]:
        # Unreachable: pass 1's matched rows all have days in `claimed`, so
        # pass 2 selects at least those. Stated as a refusal rather than left to
        # produce empty float arrays where strings belong, because an impossible
        # branch that returns a plausible value is how a wrong answer gets one.
        _err("the second pass over the frozen extract found no rows on the"
             " claimed holdout days, which the first pass had already matched")
    out = {name: np.concatenate(values) for name, values in parts.items()}
    out["available_days"] = sorted(available)
    return out


# --- the baseline ---------------------------------------------------------
def read_baseline(cfg, req, contract, *, wanted_keys):
    """The promoted baseline's own predictions for the rows being judged.

    STREAMED, for the same reason `runs.parquet` is: the NDJSON is the whole
    window's rows and the prediction set is one holdout's worth, so only the
    rows being judged are ever held.
    """
    baseline_hash = req["baseline_hash"]
    directory = os.path.join(cfg.baselines_dir, baseline_hash)
    manifest_path = os.path.join(directory, MANIFEST_NAME)
    if not os.path.isfile(manifest_path):
        _err(f"no baseline {baseline_hash[:12]} in the store, so the contract's"
             f" relative bars have nothing to be relative to."
             f" `promote-baseline.sh` publishes one.")
    manifest = _read_json(manifest_path, "baseline manifest")
    if not isinstance(manifest, dict):
        _err("the baseline manifest is not an object")
    try:
        # `baseline_hash` excludes its own field, so the manifest is passed
        # whole: stripping it here would be a second implementation of the
        # canonical form, which is what `shared/` exists to prevent.
        recomputed = baseline_mod.baseline_hash(manifest)
    except (TypeError, ValueError) as e:
        _err(f"the baseline manifest cannot be canonicalised: {e}")
    if recomputed != baseline_hash:
        _err(f"the baseline directory is named {baseline_hash[:12]} but its"
             f" manifest hashes to {recomputed[:12]}. A content key that does"
             f" not match its content records nothing, and this one is what the"
             f" contract's bars are stated against.")

    entry = (manifest.get("files") or {}).get(baseline_mod.NDJSON_NAME)
    if not isinstance(entry, dict):
        _err(f"the baseline manifest names no {baseline_mod.NDJSON_NAME}")
    rows_declared = manifest.get("ndjson_rows")
    if not isinstance(rows_declared, int) \
            or rows_declared > MAX_BASELINE_ROWS:
        _err(f"the baseline declares {rows_declared!r} NDJSON rows, which is"
             f" not a count under the {MAX_BASELINE_ROWS} ceiling")
    path = os.path.join(directory, baseline_mod.NDJSON_NAME)
    if not os.path.isfile(path):
        _err(f"the baseline manifest names {baseline_mod.NDJSON_NAME}, which is"
             f" not in {baseline_hash[:12]}")
    digest = file_digest(path)
    if digest != entry.get("sha256"):
        _err(f"{baseline_mod.NDJSON_NAME} digests to {digest[:12]} but its"
             f" manifest says {str(entry.get('sha256'))[:12]}: the promoted"
             f" baseline is not the one that was published")

    p50_column, p90_column = BASELINE_COLUMNS[contract["target"]]
    wanted = set(wanted_keys.tolist())
    found = {}
    seen_rows = 0
    with open(path) as fh:
        for number, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            seen_rows += 1
            try:
                record = json.loads(line)
            except ValueError as e:
                _err(f"{baseline_mod.NDJSON_NAME} line {number} is not JSON:"
                     f" {e}")
            key = f"{record.get('task_id')}:{record.get('run_id')}"
            if key not in wanted:
                continue
            if key in found:
                _err(f"{baseline_mod.NDJSON_NAME} has {key} more than once."
                     f" A duplicate baseline row would take whichever came last,"
                     f" and the bar is stated against one number per row.")
            found[key] = (_number_or_nan(record.get(p50_column)),
                          _number_or_nan(record.get(p90_column)))
    if seen_rows != rows_declared:
        _err(f"{baseline_mod.NDJSON_NAME} has {seen_rows} rows but its manifest"
             f" says {rows_declared}. The digest matched, so this is the"
             f" manifest describing the file wrongly rather than the file"
             f" changing -- either way the identity is not what it claims.")
    return {"columns": (p50_column, p90_column), "rows": found,
            "manifest": manifest}


def _number_or_nan(value):
    """A null p50 stays NaN, exactly as `load_baseline_predictions` leaves it."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float("nan")
    value = float(value)
    return value if np.isfinite(value) else float("nan")


# --- publication ----------------------------------------------------------
def _canonical(document):
    body = {k: v for k, v in document.items() if k != "eval_hash"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def eval_hash(document):
    return hashlib.sha256(_canonical(document)).hexdigest()


def _atomic_write_bytes(path, payload):
    """Write, fsync, rename. The rename is what makes the file appear whole.

    `qfd` reads nothing here, but the operator does, and a half-written
    `verdict.json` is a verdict as far as `cat` is concerned.
    """
    tmp = path + ".partial"
    with open(tmp, "wb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _atomic_write_table(path, table):
    tmp = path + ".partial"
    pyarrow.parquet.write_table(table, tmp)
    os.replace(tmp, path)


def _eval_table(scored, contract):
    """The per-row record, so every count in the verdict can be recomputed.

    This is the artifact that makes the verdict checkable by hand: `eval.parquet`
    plus the contract reproduces `verdict.json` and nothing else is needed.
    """
    y_true = scored["y_true"]
    p50 = scored["p50"]
    p90 = scored["p90_raw"]
    bl_p50 = scored["bl_p50"]
    ratio_ok = np.isfinite(y_true) & (y_true > 0) & (p50 > 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(ratio_ok,
                         np.maximum(np.divide(p50, y_true,
                                              out=np.full_like(p50, np.nan),
                                              where=ratio_ok),
                                    np.divide(y_true, p50,
                                              out=np.full_like(p50, np.nan),
                                              where=ratio_ok)),
                         np.nan)
    bucket = np.full(y_true.shape, "", dtype=object)
    for name, lo, hi in metrics_mod.WAIT_BUCKETS:
        bucket[np.isfinite(y_true) & (y_true >= lo) & (y_true < hi)] = name
    return pyarrow.table({
        "row_id": pyarrow.array(scored["row_id"].tolist(), pyarrow.string()),
        "task_id": pyarrow.array(scored["task_id"].tolist(), pyarrow.string()),
        "run_id": pyarrow.array(scored["run_id"].tolist(), pyarrow.int32()),
        "day": pyarrow.array(scored["day"].tolist(), pyarrow.string()),
        "target": pyarrow.array([contract["target"]] * len(y_true),
                                pyarrow.string()),
        "y_true": pyarrow.array(y_true, pyarrow.float64()),
        "p50": pyarrow.array(p50, pyarrow.float64()),
        "p90_raw": pyarrow.array(p90, pyarrow.float64()),
        "abs_error": pyarrow.array(np.abs(p50 - y_true), pyarrow.float64()),
        "ratio": pyarrow.array(ratio, pyarrow.float64()),
        "within_2x": pyarrow.array((ratio <= 2).tolist(), pyarrow.bool_()),
        "p90_covered": pyarrow.array((y_true <= p90).tolist(),
                                     pyarrow.bool_()),
        "bucket": pyarrow.array(bucket.tolist(), pyarrow.string()),
        "bl_p50": pyarrow.array(bl_p50, pyarrow.float64()),
        "bl_p90": pyarrow.array(scored["bl_p90"], pyarrow.float64()),
        "bl_abs_error": pyarrow.array(np.abs(bl_p50 - y_true),
                                      pyarrow.float64()),
    })


# The fields that DEFINE an evaluation: change any one and the numbers may
# change. `eval_sha256` is deliberately absent -- it is derived from a write that
# has not happened yet when the comparison is made, and including it made every
# re-evaluation of identical inputs refuse itself as a conflict.
IDENTITY_INPUTS = ("request_hash", "baseline_hash", "predictions_sha256",
                   "contract_hash")


def _identity(document):
    return {k: document["inputs"].get(k) for k in IDENTITY_INPUTS}


def _scoreboard(document):
    """The numbers behind a verdict, small enough to travel in the reply.

    WHY THIS CROSSES THE SOCKET AT ALL. `verdict.json` already holds everything
    here and more, and it is written into the run's `out/`, which is qfrun 2770
    -- so the identity that submits experiments cannot read it. What that
    identity could see was one word, `go` or `no-go`, and a research loop cannot
    choose its next hypothesis from a word: 6.7% against a 15% bar and 14.9%
    against a 15% bar are the same verdict and completely different situations.

    Deliberately NOT the whole document: no per-day block, no per-bucket table,
    no row counts. Those belong in the file, and a reply that grew with the
    holdout would eventually be a reply nobody bounded.
    """
    consistency = document.get("consistency") or {}
    # `.get` throughout: this also runs over a verdict document READ BACK FROM
    # DISK, and a KeyError on the reuse path would turn a valid cached verdict
    # into an opaque internal error. A missing field yields a thinner
    # scoreboard, which is visibly thin.
    return {"metrics": document.get("metrics") or {},
            "consistency": {
                "days_required": consistency.get("days_required"),
                "days_passed": consistency.get("days_passed")}}


def _existing_verdict(out_dir, document):
    """A previous verdict for this run, if it judged the SAME inputs.

    FIRST PUBLICATION WINS, like the extract and baseline stores: identical
    inputs mean identical numbers, so re-running is a no-op rather than a
    rewrite. DIFFERENT inputs under the same run id is a refusal -- one run
    directory holding two verdicts is a record nobody can cite.
    """
    path = os.path.join(out_dir, VERDICT_NAME)
    if not os.path.isfile(path):
        return None
    previous = _read_json(path, "existing verdict")
    if not isinstance(previous, dict) or not isinstance(
            previous.get("inputs"), dict):
        _err(f"{path} exists and is not a verdict document")
    if _identity(previous) != _identity(document):
        _err(f"{path} already records a verdict over different inputs"
             f" ({_identity(previous)}). One run id with two verdicts is a"
             f" record nobody can cite; evaluate under a new run.",
             error_class="verdict_already_recorded")
    if previous.get("verdict") not in ("go", "no-go"):
        _err(f"{path} exists but records no verdict, so it cannot be reused")
    # WHAT IS BEING REUSED IS VERIFIED, not merely present. Reuse returns
    # somebody else's numbers as this run's answer, so the two things that make
    # those numbers checkable have to still hold: the document has to hash to its
    # own `eval_hash`, and the per-row file has to be the one it names. Without
    # this, deleting or editing `eval.parquet` still yielded `reused: true` --
    # which is a verdict with no evidence behind it, reported as a success.
    declared = previous.get("eval_hash")
    recomputed = eval_hash(previous)
    if declared != recomputed:
        _err(f"{path} declares eval_hash {str(declared)[:12]} but its body"
             f" hashes to {recomputed[:12]}: it has been edited since it was"
             f" written, so its numbers are not the ones that were computed",
             error_class="verdict_body_altered")
    eval_path = os.path.join(out_dir, EVAL_NAME)
    if not os.path.isfile(eval_path):
        _err(f"{path} records a verdict and {EVAL_NAME} is missing, so nothing"
             f" it says can be recomputed. Refusing to reuse it.",
             error_class="eval_rows_missing")
    digest = file_digest(eval_path)
    if digest != previous["inputs"].get("eval_sha256"):
        _err(f"{EVAL_NAME} digests to {digest[:12]} but the verdict beside it"
             f" says {str(previous['inputs'].get('eval_sha256'))[:12]}: the"
             f" per-row evidence is not the evidence this verdict was computed"
             f" from",
             error_class="eval_rows_altered")
    return previous


# --- the orchestration ----------------------------------------------------
def evaluate(cfg, req, contract_name):
    """`{verdict, ...}` for the `service.Handler` to return, or `EvaluateError`.

    The signature is the one `Handler` injects: `(cfg, validated request,
    contract file name)`.
    """
    contract = load_contract(cfg, req, contract_name)
    target_column = _target_column(contract)
    _directory, manifest, runs_path = open_extract(cfg, req, contract)

    run_dir = os.path.join(cfg.eval_dir, req["run_id"])
    predictions = read_predictions(
        os.path.join(run_dir, "in", PREDICTIONS_NAME),
        declared_sha256=req["predictions_sha256"])

    extract = scan_runs(runs_path, target_column=target_column,
                        slice_values=contract["primary_slice"]
                        ["reason_resolved"],
                        wanted_keys=predictions["row_id"])
    in_slice = np.isin(
        extract["reason_resolved"],
        np.asarray(contract["primary_slice"]["reason_resolved"], dtype=str)) \
        & np.isfinite(extract["y_true"])

    try:
        index = rows_mod.check(
            pred_task_id=predictions["task_id"],
            pred_run_id=predictions["run_id"],
            pred_row_id=predictions["row_id"],
            extract_task_id=extract["task_id"],
            extract_run_id=extract["run_id"],
            extract_days=extract["day"],
            extract_in_slice=in_slice)
        days = extract["day"][index]
        day_block = rows_mod.check_day_block(
            days, extract["available_days"],
            required=required_days(manifest["request"]["as_of_date"],
                                   contract["holdout_days"]))
    except rows_mod.RowSetError as e:
        # NC11's outcome, with its own class so the suite can tell it from a
        # missing input or a contract disagreement. The comment used to claim
        # that and there was no class; a claim in a comment is not a control.
        _err(f"the prediction set is not a scorable row set: {e}",
             error_class="row_set_rejected")

    baseline = read_baseline(cfg, req, contract,
                             wanted_keys=predictions["row_id"])
    bl_p50 = np.array([baseline["rows"].get(k, (np.nan, np.nan))[0]
                       for k in predictions["row_id"].tolist()], dtype=float)
    bl_p90 = np.array([baseline["rows"].get(k, (np.nan, np.nan))[1]
                       for k in predictions["row_id"].tolist()], dtype=float)

    y_true = extract["y_true"][index]
    # THE ONE SCORED POPULATION. Both sides are computed over exactly these rows,
    # so `eligible_n` matches by construction and a relative bar compares two
    # ratios over one row set.
    #
    # The two exclusions are counted so they do not OVERLAP: a row that is both
    # out of slice and missing a baseline belongs to one of the two counts, not
    # to both. The first version subtracted one from the other, which
    # double-counts exactly the rows that are hardest to reason about.
    judged = np.isin(
        extract["reason_resolved"][index],
        np.asarray(contract["primary_slice"]["reason_resolved"], dtype=str)) \
        & np.isfinite(y_true)
    keep = judged & np.isfinite(bl_p50)
    out_of_slice = int((~judged).sum())
    baseline_missing = int((judged & ~np.isfinite(bl_p50)).sum())
    if not keep.any():
        _err("no predicted row is both in the contract's primary slice and"
             " covered by the baseline, so there is nothing the contract's bars"
             " could be evaluated over")
    lost = sorted(set(days.tolist()) - set(days[keep].tolist()))
    if lost:
        _err(f"holdout day(s) {lost} lose every row once the primary slice"
             f" ({contract['primary_slice']['reason_resolved']}) and baseline"
             f" coverage are applied. A {contract['consistency']['days_required']}"
             f"-of-{contract['holdout_days']} rule applied to fewer days is not"
             f" the rule that was agreed.")

    scored = {
        "row_id": predictions["row_id"][keep],
        "task_id": predictions["task_id"][keep],
        "run_id": predictions["run_id"][keep],
        "day": days[keep],
        "y_true": y_true[keep],
        "p50": predictions["p50"][keep],
        "p90_raw": predictions["p90_raw"][keep],
        "bl_p50": bl_p50[keep],
        "bl_p90": bl_p90[keep],
    }
    buckets = any(spec.get("bucket") is not None
                  for spec in contract["metrics"].values())
    model = metrics_mod.compute(y_true=scored["y_true"], p50=scored["p50"],
                               p90=scored["p90_raw"], days=scored["day"],
                               buckets=buckets)
    baseline_result = metrics_mod.compute(
        y_true=scored["y_true"], p50=scored["bl_p50"], p90=scored["bl_p90"],
        days=scored["day"], buckets=buckets)

    try:
        decision = verdict_mod.decide(contract, model=model,
                                      baseline=baseline_result)
    except verdict_mod.VerdictError as e:
        _err(f"the contract cannot be applied to these numbers: {e}")

    document = {
        "schema": SCHEMA,
        "run_id": req["run_id"],
        "contract": {
            "hash": req["contract"],
            "file": contract_name,
            "name": contract["name"],
            "target": contract["target"],
            "primary_slice": contract["primary_slice"],
            "holdout_days": contract["holdout_days"],
        },
        "inputs": {
            "request_hash": req["request_hash"],
            "extract_hash": manifest.get("extract_hash"),
            "baseline_hash": req["baseline_hash"],
            "predictions_sha256": req["predictions_sha256"],
            "contract_hash": req["contract"],
        },
        "rows": {
            "predicted_n": int(predictions["row_id"].size),
            "scored_n": int(keep.sum()),
            "baseline_missing_n": baseline_missing,
            "out_of_slice_n": out_of_slice,
            "nonpositive_p50_n": int((predictions["p50"] <= 0).sum()),
            "crossed_quantiles_n": int((predictions["p90_raw"]
                                        < predictions["p50"]).sum()),
        },
        "days": day_block,
        "baseline_columns": list(baseline["columns"]),
        "model": model,
        "baseline": baseline_result,
        "verdict": decision["verdict"],
        "metrics": decision["metrics"],
        "consistency": decision["consistency"],
    }

    out_dir = os.path.join(run_dir, "out")
    previous = _existing_verdict(out_dir, document)
    if previous is not None:
        return {"verdict": previous["verdict"],
                "eval_hash": previous.get("eval_hash"),
                "scoreboard": _scoreboard(previous),
                "reused": True}

    table = _eval_table(scored, contract)
    eval_path = os.path.join(out_dir, EVAL_NAME)
    _atomic_write_table(eval_path, table)
    # The per-row file's digest goes INSIDE the verdict, so the two are one
    # record: a verdict citing an `eval.parquet` that has since changed is
    # detectable rather than merely suspicious.
    document["inputs"]["eval_sha256"] = file_digest(eval_path)
    document["eval_hash"] = eval_hash(document)
    _atomic_write_bytes(os.path.join(out_dir, VERDICT_NAME),
                        json.dumps(document, sort_keys=True,
                                   indent=2).encode())
    return {"verdict": document["verdict"], "eval_hash": document["eval_hash"],
            "scored_n": document["rows"]["scored_n"],
            "scoreboard": _scoreboard(document),
            "days": day_block["claimed"], "reused": False}
