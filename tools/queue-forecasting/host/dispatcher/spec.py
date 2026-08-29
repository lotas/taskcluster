"""Closed-world validation and canonical hashing for dispatcher job specs.

Pure: no filesystem, no network, no globals, no clock. The dispatcher validates
before it touches anything, so this module is the outermost edge of the trust
boundary (auto-research-phase2-design.md D12).

Two rules run through everything here:
  1. Unknown is refused. Not ignored, not warned about.
  2. A field becomes an argv element, never part of a command string.
"""
from __future__ import annotations

import hashlib
import json
import re

SCHEMA_VERSION = 1

_SHA_RE = re.compile(r"^[0-9a-f]{40}\Z")
_MEM_RE = re.compile(r"^([1-9][0-9]{0,4})([mg])\Z")
_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./:-]{0,199}\Z")
_K_RE = re.compile(r"^[A-Za-z0-9_ ()\[\].:-]{1,200}\Z")
# ONE pattern for both of a probe's identities: an extract's request hash and a
# baseline's content hash. "64 lowercase hex" is the same claim either way, and
# two copies of it are two things that can drift.
_HASH64_RE = re.compile(r"^[0-9a-f]{64}\Z")
_NOTE_RE = re.compile(r"^[\x20-\x7e]{0,500}\Z")

# An allowlist, not a pattern. `--pdb` on an unattended runner is a wedged slot;
# `-p` loads plugins from an untrusted tree.
PYTEST_FLAGS = frozenset({
    "-q", "-x", "-v", "-s", "--tb=short", "--tb=long",
    "--maxfail=1", "--maxfail=3", "--durations=20",
})

MEM_CEILING_MB = 22 * 1024      # the trainer's compose ceiling; see facts #2
CPU_MIN, CPU_MAX = 0.5, 8.0
# TIMEOUT_MAX is SUBORDINATE to the dispatcher's outer hold deadline: a
# container's effective timeout is min(timeout_s, remaining hold budget), because
# a job at this ceiling that also needs an image build must still fit inside
# QFD_JOB_HOLD_DEADLINE_S (design D10a). The chain is
# TIMEOUT_MAX + BUILD_TIMEOUT_S + BUILD_LOCK_WAIT_S + HANDOFF_TIMEOUT_S + setup
# and teardown < JOB_HOLD_DEADLINE_S < LOCK_WAIT_S, and those numbers move
# together or not at all. `phase2-setup.sh discover` fails if the chain inverts.
TIMEOUT_MIN, TIMEOUT_MAX = 60, 3600
MAX_PATHS = 20

KINDS = {
    #                timeout        mem            cpus
    "test":     dict(timeout_s=1800, mem_limit="4g", cpus=4.0),
    "selftest": dict(timeout_s=300, mem_limit="1g", cpus=1.0),
    # An extraction runs in ANOTHER PROCESS -- the trusted extractor, in its own
    # privilege domain (D15) -- so none of these numbers describes a container.
    #
    # `mem_limit` is a slot reservation and nothing else: the dispatcher's
    # admission is an aggregate memory budget (D10), and a job that occupied
    # nothing would let any number of extractions be admitted at once while the
    # extractor serialises them anyway. 256m keeps the bookkeeping honest and
    # keeps this in the LIGHT lane -- a read must never take the training mutex,
    # or a data pull could block the nightly.
    #
    # `timeout_s` is 3600 because the first real extraction took 688s for 36 days
    # and the window ceiling is 60 (auto-research-phase2b-plan.md revision 10). A
    # timeout under the measured time would kill work the extractor had finished.
    "extract":  dict(timeout_s=3600, mem_limit="256m", cpus=1.0),
    # A probe runs agent-authored code against a frozen extract. 8g puts it
    # ABOVE the light ceiling, so the lane derives to heavy (D10) -- which is
    # correct rather than incidental: a cohort trains, so it competes with the
    # nightly for the same host and must serialise against it. Nothing about the
    # kind forces that; the memory does, and a probe that only reads a manifest
    # can ask for 1g and stay light.
    "probe":    dict(timeout_s=3600, mem_limit="8g", cpus=4.0),
}

