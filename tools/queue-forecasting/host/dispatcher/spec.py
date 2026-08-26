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


def _check_test_args(args):
    if not isinstance(args, dict):
        _err("args must be an object")
    unknown = set(args) - {"paths", "k", "pytest_args"}
    if unknown:
        _err(f"unknown args key(s) for kind test: {sorted(unknown)}")

    paths = _check_paths(args.get("paths", ["tests"]))

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


def _check_selftest_args(args):
    if args not in ({}, None):
        _err("kind selftest takes no args")
    return {}


_ARG_CHECKS = {"test": _check_test_args, "selftest": _check_selftest_args}


def normalize(raw):
    """Validate a submitted spec and return the *effective* spec.

    The effective spec carries every field, defaults included, because that is
    what runs and therefore what the audit record must hash (design D12).
    """
    if not isinstance(raw, dict):
        _err("a job spec must be a JSON object")

    unknown = set(raw) - set(_TOP_REQUIRED) - set(_TOP_OPTIONAL)
    if unknown:
        _err(f"unknown key(s): {sorted(unknown)}")
    missing = [k for k in _TOP_REQUIRED if k not in raw]
    if missing:
        _err(f"missing key(s): {missing}")

    if raw["schema"] != SCHEMA_VERSION:
        _err(f"schema must be {SCHEMA_VERSION}, got {raw['schema']!r}")

    kind = raw["kind"]
    if kind not in KINDS:
        _err(f"unknown kind {kind!r}; known: {sorted(KINDS)}")
    d = KINDS[kind]

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

    args = _ARG_CHECKS[kind](raw.get("args", {} if kind == "selftest" else {}))

    return {
        "schema": SCHEMA_VERSION,
        "kind": kind,
        "source_sha": sha,
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
