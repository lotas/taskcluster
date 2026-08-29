"""The identity of an evaluation contract. Phase 2c Task 17.

A contract is the rule a result is judged by: the slice, the metrics, the bars,
the consistency requirement and the baseline it is measured against. It lives in
the TRUSTED CHECKOUT, and that placement is the whole control -- a rule the
candidate can edit is not a rule, it is a suggestion with a hash.

WHY THIS LIVES IN `shared/`. Two privilege domains read it and neither may
depend on the other: `qfd` resolves a submitted `contract_hash` and refuses a job
naming one that is not in the checkout (NC9), and the evaluator recomputes the
hash from the file it actually read before judging anything. `shared/` is where
2b-1 put `extract_spec.py` for exactly this reason.

STDLIB-ONLY, AND JSON RATHER THAN YAML. `qfd` imports this and `qfd` is
stdlib-only (D6); there is no YAML parser in the standard library. Hashing the
raw file bytes would have avoided the parse and given up the property that
matters -- a byte-level key changes when a trailing newline does, so it could not
tell a reformatted contract from an altered one.

THE IDENTITY IS A CONTENT KEY, like `baseline_hash` and for the same reason: it
can be VERIFIED rather than trusted. `contract_hash` covers every field that
affects the judgement and nothing else, so two results citing one hash are
comparable by construction -- and a directory whose contract does not hash to its
own name is either corrupt or hand-edited, which a reader can be told rather
than left to assume.

WHAT A CONTRACT MUST NAME, and why each one is not optional:

  `baseline_hash`  -- "MAE improves by >=15% over baseline" is not a claim until
                      the baseline is named. `trainer-spec.md` states the bar
                      without it, which was fine while a human knew which
                      baseline was in play and stops being fine the moment two
                      runs are compared automatically.
  `primary_slice`  -- the go/no-go slice is `reason_resolved = 'completed'`
                      (`evaluate.py:327`). Changing the population at the same
                      time as the model makes "did it improve?" unanswerable, so
                      the population is part of the rule.
  `metrics`        -- each with its bar and direction. A metric with no declared
                      direction is a number nobody can fail.
  `consistency`    -- >=3 of 5 holdout days, so a single outlier day cannot carry
                      a verdict.
  `p90_coverage`   -- the [85%, 95%] band, which is a two-sided check: a model
                      that never misses is not calibrated, it is inflated.
"""
from __future__ import annotations

import hashlib
import json
import re

SCHEMA = 1

# The targets a contract can judge, and the column each one is measured on.
# Deliberately the same mapping `extract_spec.TARGET_COLUMNS` carries: a contract
# that judged a column the extract does not produce would validate here and fail
# at the join.
TARGETS = ("wait_time", "run_duration")

# `reason_resolved` values a slice may name. Closed-world: the predicate is a
# SET MEMBERSHIP, never an expression, because an expression is code and this
# file is read by the process that is supposed to be constraining the candidate.
RESOLVED_VALUES = ("completed", "failed", "exception", "canceled")

# A metric's direction. `lower_is_better` for error, `higher_is_better` for hit
# rates, `band` for coverage -- which is neither, and conflating it with
# "higher is better" is how an inflated p90 comes to look like a good one.
DIRECTIONS = ("lower_is_better", "higher_is_better", "band")

# What a bar can be stated as. `relative_improvement` is the >=15% form,
# `absolute_improvement` the >=5pp form, `absolute` a bare threshold (the tail
# gate: "30m+ p90 miss below 30%"), `band` a two-sided interval.
BAR_KINDS = ("relative_improvement", "absolute_improvement", "absolute", "band")

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}\Z")
_HASH64_RE = re.compile(r"^[0-9a-f]{64}\Z")

_REQUIRED = ("schema", "name", "target", "baseline_hash", "primary_slice",
             "metrics", "consistency", "holdout_days")

MAX_METRICS = 32
MAX_HOLDOUT_DAYS = 31


class ContractError(ValueError):
    """A contract that must not be used to judge anything. Names the field.

    Deliberately NOT a subclass of `SpecError`: that lives in `dispatcher`, and
    `shared` must not depend on it -- the same decision, for the same reason, as
    `ExtractSpecError`. The cost is one `except` in `spec.normalize`.
    """


def _err(msg):
    raise ContractError(msg)


def _need_name(value, what):
    """isinstance BEFORE the regex. `_NAME_RE.match(5)` is a TypeError, and
    `raw.get("name") or ""` does not help: `5 or ""` is `5`. This is the shape
    the hostile sweep in `tests/test_contract.py` exists to find, and it found
    it here on the first run."""
    if not isinstance(value, str) or not _NAME_RE.match(value):
        _err(f"{what} {value!r} must be lowercase snake_case")
    return value