# The lane is DERIVED from mem_limit, never requested (design D10). A job at or
# below the ceiling is light and may run two-up; anything larger is heavy, takes
# the shared training lock, and runs alone. There is deliberately no `lane`
# field: letting a caller pick the lane lets it pick its own concurrency limit.
LIGHT_MEM_CEILING_MB = 4 * 1024


def lane_for(mem_mb):
    return "light" if mem_mb <= LIGHT_MEM_CEILING_MB else "heavy"


_TOP_OPTIONAL = ("args", "timeout_s", "mem_limit", "cpus", "note")
_TOP_REQUIRED = ("schema", "kind", "source_sha")

# Kinds whose identity is NOT a commit. See `_extract_identity`.
_NO_SOURCE_SHA = ("extract",)

# Written into `source_ref` for an extract job. `source_sha TEXT NOT NULL` holds
# sixteen hundred live rows, so rather than migrate the column an extract stores
# its REQUEST HASH there: the column's role is "the immutable identity of what
# this job ran", which for a test is a commit and for an extraction is the
# request. This literal is what stops a reader treating that value as a commit
# and joining on it -- the value looks exactly like a sha, so the record has to
# say otherwise itself.
EXTRACT_SOURCE_REF = "extract-request (not a commit)"


class SpecError(ValueError):
    """A job spec that must not be accepted. The message is shown to the caller."""


def _err(msg):
    raise SpecError(msg)


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _mem_mb(value):
    m = _MEM_RE.match(value or "")
    if not m:
        _err(f"mem_limit must look like 512m or 8g, got {value!r}")
    n, unit = int(m.group(1)), m.group(2)
    return n * 1024 if unit == "g" else n


def _check_paths(paths):
    if not isinstance(paths, list) or not paths:
        _err("args.paths must be a non-empty list")
    if len(paths) > MAX_PATHS:
        _err(f"args.paths holds at most {MAX_PATHS} entries")
    for p in paths:
        if not isinstance(p, str) or not _PATH_RE.match(p):
            _err(f"args.paths entry is not a relative path: {p!r}")
        if p.startswith("/") or ".." in p.split("/"):
            _err(f"args.paths entry escapes the worktree: {p!r}")
    return list(paths)


# Relative to the worktree root, which is the mount point.
DEFAULT_TEST_PATHS = ["trainer/tests"]

# Where a probe's script must live. The `probe` kind exists to run agent-authored
# code, and this prefix is the whole of what "agent-authored" is allowed to mean:
# a script under `research/experiments/` cannot be a patched `trainer/src` module
# masquerading as an experiment.
PROBE_PREFIX = "research/experiments/"


def _check_test_args(args):
    if not isinstance(args, dict):
        _err("args must be an object")
    unknown = set(args) - {"paths", "k", "pytest_args"}
    if unknown:
        _err(f"unknown args key(s) for kind test: {sorted(unknown)}")

    # `trainer/tests`, not `tests`: the worktree ROOT is what gets mounted (at
    # /app/trainer), and qf-research keeps its suite one level down. The old
    # default resolved to a directory that does not exist, so an omitted --path
    # produced pytest exit 4 -- a usage error that used to be reported as a
    # failing experiment. A default that is wrong for the only repository this
    # dispatcher can run is worse than no default.
    paths = _check_paths(args.get("paths", DEFAULT_TEST_PATHS))

    k = args.get("k")
    if k is not None:
        if not isinstance(k, str) or not _K_RE.match(k):
            _err("args.k must be a short pytest -k expression")

    flags = args.get("pytest_args", ["-q"])
    if not isinstance(flags, list) or len(flags) > 10:
        _err("args.pytest_args must be a list of at most 10 flags")
    for f in flags:
        if f not in PYTEST_FLAGS:
            _err(f"args.pytest_args entry is not allowlisted: {f!r}")
    if len(set(flags)) != len(flags):
        _err("args.pytest_args holds a duplicate")

    return {"paths": paths, "k": k, "pytest_args": list(flags)}


