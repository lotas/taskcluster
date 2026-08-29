"""The evaluation request. Phase 2c Task 19.

CLOSED-WORLD, AND IDS ONLY -- never a path. Every filesystem location the
evaluator touches is derived from its OWN trusted roots plus a validated
identifier: the extract from `<extracts_dir>/<request_hash>`, the baseline from
`<baselines_dir>/<baseline_hash>`, the contract from
`<contracts_dir>/<name>.json`, and the staged predictions from
`<eval_dir>/<run_id>/in/`. A path on the wire would make the peer check the only
control between the dispatcher and the whole filesystem, and `qfd` runs in the
`docker` group -- which is root-equivalent (D5). The narrowest domain in the
system must not accept instructions from the widest one about where to read.

This is the same decision as `extract_spec`: the request is a set of typed
scalars, validated here, and refusals name the field. It lives beside the
evaluator rather than in `shared/` because -- unlike an extraction request, which
`qfd` must validate too so a bad request is refused at submit time -- only the
evaluator constructs and consumes this shape. `qfd` builds it from a job's spec
and its own pins; it does not need to re-derive it.
"""
from __future__ import annotations

import re

# 64 lowercase hex: an extract's request hash, a baseline's content hash, a
# contract's content hash, a file digest. One pattern, because it is one claim.
_HASH64_RE = re.compile(r"^[0-9a-f]{64}\Z")

# `<kind>-<YYYYmmddTHHMMSSZ>-<sha[:12]>-<seq>`, as `qfd.make_run_id` mints it.
# Matched rather than merely sanitised: the run id becomes a directory name, so
# "contains no separator" is necessary and not sufficient -- a name like `..x`
# has no separator either.
_RUN_ID_RE = re.compile(r"^[a-z][a-z0-9]{0,15}-[0-9]{8}T[0-9]{6}Z"
                        r"-[0-9a-f]{7,40}-[0-9]{1,12}\Z")

_REQUIRED = ("op", "run_id", "contract", "request_hash", "predictions_sha256")
_OPTIONAL = ("baseline_hash",)

MAX_FIELDS = 16


class RequestError(ValueError):
    """A request that must not be acted on. The message names the field."""


def _err(msg):
    raise RequestError(msg)


def _hash(value, what):
    # isinstance BEFORE the regex: `_HASH64_RE.match(5)` is a TypeError, which
    # escapes the typed refusal. This project has paid for that shape twice
    # already (extract_spec's P1, and contract.py's `name` field).
    if not isinstance(value, str) or not _HASH64_RE.match(value):
        _err(f"{what} must be 64 lowercase hex, got {value!r}")
    return value


def validate(raw):
    """A frozen request, or `RequestError`. `op` is always "evaluate"."""
    if not isinstance(raw, dict):
        _err(f"a request must be an object, got {type(raw).__name__}")
    if len(raw) > MAX_FIELDS:
        _err(f"a request has at most {MAX_FIELDS} fields, got {len(raw)}")
    missing = [k for k in _REQUIRED if k not in raw]
    if missing:
        _err(f"missing field(s): {missing}")
    unknown = set(raw) - set(_REQUIRED) - set(_OPTIONAL)
    if unknown:
        _err(f"unknown field(s): {sorted(unknown)}")

    if raw["op"] != "evaluate":
        _err(f"op must be 'evaluate', got {raw['op']!r}")

    run_id = raw["run_id"]
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        _err(f"run_id {run_id!r} is not a dispatcher run id"
             f" (<kind>-<YYYYmmddTHHMMSSZ>-<sha>-<seq>). It becomes a directory"
             f" name, so it is matched rather than sanitised.")

    contract = raw["contract"]
    if not isinstance(contract, str) or not _HASH64_RE.match(contract):
        _err(f"contract must be a contract_hash (64 lowercase hex), got"
             f" {contract!r}. A contract NAME on the wire would let the caller"
             f" choose which rule judges it; a hash is resolved against the"
             f" trusted checkout and refused if absent (NC9).")

    out = {
        "op": "evaluate",
        "run_id": run_id,
        "contract": contract,
        "request_hash": _hash(raw["request_hash"], "request_hash"),
        "predictions_sha256": _hash(raw["predictions_sha256"],
                                    "predictions_sha256"),
    }
    baseline = raw.get("baseline_hash")
    if baseline is not None:
        # OPTIONAL for the same reason it is optional on a probe: a
        # non-residual cohort reads no baseline. But a CONTRACT names one, so
        # the evaluator refuses the COMBINATION of a contract that pins a
        # baseline with a request that carries none -- checked there, where the
        # contract is in hand, not here.
        out["baseline_hash"] = _hash(baseline, "baseline_hash")
    return out