def _need_dict(value, what):
    # isinstance BEFORE any membership test. A `TypeError` from `"x" in 5`
    # escapes the typed refusal, which is a P1 this project has already paid
    # for once in `extract_spec`.
    if not isinstance(value, dict):
        _err(f"{what} must be an object, got {type(value).__name__}")


def _need_number(value, what):
    # bool BEFORE int: `isinstance(True, int)` is True, and a bar of `True`
    # would compare as 1.0 and judge every run.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _err(f"{what} must be a number, got {value!r}")
    value = float(value)
    # AND FINITE. Every comparison against NaN is False, so a NaN bar fails
    # every run while looking like a threshold; an infinite one fails every run
    # or passes every run depending on its sign. Both are a judge that has
    # stopped judging, and neither announces itself. Found by the hostile sweep
    # rather than reasoned about in advance.
    if value != value or value in (float("inf"), float("-inf")):
        _err(f"{what} must be finite, got {value!r}: every comparison against"
             f" a non-finite bar is decided by the bar rather than by the run")
    return value


def _check_bar(name, bar):
    _need_dict(bar, f"metrics.{name}.bar")
    kind = bar.get("kind")
    if kind not in BAR_KINDS:
        _err(f"metrics.{name}.bar.kind must be one of {list(BAR_KINDS)},"
             f" got {kind!r}")
    unknown = set(bar) - {"kind", "value", "low", "high"}
    if unknown:
        _err(f"unknown key(s) in metrics.{name}.bar: {sorted(unknown)}")
    if kind == "band":
        low = _need_number(bar.get("low"), f"metrics.{name}.bar.low")
        high = _need_number(bar.get("high"), f"metrics.{name}.bar.high")
        if not low < high:
            _err(f"metrics.{name}.bar is the empty band [{low}, {high}]:"
                 f" nothing can satisfy it, so every run would fail on a"
                 f" typo rather than on its numbers")
        return {"kind": kind, "low": low, "high": high}
    if "value" not in bar:
        _err(f"metrics.{name}.bar needs a value for kind {kind!r}")
    for absent in ("low", "high"):
        if absent in bar:
            _err(f"metrics.{name}.bar.{absent} is only meaningful for kind"
                 f" 'band', not {kind!r}")
    return {"kind": kind, "value": _need_number(bar["value"],
                                                f"metrics.{name}.bar.value")}


def _check_metric(name, spec):
    _need_name(name, "metric name")
    _need_dict(spec, f"metrics.{name}")
    unknown = set(spec) - {"direction", "bar", "bucket"}
    if unknown:
        _err(f"unknown key(s) in metrics.{name}: {sorted(unknown)}")
    direction = spec.get("direction")
    if direction not in DIRECTIONS:
        _err(f"metrics.{name}.direction must be one of {list(DIRECTIONS)},"
             f" got {direction!r}")
    bar = _check_bar(name, spec.get("bar"))
    if (direction == "band") != (bar["kind"] == "band"):
        # A coverage metric with a one-sided bar, or an error metric with a
        # band, is a rule that reads as though it checks calibration and does
        # not. Refused rather than interpreted.
        _err(f"metrics.{name}: direction {direction!r} and bar kind"
             f" {bar['kind']!r} disagree; a band bar needs a band direction")
    out = {"direction": direction, "bar": bar}
    bucket = spec.get("bucket")
    if bucket is not None:
        # The tail gate is a bucket metric: "30m+ p90 miss". The bucket NAME is
        # not validated against a list here -- `evaluate.WAIT_BUCKETS` owns that
        # vocabulary and importing it would put pandas in qfd's import path.
        # The evaluator refuses an unknown bucket, which is the right place: it
        # is the process that knows the buckets.
        if not isinstance(bucket, str) or not bucket:
            _err(f"metrics.{name}.bucket must be a non-empty string")
        out["bucket"] = bucket
    return out


def _check_slice(raw):
    _need_dict(raw, "primary_slice")
    unknown = set(raw) - {"reason_resolved", "anchor"}
    if unknown:
        _err(f"unknown key(s) in primary_slice: {sorted(unknown)}")
    values = raw.get("reason_resolved")
    if not isinstance(values, (list, tuple)) or not values:
        _err("primary_slice.reason_resolved must be a non-empty list")
    for value in values:
        if value not in RESOLVED_VALUES:
            _err(f"primary_slice.reason_resolved has {value!r}, which is not one"
                 f" of {list(RESOLVED_VALUES)}")
    if len(set(values)) != len(values):
        _err("primary_slice.reason_resolved repeats a value, which would double"
             " count rows in every metric")
    anchor = raw.get("anchor", "pending_at")
    if anchor != "pending_at":
        # The only anchor the baseline runs used (`--pending-eval-date`). A
        # contract naming another one would compare against a cohort the
        # baseline never scored.
        _err(f"primary_slice.anchor must be 'pending_at', got {anchor!r}")
    return {"reason_resolved": sorted(values), "anchor": anchor}