def _check_extract_args(args, *, now, settlement_lag_s):
    """Delegates to the ONE closed-world definition, in `host/shared`.

    Not a second implementation: D16 requires that qfd and the extractor agree,
    and two validators that agree by having been written twice do not agree for
    long. qfd validates so a bad request is refused cheaply and legibly at submit
    time; the extractor validates again because a caller is a caller.
    """
    import extract_spec

    if not isinstance(args, dict):
        _err("args must be an object")
    if now is None or settlement_lag_s is None:
        _err("kind extract needs a clock and a settlement lag: they are what"
             " make the completed-boundary rule enforceable, and defaulting"
             " them would make this half of the check silently absent")
    try:
        request = extract_spec.validate({"schema": SCHEMA_VERSION, **args},
                                        now=now,
                                        settlement_lag_s=settlement_lag_s)
    except extract_spec.ExtractSpecError as e:
        # TRANSLATED HERE, so `normalize`'s contract stays "raises SpecError".
        #
        # `ExtractSpecError` deliberately does not subclass `SpecError`: it lives
        # in `host/shared`, which both privilege domains import, and subclassing
        # would make `shared` depend on `dispatcher`. That decision has a cost of
        # one line in each consumer, and this is the consumer -- doing it here
        # rather than in `qfd` means every caller of `normalize` keeps the single
        # error type it already handles, instead of each one growing a second
        # `except`.
        raise SpecError(str(e)) from e
    return dict(request)


def _extract_identity(effective_args):
    """`(source_sha, source_ref)` for an extract job: the request hash."""
    import extract_spec
    return (extract_spec.request_hash(effective_args), EXTRACT_SOURCE_REF)


def _check_probe_args(args):
    """One script under `research/experiments/`, and the extract it reads.

    `path` singular, not `paths`: a probe is one script, and a list would invite
    the pytest shape into a kind that runs no pytest.
    """
    if not isinstance(args, dict):
        _err("args must be an object")
    unknown = set(args) - {"path", "extract", "baseline"}
    if unknown:
        _err(f"unknown args key(s) for kind probe: {sorted(unknown)}")

    path = args.get("path")
    if not isinstance(path, str) or not _PATH_RE.match(path):
        _err(f"args.path must be a relative path, got {path!r}")
    if ".." in path.split("/") or path.startswith("/"):
        _err(f"args.path escapes the worktree: {path!r}")
    if not path.startswith(PROBE_PREFIX):
        _err(f"args.path must be under {PROBE_PREFIX}, got {path!r}:"
             f" a probe runs agent-authored code, and that prefix is the whole"
             f" of what agent-authored is allowed to mean")
    if not path.endswith(".py") or path == PROBE_PREFIX:
        _err(f"args.path must name a .py file, got {path!r}: the entrypoint runs"
             f" it with the venv interpreter, so anything else fails inside the"
             f" container instead of here")

    extract = args.get("extract")
    if not isinstance(extract, str) or not _HASH64_RE.match(extract):
        _err(f"args.extract must be an extract request hash (64 lowercase hex),"
             f" got {extract!r}. The extract must already exist: `qf extracts`"
             f" lists what is published, and `qf extract` publishes one")

    # OPTIONAL, and it has to be: a non-residual cohort reads no baseline, and
    # requiring one would mean inventing a baseline for a probe that never
    # consults it. Absent means absent -- there is no default and no "latest",
    # because a comparison whose baseline was chosen for it cannot say what it
    # was measured against.
    out = {"path": path, "extract": extract}
    baseline = args.get("baseline")
    if baseline is not None:
        if not isinstance(baseline, str) or not _HASH64_RE.match(baseline):
            _err(f"args.baseline must be a baseline hash (64 lowercase hex),"
                 f" got {baseline!r}. `qf baselines` lists what is published,"
                 f" and promote-baseline.sh publishes one")
        out["baseline"] = baseline
    return out


