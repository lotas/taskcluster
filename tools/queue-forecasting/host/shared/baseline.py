"""The identity of a promoted baseline set. Phase 2b-3 Task 13.

A baseline has the same provenance problem as an extract -- the same window,
produced twice, yields different content -- so it gets the same answer:
**immutable publication under a content key**.

WHY THIS IS PROMOTION AND NOT PRODUCTION. The baseline comes from
`node src/predictor.js` inside `docker compose run --rm predictor`
(`scripts/run_training.sh` steps 1.6 and 2), which needs a database credential
AND docker. No existing domain may hold both: `qfextract` must not be in `docker`
(D15) and `qfd` is in `docker` but must never hold the credential. The nightly
already produces these files in the DEPLOYMENT domain, which legitimately has
both -- so promotion reuses that domain rather than inventing a fourth
root-equivalent one.

The store itself sits OUTSIDE the deployment domain's write access. If the deploy
user owned it, the domain that produces baselines could also rewrite published
ones, and "immutable" would rest on nobody choosing to. Promotion is a mediated
write: `promote-baseline.sh` runs as root and will only ever write the shape it
validates. **Being able to publish through a validating step is not the same as
being able to write to the directory.**

THE ONE VALUE THAT CANNOT BE DERIVED. `exclude_dates` -- the Policy B filtered
baseline -- changes the percentile HISTORY the predictor consulted, not the rows it
emitted, so it is not recoverable from the artifact. It is declared by whoever
promotes, and the manifest records that it is declared. A manifest that presented
a declared value as a derived one would be the strongest-looking claim in the
record and the weakest fact in it.

WHY THIS LIVES IN `shared/`. Three things read it and none may depend on either
of the others: `promote-baseline.sh` publishes through it from the DEPLOYMENT
domain, `qfd` recomputes a promoted set's hash when it pins one to a probe, and
the EVALUATOR recomputes it again before judging anything against that set. It
started out under `dispatcher/`, which meant a root script in the deployment
domain importing from the dispatcher's tree -- and the moment a second domain
needed it, that placement was the same mistake `shared/extract_spec.py` exists to
avoid. A content key with two implementations is not a cross-check, it is a
disagreement with no arbiter, so there is exactly one.

Pure and stdlib-only: no filesystem writes, no clock. `describe` reads files and
returns a manifest; publication is the shell script's job.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

# The aggregate NDJSON the trainer joins on `(task_id, run_id)` for the residual
# feature (`data_loader.load_baseline_predictions`).
NDJSON_NAME = "baseline_predictions.ndjson"

# Per-holdout-day baselines, named `<YYYY-MM-DD>.json` by
# `scripts/run_training.sh` step 2.
_DAY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json\Z")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")

# Bounded so a malformed directory cannot turn into a manifest nobody reads. A
# year of daily baselines is 365; anything past that is a directory that has
# stopped being a baseline set.
MAX_DAYS = 400


class BaselineError(ValueError):
    """A directory that must not be promoted. The message names the file."""


def _digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_ndjson(path):
    """Row count and the `pending_at` range, from the CONTENT.

    Read out rather than declared: a declared window can be wrong and nothing
    would notice. The trainer drops `pending_at` when it loads this file, so
    nothing downstream would ever contradict a wrong claim about it.
    """
    rows = 0
    lo = hi = None
    with open(path) as fh:
        for number, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError as e:
                raise BaselineError(
                    f"{os.path.basename(path)} line {number} is not JSON: {e}"
                ) from None
            rows += 1
            pending = record.get("pending_at")
            if isinstance(pending, str) and pending:
                lo = pending if lo is None else min(lo, pending)
                hi = pending if hi is None else max(hi, pending)
    if rows == 0:
        raise BaselineError(
            f"{os.path.basename(path)} has no rows. A baseline that joins to"
            f" nothing makes every residual NaN -- which trains, and produces a"
            f" number.")
    return rows, lo, hi


def describe(directory, *, exclude_dates):
    """The manifest for a baseline directory, or a refusal naming the file.

    CLOSED-WORLD, like the artifact allowlist and for the same reason: a baseline
    directory accumulates per-day files over months, and "everything here" is not
    a description of anything. An unrecognised entry is refused rather than
    ignored -- it is either something that belongs in the identity or something
    that should not be published, and both need a human to say which.
    """
    if not isinstance(exclude_dates, (list, tuple)):
        raise BaselineError(
            f"exclude_dates must be a list of YYYY-MM-DD strings, got"
            f" {type(exclude_dates).__name__}")
    for date in exclude_dates:
        if not isinstance(date, str) or not _DATE_RE.match(date):
            raise BaselineError(f"exclude_dates entry {date!r} is not YYYY-MM-DD")

    try:
        entries = sorted(os.listdir(directory))
    except OSError as e:
        raise BaselineError(f"cannot read {directory}: {e}") from None

    days = []
    files = {}
    for name in entries:
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            raise BaselineError(
                f"{name} is not a regular file. A baseline set is an aggregate"
                f" NDJSON and per-day JSONs, and nothing else.")
        if name == NDJSON_NAME:
            files[name] = {"sha256": _digest(path),
                           "bytes": os.path.getsize(path)}
            continue
        match = _DAY_RE.match(name)
        if not match:
            raise BaselineError(
                f"{name} is not part of a baseline set: expected"
                f" {NDJSON_NAME} or <YYYY-MM-DD>.json. Refusing rather than"
                f" ignoring it -- it either belongs in the identity or should not"
                f" be published.")
        days.append(match.group(1))
        files[name] = {"sha256": _digest(path),
                       "bytes": os.path.getsize(path)}

    if NDJSON_NAME not in files:
        raise BaselineError(
            f"{directory} has no {NDJSON_NAME}: that is the aggregate the"
            f" trainer joins on (task_id, run_id) for the residual feature, so a"
            f" set without it cannot serve a residual cohort")
    if not days:
        raise BaselineError(
            f"{directory} has no per-day JSONs, so there is no holdout day this"
            f" baseline could score")
    if len(days) > MAX_DAYS:
        raise BaselineError(
            f"{directory} holds {len(days)} per-day files, past the {MAX_DAYS}"
            f" ceiling: that is a directory that has stopped being one baseline"
            f" set")

    rows, lo, hi = _read_ndjson(os.path.join(directory, NDJSON_NAME))

    return {
        "schema": 1,
        "files": files,
        "days": sorted(days),
        "ndjson_rows": rows,
        "pending_at_min": lo,
        "pending_at_max": hi,
        # DECLARED, and said so. Sorted so the order somebody typed it in does
        # not become part of the identity.
        "exclude_dates": sorted(exclude_dates),
        "exclude_dates_provenance":
            "declared by the promoter; not derivable from these files, because"
            " it changes the percentile history the predictor consulted rather"
            " than the rows it emitted",
    }


def canonical(manifest):
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()


def baseline_hash(manifest):
    """A CONTENT key, so promoting identical files twice yields one artifact.

    Deliberately covers no timestamp. A `promoted_at` inside the identity would
    make every promotion a new artifact, which is exactly what a content key
    exists to prevent -- the promotion time belongs beside the manifest, not
    inside it.
    """
    body = {k: v for k, v in manifest.items() if k != "baseline_hash"}
    return hashlib.sha256(canonical(body)).hexdigest()