def _check_consistency(raw):
    _need_dict(raw, "consistency")
    unknown = set(raw) - {"days_required"}
    if unknown:
        _err(f"unknown key(s) in consistency: {sorted(unknown)}")
    value = raw.get("days_required")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _err(f"consistency.days_required must be a positive integer, got"
             f" {value!r}")
    return {"days_required": value}


def validate(raw):
    """A frozen, canonical contract, or `ContractError` naming the field.

    Returns a plain dict rather than a mapping proxy: unlike an extract request
    this is not relayed across a trust boundary, and `canonical` has to be able
    to serialise it.
    """
    _need_dict(raw, "contract")
    missing = [k for k in _REQUIRED if k not in raw]
    if missing:
        _err(f"missing key(s): {missing}")
    unknown = set(raw) - set(_REQUIRED) - {"contract_hash", "note"}
    if unknown:
        _err(f"unknown key(s): {sorted(unknown)}")

    if raw["schema"] != SCHEMA:
        _err(f"schema must be {SCHEMA}, got {raw['schema']!r}")
    _need_name(raw.get("name"), "name")
    if raw["target"] not in TARGETS:
        _err(f"target must be one of {list(TARGETS)}, got {raw['target']!r}")
    if not isinstance(raw["baseline_hash"], str) \
            or not _HASH64_RE.match(raw["baseline_hash"]):
        _err(f"baseline_hash must be 64 lowercase hex, got"
             f" {raw['baseline_hash']!r}. A bar stated over 'baseline' names"
             f" nothing until the baseline is named.")

    metrics = raw["metrics"]
    _need_dict(metrics, "metrics")
    if not metrics:
        _err("metrics is empty: a contract that judges nothing is not a rule")
    if len(metrics) > MAX_METRICS:
        _err(f"metrics has {len(metrics)} entries, past the {MAX_METRICS} ceiling")

    days = raw["holdout_days"]
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        _err(f"holdout_days must be a positive integer, got {days!r}")
    if days > MAX_HOLDOUT_DAYS:
        _err(f"holdout_days is {days}, past the {MAX_HOLDOUT_DAYS} ceiling")

    consistency = _check_consistency(raw["consistency"])
    if consistency["days_required"] > days:
        # A rule nothing can satisfy fails every run for a reason that has
        # nothing to do with the model.
        _err(f"consistency.days_required is {consistency['days_required']} but"
             f" holdout_days is {days}: no run could ever satisfy it")

    out = {
        "schema": SCHEMA,
        "name": raw["name"],
        "target": raw["target"],
        "baseline_hash": raw["baseline_hash"],
        "primary_slice": _check_slice(raw["primary_slice"]),
        "metrics": {name: _check_metric(name, spec)
                    for name, spec in sorted(metrics.items())},
        "consistency": consistency,
        "holdout_days": days,
    }
    if raw.get("note") is not None:
        # IN the identity, unlike a baseline's `promoted_at`. A note is what a
        # reader is told the rule means, so two contracts differing only in
        # their note are two different rules as far as anyone reading a verdict
        # is concerned.
        if not isinstance(raw["note"], str):
            _err("note must be a string")
        out["note"] = raw["note"]
    return out


def canonical(contract):
    body = {k: v for k, v in contract.items() if k != "contract_hash"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def contract_hash(contract):
    """A CONTENT key over everything that affects the judgement.

    `contract_hash` itself is excluded, so a file can carry its own hash -- the
    same self-describing shape as a baseline manifest, and verified the same way:
    recompute and compare, never trust the field.
    """
    return hashlib.sha256(canonical(validate(contract))).hexdigest()


def load(path):
    """`(contract, contract_hash)` from a file, or `ContractError`.

    Validates and rehashes; a file whose declared `contract_hash` disagrees with
    its body is refused HERE rather than at the point of use, because every
    caller would otherwise have to remember to check.
    """
    try:
        with open(path) as fh:
            raw = json.load(fh)
    except OSError as e:
        _err(f"cannot read {path}: {e}")
    except ValueError as e:
        _err(f"{path} is not JSON: {e}")
    contract = validate(raw)
    digest = hashlib.sha256(canonical(contract)).hexdigest()
    declared = raw.get("contract_hash")
    if declared is not None and declared != digest:
        _err(f"{path} declares contract_hash {str(declared)[:12]} but its body"
             f" hashes to {digest[:12]}: the file has been edited since it was"
             f" written, and a content key that does not match its content"
             f" records nothing")
    return contract, digest