def _check_selftest_args(args):
    if args not in ({}, None):
        _err("kind selftest takes no args")
    return {}


_ARG_CHECKS = {"test": _check_test_args,
               "selftest": _check_selftest_args,
               "probe": _check_probe_args}


def normalize(raw, *, now=None, settlement_lag_s=None):
    """Validate a submitted spec and return the *effective* spec.

    The effective spec carries every field, defaults included, because that is
    what runs and therefore what the audit record must hash (design D12).
    """
    if not isinstance(raw, dict):
        _err("a job spec must be a JSON object")

    unknown = set(raw) - set(_TOP_REQUIRED) - set(_TOP_OPTIONAL)
    if unknown:
        _err(f"unknown key(s): {sorted(unknown)}")

    if raw.get("schema") != SCHEMA_VERSION:
        _err(f"schema must be {SCHEMA_VERSION}, got {raw.get('schema')!r}")

    kind = raw.get("kind")
    if kind not in KINDS:
        _err(f"unknown kind {kind!r}; known: {sorted(KINDS)}")
    d = KINDS[kind]

    required = [k for k in _TOP_REQUIRED
                if not (k == "source_sha" and kind in _NO_SOURCE_SHA)]
    missing = [k for k in required if k not in raw]
    if missing:
        _err(f"missing key(s): {missing}")

    source_ref = None
    if kind in _NO_SOURCE_SHA:
        if "source_sha" in raw:
            # REFUSED, not ignored. A caller that believed it was choosing the
            # identity of an extract would be wrong, and silently.
            _err(f"kind {kind} takes no source_sha: its identity is its request,"
                 f" and the dispatcher derives it")
        sha = None                      # filled in below, from the request
    else:
        sha = raw["source_sha"]
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            _err("source_sha must be a full lowercase 40-hex commit id")

    timeout_s = raw.get("timeout_s", d["timeout_s"])
    if not _is_int(timeout_s) or not (TIMEOUT_MIN <= timeout_s <= TIMEOUT_MAX):
        _err(f"timeout_s must be an int in [{TIMEOUT_MIN},{TIMEOUT_MAX}]")

    mem_limit = raw.get("mem_limit", d["mem_limit"])
    if not isinstance(mem_limit, str):
        _err("mem_limit must be a string")
    mem_mb = _mem_mb(mem_limit)
    if mem_mb > MEM_CEILING_MB:
        _err(f"mem_limit exceeds the host ceiling of {MEM_CEILING_MB}m")
    lane = lane_for(mem_mb)

    cpus = raw.get("cpus", d["cpus"])
    if not _is_num(cpus) or not (CPU_MIN <= float(cpus) <= CPU_MAX):
        _err(f"cpus must be a number in [{CPU_MIN},{CPU_MAX}]")

    note = raw.get("note", "")
    if not isinstance(note, str) or not _NOTE_RE.match(note):
        _err("note must be printable ASCII, at most 500 characters, no newlines")

    if kind == "extract":
        args = _check_extract_args(raw.get("args", {}), now=now,
                                   settlement_lag_s=settlement_lag_s)
        sha, source_ref = _extract_identity(args)
    else:
        args = _ARG_CHECKS[kind](raw.get("args", {}))

    return {
        "schema": SCHEMA_VERSION,
        "kind": kind,
        "source_sha": sha,
        "source_ref": source_ref,
        "lane": lane,
        "args": args,
        "timeout_s": timeout_s,
        "mem_limit": mem_limit,
        "cpus": float(cpus),
        "note": note,
    }


def canonical(effective):
    """Canonical JSON bytes: sorted keys, no whitespace, UTF-8."""
    return json.dumps(effective, sort_keys=True, separators=(",", ":")).encode()


def spec_hash(effective):
    return hashlib.sha256(canonical(effective)).hexdigest()


def mem_mb(mem_limit):
    """Public wrapper: store.py sizes its memory reservation from the same
    parser the validator uses, so the two cannot drift (design D10)."""
    return _mem_mb(mem_limit)
