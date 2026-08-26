# Auto-Research Loop Phase 2a Implementation Plan — the trusted spine

Design: `auto-research-phase2-design.md` §4 and §5. Parent:
`auto-research-loop-design.md` §3.4, §4, §7, §13.1, §14. Inherited constraints:
`auto-research-phase1-design.md` §4.1 (trusted mirror ≠ deploy checkout) and §6
(build provenance).

Revision 12, 2026-08-25: closing the prose review. One real defect remained —
**confirmation over an empty inventory is not confirmation**, which would have let
a restart release a `BUILDING` job's lock and reservation on a vacuously-true
check — plus the classic-builder residual becomes a measured quantity with a
pre-registered decision rule, and the two fault gates become Task 8b. Changes:
Tasks 6, 8b (new), 10, and the acceptance list. Remaining uncertainty now belongs
in those executable gates rather than in another document pass.

Revision 11, 2026-08-25: ninth review round, plus the decision to **drop buildx
for the classic builder** (design D10). That removes the builder's two-phase
identity, its clean-stop coverage and its image pin — three findings that existed
only because of the buildx path — and records the confirmation strength given up
in exchange. Revision 10's note follows.

Revision 10, 2026-08-25: ninth review round. `BUILDING` was added to the
transition table and to none of the state-dependent SQL; the `resources` table was
promised in the test list and absent from the code; restart recharged a `BUILDING`
job as its 2 GB builder rather than its 22 GB reservation; and **neither socket's
parent directory was traversable by its intended clients**, so the whole control
plane was unreachable. Builder-lifecycle items (two-phase identity, clean-stop
coverage, the BuildKit pin's provenance) were resolved by dropping buildx —
see revision 11 above. Changes: Tasks 2, 6, 7 and the acceptance list.

Revision 9, 2026-08-25: eighth review round. `force-release` was reachable by
`research` — a security regression introduced in revision 8, since the client
socket's group contains the untrusted user; it moves to a root/deploy-only admin
socket. Revision 8's persistent buildx builder also could not satisfy its own
contract (no per-run label, survives the build, no `--load`, unpinned BuildKit
image), so builders become ephemeral and identified by container id; a `BUILDING`
state supplies the transition the fail-closed path needed; and restart recovery
now starts from SQLite rather than `docker ps`. Changes: Tasks 2, 4, 5, 6, 7, 8,
10 and the acceptance list.

Revision 8, 2026-08-25: seventh review round. `dequeue`'s UPDATE never wrote the
hold columns its event payload claimed (so verification would have failed on the
first dequeue and restart had nothing to restore); unconfirmed **build** work
escaped the fail-closed rule that unconfirmed container kills now obey;
`kill_unconfirmed` was terminal while still holding resources, with no path back;
restart ran cleanup before re-establishing the mutex; and the handoff container
carried no label, so "all containers stopped" could pass while it ran. Changes:
Tasks 2, 5, 6, 7, 8, 10, 11 and the acceptance list.

Revision 7, 2026-08-25: sixth review round — three gaps about what a *release*
proves. The outer deadline released the mutex after an unconfirmed kill; the
intent marker was published non-atomically under a fixed name; and the deadline
did not survive a restart. The deadline arithmetic also left nothing for setup or
handoff. Changes: Tasks 1, 2, 6, 7, 7b, 8, 10 and the acceptance list.

Revision 6, 2026-08-25: fifth review round. The intent gate was itself a
reader/writer `flock` and inherited the same barging, so it becomes a
**writer-visible marker file** (not a lock, so nothing to barge);
`HOLD_CEILING_S` becomes a runner-enforced `JOB_HOLD_DEADLINE_S` covering every
phase; the nightly wrapper's **fail-open `flock`-missing branch** is closed; both
shared filesystem objects are startup-gated; and NC8(e)'s impossible two-lane
orphan setup is split into two runs. Changes: Tasks 6, 7, 7b, 8, 10 and the
acceptance list.

Revision 5, 2026-08-25: fourth review round, with two mechanisms settled by
experiment on this host: shared `flock`s **barge past a queued exclusive
waiter** (so a bounded wait cannot prevent starvation — hence the intent gate),
and `flock` ownership is **per open file description** (so each job needs its own
`open()`). Changes: Task 1 (`TIMEOUT_MAX`), Task 6 (one admission sequence, the
intent gate, per-job descriptors, build lock, both-lane recovery), Task 7 (three
inodes, new env), Task 7b (four lines, not one), Task 8 (three new NC8 clauses),
Task 10, and the acceptance list.

Revision 4, 2026-08-25: third review round. The mutex became a shared/exclusive
protocol the nightly script participates in (new Task 7b — **the one line of
running deployment code 2a changes**), the image build's reservation stopped
overlapping the job's, `dequeue` learned to chain the lease fields it sets, the
handoff got terminal-state semantics, and NC8's creation-order test — a leftover
from the `/tmp` design — was replaced with one that can actually run.

Revision 3, 2026-08-25: second review round. Changes here are Task 2
(`verify_chain` body, lease events, artifact projection), Task 6 (restart
recovery, handoff ownership), Task 7 (the lock's group and inode, cron migration
as a start-up prerequisite, budget figures), Task 8 (NC8 arithmetic, NC15
expectations), Tasks 9 and 10, and the Task 13 cross-reference.

Revision 2, 2026-08-25: revised with the design after a review round. The
changes here are Tasks 1, 2, 5, 6, 7 and 8, the two new verified facts, and the
Task 13/14 ordering.

**Deliverable.** A root-owned dispatcher that runs a pinned, sandboxed job on
request from the `research` user, with append-only hash-chained state, and six
negative controls proving the boundary fails closed. No data plane, no
contracts, no evaluator, no autonomy.

## Conventions

- Tasks are ordered. Phase 2a-1 needs no host access and no privileges; Phase
  2a-2 is privileged and is run by the human, per the Phase 0 precedent.
- Paths in this repo map into the monorepo as
  `host/x` → `tools/queue-forecasting/host/x`. On the host,
  `$TRUSTED=/srv/queue-forecasting` and the dispatcher's directory is
  `$TRUSTED/tools/queue-forecasting/host/dispatcher`.
- Test-first where the code is pure. `spec.py`, `store.py`, `sandbox.py` and
  `image.py`'s key computation have no I/O worth mocking, so their tests are
  written before them and every case names the failure it prevents.
- Tests are stdlib `unittest`, run with `python3 -m unittest discover -s
  host/dispatcher/tests`. No pytest, no network, no privileges — see design D6.
- A negative control that could not be meaningfully attempted is **VOID**, and
  VOID is a failure. Carried unchanged from `nc-suite.sh`.

## Verified facts this plan depends on

Read from the tree on 2026-08-25; re-check before implementing if the tree has
moved.

1. `trainer/Dockerfile` builds with `WORKDIR /app/trainer` and
   `uv sync --frozen --no-install-project`, so the venv lands at
   `/app/trainer/.venv` — the path a read-only research mount shadows. Hence
   design D8.
2. `docker-compose.yml` gives the `trainer` service `env_file: .env`,
   `DATABASE_URL`, a read-write `./trainer:/app/trainer` mount, `mem_limit:
   22g`, `oom_score_adj: 200`, and entrypoint `uv run python -m src.train`.
   None of that survives into the sandbox.
3. `scripts/daily_walk_forward.sh:213` acquires its lock with
   `exec 9>"$LOCK_FILE"` — a **write** open — defaulting to
   `/tmp/queue-forecasting-walk-forward.lock`. Two consequences, both
   load-bearing: the dispatcher needs `PrivateTmp=no` to see the same namespace,
   **and** the inode must be group-writable by both users, because whoever
   creates it first owns it at 0644 and the other side's `exec 9>` then fails —
   in the nightly script, fatally (design D5).
4. `trainer/pyproject.toml` declares no `[build-system]`; `uv.lock` records the
   project as virtual. `[tool.uv] dev-dependencies` includes `pytest`, `ruff`
   and `onnxruntime`, so `uv sync` (which installs dev deps by default) puts
   pytest in the trusted image — that is what makes the `test` kind possible
   with no extra manifest.
5. `trainer/src/data_loader.py` sets `CACHE_DIR` relative to the module and
   reads `os.environ["DATABASE_URL"]` in six places. 2a does not touch either;
   it only guarantees the sandbox has no `DATABASE_URL` to find.
6. Phase 0's egress rules are **uid-scoped nftables on the `research` uid**
   plus a tinyproxy allowlist. A new system user is therefore unrestricted, so
   the dispatcher reaching `github.com` needs no new rule — but Task 10 verifies
   that rather than assuming it.
7. `phase0-setup.sh` dies if `research` is in the `docker` group. Nothing in
   this plan changes that; the new membership is `qfclient`, which grants a
   socket, not a runtime.
8. `host/nc-suite.sh` establishes `refuse` / `canary` / `exists` semantics and
   the VOID convention. Phase 2a's suite reuses them verbatim, and `nc7-lib.sh`
   supplies `score_http` / `score_git` for NC14.
9. `trainer/src/data_loader.py:199-247` (`_build_query`) splices `c.filters`
   into its `WHERE` clause and selects `c.target_column`, both from a config in
   the research repo. 2b's extractor must therefore be a closed-world typed
   request, not a reuse of this function (design D4). Nothing in 2a depends on
   it; it is recorded here so 2b does not start from the wrong premise.
10. The host has ~29.4 GB of RAM and the trainer's compose cap is 22 GB. Any
   admission budget is therefore an aggregate, not a per-job, question
   (design D10).

## File Structure

```
host/dispatcher/
  spec.py                    Task 1
  store.py                   Task 2
  source.py                  Task 3
  image.py                   Task 4
  trainer-env.Dockerfile     Task 4
  env/pyproject.toml         Task 4  (human-promoted copy)
  env/uv.lock                Task 4  (human-promoted copy)
  sandbox.py                 Task 5
  qfd.py                     Task 6
  qf                         Task 6  (client, chmod +x)
  nc13-inside.sh             Task 8
  handoff-inside.sh          Task 6  (trusted artifact normalisation, D9)
  qf-dispatch.service        Task 7
  qf-locks.conf              Task 7  (systemd-tmpfiles: shared lock inodes)
  qf-runs-prune.service      Task 7
  qf-runs-prune.timer        Task 7
  tests/
    test_spec.py             Task 1
    test_store.py            Task 2
    test_source.py           Task 3
    test_image.py            Task 4
    test_sandbox.py          Task 5
    test_protocol.py         Task 6
host/nc-suite-phase2.sh      Task 8
host/fault-gates-phase2.sh   Task 8b (kill-during-build; crash-per-phase)
host/phase2-setup.sh         Task 7
host/nc12-sha.txt            Task 14 (recorded, committed)
host/nc-evidence-phase2a.txt Task 13 (generated)
```

---

# Phase 2a-1 — repo-side work

## Task 1: `spec.py` — closed-world job specs, test-first

Write `host/dispatcher/tests/test_spec.py` first. Every case is a hole a
reviewer would otherwise find in production.

```python
# Tests for spec.py. No I/O. Run:
#   python3 -m unittest discover -s host/dispatcher/tests
#
# Each case names the failure it prevents. A job field is not a convenience:
# it is the only thing an untrusted caller controls, so a field that is
# accepted loosely is a hole in the boundary (design D12).
import unittest
import spec

SHA = "3f1c" + "0" * 36


def base(**over):
    d = {"schema": 1, "kind": "test", "source_sha": SHA}
    d.update(over)
    return d


class TestTopLevel(unittest.TestCase):
    def test_minimal_spec_normalizes_and_fills_kind_defaults(self):
        eff = spec.normalize(base())
        self.assertEqual(eff["timeout_s"], 1800)
        self.assertEqual(eff["mem_limit"], "4g")
        self.assertEqual(eff["cpus"], 4.0)
        self.assertEqual(eff["args"]["paths"], ["tests"])
        self.assertEqual(eff["lane"], "light")   # derived, not supplied

    def test_unknown_top_level_key_is_refused(self):
        # The whole point of closed-world validation: a caller must not be able
        # to introduce a field a later version might honour.
        with self.assertRaises(spec.SpecError):
            spec.normalize(base(dockerfile="/home/research/evil"))

    def test_unknown_schema_version_is_refused(self):
        with self.assertRaises(spec.SpecError):
            spec.normalize(base(schema=2))

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(spec.SpecError):
            spec.normalize(base(kind="confirm"))  # arrives in 2d, not now

    def test_short_or_uppercase_sha_is_refused(self):
        for bad in ["3f1c", SHA.upper(), "z" * 40, "", None, 40 * "0" + "0"]:
            with self.assertRaises(spec.SpecError):
                spec.normalize(base(source_sha=bad))

    def test_branch_name_is_not_a_sha(self):
        # Pinning means a SHA. A ref would be mutable, which is the hole §7
        # exists to close.
        with self.assertRaises(spec.SpecError):
            spec.normalize(base(source_sha="feat/queue-forecasting"))


class TestLimits(unittest.TestCase):
    def test_mem_limit_above_host_ceiling_is_refused(self):
        with self.assertRaises(spec.SpecError):
            spec.normalize(base(mem_limit="32g"))

    def test_mem_limit_units_and_shape(self):
        self.assertEqual(spec.normalize(base(mem_limit="512m"))["mem_limit"], "512m")
        for bad in ["8G", "8gb", "8", "0g", "-8g", "8 g"]:
            with self.assertRaises(spec.SpecError):
                spec.normalize(base(mem_limit=bad))

    def test_timeout_ceiling_is_coupled_to_the_nightly_wait(self):
        # 3600 in 2a: the whole chain (timeout + build + build-lock wait +
        # handoff + setup/teardown) must fit inside JOB_HOLD_DEADLINE_S, which
        # must fit inside LOCK_WAIT_S (design D10a). Revision 6 set the deadline
        # to exactly TIMEOUT_MAX + BUILD_TIMEOUT_S, leaving nothing for either.
        # This assertion is here so the coupling breaks a test rather than a
        # night's training.
        self.assertEqual(spec.TIMEOUT_MAX, 3600)

    def test_timeout_and_cpu_ranges(self):
        for bad in [0, 59, 3601, "1800", 1800.5, True]:
            with self.assertRaises(spec.SpecError):
                spec.normalize(base(timeout_s=bad))
        for bad in [0, 0.1, 16, "4"]:
            with self.assertRaises(spec.SpecError):
                spec.normalize(base(cpus=bad))

    def test_bool_is_not_an_int(self):
        # In Python True == 1. A spec that accepts True for timeout_s would
        # store a nonsense record and pass every later type check.
        with self.assertRaises(spec.SpecError):
            spec.normalize(base(cpus=True))


class TestLaneIsDerivedNotRequested(unittest.TestCase):
    # A caller-selected lane is a caller-selected concurrency limit. The first
    # revision let `test` ask for a lane while independently allowing 22g, so
    # two light jobs could admit 44g on a 29g host -- the exact failure NC8
    # exists to prevent (design D10).
    def test_lane_field_is_not_accepted_at_all(self):
        with self.assertRaises(spec.SpecError):
            spec.normalize(base(lane="heavy"))

    def test_small_memory_derives_light(self):
        self.assertEqual(spec.normalize(base(mem_limit="4g"))["lane"], "light")

    def test_memory_above_the_light_ceiling_derives_heavy(self):
        # This is how NC8 gets a heavy job before any heavy kind exists: by
        # exercising the real admission rule instead of a flag added for it.
        self.assertEqual(spec.normalize(base(mem_limit="8g"))["lane"], "heavy")

    def test_the_ceiling_is_inclusive_and_stated(self):
        self.assertEqual(spec.LIGHT_MEM_CEILING_MB, 4 * 1024)
        self.assertEqual(spec.normalize(base(mem_limit="4096m"))["lane"], "light")
        self.assertEqual(spec.normalize(base(mem_limit="4097m"))["lane"], "heavy")


class TestTestArgs(unittest.TestCase):
    def test_absolute_path_is_refused(self):
        with self.assertRaises(spec.SpecError):
            spec.normalize(base(args={"paths": ["/etc/passwd"]}))

    def test_parent_traversal_is_refused(self):
        for bad in ["../src", "tests/../../etc", "..", "tests/.."]:
            with self.assertRaises(spec.SpecError):
                spec.normalize(base(args={"paths": [bad]}))

    def test_node_id_selection_is_allowed(self):
        eff = spec.normalize(base(args={"paths": ["tests/test_model.py::test_fit"]}))
        self.assertEqual(eff["args"]["paths"], ["tests/test_model.py::test_fit"])

    def test_pytest_args_are_an_allowlist_not_a_pattern(self):
        self.assertEqual(
            spec.normalize(base(args={"pytest_args": ["-q", "--tb=short"]}))["args"]["pytest_args"],
            ["-q", "--tb=short"],
        )
        for bad in ["--pdb", "-pno:randomly", "-p", "--co", "--maxfail=99", "-q -x"]:
            with self.assertRaises(spec.SpecError):
                spec.normalize(base(args={"pytest_args": [bad]}))

    def test_duplicate_pytest_flag_is_refused(self):
        with self.assertRaises(spec.SpecError):
            spec.normalize(base(args={"pytest_args": ["-q", "-q"]}))

    def test_k_expression_shape(self):
        self.assertEqual(spec.normalize(base(args={"k": "hazard and not slow"}))["args"]["k"],
                         "hazard and not slow")
        for bad in ["`id`", "$(id)", "a" * 201, ";rm -rf /"]:
            with self.assertRaises(spec.SpecError):
                spec.normalize(base(args={"k": bad}))

    def test_too_many_paths_is_refused(self):
        with self.assertRaises(spec.SpecError):
            spec.normalize(base(args={"paths": [f"tests/t{i}.py" for i in range(21)]}))

    def test_selftest_takes_no_args(self):
        spec.normalize(base(kind="selftest", args={}))
        with self.assertRaises(spec.SpecError):
            spec.normalize(base(kind="selftest", args={"paths": ["tests"]}))


class TestNote(unittest.TestCase):
    def test_note_is_printable_and_bounded(self):
        spec.normalize(base(note="H-0031 baseline"))
        for bad in ["x" * 501, "line\nbreak", "bell\x07"]:
            with self.assertRaises(spec.SpecError):
                spec.normalize(base(note=bad))


class TestHash(unittest.TestCase):
    def test_hash_is_over_the_effective_spec_not_the_input(self):
        # Two inputs that mean the same job must record the same hash, or the
        # audit record is faithful to typing rather than to what ran.
        a = spec.spec_hash(spec.normalize(base()))
        b = spec.spec_hash(spec.normalize(base(timeout_s=1800,
                                               mem_limit="4g", cpus=4.0,
                                               args={"paths": ["tests"]})))
        self.assertEqual(a, b)

    def test_key_order_does_not_change_the_hash(self):
        raw = {"source_sha": SHA, "kind": "test", "schema": 1}
        self.assertEqual(spec.spec_hash(spec.normalize(raw)),
                         spec.spec_hash(spec.normalize(base())))

    def test_different_memory_changes_the_hash_and_the_lane(self):
        big = spec.normalize(base(mem_limit="8g"))
        self.assertNotEqual(spec.spec_hash(spec.normalize(base())),
                            spec.spec_hash(big))
        self.assertEqual(big["lane"], "heavy")


if __name__ == "__main__":
    unittest.main()
```

Then write `host/dispatcher/spec.py`:

```python
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

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_MEM_RE = re.compile(r"^([1-9][0-9]{0,4})([mg])$")
_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./:-]{0,199}$")
_K_RE = re.compile(r"^[A-Za-z0-9_ ()\[\].:-]{1,200}$")
_NOTE_RE = re.compile(r"^[\x20-\x7e]{0,500}$")

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
```

**Verify:** `python3 -m unittest discover -s host/dispatcher/tests -p 'test_spec.py'`.

## Task 2: `store.py` — SQLite, one hash chain, atomic dequeue

`host/dispatcher/tests/test_store.py`, first. Cases, each with the reason:

| Case | Prevents |
|---|---|
| a fresh store has chain head at seq 0 and the genesis hash | an empty chain that verifies vacuously |
| `submit` writes one `jobs` row and one `SUBMITTED` event, in one transaction | a job that exists without an event, or the reverse |
| chain hashes cover `seq`, `at`, `run_id`, `kind` and payload | a reordering or a payload edit that still verifies |
| `verify_chain` fails after a payload is edited with raw SQL | a chain that only checks itself |
| `verify_chain` fails after a `jobs` row is edited with raw SQL | the projection drifting from the authority (design D7) |
| `peek(lane)` reports the head without changing state | admission taken before a state transition exists to undo |
| after one `submit` + `dequeue`, `verify_chain` still agrees | the revision-3 bug where the dequeue's event omitted the lease fields its UPDATE set, so the very first dequeue broke verification |
| `dequeue` persists `hold_started_at`/`hold_deadline_at` **in the UPDATE**, not only in the event payload, and `verify_chain` agrees immediately afterwards | the revision-7 bug: payload-only columns leave NULLs, so verification fails on the first dequeue and restart has no deadline to restore |
| adoption restores the **remaining** budget rather than a fresh one | repeated restarts extending one lock hold past `LOCK_WAIT_S` |
| `CLEANUP_BLOCKED` is non-terminal, permits only `→ FAILED`, and a job in it still shows its reservation and lock held | a terminal state that keeps resources with no path back |
| a `BUILDING` job **occupies its lane**, **renews its lease**, and is reclaimable only once every recorded resource is confirmed stopped | the revision-9 omission: `BUILDING` in `ALLOWED` and in no query, so such a job silently freed its lane and lost its lease |
| the state sets are asserted against `ALLOWED`'s keys, so a new state cannot be added without appearing in them | the same omission happening again |
| `dequeue(lane)` returns at most one job, moves it to `LEASED`, and returns the **post-update** row | a runner acting on stale lease fields |
| a second `dequeue("heavy")` returns `None` while one is `LEASED` or `RUNNING` | the lane cap being advisory |
| `lease_expires_at` is an absolute UTC instant, and `renew(run_id, owner)` extends it only for the current owner | a duration stored as an instant, and a foreign renewal |
| `renew` on a job the caller does not own returns False and changes nothing | a reclaimed job being resurrected by its old runner |
| no `LEASED → QUEUED` transition exists, and attempting it raises | the first revision's contention path, which the state table forbade |
| N threads submitting while workers dequeue produces no lost, duplicated or interleaved-corrupt rows, against a real SQLite file | the threading defect below, and a fake-runner test that would have hidden it |
| `dequeue` respects submission order | starvation by luck |
| `reclaim(now)` moves an expired-lease `LEASED`/`RUNNING` job to `FAILED` with `error_class='reclaimed'` | a crashed run stranded forever |
| `adopt(run_id, container_id)` keeps a reconciled run `RUNNING` and extends the lease | reaping a live container after a dispatcher restart |
| every illegal transition raises | a silent state machine |
| `pins` accepts new keys without a schema change | 2b/2c needing a migration |
| `queued_count_for_uid` caps a flooder | one caller filling the queue |
| `verify_chain` detects an edit to **every** column in `PROJECTED`, driven as a loop over the tuple so a column cannot be added without a test | a projection check that silently omits a field |
| specifically `lease_owner`, `lease_expires_at`, and an artifact's `path` and `bytes` as well as its `sha256` | the three gaps revision 2 left after widening the projection once |
| a `pins`, `artifacts` or `resources` row with no corresponding event is reported | a row inserted directly into a side table |
| `resources` replay is keyed by `(role, container_id)` and compares `created_at` **and** `released_at`; mutation tests cover a missing row, an extra row, a changed timestamp, and a deleted row | forced cleanup inventorying a table verification does not actually check — revision 9's test list promised this and the code did not implement it |
| a `release_resource` for a `(role, container_id)` that was never created is reported | a release record fabricated for a container that never existed |
| `admit(mem_mb)` refuses when admitted memory plus the request exceeds the budget, and releases on completion | two jobs summing past the host's RAM (design D10) |
| `admit` refuses when free disk is below the floor | scheduling into a full filesystem (design §4.5) |

Then `host/dispatcher/store.py`. The schema and the chain, verbatim; the rest is
ordinary SQL around it.

```python
"""Live state for the dispatcher: SQLite, WAL, one append-only hash chain.

`events` is the authority; `jobs` is a materialised projection maintained in
the same transaction (design D7). `verify_chain` therefore does two things:
recompute the chain, and replay it into a projection and compare. Checking only
the first would let a direct UPDATE on `jobs` pass.

Single writer by construction, and literally so: one **DB-owner thread** inside
the one dispatcher process serves every call over a queue. `sqlite3.connect()`
binds a connection to its creating thread, so sharing this object between a
scheduler thread and the socket handler would raise `ProgrammingError` -- and
serialising through one thread also serialises the hash chain for free.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

GENESIS = "0" * 64
SCHEMA = 1

STATES = ("QUEUED", "LEASED", "BUILDING", "RUNNING", "CLEANUP_BLOCKED",
          "SUCCEEDED", "FAILED", "TIMEOUT", "CANCELLED", "REFUSED")
TERMINAL = frozenset({"SUCCEEDED", "FAILED", "TIMEOUT", "CANCELLED", "REFUSED"})
# The state sets every state-dependent query must use. Revision 9 added BUILDING
# to ALLOWED and to nothing else, so a BUILDING job vacated its lane, could not
# renew its lease, and was invisible to reclaim -- three silent bugs from one
# omission. Naming the sets once is the fix; open-coding a state list in SQL is
# the bug.
ADMITTED_STATES = ("LEASED", "BUILDING", "RUNNING", "CLEANUP_BLOCKED")
LEASE_ACTIVE_STATES = ("LEASED", "BUILDING", "RUNNING")

ALLOWED = {
    None:              {"QUEUED", "REFUSED"},
    "QUEUED":          {"LEASED", "CANCELLED"},
    "LEASED":          {"BUILDING", "RUNNING", "FAILED", "CANCELLED"},
    # BUILDING exists because an unconfirmed BUILDER shutdown must reach
    # CLEANUP_BLOCKED, and LEASED had no such edge -- revision 8 specified a
    # transition this table forbade.
    "BUILDING":        {"RUNNING", "FAILED", "CANCELLED", "CLEANUP_BLOCKED"},
    "RUNNING":         {"SUCCEEDED", "FAILED", "TIMEOUT", "CANCELLED",
                        "CLEANUP_BLOCKED"},
    # Non-terminal on purpose. A job whose workload could not be CONFIRMED dead
    # still holds its training-lock descriptor and its memory reservation, so it
    # must not be terminal: the general rule is that admission lasts until a
    # terminal state, and revision 7 marked these FAILED while they kept both,
    # with nothing saying how admission ever resumed. The reaper keeps polling;
    # confirmation moves it to FAILED and releases everything; `qf force-release`
    # is the operator escape.
    "CLEANUP_BLOCKED": {"FAILED"},
}

DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
  run_id            TEXT PRIMARY KEY,
  kind              TEXT NOT NULL,
  lane              TEXT NOT NULL,
  state             TEXT NOT NULL,
  spec_json         TEXT NOT NULL,
  spec_hash         TEXT NOT NULL,
  source_sha        TEXT NOT NULL,
  source_ref        TEXT,
  image_digest      TEXT,
  submitted_by_uid  INTEGER NOT NULL,
  submitted_at      TEXT NOT NULL,
  started_at        TEXT,
  finished_at       TEXT,
  -- The outer hold deadline is PERSISTED, not kept in the runner's memory:
  -- otherwise an adopted job gets a fresh budget on every restart and repeated
  -- restarts extend one lock hold without limit (design 4.2 step 1a).
  hold_started_at   TEXT,
  hold_deadline_at  TEXT,
  attempts          INTEGER NOT NULL DEFAULT 0,
  lease_owner       TEXT,
  lease_expires_at  TEXT,
  container_id      TEXT,
  exit_code         INTEGER,
  error_class       TEXT,
  wall_s            REAL,
  rss_high_water_kb INTEGER
);
CREATE INDEX IF NOT EXISTS jobs_lane_state ON jobs(lane, state);
CREATE INDEX IF NOT EXISTS jobs_state_submitted ON jobs(state, submitted_at);
CREATE TABLE IF NOT EXISTS pins(
  run_id TEXT NOT NULL REFERENCES jobs(run_id),
  key    TEXT NOT NULL,
  value  TEXT NOT NULL,
  PRIMARY KEY(run_id, key)
);
CREATE TABLE IF NOT EXISTS resources(
  -- Every container this run created, by role. `jobs.container_id` is
  -- candidate-only, so forced cleanup and restart recovery inventory THIS
  -- rather than a label query: an ephemeral builder cannot carry a per-run
  -- label, and a
  -- container that has already stopped is invisible to `docker ps`.
  run_id       TEXT NOT NULL REFERENCES jobs(run_id),
  role         TEXT NOT NULL,          -- candidate | handoff
  container_id TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  released_at  TEXT,
  PRIMARY KEY(run_id, role, container_id)
);
CREATE TABLE IF NOT EXISTS artifacts(
  run_id TEXT NOT NULL REFERENCES jobs(run_id),
  name   TEXT NOT NULL,
  path   TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  bytes  INTEGER NOT NULL,
  PRIMARY KEY(run_id, name)
);
CREATE TABLE IF NOT EXISTS events(
  seq          INTEGER PRIMARY KEY,
  at           TEXT NOT NULL,
  run_id       TEXT,
  kind         TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  prev_hash    TEXT NOT NULL,
  hash         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id);
"""


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def event_hash(prev_hash, seq, at, run_id, kind, payload_json):
    """Chain link. `seq` is inside the digest, so a reordering is detectable.

    seq is assigned explicitly (not AUTOINCREMENT) inside the same IMMEDIATE
    transaction, precisely so it can be hashed.
    """
    material = "\n".join([prev_hash, str(seq), at, run_id or "", kind, payload_json])
    return hashlib.sha256(material.encode()).hexdigest()


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(DDL)
        self.db.execute("INSERT OR IGNORE INTO schema_meta VALUES('schema', ?)",
                        (str(SCHEMA),))

    # --- chain -----------------------------------------------------------
    def _head(self):
        row = self.db.execute(
            "SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        return (0, GENESIS) if row is None else (row["seq"], row["hash"])

    def _append(self, at, run_id, kind, payload):
        seq_prev, prev_hash = self._head()
        seq = seq_prev + 1
        pj = _canon(payload)
        h = event_hash(prev_hash, seq, at, run_id, kind, pj)
        self.db.execute(
            "INSERT INTO events(seq, at, run_id, kind, payload_json, prev_hash, hash)"
            " VALUES(?,?,?,?,?,?,?)", (seq, at, run_id, kind, pj, prev_hash, h))
        return seq, h

    def verify_chain(self):
        """Return (ok, problems). Recompute the chain, then replay it into a
        projection and compare with `jobs`."""
        problems, prev, expect = [], GENESIS, 1
        projection = {}
        for row in self.db.execute("SELECT * FROM events ORDER BY seq"):
            if row["seq"] != expect:
                problems.append(f"seq gap at {row['seq']} (expected {expect})")
            if row["prev_hash"] != prev:
                problems.append(f"prev_hash mismatch at seq {row['seq']}")
            h = event_hash(prev, row["seq"], row["at"], row["run_id"],
                           row["kind"], row["payload_json"])
            if h != row["hash"]:
                problems.append(f"hash mismatch at seq {row['seq']}")
            prev, expect = row["hash"], row["seq"] + 1
            self._replay(projection, row)
        for run_id, expected in projection.items():
            got = self.db.execute("SELECT * FROM jobs WHERE run_id=?",
                                  (run_id,)).fetchone()
            if got is None:
                problems.append(f"{run_id}: event chain has it, jobs does not")
                continue
            # Every projected column, not just state. Naming the field matters:
            # "the chain disagrees" is not an actionable report.
            for col in self.PROJECTED:
                if got[col] != expected.get(col):
                    problems.append(
                        f"{run_id}.{col}: jobs has {got[col]!r},"
                        f" chain has {expected.get(col)!r}")
            for key, value in sorted(expected["pins"].items()):
                row = self.db.execute(
                    "SELECT value FROM pins WHERE run_id=? AND key=?",
                    (run_id, key)).fetchone()
                if row is None or row["value"] != value:
                    problems.append(f"{run_id}: pin {key} disagrees")
            for (role, cid), meta in sorted(expected["resources"].items()):
                row2 = self.db.execute(
                    "SELECT created_at, released_at FROM resources"
                    " WHERE run_id=? AND role=? AND container_id=?",
                    (run_id, role, cid)).fetchone()
                if row2 is None or dict(row2) != meta:
                    problems.append(f"{run_id}: resource {role}/{cid} disagrees")
            for name, meta in sorted(expected["artifacts"].items()):
                row = self.db.execute(
                    "SELECT path, sha256, bytes FROM artifacts"
                    " WHERE run_id=? AND name=?", (run_id, name)).fetchone()
                if row is None or dict(row) != meta:
                    problems.append(f"{run_id}: artifact {name} disagrees")
            seen_res = {(r["role"], r["container_id"]) for r in self.db.execute(
                "SELECT role, container_id FROM resources WHERE run_id=?",
                (run_id,))}
            for orphan in sorted(seen_res - set(expected["resources"])):
                problems.append(f"{run_id}: resource row {orphan} has no event")
            for tbl, key in (("pins", "key"), ("artifacts", "name")):
                seen = {r[key] for r in self.db.execute(
                    f"SELECT {key} FROM {tbl} WHERE run_id=?", (run_id,))}
                for orphan in sorted(seen - set(expected[tbl])):
                    problems.append(f"{run_id}: {tbl} row {orphan} has no event")
        extra = {r["run_id"] for r in self.db.execute("SELECT run_id FROM jobs")}
        for run_id in sorted(extra - set(projection)):
            problems.append(f"{run_id}: in jobs with no event chain")
        return (not problems), problems

    # Every projected field is replayed, not just `state`. The design's claim is
    # that an edit to a projected row is detectable; a state-only comparison
    # would let an edit to spec_json, source_sha, image_digest, exit_code, a
    # timestamp, the resource high-water mark, a pin or an artifact digest pass
    # verification -- which are exactly the fields a verdict is argued from.
    # EVERY column of `jobs`, lease fields included. Revision 2 omitted
    # lease_owner/lease_expires_at and left renew() unchained, so an edit to
    # either was undetectable. The non-authoritative set is empty; if a column
    # is ever removed from here, name it and say why, because an unexplained
    # omission reads as coverage.
    PROJECTED = ("kind", "lane", "state", "spec_json", "spec_hash", "source_sha",
                 "source_ref", "image_digest", "submitted_by_uid", "submitted_at",
                 "started_at", "finished_at", "hold_started_at",
                 "hold_deadline_at", "attempts", "lease_owner",
                 "lease_expires_at", "container_id", "exit_code", "error_class",
                 "wall_s", "rss_high_water_kb")
    NON_AUTHORITATIVE = ()   # intentionally empty

    @staticmethod
    def _replay(projection, row):
        """Apply one event. Event payloads carry the values they set, so the
        projection is reconstructible without reading `jobs` at all."""
        p = json.loads(row["payload_json"])
        rid = row["run_id"]
        if row["kind"] in ("SUBMITTED", "REFUSED"):
            projection[rid] = {"pins": {}, "artifacts": {}, "resources": {},
                               **p["fields"]}
        elif rid in projection:
            job = projection[rid]
            if row["kind"] == "STATE":
                if job["state"] != p["from"]:
                    job["_problem"] = f"event says from={p['from']}, replay has {job['state']}"
                job.update(p["fields"])
            elif row["kind"] == "PIN":
                job["pins"][p["key"]] = p["value"]
            elif row["kind"] == "LEASE":
                # Assignment AND renewal both land here, so a lease edit is
                # detectable. An hour-long job renewing every ~5 min adds a few
                # dozen text rows; the alternative is a blind spot.
                job.update(p["fields"])
            elif row["kind"] == "ARTIFACT":
                job["artifacts"][p["name"]] = {"path": p["path"],
                                               "sha256": p["sha256"],
                                               "bytes": p["bytes"]}
            elif row["kind"] == "RESOURCE":
                # Keyed by (role, container_id): a run legitimately has several
                # containers per role over its life, and a release must match the
                # exact one it claims to release.
                key = (p["role"], p["container_id"])
                if p["op"] == "create":
                    job["resources"][key] = {"created_at": p["created_at"],
                                             "released_at": None}
                elif key in job["resources"]:
                    job["resources"][key]["released_at"] = p["released_at"]
```

Every mutation appends the fields it sets, `renew` included — see below. An
event kind that changes `jobs` without a payload entry for the changed column is
exactly the bug the mutation tests exist to catch.

The remaining methods (`submit`, `refuse`, `transition`, `dequeue`, `adopt`,
`reclaim`, `set_pin`, `add_artifact`, `queued_count_for_uid`, `get`, `list`)
each open `BEGIN IMMEDIATE`, write the `jobs` change and the matching event, and
`COMMIT` — with `transition` refusing anything outside `ALLOWED` by raising, and
`dequeue` shaped as:

```python
    def add_resource(self, run_id, *, role, container_id, now):
        """Record a container this run created. Transactional and chained, like
        every other mutation: revision 9 promised RESOURCE replay in the test
        list and shipped neither the method nor the event, so the table forced
        cleanup depends on was outside verification entirely."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                "INSERT INTO resources(run_id, role, container_id, created_at)"
                " VALUES(?,?,?,?)", (run_id, role, container_id, now))
            self._append(now, run_id, "RESOURCE",
                         {"op": "create", "role": role,
                          "container_id": container_id, "created_at": now})
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def release_resource(self, run_id, *, role, container_id, now):
        """Mark a resource released. Called only after Docker has POSITIVELY
        confirmed the container is stopped or absent -- the release record is a
        claim about reality, and an unconfirmed one is how the mutex leaks."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                "UPDATE resources SET released_at=? WHERE run_id=? AND role=?"
                " AND container_id=?", (now, run_id, role, container_id))
            self._append(now, run_id, "RESOURCE",
                         {"op": "release", "role": role,
                          "container_id": container_id, "released_at": now})
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def peek(self, lane):
        """The head of a lane's queue, without changing anything.

        Admission (memory budget, and the training lock for heavy) is taken
        BETWEEN peek and dequeue. The first revision dequeued first and pushed
        the job back to QUEUED when the flock failed, which the state table
        forbids -- an ordinary contention would have raised.
        """
        return self.db.execute(
            "SELECT * FROM jobs WHERE lane=? AND state='QUEUED'"
            " ORDER BY submitted_at, run_id LIMIT 1", (lane,)).fetchone()

    def dequeue(self, lane, *, owner, now, lease_expires_at, hold_deadline_at,
                max_running):
        """Atomically lease the head of `lane`. Returns the POST-update row, or
        None if the lane is full or the queue emptied under us -- in which case
        the caller releases the admission it took and re-peeks.

        `lease_expires_at` is an absolute UTC instant supplied by the caller,
        not a duration: storing the duration (as the first revision did) makes
        every lease malformed and every reclaim decision arbitrary.
        """
        self.db.execute("BEGIN IMMEDIATE")
        try:
            busy = self.db.execute(
                "SELECT COUNT(*) c FROM jobs WHERE lane=? AND state IN"
                f" ({','.join('?' * len(ADMITTED_STATES))})",
                (lane, *ADMITTED_STATES)).fetchone()["c"]
            if busy >= max_running:
                self.db.execute("COMMIT")
                return None
            row = self.db.execute(
                "SELECT * FROM jobs WHERE lane=? AND state='QUEUED'"
                " ORDER BY submitted_at, run_id LIMIT 1", (lane,)).fetchone()
            if row is None:
                self.db.execute("COMMIT")
                return None
            # The event must carry EVERY column this transaction changes.
            # Revision 3 listed only state and attempts while the UPDATE also
            # set lease_owner and lease_expires_at -- and verify_chain now
            # compares those, so the FIRST dequeue would have reported a
            # disagreement. A projection is only as good as its payloads.
            fields = {"state": "LEASED", "attempts": row["attempts"] + 1,
                      "lease_owner": owner,
                      "lease_expires_at": lease_expires_at,
                      "hold_started_at": now,
                      "hold_deadline_at": hold_deadline_at}
            self.db.execute(
                # hold_started_at/hold_deadline_at are written HERE, not merely
                # announced in the event payload. Revision 7 put them in the
                # payload only, so the columns stayed NULL: verify_chain would
                # have disagreed on the very first dequeue, and restart recovery
                # would have had no deadline to restore.
                "UPDATE jobs SET state='LEASED', lease_owner=?, lease_expires_at=?,"
                " hold_started_at=?, hold_deadline_at=?,"
                " attempts=attempts+1 WHERE run_id=? AND state='QUEUED'",
                (owner, lease_expires_at, now, hold_deadline_at, row["run_id"]))
            self._append(now, row["run_id"], "STATE",
                         {"from": "QUEUED", "to": "LEASED", "owner": owner,
                          "fields": fields})
            fresh = self.db.execute("SELECT * FROM jobs WHERE run_id=?",
                                    (row["run_id"],)).fetchone()
            self.db.execute("COMMIT")
            return dict(fresh)
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def renew(self, run_id, *, owner, lease_expires_at, now):
        """Ownership-checked lease renewal. A job may run up to TIMEOUT_MAX
        (3600 s); any sane lease is shorter, so without renewal the reclaimer
        eats live work.
        Returns False if the caller no longer owns the lease.

        Chained, like every other mutation: revision 2 updated `jobs` here with
        no event, which put lease_expires_at outside verification entirely.
        """
        self.db.execute("BEGIN IMMEDIATE")
        try:
            cur = self.db.execute(
                "UPDATE jobs SET lease_expires_at=? WHERE run_id=? AND lease_owner=?"
                f" AND state IN ({','.join('?' * len(LEASE_ACTIVE_STATES))})",
                (lease_expires_at, run_id, owner, *LEASE_ACTIVE_STATES))
            if cur.rowcount == 1:
                self._append(now, run_id, "LEASE",
                             {"fields": {"lease_owner": owner,
                                         "lease_expires_at": lease_expires_at}})
            self.db.execute("COMMIT")
            return cur.rowcount == 1
        except Exception:
            self.db.execute("ROLLBACK")
            raise
```

`reclaim(now)` is deliberately **not** a pure timer: it selects expired-lease
jobs in **`LEASE_ACTIVE_STATES`** and, for each, asks Docker about **every
container in its `resources` row** — candidate and handoff — rather than about one
labelled container, since a `BUILDING` job may have no container at all and a
stopped one is invisible to `docker ps`. Any resource alive means
adopt and renew; all confirmed absent means `FAILED` with
`error_class='reclaimed'` and the logs preserved — **unless the resource set is
empty**, in which case there is nothing to have confirmed and the job takes the
cancellation-settle path instead of a free pass. Expiry alone reaps live runs,
which on an hour-long job is a guaranteed false positive.

**Verify:** the table of cases above, all green.

## Task 3: `source.py` — bare mirror, reachability, worktree

Interface, and the behaviour each function must have. Tests use a local fixture
repository created in a temp dir with `git init` — no network, no token.

```python
class Source:
    def __init__(self, mirror_dir, remote_url, token_path): ...
    def ensure_mirror(self):
        # git init --bare; core.hooksPath=/dev/null; remote add origin <url>
        # Idempotent. Hooks are disabled on OUR repo because the objects that
        # land in it are authored by the agent (design D3).
    def fetch(self):
        # git -c credential.helper=<helper> fetch --prune origin
        #     '+refs/heads/*:refs/remotes/origin/*'
        # The token is supplied by an askpass/credential helper reading a
        # mode-0400 file. It never appears in argv, in a URL, or in a log.
    def resolve(self, sha) -> str:
        # Returns the remote-tracking ref the SHA is reachable from, or raises
        # NotPublished. Fetches once if the object is unknown, then re-checks.
        # Reachability, not mere existence: `git cat-file -e` would happily
        # accept a force-dropped commit that no human can look at.
    def add_worktree(self, sha, dest):
        # git worktree add --detach --no-checkout <dest> <sha> && git -C <dest> checkout
        # No submodule init. Never runs any command inside <dest> afterwards.
    def remove_worktree(self, dest): ...
```

Test cases:

| Case | Prevents |
|---|---|
| `ensure_mirror` is idempotent and sets `core.hooksPath=/dev/null` | an agent-authored hook running as `qfd` |
| `resolve` raises `NotPublished` for a commit present in the object store but not reachable from any remote ref | running a commit no human can inspect (design D3) |
| `resolve` returns the ref name for a reachable commit, and it is recorded | a verdict with no URL behind it |
| `resolve` raises for an unknown SHA after one fetch attempt | an unbounded fetch loop on a typo |
| `add_worktree` produces the exact tree of that SHA and leaves the mirror's HEAD alone | cross-contamination between concurrent runs |
| `remove_worktree` then `add_worktree` at the same path succeeds | stale `worktrees/` metadata wedging the run directory |
| the token file's contents never appear in any captured command line | the Phase 1 §7.2 rule, carried forward |

## Task 4: `image.py` and the trusted image

`host/dispatcher/trainer-env.Dockerfile` — the shape is inherited from
`trainer/Dockerfile`; the provenance and three details are new (design D8, D11).

```dockerfile
# TRUSTED. Root-owned, read only from the trusted checkout. The copy of
# trainer/Dockerfile that travels with qf-research is ignored entirely
# (auto-research-phase1-design.md §6).
#
# Base pinned by digest: `phase2-setup.sh pin-base` prints the line to paste.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:REPLACE_ME

# LightGBM needs the OpenMP runtime; the -slim base omits it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*

# The environment must NOT live under /app/trainer: that path is a read-only
# mount of the research worktree at runtime and would shadow .venv (D8).
ENV UV_PROJECT_ENVIRONMENT=/opt/qfenv
WORKDIR /opt/qfbuild
# Only the human-promoted manifests. The build context contains exactly these
# two files plus this Dockerfile -- image.py asserts it.
COPY pyproject.toml uv.lock ./
# --locked asserts lock and manifest agree (--frozen would merely trust the
# lock). --no-install-project keeps a [build-system] table from ever executing.
RUN uv sync --locked --no-install-project

# The in-container identity. gid 10001 is group qfrun on the host, which qfd
# joins so it can hand `out/` over by group (design §4.4).
RUN groupadd -g 10001 qfrun && useradd -u 10001 -g 10001 -M -s /usr/sbin/nologin qfrun

ENV PATH=/opt/qfenv/bin:$PATH \
    PYTHONPATH=/app/trainer \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    TMPDIR=/tmp
WORKDIR /app/trainer
# No ENTRYPOINT: the dispatcher supplies an absolute interpreter path as argv.
# `uv run` at runtime would want to re-resolve, which needs write access and,
# on drift, network -- neither of which the sandbox has.
```

`env/pyproject.toml` and `env/uv.lock` are **copies of the trainer's current
manifests, promoted by a human**. Copy them in this task and record in
`host/README.md` that refreshing them is a reviewed act with a diff, never a
sync.

`image.py`:

```python
CONTEXT_FILES = ("Dockerfile", "pyproject.toml", "uv.lock")

def content_key(trusted_dir) -> str:
    # sha256 over: base image digest parsed out of the Dockerfile FROM line, the
    # then the bytes of the Dockerfile, pyproject.toml and uv.lock, each
    # length-prefixed so concatenation is unambiguous. First 16 hex.

def build_context(trusted_dir, tmpdir) -> str:
    # Copies exactly CONTEXT_FILES out of trusted_dir into tmpdir, then
    # ASSERTS that the resulting directory listing equals CONTEXT_FILES.
    # This assertion is NC12's mechanism, not a sanity check: it is what makes
    # "no qf-research file participated in the build" a fact rather than a
    # claim.

def ensure_image(trusted_dir, runner) -> tuple[str, str]:
    # Returns (tag, image_digest). Builds only if `docker image inspect` on the
    # tag misses. image_digest is the image config Id -- locally built images
    # have no RepoDigests (design D11).
```

Test cases:

| Case | Prevents |
|---|---|
| `content_key` changes when any of the three files changes by one byte | a stale image silently reused after a manifest edit |
| `content_key` changes when the pinned base digest changes | a base-image swap going unrecorded |
| after `ensure_image`, `docker image inspect <returned id>` succeeds locally | handing the sandbox an id Docker cannot run. Trivially true for the classic builder, kept because it is exactly what breaks if anyone switches to a driver whose result stays in a build cache (design D10) |
| `content_key` is stable across repeated calls and independent of file mtimes | spurious rebuilds |
| length-prefixing: moving a byte from the end of `pyproject.toml` to the start of `uv.lock` changes the key | a concatenation collision |
| `build_context` on a directory containing extra files still yields exactly three, and raises if a required one is missing | the poisoned-manifest path (NC12) |
| `build_context` refuses a `trusted_dir` whose realpath is outside the trusted root | NC10 |
| the Dockerfile's `FROM` is rejected if it carries no `@sha256:` digest | an unpinned base |

## Task 5: `sandbox.py` — the flag set, as data

The D2 flag set is a control, so it is built by a pure function and asserted by
tests rather than typed into a shell string.

*(As shipped this function is `docker_create_argv`, paired with
`docker_start_argv`; see the amendment after the flag list for why the single
`docker run` verb had to be split.)*

```python
def docker_run_argv(*, image_ref, run_id, spec_hash, kind, src_mount, out_mount,
                    entrypoint_argv, mem_limit, cpus, uid_gid="10001:10001",
                    tmpfs_size="1g", extra_ro_mounts=()) -> list[str]:
    """Construct the argv for one sandboxed run. Pure; no filesystem access.

    Every flag below is part of the boundary (auto-research-phase2-design.md D2).
    Absent flags are the failure mode this function exists to prevent, so the
    tests assert presence, not absence.
    """
```

`image_ref` must be an inspected image **ID** (`^sha256:[0-9a-f]{64}$`), not a
tag: a tag can be re-pointed between `ensure_image` and `docker run`, and the
recorded `image_digest` would then describe something other than what ran.

It emits, in order: `docker create --rm`, `--name qf-<run_id>-<role>`, `--label
qf.run_id=`, **`--label qf.role=candidate|handoff`**, `--label qf.spec_hash=`,
`--label qf.kind=`, `--log-driver none`,
`--network none`,
`--read-only`, `--cap-drop ALL`, `--security-opt no-new-privileges`,
`--user <uid_gid>`, `--pids-limit 512`, `--memory <m>`, `--memory-swap <m>`,
`--cpus <c>`, `--oom-score-adj 500`, `--tmpfs /tmp:rw,nosuid,nodev,size=<s>`,
`-v <src>:/app/trainer:ro`, `-v <out>:/out:rw`, each `extra_ro_mounts` as
`-v <src>:<dst>:ro`, then `image_ref` and `entrypoint_argv`.

**Amended after review (round 6): `create`, not `run`.** The plan said `docker
run`, and one command that creates and starts cannot tell the dispatcher when
the container began to exist — `Popen` returning says the local CLI was spawned.
Until the daemon binds the name, `docker inspect` answers "No such object",
which every confirmation path treats as a positive absence, so a sweep in that
window releases the resource row and the training mutex over a container that is
about to start. The builder therefore emits a `docker create` argv (all the flags
above, unchanged) and `docker_start_argv(run_id, role)` emits `docker start
--attach qf-<run_id>-<role>`; the create is synchronous, so its exit status is
the acknowledgement, and it is the last thing the phase gate does. See
`host/README.md` for the two consequences (`{{.State.Status}}` as the probe, and
`docker rm -f` closing the kill escalation) and NC16 for the live check.

Two `error_class` values come with it, and they differ in what the operator is
told rather than in what is held: `container_start_failed` (the create was
refused and the name reads absent) and `start_unconfirmed` (anything else).
Neither releases its resource row, because **a row may be released without
confirmation only when Docker was never asked about that name** — a daemon can
complete a submitted request after the client that submitted it has died, so an
absence read back after a failed create is a reading, not a proof.

Nor is confirmation itself allowed to believe the first absence, which is only a
reading for the same reason: `KILL_CONFIRM_S` bounds how long it polls, not how
long absence must hold. So the ambiguity is persisted *before* the create is
issued (`store.absence_settles_pin`, one pin per role) and taken down only by
Docker's answer; `Runner._account_for` — now the only place a row is
released on an inspection — keeps removing as well as probing and requires the
absence to hold for `BUILD_SETTLE_S`, with any sighting pushing the instant
forward; and `Store.reclaim` counts an unsettled absence as unknown. The window
takes `BUILD_SETTLE_S` because this is the same question as an abandoned build —
daemon-side work whose client is gone — and inherits the same documented D10
residual rather than pretending to a proof.

The create being synchronous also means it *spends the hold*: expiry is
re-checked between the create and the `docker start`, and every wait budget is
measured after the create rather than before it (the handoff has no deadline
watcher, so an over-granted wait there just runs past the hold).

Two rules follow from the same fix and are worth stating separately, because
they are about recovery rather than about the create: **startup recovery hands
back a hold only when something must keep asking** (a recorded inventory means
forced cleanup, whatever the state — nothing in a restarted process can resume a
run whose `docker start --attach` client died with the old one, and an undriven
hold is renewed for ever by `reclaim`), and **the persisted hold deadline
outranks every probe outcome** (past `hold_deadline_at`, a job with a recorded
inventory goes to CLEANUP_BLOCKED whether Docker said alive, unknown, or
absent-but-unsettled; only a settled absence keeps its cleaner
release-and-fail). Checking that inside the *alive* branch alone left the unknown
case RUNNING, which is a stall with no escape: `resolve_blocked` lists
CLEANUP_BLOCKED only and `force-release` refuses anything else, so neither the
automatic path nor the operator could act. Whether the container is `running` or
merely `created` does not matter to either rule: the kill escalation ends in
`docker rm -f`, so both confirm.

`add_resource` also refuses outside `PHASE_ACTIVE_STATES` (= `RUNNING`).
`reclaim` can move a run out from under a phase that already holds its gate: to
FAILED when the inventory was momentarily and legitimately empty (the candidate
exited and `--rm` took its container), or to CLEANUP_BLOCKED when it never had
one. A row inserted after either means a real container attached to a job whose
reservation and lane are freed and which nothing looks at again — `expired`
covers lease-active states only, `resolve_blocked` covers CLEANUP_BLOCKED only,
and in the CLEANUP_BLOCKED case the confirmation already running releases the new
row as "gone" because the name is not bound yet. `not terminal` is the wrong test;
CLEANUP_BLOCKED is not terminal and is precisely where no new workload may
appear. Refusing the record makes it unreachable, because containers are recorded
before they can exist. The phase takes the revoked path.

The ambiguity is pinned by `_record_container`, before the row, for the same
reason: a recorded name whose container does not exist yet reads exactly like one
whose container was removed, and a confirmation pass holds no guard. What it pins
there is `ABSENCE_NOT_YET_ISSUED`, not an instant — an instant is a bet that a
request in flight completes within a window, and before the request is issued
there is nothing to bet on. Otherwise a phase stalled past BUILD_SETTLE_S (with
its renewer stalled too) becomes a "settled absence" and has its row released
just before it issues the create.

The pin comes down on the **answer**, not on the asking. Writing the instant
"immediately before the request goes out" is still before it, so a thread
descheduled between the two statements reproduces the same defect in a narrower
window — and a window is not a fix. Exit 0 clears it; a non-zero exit or a
timeout replaces it with `now + BUILD_SETTLE_S`, because those mean the request
*was* issued.

Because the sentinel is immune to elapsed time, it needs an OWNER, and losing
one is a permanent stall: it would refuse every absence for ever — cleanup could
never confirm, the job would sit in CLEANUP_BLOCKED, and the lock, lane and
reservation would stay held until an operator ran `force-release`. Three ways to
lose it, so three owners, all converting to `now + BUILD_SETTLE_S` through
`Store.settle_unissued_creates`:

- **the phase abandons the create.** `Runner._unacked_create` wraps the record
  *and* the create in both phases. A finalizer rather than more `except` clauses:
  `subprocess.run` raises `OSError` when a fork or exec never reached the daemon,
  a DB call can raise, and the next exception this code meets has not been
  written yet — enumerating them means the next one is a stall. It must not
  raise, since it runs with the real diagnosis in flight.
- **the conversion fails.** `Runner.retry_unsettled`, drained by the reaper each
  pass ahead of `resolve_blocked`. Swallowing the failure is not accepting it:
  nothing forces a restart, so a DB failure that clears a second later would
  leave the pin ownerless for the life of the daemon. The queue is in memory
  because the store is what failed; it empties only on success, which includes
  "nothing left to convert".
- **the process dies with the pin up.** `Recovery._settle_unissued`, before the
  run's first confirmation: a restart is the one moment when "no phase can issue
  this create" is true of every role at once.

An instant rather than a clear (no owner can tell "never asked" from "asked,
and the answer died with the client", and the daemon may still bind the name);
starting *now* (the time qfd spent down is not time anything was watching); and
never touching an instant already running, or a retry or crash loop would extend
the window for ever.

Relatedly: `resolve_blocked` no longer overwrites `error_class` with
`reclaimed_after_block`. The terminal CAUSE survives and the resolution is a pin
(`unblocked_at`).

`force-release` gained a second pass. A positive "live" from Docker refuses
always; an **unknown** is refused on the first call, which revokes the hold and
freezes the inventory, and accepted on the second — the flag can override
Docker's silence, but not against an inventory that could still have grown while
nobody could see it. With no registered hold there is no phase gate to win, so
one call is enough.

Test cases:

| Case | Prevents |
|---|---|
| every required flag is present, and `--memory-swap` equals `--memory` | swap turning a memory cap into a thrash |
| no `--env-file`, no `-e DATABASE_URL`, no `/var/run/docker.sock` mount, ever | the compose habits of `docker-compose.yml` leaking in |
| a relative mount path raises | a mount resolved against the daemon's cwd |
| a mount destination outside `/app/trainer`, `/out` or an allowlisted extra raises | a job mounting over `/opt/qfenv` |
| `entrypoint_argv` is passed as separate argv elements, never joined | argv-to-shell collapse |
| the `test` entrypoint is `/opt/qfenv/bin/python -m pytest -p no:cacheprovider …` and `-p` cannot come from the spec | plugin loading from an untrusted tree |
| `--user` defaults to `10001:10001` and cannot be `0:0` | root in the container |
| a `mem_limit` above the ceiling raises here too | a bypass of `spec.py` by a later caller |
| a tag or bare name as `image_ref` raises; only `sha256:<64 hex>` is accepted | a tag race between the recorded digest and the started image |
| `--log-driver none` is always present | a capped log file while Docker's own log store grows unbounded (design §4.5) |
| **both** the candidate and the handoff invocation carry `qf.run_id` *and* `qf.role`, asserted by constructing each and inspecting the argv | revision 7 labelled only the candidate, so a label-based "all containers stopped" check could pass while the handoff still ran — and forced cleanup depends entirely on that inventory |

## Task 6: `qfd.py` and the `qf` client

`qfd.py` is the only stateful piece. Structure:

- **Startup.** Read config from the environment set by the unit
  (`QFD_TRUSTED_DIR`, `QFD_STATE_DIR`, `QFD_RUNS_DIR`, `QFD_SOCKET`,
  `QFD_REMOTE`, `QFD_TOKEN_FILE`, `QFD_LOCK_FILE`, **`QFD_INTENT_DIR`**,
  `QFD_BUILD_LOCK`, `QFD_JOB_HOLD_DEADLINE_S`, `QFD_KILL_CONFIRM_S`,
  `QFD_ADMIN_SOCKET`, `QFD_ADMIN_UID`, `QFD_STOP_TIMEOUT_S`,
  `QFD_BUILD_SETTLE_S`) — this list has now twice claimed coverage the
  enumeration did not have, so it is written to be checked against the unit file
  rather than read. **`QFD_ADMIN_UID`** is the numeric deploy uid authorised on
  the admin socket alongside root: "the deploy user" was named in prose with no
  configuration or discovery source, which is not an access-control rule anyone
  can enforce. Startup refuses to run if it is unset or does not resolve. Resolve every trusted path
  with `os.path.realpath` and refuse to start if one falls outside
  `QFD_TRUSTED_DIR` (NC10, at startup as well as per job). Open the store,
  `verify_chain` and log the result, `ensure_mirror`, reconcile against Docker
  by label, bind both sockets: `client/sock` `chown`ed `qfd:qfclient` mode 0660 inside a
  `0750 qfd:qfclient` subdirectory, and `admin/sock` `chown`ed `qfd:qfheavy` mode
  0660 inside a `0750 qfd:qfheavy` one. The **parent** stays `0711` so each group
  can traverse to its own socket without being able to list the other's.
- **Socket.** One connection, one JSON request line, one JSON response line.
  **Two sockets.** The client socket (`0660 qfd:qfclient`) carries `ping`
  (dispatcher commit, schema version, lane occupancy, admitted memory, free disk,
  and any `CLEANUP_BLOCKED` stall), `submit`, `status`, `list`, `cancel`,
  `verify-chain`,
  `trusted-paths` (each trusted path with its realpath and SHA-256 — the
  live half of NC10). The **admin socket** (`0660 qfd:qfheavy`, which `research`
  is not in) carries `force-release` and nothing else, and refuses any peer uid
  outside {root, deploy}. Caller uid comes from `SO_PEERCRED` on both — which is
  how the refusal is enforced on the admin socket and how the audit record is
  written on either, two purposes that revision 8 conflated into one.
- **Threading model.** One **DB-owner thread** (every store call goes through
  it over a queue), one socket-accept thread, and a worker pool of three: two
  light, one heavy. Not one blocking thread per lane — that cannot deliver two
  light workers, and it would share a thread-bound SQLite connection.
- **One admission sequence, in this order, and no other.** Revisions 3 and 4
  each specified a different order in two different paragraphs, which between
  them recreated the 22+2 GB deadlock and left an unleased job being built for.
  The sequence is:
  1. `peek(lane)` — read-only; no state change yet.
  2. **Scan `$QFD_INTENT_DIR` for markers** — the **drain gate**, and
     deliberately *not* a lock. Markers are per-invocation
     (`nightly.<pid>.<epoch>.intent`), so there is no well-known name to race
     over and no invocation can delete another's declaration. Any **live** marker
     (PID exists, deadline in the future) means admit nothing, sleep, re-peek. A
     **stale** one (dead PID or expired deadline) is logged loudly, unlinked and
     ignored. An **unreadable or malformed** one fails **closed** — treated as
     live intent and alarmed, since a file that cannot be parsed cannot be shown
     to be stale — with mtime beyond `LOCK_WAIT_S + QFD_MARKER_STALE_MARGIN_S`
     (900) as the eventual escape hatch, so corruption delays the loop instead of
     ending it. Revision 7 said "plus margin" without naming or configuring
     one. Revision 5 made this gate a
     reader/writer `flock`, which inherited exactly the barging it existed to
     stop: nightly queued `LOCK_EX` on the gate while workers took `LOCK_SH`, so
     a second worker could barge past the queued nightly through a gate the first
     worker was momentarily holding. A file's existence is visible to every
     reader in every order — nothing to contend for, nothing to barge (D10a).
  3. **`open()` the training lock afresh** and take `LOCK_SH|LOCK_NB` (light) or
     `LOCK_EX|LOCK_NB` (heavy) on **that descriptor**, which is stored in the
     job's runtime record and closed only at terminal cleanup. `LOCK_NB` is not
     optional anywhere in this sequence: a worker that blocks while holding
     anything turns a momentary hold into a long one. `flock` ownership is per
     open file description — a descriptor shared between workers loses the lock
     the moment its first user closes it, confirmed by experiment.
  4. Charge **one** reservation of `max(mem_limit, IMAGE_BUILD_MEM_MB)` against
     `ADMITTED_MEM_BUDGET`, plus the disk floor against
     `OUT_QUOTA + ARTIFACT_CAP`. One reservation covers both the build and the
     run, because the container does not exist during the build — charging both
     is what made a 22 GB job with a cold cache permanently unadmittable.
  5. (Nothing to release — the gate was read, not held. Revision 5's version
     acquired a lock here, which is what made barging possible.)
  6. `dequeue` → `LEASED`. Everything contended has already been acquired, so
     there is no `LEASED → QUEUED` and no defer state (design §4.2).
  7. Build if needed (next bullet), then run.
  A lost race at step 6 releases the lock, the reservation and the descriptor,
  and re-peeks. Contention never produces a state transition.
- **One hard outer deadline, enforced.** `QFD_JOB_HOLD_DEADLINE_S` (7800) runs
  **from the training lock's acquisition to its descriptor's close**, covering
  worktree setup, every wait, the build, the run, the handoff, hashing and
  cleanup, and it is **read from the database, not the clock** — `hold_deadline_at`
  is persisted at dequeue, so an adopted job resumes its remaining budget instead
  of receiving a fresh one.
  **On expiry the runner starts forced cleanup; it does not release the lock.**
  It kills, then polls `docker inspect` for **every container recorded in
  `resources` for that run — candidate and handoff*** — until Docker positively
  reports each stopped, plus (if a build was in flight) no live `docker build`
  client and `QFD_BUILD_SETTLE_S` elapsed, and only then closes the training
  descriptor. The inventory is the `resources` table rather than a label query,
  because a stopped container is invisible to `docker ps`. A subprocess timeout on `docker kill`
  proves the CLI stopped waiting, not that the workload died; closing the
  descriptor on that basis hands the mutex to nightly while live work continues —
  the failure the mutex exists to prevent, reached through the mechanism meant to
  bound it. If confirmation does not arrive within `QFD_KILL_CONFIRM_S` (300), the
  job moves to the **non-terminal** `CLEANUP_BLOCKED` state with
  `error_class=kill_unconfirmed`, **retaining its descriptor and its
  reservation**, and the dispatcher alarms and stops admitting. Revision 7 called
  that state `FAILED`, which contradicted the rule that admission lasts until a
  terminal state and left no answer to "what if Docker confirms death at second
  301". The contract:
  - a **reaper** re-polls every `QFD_REAP_INTERVAL_S` (60);
  - on confirmation → `FAILED`, descriptor closed, reservation released,
    admissions resume by themselves;
  - while any `CLEANUP_BLOCKED` job exists there are **no admissions**, and `qf
    status`/`ping` report it as the reason;
  - **`qfadmin force-release <run-id> --i-have-verified-nothing-is-running`** is
    the operator escape, on a **separate admin socket**
    (`/run/qf-dispatch/admin.sock`, `0660 qfd:qfheavy`) — *not* the client
    socket, whose group contains `research`. Revision 8 put it there, which let
    the untrusted agent assert it had verified shutdown and release the mutex over
    live work; `SO_PEERCRED` records who called, it does not authorise them.
    Still recorded as an event with the caller's uid, because audit and
    authorisation are both needed, separately.
  A skipped nightly run is recoverable; a released lock over live work is not.
  Revision 5 published
  a `HOLD_CEILING_S` *formula* (`timeout_s + build + kill grace`) and called it a
  ceiling, but it omitted the wait for `build.lock`, the handoff timeout, and
  setup and teardown — so the number the nightly wait was designed against was an
  estimate wearing a constant's clothes. The per-phase bounds below still exist,
  to make the deadline unlikely to be reached rather than to define it.
- **Startup preconditions, all fail-closed, covering both shared filesystem
  objects.** Revision 5 validated only the training lock, so a missing or
  mis-permissioned intent directory would have silently restored starvation. The
  dispatcher refuses to start if: `QFD_LOCK_FILE` is missing or not writable by a
  group `qfd` belongs to; `QFD_INTENT_DIR` is missing, is not `2770
  root:qfheavy` (the setgid bit included — see Task 7), or is not readable **and**
  writable by `qfd` (it must be able to
  unlink a stale marker); the intent directory the **nightly script** resolves is
  not the same one by device and inode;
  the root-owned cron-migration marker `/etc/qf-dispatch/lock-migrated` is
  absent (design D5 — an un-migrated cron entry locks a *different inode*, which
  is no mutex at all); or `QFD_RUNS_DIR` is not group-`qfclient` traversable.
- **Startup resource reconstruction, before any worker starts.** Admitted memory
  and the flock are both process-local, so a restart must rebuild them from what
  is actually running:
  0. **Enumerate every non-terminal job in SQLite** — `LEASED`, `BUILDING`,
     `RUNNING`, `CLEANUP_BLOCKED` — and drive recovery from that list, not from
     `docker ps`. Revision 8 started from live containers, so a
     `CLEANUP_BLOCKED` job whose workload died while `qfd` was down was never
     discovered: it stayed `CLEANUP_BLOCKED`, and the no-admissions rule stopped
     the loop **permanently**.
  1. For each, re-acquire the lane-appropriate lock on a fresh descriptor
     **before any cleanup**, and re-charge **the job's original logical
     reservation**, `max(mem_limit, IMAGE_BUILD_MEM_MB)` from its stored spec —
     not the live container's `HostConfig.Memory`. Revision 9 preferred the
     container's own cap, which undercharges a `BUILDING` job badly: a 22 GB job
     whose only live container is its 2 GB builder would come back charged 2 GB,
     and the budget would then admit work the real reservation excluded. Where a
     live candidate's cap **exceeds** the stored reservation, take the larger and
     log the discrepancy; where several resources are unexpectedly live at once,
     treat it as an invariant violation, clean them up, and hold admissions until
     that completes. If a nightly incumbent blocks acquisition, go to the `mutex_lost`
     kill-and-confirm path.
     Step 1 covers **both lanes** — `LOCK_SH` for a light job, `LOCK_EX` for a
     heavy one — because revision 4 re-acquired locks for heavy orphans only,
     leaving an orphaned light container running with no `LOCK_SH` at all while
     nightly could take `LOCK_EX`. (Revisions 7–8 stated this twice, once here and
     once as a separate step; it is one step.)
  2. **Restore each adopted job's remaining hold budget from
     `hold_deadline_at`**, and if it has already passed, run forced cleanup —
     kill confirmation included — while still holding the lock from step 1.
     Revision 6 kept the deadline in the runner's memory only, so every restart
     handed the job a fresh budget and repeated restarts could hold the lock
     indefinitely.
  3. **Resolve `CLEANUP_BLOCKED` and `BUILDING` jobs — and an empty inventory is
     NOT a confirmation.** Where a job has recorded containers, "every container
     confirmed stopped" is a real check and moves it to `FAILED` with its lock and
     reservation released; that is what unsticks a loop whose workload died during
     the outage. Where a job has **no** recorded containers the same sentence is
     *vacuously true*, and acting on it would release a `BUILDING` job — which
     under the classic builder owns no container of ours at all — the instant a
     restart found it. So a `BUILDING` job **retains** its reconstructed lock and
     reservation and goes through the same cancellation-settle procedure as any
     abandoned build (no live `docker build` client, then
     `QFD_BUILD_SETTLE_S`) before it may become `FAILED`. Anything still alive
     resumes the reaper.
     The invariant is general — *confirmation over an empty set is not
     confirmation* — and applies to `reclaim` and forced cleanup as well. Every
     such check asserts there was something to inspect before it believes its own
     answer.
  4. Only then start the worker pool.
  `ExecStopPost` stops labelled containers on a clean shutdown, so an ordinary
  restart has nothing to re-adopt and the window does not arise.
- **Runner.** Create the run directory (`out/` as `qfd:qfrun` mode 2770,
  `artifacts/` as `qfd:qfclient` mode 0750), resolve the SHA to a ref, add the
  worktree, **ensure the image at step 7 — after the lease, never before
  admission** (the earlier wording here said the opposite and contradicted the
  sequence above), build the argv from the inspected
  image ID, `subprocess.Popen` with stdout/stderr through a **bounded writer**,
  sample the container's cgroup `memory.current` every 5 s for a high-water
  mark, sample `out/` size every 2 s against `OUT_QUOTA`, renew the lease every
  `lease_s / 3`, enforce `timeout_s` with `docker stop -t 10` then `docker kill`
  — each Docker CLI call under its own subprocess timeout, so a hung daemon
  cannot extend the hold past the deadline — run the handoff container, write `result.json`, record
  artifacts and the terminal state, remove the worktree, release the admission.
- **Image build: after the lease, under its own lock, with a re-check and a
  timeout.** The build happens at step 7, *after* the job is `LEASED`, because
  building for a `QUEUED` job would need a `QUEUED → FAILED` edge the state table
  does not have. It takes `LOCK_EX` on the dispatcher-private `build.lock` and
  **re-checks the content key under it** — two light workers can miss the same
  key at once, and both hold only `LOCK_SH` on the training lock, so nothing else
  would stop them building twice. **Builds use the classic builder:**
  `DOCKER_BUILDKIT=0 docker build --memory <IMAGE_BUILD_MEM_MB>m --force-rm -t
  qf-trainer-env:<key> <context>`. Classic honours the build-time resource flags
  BuildKit ignores, so each `RUN` step is capped; the image lands in the local
  store, so `ensure_image` can inspect the tag and hand the sandbox a runnable id;
  and there is no builder container, no `moby/buildkit` image inside the trusted
  boundary, and nothing extra to pin, provision or inventory. Design D10 records
  why three revisions of buildx machinery were abandoned and what confirmation
  strength was given up with them.
  Each attempt **opens its own descriptor** on `build.lock` and waits at most
  `BUILD_LOCK_WAIT_S` (900), and **the build phase's deadline includes that
  wait** — otherwise one timed-out build lets the next job wait 1800 s and then
  build for another 1800. `BUILD_TIMEOUT_S` (1800) bounds the build itself. The
  job is in `BUILDING` throughout, so failure, lock-wait expiry or timeout is
  `BUILDING → FAILED` with `error_class` in `{image_build_failed,
  image_build_timeout, image_build_lock_timeout}`. No separate memory charge:
  step 4's `max()` reservation already covers it.
- **Abandoning a build is the one release not backed by inspection.** The
  `docker build` client is our own child, so its death is a `waitpid`; the daemon
  cancels a build when its client disconnects; `--force-rm` removes intermediate
  containers on failure as well as success; and the runner then waits
  `BUILD_SETTLE_S` (30) before releasing the training descriptor. Daemon-side
  build work is not enumerable, so this rests on documented behaviour rather than
  on a container inspection — accepted deliberately (design D10), bounded by
  `--memory`, and the reason build abandonment does **not** route through
  `CLEANUP_BLOCKED`: an operator gate on every abandoned build would stop the loop
  far more often than the residual justifies.
- **Bounded log capture.** Each stream is written through a writer that stops at
  `LOG_CAP` (16 MiB), appends a truncation marker, and kills the container with
  `error_class=log_overflow`. Docker's own driver is `none` (Task 5), so the
  dispatcher's cap is the only place the bytes land.
- **Handoff, in four steps, because ownership is the whole difficulty.** uid
  10001 cannot write into a `0750 qfd:qfclient` directory, and anything it
  created there would be owned by 10001 — the original problem one level down.
  So:
  1. `qfd` **pre-creates** each allowlisted destination in `artifacts/` as
     `qfd:qfclient` mode `0660`, empty.
  2. A second container runs the trusted `handoff-inside.sh` as `10001:10001`
     **with `--group-add <qfclient gid>`** and `/out` plus `/artifacts` mounted
     read-write. The candidate's own container never gets `--group-add`; only
     this one does.
  3. The script copies content **into the pre-created files**, never creating
     new ones, and **refuses any source that is not a regular file** — a symlink
     would read outside the mount and a FIFO would block the copy forever, which
     is also why this container carries its own `HANDOFF_TIMEOUT_S` (120).
  4. `qfd`, still the owner, hashes each file, records name/path/size/digest as
     an `ARTIFACT` event, and drops the mode to `0640`.
  The same uid in step 2 is the point: a hostile candidate can leave
  `predictions.parquet` mode 0600, which `qfd` — owner of the directory but not
  the file — could neither read nor chmod.
  **A failed handoff fails the job.** A candidate that exited 0 but whose
  artifacts trusted code could not collect must not read `SUCCEEDED`. The job
  becomes `FAILED` with `error_class` in `{handoff_bad_type, handoff_timeout,
  handoff_missing_artifact, handoff_oversize}`; the candidate's own exit code
  stays in `exit_code` and the handoff's goes to `result.json`, so triage can
  still tell "the experiment failed" from "the collection failed". Disk
  accounting charges `OUT_QUOTA + ARTIFACT_CAP`, since both copies exist at
  once.
- **Refusals** are terminal states with a reason, recorded as a `REFUSED`
  event. A refusal is never a crash: the caller gets the reason and the record
  keeps it.

`qf` (client) supports:

```
qf ping
qf submit --kind test --sha <40hex> [--path tests/test_model.py]
         [-k EXPR] [--flag -q] [--timeout 1800] [--mem 8g] [--cpus 4]
         [--note "..."] [--wait]
qf status <run-id> [--json]
qf list [--state QUEUED] [--limit 20]
qf cancel <run-id>
qf verify-chain
qf trusted-paths
qf logs <run-id>          # reads the file directly; the socket is control-only (D9)
```

`--wait` polls `status` until terminal and exits with the job's exit code, so a
human or a script gets a normal shell exit without inventing a streaming
protocol.

`tests/test_protocol.py` runs the server in-process against a socket in a temp
directory with a **fake runner** (no Docker, no git):

| Case | Prevents |
|---|---|
| a valid `submit` returns a run id and the job reaches `QUEUED` | — |
| an invalid spec returns `{"ok": false, "error": …}` with the `SpecError` message and records `REFUSED` | a refusal that leaves no trace |
| a payload claiming a different uid is ignored; `SO_PEERCRED` wins | uid spoofing over the socket |
| a request over 64 KiB, or without a newline, is rejected | an unbounded read |
| malformed JSON gets an error response, and the daemon survives | a one-line denial of service |
| an unknown op is refused by name | silent op growth |
| `submit` beyond the per-uid queued cap is refused | queue flooding |
| `trusted-paths` reports realpaths under the trusted root, with digests | NC10's live half |
| two concurrent `submit`s get distinct run ids | a run-id collision clobbering a directory |
| `cancel` on a `QUEUED` job is honoured; on a terminal job it is refused | a cancel that pretends |
| `force-release` is absent from the client socket's op table entirely, and on the admin socket is refused for any peer uid outside {root, deploy} | the revision-8 regression: an escape hatch reachable by the untrusted user |
| `force-release` without the long flag is refused, and with it records an event carrying the caller's uid | an escape hatch that leaves no trace |
| while a `CLEANUP_BLOCKED` job exists, `submit` still succeeds but nothing is admitted, and `ping`/`status` name it as the reason | a silent stall that looks like an idle dispatcher |
| a `submit` whose `mem_limit` exceeds the remaining budget is **queued**, not refused | contention reported as invalidity |
| the store is exercised from several threads at once against a real file, with workers dequeuing concurrently | the thread-bound-connection defect, which a fake runner alone would hide |

Run-id format: `<kind>-<YYYYmmddTHHMMSSZ>-<sha[:12]>-<seq>`, with `seq` from the
event chain, so ids sort chronologically and never collide.

## Task 7: units and `phase2-setup.sh`

`qf-dispatch.service`:

```ini
[Unit]
Description=Queue-forecasting trusted experiment dispatcher
After=docker.service
Requires=docker.service

[Service]
Type=simple
User=qfd
Group=qfd
SupplementaryGroups=docker qfrun qfclient
ExecStart=/usr/bin/python3 /srv/queue-forecasting/tools/queue-forecasting/host/dispatcher/qfd.py
Environment=QFD_TRUSTED_DIR=/srv/queue-forecasting/tools/queue-forecasting/host/dispatcher
Environment=QFD_STATE_DIR=/var/lib/qf-platform
Environment=QFD_RUNS_DIR=/var/lib/qf-runs
Environment=QFD_SOCKET=/run/qf-dispatch/sock
Environment=QFD_REMOTE=https://github.com/lotas/qf-research
Environment=QFD_TOKEN_FILE=/etc/qf-dispatch/github-token
Environment=QFD_LOCK_FILE=/var/lib/qf-locks/heavy-training.lock
Environment=QFD_INTENT_DIR=/var/lib/qf-locks/intent.d
Environment=QFD_BUILD_LOCK=/var/lib/qf-platform/build.lock
Environment=QFD_BUILD_TIMEOUT_S=1800
Environment=QFD_BUILD_LOCK_WAIT_S=900
Environment=QFD_BUILD_SETTLE_S=30
Environment=QFD_JOB_HOLD_DEADLINE_S=7800
Environment=QFD_KILL_CONFIRM_S=300
Environment=QFD_STOP_TIMEOUT_S=10
Environment=QFD_SOCKET=/run/qf-dispatch/client/sock
Environment=QFD_ADMIN_SOCKET=/run/qf-dispatch/admin/sock
Environment=QFD_ADMIN_UID=%%DEPLOY_UID%%
Environment=QFD_REAP_INTERVAL_S=60
Environment=QFD_SETUP_TEARDOWN_ALLOWANCE_S=600
Environment=QFD_MARKER_STALE_MARGIN_S=900
Environment=QFD_LOCK_MIGRATED_MARKER=/etc/qf-dispatch/lock-migrated
Environment=QFD_ADMITTED_MEM_BUDGET_MB=22528
Environment=QFD_TIMEOUT_MAX_S=3600
Environment=QFD_LOCK_WAIT_S=9000
Environment=QFD_IMAGE_BUILD_MEM_MB=2048
Environment=QFD_LIGHT_WORKERS=2
Environment=QFD_LOG_CAP_MB=16
Environment=QFD_ARTIFACT_CAP_MB=2048
Environment=QFD_HANDOFF_TIMEOUT_S=120
Environment=QFD_DISK_FLOOR_GB=20
RuntimeDirectory=qf-dispatch qf-dispatch/client qf-dispatch/admin
# 0711 on the parent: TRAVERSABLE but not listable. Revision 9 had 0750 qfd:qfd,
# which made BOTH sockets unreachable -- research, deploy, qfclient and qfheavy
# alike -- because chowning a socket does not grant traversal of its directory.
# The whole control plane was unusable and nothing in the acceptance list would
# have caught it, since every check assumed a reachable socket.
RuntimeDirectoryMode=0711
StateDirectory=qf-platform qf-runs
StateDirectoryMode=0750
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectControlGroups=no
PrivateDevices=yes
RestrictSUIDSGID=yes
ReadWritePaths=/var/lib/qf-platform /var/lib/qf-runs /var/lib/qf-locks
# Leave nothing behind on a clean stop: an orphaned heavy container would keep
# 22g while its flock died with the process (design 4.2).
ExecStopPost=/usr/bin/env bash -c 'docker ps -q --filter label=qf.run_id | xargs -r docker stop -t 10'
# PrivateTmp is off for defence in depth only: the lock now lives under
# /var/lib, so a private /tmp would not break it. It stays off because a future
# reader who moves the lock back to /tmp would otherwise get two private inodes
# and no mutex, silently -- which is what two 22g trainers on a ~29g host cost
# in 2026-07, twice.
#
# The real requirements are elsewhere and are checked at startup:
#   - ONE inode. flock is per inode, so `LOCK_FILE` must name the same file the
#     deploy user's cron entry names -- hence QFD_LOCK_MIGRATED_MARKER.
#   - Shared PERMISSION, not just a shared namespace: daily_walk_forward.sh:213
#     opens the lock for WRITE (`exec 9>`), so the inode is 0660 root:qfheavy.
#   - qfheavy contains qfd and the deploy user, NEVER research: qfclient does
#     contain research, which would let the agent hold the mutex indefinitely.
PrivateTmp=no
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`ProtectControlGroups=no` is deliberate: the runner reads the container's
`memory.current` for the high-water mark that parent §15 asks for.

`qf-locks.conf`, installed to `/etc/tmpfiles.d/`, provisions **one** lock inode
in a directory both users can traverse:

```
#Type Path                                    Mode UID  GID     Age Argument
d     /var/lib/qf-locks                       0755 root root    -   -
f     /var/lib/qf-locks/heavy-training.lock   0660 root qfheavy -   -
d     /var/lib/qf-locks/intent.d              2770 root qfheavy -   -
```

**One lock inode and one marker directory** — and the second is deliberately not
a lock. `heavy-training.lock` is the mutex. `intent.d` holds the nightly run's
intent marker, which stops new admissions while it waits; making that gate a
second reader/writer `flock` (revision 5) reproduced the barging it was meant to
fix, because the dispatcher's shared acquisition could slip past a queued
exclusive nightly. A marker file's existence is visible to every reader in every
order, so there is nothing to barge. `2770` — **setgid**, and that bit is
load-bearing rather than decorative: without it a marker's group comes from the
deploy user's primary group and its mode from that user's umask, so under
`umask 077` `qfd` could not read the declaration and would admit straight through
it. The deploy user must be able to create markers and `qfd` to unlink stale
ones; `qfheavy` because `research` must be able to do neither. `build.lock` is dispatcher-private under
`/var/lib/qf-platform`, since the nightly side has no part in it.

Three deliberate choices, each of which revision 2 got wrong:

- **One file, not two.** `flock` is per inode: provisioning the legacy
  `/tmp/queue-forecasting-walk-forward.lock` as a second regular file would
  create a second, independent mutex, and an un-migrated cron entry would take
  that one while the dispatcher took this one. Both would then run. There is no
  transitional safety here, which is why migration is a start-up prerequisite
  rather than a follow-up.
- **Not under `/var/lib/qf-platform`.** `StateDirectory=` creates that as
  `qfd:qfd` mode 0750, so the deploy user cannot traverse into it. `qf-locks` is
  its own 0755 directory for exactly that reason.
- **`qfheavy`, not `qfclient`.** `qfclient` contains `research`; a group-writable
  lock in it would let the untrusted agent hold the mutex indefinitely and stop
  nightly training, with no job involved and therefore no timeout to save it.

The symlink alternative — the legacy `/tmp` name pointing at this inode — is
rejected, but **not** for the reason revision 3 gave here. That revision claimed
`fs.protected_symlinks=1` would stop the deploy user following a root-owned
symlink in `/tmp`; the kernel permits the follow when the symlink's owner matches
the **directory's** owner, and `/tmp` is root-owned, so it would in fact work.
The argument that holds is about the **absent** name: any name in a 1777
directory is plantable while it does not exist, so if `research` creates
`/tmp/queue-forecasting-walk-forward.lock` before `systemd-tmpfiles` runs, the
nightly `exec 9>` opens an agent-owned file and the agent can break that run or
hold its mutex. The sticky bit prevents deleting someone else's file; it does not
reserve a name. Nothing lives in `/tmp`, and the legacy name is retired rather
than aliased — free, since migration is a start-up prerequisite anyway.

`qf-runs-prune.service` runs a one-shot prune of run directories older than 90
days (design D13); `.timer` fires it daily.

`host/phase2-setup.sh`, idempotent subcommands in the `phase0-setup.sh` idiom
(`--check` dry run, `would`/`info`/`skip`/`die` helpers, `discover` first):

| Subcommand | Does |
|---|---|
| `discover` | python3 and docker versions, cgroup v2, `$TRUSTED` exists and is **not** `$DEPLOY_DIR`, mirror commit, the lock inode and `$INTENT_DIR` with their owners/modes, host RAM against `ADMITTED_MEM_BUDGET`, the filesystem behind `/var/lib/qf-runs` and **whether it can enforce a per-directory quota** (design §4.5 measure 3), free space, whether a new system uid can reach `github.com` (facts #6), current nftables rules |
| `dispatch-user` | `groupadd -g 10001 qfrun`; `groupadd qfclient`; `useradd -r -d /var/lib/qf-platform -s /usr/sbin/nologin qfd`; add `qfd` to `docker,qfrun,qfclient`; add `research` and the deploy user to `qfclient`; **re-assert** that `research` is not in `docker` |
| `locks` | `groupadd qfheavy`; add `qfd` and the deploy user (and **assert `research` is not a member**); install `qf-locks.conf`; `systemd-tmpfiles --create`; then verify **as both users** that `exec 9>` on the lock succeeds and that `flock -n` from one blocks the other; that the deploy user can create and `qfd` can unlink a marker in `$INTENT_DIR`; and that `research` can do neither |
| `cron-lock-path` | print the exact `LOCK_FILE=/var/lib/qf-locks/heavy-training.lock` **and `INTENT_DIR=/var/lib/qf-locks/intent.d`** lines for the deploy user's `daily_walk_forward.sh` crontab entry, then verify with `crontab -l -u <deploy>` that both are there and that **both sides' lock paths and both sides' intent directories `stat` to the same device and inode** — revision 6's marker attested only the lock, so a divergent intent path would have silently restored starvation; on success write the root-owned marker `/etc/qf-dispatch/lock-migrated` that `qfd` requires to start. It prints and checks; it does not edit another user's crontab |
| `builder-probe` | Measure the classic builder rather than trusting it: (1) `DOCKER_BUILDKIT=0 docker build` is available and succeeds on a two-line fixture; (2) `--memory 64m` is **honoured** — a step that allocates more must fail; (3) `--force-rm` leaves no intermediate containers after a *failing* build as well as a passing one; (4) **cancellation timing** — start a slow build, kill the client, and measure how long daemon-side work takes to stop, repeated 5×. Prints each measurement. **Fails** if any run exceeds `QFD_BUILD_SETTLE_S`, and the documented response is to move building out of `qfd` (design D10), not to raise the window |
| `runs-dir` | `chgrp qfclient /var/lib/qf-runs && chmod 0750`, because `StateDirectory=` creates it `qfd:qfd` and clients could otherwise not traverse to run directories they are meant to read |
| `pin-base` | `docker pull` the base image, print the `FROM …@sha256:…` line for a human to commit into `trainer-env.Dockerfile`. It prints; it does not edit a file in the trusted checkout |
| `token` | install a `Contents: read`-only token from a file argument as `/etc/qf-dispatch/github-token` (0400 `qfd:qfd`); verify read works and **write does not** (NC14) |
| `install` | symlink `/usr/local/bin/qf` (client) and `/usr/local/sbin/qfadmin` (admin socket client, mode 0750 root:qfheavy) to the trusted checkout, install both units and the timer, `daemon-reload`, enable, start, print the running dispatcher's commit |
| `mirror-refresh` | fetch and hard-reset `$TRUSTED`, then `systemctl restart qf-dispatch`, because the dispatcher executes from the mirror (design §7 risk 4) |
| `verify` | run `host/nc-suite-phase2.sh` and refresh the evidence file |

## Task 7b: the nightly script's `flock` line

The only change 2a makes to running deployment code, and it is not optional:
the shared/exclusive protocol (design D10a) requires the nightly side to **wait**
rather than skip.

```diff
+INTENT_DIR="${INTENT_DIR:-/var/lib/qf-locks/intent.d}"
+LOCK_WAIT_S="${LOCK_WAIT_S:-9000}"
...
-if command -v flock >/dev/null 2>&1; then
-  exec 9>"$LOCK_FILE"
-  if ! flock -n 9; then
-    echo "ERROR: another walk-forward run is already active; lock file: $LOCK_FILE" >&2
-    exit 1
-  fi
-else
-  echo "WARN: flock not found; running without a cron overlap lock." >&2
-fi
+# flock is REQUIRED. The previous branch warned and trained anyway, so the whole
+# mutex -- and every memory guarantee that rests on it -- was bypassable by a
+# PATH missing one binary. Two 22g trainers on a ~29g host froze this box twice
+# in 2026-07; that is what the warn-and-continue branch was risking.
+require_command flock
+
+# Declare intent BEFORE waiting, as a marker file rather than a lock. Shared
+# flocks barge past a queued exclusive waiter -- verified on this host: a second
+# LOCK_SH was granted while an EX waiter sat in the queue, and the waiter
+# entered only after every shared holder left. A lock-based gate inherits that
+# same defect (revision 5 did exactly this); a file's existence does not, because
+# nothing contends for it. The dispatcher reads this marker and admits no new
+# work while it is live. See auto-research-phase2-design.md D10a.
+# Per-invocation name, published ATOMICALLY. Writing a fixed nightly.intent in
+# place (revision 6) let qfd read it half-written, let two invocations overwrite
+# each other, and let either EXIT trap delete the other's declaration. This trap
+# removes only THIS invocation's file.
+INTENT_FILE="$INTENT_DIR/nightly.$$.$(date +%s).intent"
+tmp="$INTENT_FILE.tmp"
+( umask 027; printf 'pid=%d\ndeadline=%d\n' "$$" "$(( $(date +%s) + LOCK_WAIT_S ))" > "$tmp" ) \
+  || { echo "ERROR: cannot write $tmp" >&2; exit 1; }
+chmod 0640 "$tmp" && mv -f "$tmp" "$INTENT_FILE" \
+  || { echo "ERROR: cannot publish $INTENT_FILE" >&2; rm -f "$tmp"; exit 1; }
+trap 'rm -f "$INTENT_FILE" "$tmp"' EXIT
+
+exec 9>"$LOCK_FILE"
+# Bounded wait, not -n: light experiments hold this lock SHARED for their
+# lifetime, so -n would skip the night whenever one was in flight. The bound
+# must exceed QFD_JOB_HOLD_DEADLINE_S plus QFD_KILL_CONFIRM_S: the dispatcher
+# holds the lock past its deadline rather than release it over a kill it could
+# not confirm, so this run can still skip a night. Deliberate -- a released lock
+# over live work is the failure the mutex exists to prevent.
+if ! flock -w "$LOCK_WAIT_S" 9; then
+  echo "ERROR: walk-forward lock not acquired within ${LOCK_WAIT_S}s; lock file: $LOCK_FILE" >&2
+  exit 1
+fi
```

The marker carries a PID and an absolute deadline so the dispatcher can tell a
live declaration from the leftovers of a crashed run; it is published by `rename`
so no reader sees a partial file; its mode is set explicitly rather than inherited
from a umask (the directory's setgid bit fixes the group, Task 7); and the `EXIT`
trap removes only this invocation's file. Document `LOCK_FILE`, `INTENT_DIR` and
`LOCK_WAIT_S` in the usage block, and state the invariant
`LOCK_WAIT_S > QFD_JOB_HOLD_DEADLINE_S + QFD_KILL_CONFIRM_S` in
`host/README.md`.
`phase2-setup.sh discover` **fails** — not warns — if it inverts, because the
failure mode is a silently skipped or starved nightly run.

Three changes, then: `require_command flock`, the intent marker with its trap,
and the bounded wait. The fail-open branch is the one that mattered most and the
one earlier revisions never looked at — it was outside the `if` they were
editing.

This lands **before** `install`, so the two sides are never running different
protocols: an unmigrated nightly script with `-n` plus a dispatcher holding
`LOCK_SH` means skipped nightly runs.

## Task 8: `nc13-inside.sh` and `nc-suite-phase2.sh`

`handoff-inside.sh` is written in Task 6 alongside the runner that invokes it;
NC15's canary is what proves it works.

`nc13-inside.sh` runs **inside** the dispatcher-built sandbox, mounted
read-only from the trusted checkout, as the `selftest` entrypoint. Asserting
isolation from a hand-rolled `docker run` in the suite would test a copy of the
flags; this tests the flags that actually ran.

```sh
#!/bin/sh
# NC13, from inside the sandbox. Mounted read-only from the trusted checkout;
# never read from the research worktree (NC10).
#
# Same discipline as nc-suite.sh: a refusal only counts if the attempt was
# possible, so each group is preceded by a canary that must SUCCEED. VOID is a
# failure. Results go to stdout and /out/nc13.json; exit 1 on any failure.
set -u
PY=/opt/qfenv/bin/python
pass=0; fail=0
ok()   { echo "ok    $1"; pass=$((pass+1)); }
bad()  { echo "FAIL  $1"; fail=$((fail+1)); }
void() { echo "VOID  $1"; fail=$((fail+1)); }

# Canary: the environment is real. Without this, every "cannot" below could be
# "python is broken".
$PY -c 'import lightgbm, pandas, pytest' 2>/dev/null \
  && ok "canary: trusted environment imports" \
  || void "canary: trusted environment imports"

# Identity
[ "$(id -u)" = "10001" ] && ok "runs as uid 10001" || bad "runs as uid $(id -u)"
grep -q '^CapEff:\s*0\{16\}$' /proc/self/status \
  && ok "no effective capabilities" || bad "effective capabilities present"

# Network: --network none
$PY - <<'PY' 2>/dev/null && bad "DNS resolves" || ok "DNS does not resolve"
import socket; socket.gethostbyname("github.com")
PY
$PY - <<'PY' 2>/dev/null && bad "outbound TCP connects" || ok "outbound TCP refused"
import socket; socket.create_connection(("1.1.1.1", 443), timeout=5)
PY

# Container runtime
[ -d /run ] && ok "canary: /run exists" || void "canary: /run exists"
[ -S /var/run/docker.sock ] && bad "docker socket present" || ok "no docker socket"

# Source mount is read-only, and writable output exists
[ -r /app/trainer/pyproject.toml ] \
  && ok "canary: source mount readable" || void "canary: source mount readable"
( : > /app/trainer/.nc13 ) 2>/dev/null \
  && bad "source mount is writable" || ok "source mount is read-only"
( : > /app/trainer/data/.nc13 ) 2>/dev/null \
  && bad "trainer/data is writable" || ok "trainer/data is not writable"
( : > /out/.nc13 ) 2>/dev/null \
  && ok "canary: /out is writable" || void "canary: /out is writable"

# Credentials
[ -z "${DATABASE_URL:-}" ] && ok "DATABASE_URL unset" || bad "DATABASE_URL is set"
env | grep -qiE 'password|secret|token|pulse|taskcluster' \
  && bad "credential-shaped env var present" || ok "no credential-shaped env vars"
find / -xdev -maxdepth 3 -name '.env' -readable 2>/dev/null | grep -q . \
  && bad ".env readable" || ok "no readable .env"
[ -e /srv/queue-forecasting ] && bad "trusted checkout visible" \
  || ok "trusted checkout not visible"

printf '{"pass":%d,"fail":%d}\n' "$pass" "$fail" > /out/nc13.json
echo "== NC13: pass=$pass fail=$fail =="
[ "$fail" -eq 0 ]
```

`host/nc-suite-phase2.sh` — run as root, submits as `research`, reuses
`nc-suite.sh`'s helpers:

- **NC8.** Seventeen clauses — (a), (b), (b2), (b3), (c)–(i), (g2)–(g6) —
  because the mutex has that many ways to be wrong, thirteen of them found by
  review rather than by design.
  *Canary:* a heavy job (`--mem 8g`, above the light ceiling, so the lane is
  derived — there is no flag to pass) reaches `RUNNING` with the lock free.
  *Refusal:* hold the lock from an unrelated process
  (`flock -n "$LOCK" sleep 90 &`), submit a heavy job, require `QUEUED` for
  15 s, release, require it to start.
  *Exclusion:* submit two heavy jobs and poll — never both `RUNNING`.
  *Permission and immutability:* the creation-order test from revision 3 cannot
  run and has been dropped — the lock's directory is `0755 root:root`, so neither
  runtime user can unlink or recreate the inode, and `qfd` refuses to start when
  it is missing. That was a leftover from the `/tmp` design, where either user
  could win a create race. What is asserted instead: as `qfd` and as the deploy
  user, `unlink` and re-create are both **refused**, `exec 9>` **succeeds** for
  both, and a `flock` held by one is seen by the other.
  *One inode:* `stat -c '%d:%i'` on the dispatcher's `QFD_LOCK_FILE` and on the
  `LOCK_FILE` in the deploy user's crontab entry must be **equal**, and the
  marker `/etc/qf-dispatch/lock-migrated` must exist. Two provisioned paths are
  two mutexes; this is the assertion that catches it.
  *Group:* `research` must not be a member of `qfheavy`, and as `research`,
  `exec 9>` on the lock must be **refused** — otherwise the agent can stop
  nightly training at will.
  *Budget:* two light jobs cannot sum past the budget (they are capped at 4 GB
  each), so the arithmetic is tested the way it actually arises: a 22 GB heavy
  job plus a 4 GB light job never run concurrently.
  *Protocol, from both sides, against every failure the experiments found*
  (design D10a). Sampling the lock passes none of these; a bounded wait alone
  passes only the first:
  *(a)* while a **light** job holds `LOCK_SH`, a stand-in nightly
  (`flock -w 60 9` on the same inode) **waits and then proceeds**, not exits.
  *(b)* **Starvation, tested by actively trying to barge.** With one light job
  running, place the nightly marker and queue a stand-in nightly on the training
  lock, then submit **repeatedly** — every admission must be refused for as long
  as the marker is live, and nightly must enter once the running job drains.
  Revision 5's version only checked that admissions stopped *after* nightly held
  its gate, which proves nothing about barging; the whole failure mode is a
  request arriving while the waiter is queued.
  *(b2)* **Stale marker:** a marker whose PID is dead, and one whose deadline has
  passed, must each be logged, unlinked and ignored, so a crashed nightly cannot
  wedge the dispatcher forever.
  *(b3)* **Marker permissions:** as `research`, creating a marker and deleting an
  existing one must both be **refused** — otherwise the agent can either stop the
  dispatcher indefinitely or suppress a real nightly declaration.
  *(c)* **Per-descriptor ownership:** with two light jobs running, the first
  finishing must **not** release the second's `LOCK_SH` — a stand-in nightly
  stays blocked until the second finishes. A module-level shared descriptor
  passes (a) and (b) and fails this.
  *(d)* **`flock` works on this filesystem at all:** two exclusive holders on the
  lock's real path must serialise. On overlayfs and some network filesystems
  `flock` can be a no-op or node-local, which would make every clause above
  vacuous — and a vacuous mutex reads exactly like a working one.
  *(e)* **Orphan recovery, in two separate runs.** Revision 5 asked for a light
  and a heavy orphan alive together; a correct dispatcher can never produce that
  state (a heavy job holds `LOCK_EX`), and reconciliation could not represent it
  anyway, since one descriptor cannot hold `SH` and `EX` at once. So: `SIGKILL`
  with a **light** orphan → restart → `LOCK_SH` re-acquired on a fresh
  descriptor; separately, `SIGKILL` with a **heavy** orphan → restart →
  `LOCK_EX` re-acquired. Each repeated with a stand-in nightly holding the lock
  across the restart, asserting the orphan is killed with
  `error_class=mutex_lost`. Revision 4 recovered heavy orphans only, which is the
  bug these runs exist to catch.
  *(f)* **The nightly wrapper fails closed without `flock`:** with `flock` off its
  `PATH`, `daily_walk_forward.sh` must exit non-zero **before** training. Until
  Task 7b it warned and trained anyway, so a `PATH` missing one binary bypassed
  the entire mutex.
  *(g)* **The hold deadline is enforced, and release requires confirmed death:**
  a job contrived to overrun `QFD_JOB_HOLD_DEADLINE_S` is killed, and the training
  descriptor is closed **only after Docker reports every container for that run
  stopped**. Then the harder half: with `docker kill` made to time out and
  confirmation withheld, the lock must **remain held**, the job recorded
  `kill_unconfirmed`, admissions stopped, and an alarm raised. A CLI that stopped
  waiting is not a workload that stopped running, and revision 6 released the
  mutex on exactly that basis. Then the **recovery** half: let Docker confirm
  death *after* the 300-second failure and assert the job leaves
  `CLEANUP_BLOCKED` for `FAILED`, the reservation is released, and admissions
  resume without operator action. And run the whole clause once more with the
  deadline expiring **during the handoff**, which only works if the handoff
  container carries `qf.run_id`/`qf.role` (Task 5) — otherwise the inventory
  reports "all stopped" while it runs.
  *(g2)* **Abandoned build:** killing a build mid-flight leaves no `docker build`
  client, the job goes `BUILDING → FAILED`, and the descriptor is released only
  after the settle window — the confirmation strength D10 knowingly accepts.
  *(g3)* **Restart while `CLEANUP_BLOCKED`,** both ways: workload still live
  (reaping resumes) and workload confirmed stopped while `qfd` was down (job
  reaches `FAILED`, resources release, admissions resume). The second case is the
  permanent stall revision 8 would have produced, since nothing live existed for
  `docker ps` to find.
  *(g4)* **`force-release` authorisation:** as `research`, on the admin socket,
  it is **refused**; on the client socket the operation does not exist. This is a
  containment control, not an ergonomic one — revision 8 shipped the hatch to the
  untrusted user. **Positive canaries first**, since a refusal proves nothing if
  nothing can connect at all: `research` reaches the client socket and the deploy
  user reaches the admin socket. Revision 9's `0750 qfd:qfd` runtime directory made
  both unreachable, and every other check in this suite assumed a reachable socket.
  *(g6)* **`BUILDING` is a first-class state:** such a job occupies its lane,
  renews its lease, is reclaimed only after every recorded resource is confirmed
  stopped, and a restart during it recharges the job's **full** reservation rather
  than its builder's 2 GB cap.
  *(g5)* **Build lands locally:** after a build, `docker image inspect` returns
  the exact id handed to the sandbox. Trivially true for the classic builder,
  asserted anyway because it is what would catch a future switch to a driver that
  does not load (design D10).
  *(g5b)* **Abandoned build:** no `docker build` client survives, the descriptor
  is released only after `QFD_BUILD_SETTLE_S`, and the job is `BUILDING → FAILED`
  rather than `CLEANUP_BLOCKED` — the deliberately weaker path D10 accepts.
  *(h)* **Intent-marker concurrency:** two nightly invocations at once each keep
  their own declaration and neither trap removes the other's; a half-written or
  unreadable marker fails **closed** and alarms; a stale-marker removal racing a
  fresh declaration does not remove the new one; and a marker written under
  `umask 077` is still readable by `qfd`, which is what the directory's setgid bit
  is for.
  *(i)* **The deadline survives a restart:** `SIGKILL` the dispatcher with an
  orphan whose `hold_deadline_at` is nearly past, restart, and assert the
  forced-cleanup path runs on the **restored** budget rather than a fresh one.
  *Cold-cache full size:* a 22 GB job with the image cache emptied completes,
  proving the single `max()` reservation covers build and run rather than summing
  them. Revision 3 charged both and made this case unadmittable forever.
  *Build failure and duplication:* a build that fails and one that exceeds
  `QFD_BUILD_TIMEOUT_S` each leave the job `FAILED` with the matching
  `error_class`; and two light jobs submitted at the same missing content key
  produce exactly **one** build, since the second waits on `build.lock` and its
  re-check hits.
- **NC10.** `qf trusted-paths` must report, for the Dockerfile, both manifests,
  `nc13-inside.sh` and the dispatcher's module directory, a realpath under
  `$TRUSTED` and a SHA-256 the suite recomputes independently with
  `sha256sum`. Plus: a `submit` carrying an invented path field is refused by
  name (there is no field to redirect — that is the control, and it is asserted
  rather than assumed).
- **NC12.** Reads `host/nc12-sha.txt` (Task 13). Records the image content key
  before, submits a `test` job at the poisoned SHA, and asserts: the job runs,
  the content key is byte-identical afterwards, `docker run … pip list` (via a
  `selftest` at the same SHA) does not show the bogus dependency, and the
  build-context assertion in `image.py` logged exactly three files. Missing
  `nc12-sha.txt` is **VOID**, not skip.
- **NC13.** `qf submit --kind selftest --sha <head> --wait` must exit 0, and
  the suite additionally greps the run's stdout for a `FAIL`/`VOID` line so an
  exit code alone cannot certify it.
- **NC14.** Phase 1's discipline, not a single unnamed "write attempt": one
  canary and three separately-named refusals, each with a **valid payload** so a
  422 cannot masquerade as containment.
  *Canary:* authenticated `GET /repos/lotas/qf-research` returns 200 — the
  credential works, so a refusal below means something.
  *R1:* git smart-HTTP push to a **disposable** ref (never the deploy branch),
  scored with `score_git`.
  *R2:* `POST /git/refs` with both `ref` and `sha` present, scored with
  `score_http`.
  *R3:* `POST /pulls` with both preflight conditions satisfied (two real
  branches that differ), scored with `score_http`.
  The token is read from its mode-0400 file into a 700 scratch dir, never argv,
  and the evidence must contain neither the token nor URL userinfo — the suite
  checks its own output before exiting.

- **NC15.** Disk containment, which needs a deliberately hostile job, so the
  fixture branch from Task 13 carries two scripts under `research/experiments/`
  and the suite runs them through the ordinary `test` path.
  *Canary:* a well-behaved job's artifacts appear in `artifacts/` at mode 0640
  **owned `qfd`, group `qfclient`** — which is only true if the pre-create /
  `--group-add` / chmod sequence of Task 6 is right — and are readable as
  `research`. Without this canary the refusals below could be measuring a
  handoff that never produced anything.
  *Log cap:* a job writing an endless stream to stdout is killed with
  `error_class=log_overflow`, and neither log file exceeds `QFD_LOG_CAP_MB`.
  *Output quota:* a job writing endlessly into `/out` is killed at `OUT_QUOTA`,
  and `du` of the run directory confirms the bound held.
  *Admission floor:* with `QFD_DISK_FLOOR_GB` temporarily raised above actual
  free space, a new job stays `QUEUED` and nothing is written.
  *Hostile modes:* a job that leaves `/out/predictions.parquet` mode 0600 still
  yields a readable `artifacts/predictions.parquet`, because the handoff runs as
  the uid that owns it (design D9).
  *Hostile file types:* a job that leaves a **symlink** and a job that leaves a
  **FIFO** at an allowlisted name are both refused by the handoff, and the FIFO
  case terminates at `QFD_HANDOFF_TIMEOUT_S` instead of wedging the worker.

Evidence is appended to `host/nc-evidence-phase2a.txt`, and the suite checks
its own output for secrets before exiting, per Phase 1 §7.2.

## Task 8b: the two fault gates

Prose review has reached the point of diminishing returns; what remains is
behaviour under interruption, which only an executable test can settle. Two gates,
in `host/fault-gates-phase2.sh`, run as root after Task 12 and before Task 14.

**Gate A — kill `qfd` during a long build.** Submit a job whose content key misses
against a deliberately slow fixture Dockerfile (a `RUN sleep 600`), then kill the
dispatcher three ways, one per iteration:

1. its own subprocess timeout path (`QFD_BUILD_TIMEOUT_S` lowered for the test),
2. `kill -9` on the daemon,
3. `systemctl stop qf-dispatch`.

After each, assert: **no `docker build` client survives**; daemon-side work stops
within `QFD_BUILD_SETTLE_S`, measured and printed; and on restart the `BUILDING`
job **retains its lock and reservation** and goes through the cancellation-settle
procedure — it must not be released merely because it has no `resources` row. That
last assertion is the one that fails against a vacuous empty-set check, which is
exactly the defect this gate exists to catch. Any measured cancellation beyond
the window triggers the design's D10 decision rule: building leaves `qfd` rather
than the window growing.

**Gate B — crash after each startup phase.** A `QFD_FAULT_AFTER` environment
variable (honoured only when `QFD_ALLOW_FAULT_INJECTION=1`, which the unit never
sets) aborts the process immediately after a named reconciliation phase:
`enumerate`, `lock`, `recharge`, `deadline`, `resolve_blocked`. For each, with a
live orphan present, start the dispatcher, let it die at that phase, restart it
cleanly, and assert **exactly one of two outcomes**: every resource is still
**held**, or verified cleanup **completed**. An intermediate release — lock closed
while a container lives, reservation freed with work outstanding, `FAILED`
recorded without confirmation — fails the gate. Run each phase with the orphan
both alive and already-dead, since the two exercise different branches.

Both gates write to `host/fault-evidence-phase2a.txt`, including every measured
cancellation time, because the D10 decision depends on those numbers rather than
on a pass/fail.

## Task 9: documentation and amendments

1. Apply the §9 amendment table of `auto-research-phase2-design.md` to
   `auto-research-loop-design.md`. Grep it for `qf-platform` and `qf-service`
   first — Phase 1 §10 records that the amendment tables miss passages.
2. `host/README.md`: a Phase 2a section covering the `qfd` user and the
   `qfrun`/`qfclient` groups, the two state directories, the socket, the token
   and its rotation owner, `PrivateTmp=no` and why, the fact that updating the
   dispatcher requires `mirror-refresh` plus a restart, and how to refresh the
   promoted `env/` manifests.
3. Add NC12, NC13, NC14 **and NC15** to the design's control table (§13.1 as
   amended) so the Phase 4 gate counts the right number of controls — six in 2a,
   two in 2c.

---

# Phase 2a-2 — host work (privileged; the human runs it)

## Task 10: discover

`sudo ./host/phase2-setup.sh discover`. Read the output. Specifically confirm:
`$TRUSTED` is the root-owned mirror and **not** `$DEPLOY_DIR`; cgroup v2 is in
use (the high-water sampler reads `memory.current`); python3 is ≥ 3.9; a
non-`research` uid can reach `github.com` without the proxy; and
the lock inode is provisioned `0660 root:qfheavy` in a `0755 root:root`
directory and `$INTENT_DIR` as `2770 root:qfheavy` (setgid), with `research` **not** in
that group; the deploy user's crontab entry
for `daily_walk_forward.sh`, whether it carries `LOCK_FILE=`, and whether the
script's `flock` is the bounded `-w` form (Task 7b) rather than `-n`;
the whole timeout chain — `TIMEOUT_MAX + BUILD_TIMEOUT_S + BUILD_LOCK_WAIT_S +
HANDOFF_TIMEOUT_S + SETUP_TEARDOWN_ALLOWANCE_S` under `JOB_HOLD_DEADLINE_S` under
`LOCK_WAIT_S - KILL_CONFIRM_S` (the setup/teardown term was stated in the design
and omitted from this check in revision 7) (a **failure**, not a warning, if any link
inverts); that the nightly script treats `flock` as required rather than
optional; that `$INTENT_DIR` exists as `2770 root:qfheavy` with the setgid bit
set and resolves to the **same device and inode** on both sides; that `flock` actually serialises two exclusive
holders on the lock's real path, since on some filesystems it is a no-op or
node-local; **the classic builder's real behaviour** (below); and whether
`/var/lib/qf-runs`'s filesystem can enforce a per-directory quota, since that
decides whether the `out/` bound is enforced or sampled.

Stop here if `$TRUSTED` and `$DEPLOY_DIR` are the same path. Phase 1 §4.1 is
what makes them different, and a merged pair silently voids NC10.

## Task 11: create the identity, pin the base, install the token, start the unit

```bash
sudo ./host/phase2-setup.sh dispatch-user
sudo ./host/phase2-setup.sh locks           # lock inode + intent.d + qfheavy
sudo ./host/phase2-setup.sh cron-lock-path  # prints LOCK_FILE= and INTENT_DIR=,
                                            # verifies both, writes the marker
sudo ./host/phase2-setup.sh pin-base        # prints the FROM line
#   -> commit that line into trainer-env.Dockerfile, push, mirror-refresh
sudo ./host/phase2-setup.sh token /path/to/read-only-token
sudo ./host/phase2-setup.sh install
```

Mint the token first, in GitHub: fine-grained, repository `lotas/qf-research`
only, **`Contents: read`** and nothing else, no `Issues`, no `Workflows`. It is
a different token from the agent's; the agent must never be able to read it.

Record in `host/README.md` who rotates it and when.

## Task 12: the first pinned job

Task 7b must already be applied: with the nightly script still on `flock -n`, a
dispatcher job holding `LOCK_SH` silently costs that night's walk-forward.

As `research`, with no SSH to anywhere and no `docker` access:

```bash
sudo -H -u research bash -lc 'qf ping'
sudo -H -u research bash -lc 'qf submit --kind test --sha <published sha> --wait'
```

Expect: the image builds once (a few minutes), the worktree materialises, pytest
runs against `trainer/tests`, and the exit code comes back. Some trainer tests
may fail in the sandbox — anything expecting a writable tree or a live
`DATABASE_URL` will. **That is a finding, not a blocker**: record which ones,
and fix them in `qf-research`, not by loosening the sandbox.

Then:

```bash
sudo -H -u research bash -lc 'qf list --limit 5'
sudo -H -u research bash -lc 'qf verify-chain'
sudo -H -u research bash -lc 'qf logs <run-id> | tail -40'
sudo -H -u research bash -lc 'qf submit --kind test --sha $(git -C ~/qf-research rev-parse HEAD)'
```

The last one must be **refused** if that commit is not pushed — that is
acceptance criterion 3, and it is easiest to observe by accident here.

## Task 13: the negative-control fixtures (NC12 and NC15)

On a scratch branch of `qf-research`, using the **agent's** token:

```bash
git checkout -b nc12-poisoned-manifest
# trainer/pyproject.toml: add a [build-system] table pointing at a backend that
# would fail loudly if it ever executed, and a dependency that does not exist.
git commit -am 'NC12 fixture: poisoned manifest, must not affect the image'
git push -u origin nc12-poisoned-manifest
git rev-parse HEAD > ../nc12-sha            # then record it in host/nc12-sha.txt
```

Submit a `test` job at that SHA and assert the three NC12 properties. On the same branch, add the **five** hostile fixtures NC15 needs under
`research/experiments/`: one that writes an endless stream to stdout; one that
writes endlessly into `/out`; one that writes `/out/predictions.parquet` at mode
0600; one that leaves a **symlink** at an allowlisted artifact name; and one that
leaves a **FIFO** there. The last two are what prove the handoff's file-type
refusal and its timeout, so omitting them would leave two NC15 clauses
unexercised.

Commit `host/nc12-sha.txt` so later runs of the suite are automatic rather than
VOID, and leave the branch in place — it is a fixture, not litter. Note it in
`host/README.md` so nobody deletes it as stale.

**This task precedes the suite deliberately.** In revision 1 it followed it, so
the ordered plan guaranteed a VOID on NC12: the suite reads
`host/nc12-sha.txt`, and nothing had written it yet.

## Task 14: run every suite, record evidence

```bash
sudo ./host/nc-suite-phase2.sh          # NC8, NC10, NC12, NC13, NC14, NC15
sudo ./host/fault-gates-phase2.sh       # gates A and B (Task 8b)
sudo ./host/nc-suite.sh                 # Phase 0: NC1-6 still closed
sudo -H -u research bash -lc '/srv/.../host/nc7-suite.sh'   # Phase 1: NC7
docker ps --format '{{.Names}}\t{{.Status}}'   # uptimes span the phase
```

All three suites exit 0 with `failed=0`. Refresh
`host/nc-evidence-phase0.txt`, `host/nc-evidence-phase1.txt`, and write
`host/nc-evidence-phase2a.txt`. Confirm no token and no URL userinfo appear in
any of them.

---

# Acceptance

Design §5, restated as a checklist:

1. `python3 -m unittest discover -s host/dispatcher/tests` passes with no
   network and no privileges, covering every case named in Tasks 1–6.
2. `qf submit --kind test --sha <published>` as `research` runs the suite in the
   sandbox and returns its exit code; the run directory holds stdout, stderr and
   `result.json`.
3. An unpublished SHA is refused, and the refusal names why.
4. `qf verify-chain` agrees; a row edited directly in `jobs` makes it disagree.
5. **NC8** heavy exclusion against `daily_walk_forward.sh`'s lock; one device
   and inode with the migration marker present; neither runtime user can unlink
   or recreate it while both can write-open and mutually `flock` it; `research`
   cannot open it at all; a 22 GB heavy and a 4 GB light job never overlap; and
   the eleven protocol clauses of Task 8 — nightly waits rather than exits;
   **repeated admissions cannot barge a queued nightly**; a stale marker is
   reclaimed and `research` can neither place nor remove one; one light job
   finishing does not release the other's `LOCK_SH`; `flock` genuinely serialises
   on that filesystem; orphan recovery re-acquires the right mode in each lane,
   tested in **separate runs**; the nightly wrapper **fails closed without
   `flock`**; a job overrunning `QFD_JOB_HOLD_DEADLINE_S` is killed **and its
   descriptor released only on confirmed death**, staying held when a kill cannot
   be confirmed; an **unconfirmed builder shutdown** takes the same path from
   `BUILDING`; a **restart while `CLEANUP_BLOCKED`** resumes reaping or reaches
   `FAILED` and resumes admissions; **`research` is refused `force-release`** and
   the client socket does not carry it; marker publication survives concurrent
   nightlies, partial writes and a hostile umask; and the hold deadline survives a
   restart.
6. **NC10** trusted paths resolve under `$TRUSTED`, with digests the suite
   recomputes.
7. **NC12** poisoned `pyproject.toml`: content key byte-identical, exactly
   three files in the build context, bogus dependency absent. The pinned
   `docker image inspect` returns
   the exact id handed to the sandbox. Plus: a 22 GB job
   with an emptied image cache completes (one `max()` reservation, not a sum); a
   failed build and a timed-out build each leave the job `FAILED` with the
   matching `error_class`; and two light jobs at the same missing key produce one
   build, not two.
8. **NC13** every in-container assertion passes, with canaries.
9. **NC14** the dispatcher's token reads (canary) and is separately refused a
   git push, a REST ref creation and a pull request, scored with `nc7-lib.sh`.
10. **NC15** log cap, output quota, disk-floor admission, a readable artifact
    from a hostile 0600 output, and refusal of a symlink or FIFO at an
    allowlisted artifact name — the FIFO terminating on the handoff timeout
    rather than wedging a worker. Each refusal leaves the job `FAILED` with the
    matching `error_class`, never `SUCCEEDED`.
11. `qf verify-chain` detects an edit to **every** column of `jobs` — lease and
    hold fields included — and to a `pins` row or an artifact's path, size or
    digest; and it agrees immediately after a single `submit` + `dequeue`, which
    is what the revision-7 payload-only hold columns would have broken.
12. Restart recovery, exercised against a **deliberate orphan** in **each lane**
    — `SIGKILL` the daemon, or start it with labelled containers already running,
    because `ExecStopPost` means an ordinary restart leaves nothing to re-adopt.
    Startup re-charges admission from each container's own `HostConfig.Memory`
    and re-acquires that container's lock in its own mode on a fresh descriptor;
    anything whose lock cannot be re-acquired is killed with
    `error_class=mutex_lost`.
13. **Fault gate A:** killing `qfd` mid-build three ways (subprocess timeout,
    `SIGKILL`, `systemctl stop`) leaves no build client, cancels daemon work
    inside `QFD_BUILD_SETTLE_S` — measured and printed — and the restarted
    dispatcher **keeps** the `BUILDING` job's lock and reservation through
    cancellation-settle rather than releasing them on an empty `resources` row.
    Any over-window measurement triggers the D10 decision rule.
14. **Fault gate B:** a crash after each of the five reconciliation phases leaves
    resources either held or verifiably cleaned up — never an intermediate
    release — with the orphan both alive and already-dead.
15. Phase 0 and Phase 1 suites still exit 0 with `failed=0`.
16. The live stack is undisturbed and the nightly walk-forward completes on the
    day 2a lands — specifically the first run **after** the cron entry moved to
    the durable lock path *and* Task 7b's intent-then-wait change landed, since
    those are the changes that can break it. Verify once more with **two** light
    jobs deliberately in flight and overlapping at the nightly start time: the
    run must be delayed, not skipped and not starved.
17. Evidence recorded, secret-free — including
    `host/fault-evidence-phase2a.txt` with every measured cancellation time.

**Not in 2a**, and a reviewer should push back if any of it appears: contracts,
`contract_hash`, Postgres access from the dispatcher, extracts, baselines, the
predictions-only trainer change, the evaluator, `eval.parquet`, `verdict.py`,
the independent derivation, `screen`/`confirm`/`probe`/`query`/`summarize`,
pre-registration, the bus, or any change to `trainer/data/models/` or the live
predictor. The single exception, and it is deliberate: `daily_walk_forward.sh`'s
locking sequence (Task 7b — four lines and two variables), without which the
mutex is neither correct nor starvation-free.
