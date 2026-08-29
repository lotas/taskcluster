"""The sandbox flag set, as data.

The D2 flag set is a CONTROL, not a convenience, so it is built by a pure
function and asserted by tests rather than typed into a shell string. Absent
flags are the failure mode this module exists to prevent: the tests assert
presence, and the negative assertions (no --env-file, no DATABASE_URL, no
docker.sock) exist because `docker-compose.yml` does all three for the trainer
and those habits are exactly what must not leak in.
"""
from __future__ import annotations

import re

import spec as spec_mod

PIDS_LIMIT = 512
OOM_SCORE_ADJ = 500
DEFAULT_UID_GID = "10001:10001"
DEFAULT_TMPFS = "1g"
VENV_PYTHON = "/opt/qfenv/bin/python"
TRUSTED_MOUNT_PREFIX = "/trusted/"

SRC_DEST = "/app/trainer"
OUT_DEST = "/out"
ARTIFACTS_DEST = "/artifacts"

# Phase 2b-2. The frozen extract, and the one writable hole in the read-only
# tree.
#
# `DATA_DEST` is NESTED INSIDE `SRC_DEST`, deliberately: `CACHE_DIR` and the
# model output path are computed relative to the trainer module
# (`trainer/src/data_loader.py:22`), so the writable directory has to land inside
# the read-only tree rather than beside it. That nesting is the reason 2b needs
# no path refactor inside `qf-research`.
EXTRACT_DEST = "/extract"

# Phase 2b-3. The promoted baseline, read-only for the same reason /extract is:
# a promoted baseline is immutable, and a run that could write to it would change
# what a recorded comparison was measured against -- silently, because the
# baseline hash a probe pins is computed at promotion and never recomputed on
# read.
BASELINE_DEST = "/baseline"

# `SRC_DEST + "/trainer/data"`, and the extra level is not a typo.
#
# The mounted tree is the qf-research WORKTREE ROOT, and that repository puts the
# trainer package one level down -- `extract-qf-research.sh` renames
# `tools/queue-forecasting/trainer/` to `trainer/`. So inside the container the
# module is at `/app/trainer/trainer/src/`, which is also why a `test` job's
# default path is `trainer/tests` rather than `tests`.
#
# `CACHE_DIR` is `<module>/../data` (`trainer/src/data_loader.py:22`), i.e.
# `trainer/data` in that layout. An earlier version of this constant said
# `/app/trainer/data`, one level short, and the container refused to start:
#
#   error mounting ".../data" to rootfs at "/app/trainer/data": create
#   mountpoint for /app/trainer/data mount: mkdirat ...: read-only file system
#
# Two things were wrong at once and the error named only the second: the path was
# not where the trainer writes, AND the mountpoint did not exist to mount onto.
# See `Runner._ensure_probe_mountpoint` for that half.
DATA_DEST = SRC_DEST + "/trainer/data"

ROLES = ("candidate", "handoff")

_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}\Z")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_UID_GID_RE = re.compile(r"^([0-9]{1,10}):([0-9]{1,10})\Z")
_TMPFS_RE = re.compile(r"^[1-9][0-9]{0,4}[mg]\Z")


class SandboxError(ValueError):
    """A run configuration that must not reach Docker."""


def _check_mount_source(path, what):
    if not isinstance(path, str) or not path.startswith("/"):
        # A relative path is resolved against the DAEMON's cwd, not ours, so it
        # would mount something nobody chose.
        raise SandboxError(f"{what} must be an absolute path, got {path!r}")
    if ":" in path:
        raise SandboxError(f"{what} must not contain ':': {path!r}")


# Destinations allowed in ONE direction only, and the asymmetry is the point.
#
#   /extract   is a PUBLISHED, IMMUTABLE artifact (D20). A candidate that could
#              write to it would corrupt the input to a recorded result, and
#              invisibly: the manifest's digests describe what was extracted and
#              nothing re-checks them before a later read.
#
#   /baseline  is the same argument about the other input. Its hash is a CONTENT
#              key computed once at promotion, so a writable mount would leave
#              every probe that cites that hash citing bytes it never saw.
#
#   /app/trainer/data  is the opposite. The tree around it is read-only and the
#              trainer must still write its cache and its model output, so a
#              read-only mount here fails at the first cache write -- deep inside
#              pandas, with an error naming a path nobody chose.
#
# `/artifacts` and `/trusted/*` keep their existing latitude. Tightening them
# would be a change to an evidenced path with no demonstrated need, and this
# table is about adding two destinations rather than revisiting two others.
_RO_ONLY_DESTS = (EXTRACT_DEST, BASELINE_DEST)
_RW_ONLY_DESTS = (DATA_DEST,)


def _check_extra_dest(dest, *, writable):
    """Allowlist, not a denylist, and now direction-aware.

    A job mounting over /opt/qfenv would replace the interpreter the entrypoint
    names; a job mounting read-write over /extract would rewrite an artifact a
    result already cites.
    """
    if dest in _RO_ONLY_DESTS:
        if writable:
            raise SandboxError(
                f"{dest} may only be mounted read-only: a published extract or"
                f" baseline is immutable, and a run that could write to it would"
                f" change the input to results that already cite it")
        return
    if dest in _RW_ONLY_DESTS:
        if not writable:
            raise SandboxError(
                f"{dest} may only be mounted read-write: the trainer computes"
                f" CACHE_DIR relative to its module, so a read-only mount here"
                f" fails at the first cache write inside a library, with an"
                f" error naming a path nobody chose")
        return
    if dest == ARTIFACTS_DEST:
        return
    if dest.startswith(TRUSTED_MOUNT_PREFIX) and ".." not in dest.split("/"):
        return
    raise SandboxError(
        f"mount destination {dest!r} is not allowlisted; extras may only be"
        f" {ARTIFACTS_DEST}, {EXTRACT_DEST}, {BASELINE_DEST}, {DATA_DEST}, or"
        f" a path under"
        f" {TRUSTED_MOUNT_PREFIX}")


def container_name(run_id, role):
    """The one place the container name is spelled.

    It is the identifier of record: `resources` rows are written before the
    container exists, so they cannot carry the 64-hex id, and Docker accepts a
    name anywhere it accepts an id.
    """
    if not _RUN_ID_RE.match(run_id or ""):
        raise SandboxError(f"run_id is not a safe container-name component:"
                           f" {run_id!r}")
    if role not in ROLES:
        raise SandboxError(f"role must be one of {ROLES}, got {role!r}")
    return f"qf-{run_id}-{role}"


def docker_start_argv(run_id, role):
    """The second half of the create-then-start protocol.

    Separating the two verbs is what makes "the container exists" a fact the
    dispatcher can establish BEFORE it stops holding the phase gate. `docker
    run` cannot: it is one command that creates and starts, and `Popen`
    returning says only that the local CLI was spawned. Until the daemon binds
    the name, `docker inspect` answers "No such object" -- which every
    confirmation path here reads as a POSITIVE absence -- so a sweep could
    release the resource row and the training mutex while a container was still
    on its way up.

    `--attach` (and no `-t` anywhere) keeps stdout and stderr separate streams
    on the client, exactly as `docker run` gave them, so the bounded log writers
    are unaffected; the exit status is still the container's.
    """
    return ["docker", "start", "--attach", container_name(run_id, role)]


def docker_create_argv(*, image_ref, run_id, spec_hash, kind, src_mount,
                       out_mount, entrypoint_argv, mem_limit, cpus,
                       role="candidate", uid_gid=DEFAULT_UID_GID,
                       tmpfs_size=DEFAULT_TMPFS, extra_ro_mounts=(),
                       extra_rw_mounts=(), group_add=()):
    """Construct the argv that CREATES one sandboxed run. Pure; no filesystem
    access. `docker_start_argv` runs it afterwards.

    Every flag below is part of the boundary (auto-research-phase2-design.md D2).
    Absent flags are the failure mode this function exists to prevent, so the
    tests assert presence, not absence.

    `role` and the two mount/group extras are not in the plan's signature but are
    required by the handoff it specifies (Task 6): the handoff is a second
    container that must carry `qf.role=handoff`, mount `/artifacts` read-write,
    and run with `--group-add <qfclient gid>`. Labelling only the candidate is
    the revision-7 defect -- a label-based "all containers stopped" check passes
    while the handoff still runs.
    """
    if not _IMAGE_ID_RE.match(image_ref or ""):
        # A tag can be re-pointed between ensure_image and the create, and the
        # recorded image_digest would then describe something other than what ran.
        raise SandboxError(
            f"image_ref must be an inspected image id (sha256:<64 hex>), got"
            f" {image_ref!r}")
    if role not in ROLES:
        raise SandboxError(f"role must be one of {ROLES}, got {role!r}")
    if not _RUN_ID_RE.match(run_id or ""):
        raise SandboxError(f"run_id is not a safe container-name component:"
                           f" {run_id!r}")

    m = _UID_GID_RE.match(uid_gid or "")
    if not m:
        raise SandboxError(f"user must be <uid>:<gid>, got {uid_gid!r}")
    if m.group(1) == "0" or m.group(2) == "0":
        raise SandboxError("the sandbox never runs as uid 0 or gid 0")

    mem_mb = spec_mod.mem_mb(mem_limit)
    if mem_mb > spec_mod.MEM_CEILING_MB:
        # spec.py already refuses this; re-checked here so a LATER caller that
        # builds an argv without going through spec.py cannot bypass the ceiling.
        raise SandboxError(
            f"mem_limit {mem_limit} exceeds the host ceiling of"
            f" {spec_mod.MEM_CEILING_MB}m")
    if not isinstance(cpus, (int, float)) or isinstance(cpus, bool) or cpus <= 0:
        raise SandboxError(f"cpus must be a positive number, got {cpus!r}")
    if not _TMPFS_RE.match(tmpfs_size or ""):
        raise SandboxError(f"tmpfs_size must look like 512m or 1g, got"
                           f" {tmpfs_size!r}")

    if not entrypoint_argv or not isinstance(entrypoint_argv, (list, tuple)):
        raise SandboxError("entrypoint_argv must be a non-empty list")
    for part in entrypoint_argv:
        if not isinstance(part, str):
            raise SandboxError(f"entrypoint_argv element is not a string:"
                               f" {part!r}")

    _check_mount_source(src_mount, "src_mount")
    _check_mount_source(out_mount, "out_mount")

    mem_flag = f"{mem_mb}m"
    argv = [
        # `create`, not `run`: see docker_start_argv. `--rm` is accepted here
        # and sets AutoRemove on the container, so the name still disappears
        # when it is removed and `docker inspect` still answers "No such
        # object" -- the positive absence `confirm_all_stopped` needs.
        "docker", "create", "--rm",
        "--name", container_name(run_id, role),
        "--label", f"qf.run_id={run_id}",
        "--label", f"qf.role={role}",
        "--label", f"qf.spec_hash={spec_hash}",
        "--label", f"qf.kind={kind}",
        # Docker's own log store is unbounded; the dispatcher's bounded writer
        # is the only place these bytes are allowed to land (design §4.5).
        "--log-driver", "none",
        "--network", "none",
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", uid_gid,
    ]
    for gid in group_add:
        # Only the handoff gets this. The candidate must never be in qfclient,
        # or it could write into artifacts/ directly.
        argv += ["--group-add", str(gid)]
    argv += [
        "--pids-limit", str(PIDS_LIMIT),
        "--memory", mem_flag,
        # Equal to --memory: otherwise the cap becomes a thrash rather than a
        # limit, and the OOM the accounting relies on never arrives.
        "--memory-swap", mem_flag,
        "--cpus", str(cpus),
        "--oom-score-adj", str(OOM_SCORE_ADJ),
        "--tmpfs", f"/tmp:rw,nosuid,nodev,size={tmpfs_size}",
        "-v", f"{src_mount}:{SRC_DEST}:ro",
        "-v", f"{out_mount}:{OUT_DEST}:rw",
    ]
    # Emitted AFTER the source mount, so a nested destination follows its
    # parent. Docker orders bind mounts by destination depth, so nesting works
    # either way in practice -- emitting parent-first means a reader does not have
    # to know that, and a later reordering cannot come to depend on it silently.
    for src, dest in extra_ro_mounts:
        _check_mount_source(src, "extra_ro_mount source")
        _check_extra_dest(dest, writable=False)
        argv += ["-v", f"{src}:{dest}:ro"]
    for src, dest in extra_rw_mounts:
        _check_mount_source(src, "extra_rw_mount source")
        _check_extra_dest(dest, writable=True)
        argv += ["-v", f"{src}:{dest}:rw"]

    argv.append(image_ref)
    # Separate argv elements, never joined: an argv-to-shell collapse is how a
    # validated field becomes a command.
    argv.extend(entrypoint_argv)
    return argv


def entrypoint_for(effective):
    """The in-container argv for a validated spec.

    `-p no:cacheprovider` is supplied HERE and can never come from the spec:
    `-p` loads plugins from the untrusted tree, so it is absent from
    spec.PYTEST_FLAGS and injected by trusted code instead. Also, the tree is
    mounted read-only, so pytest's cache would fail to write.
    """
    kind = effective["kind"]
    if kind == "test":
        args = effective["args"]
        argv = [VENV_PYTHON, "-m", "pytest", "-p", "no:cacheprovider"]
        argv += list(args["pytest_args"])
        if args.get("k"):
            argv += ["-k", args["k"]]
        argv += list(args["paths"])
        return argv
    if kind == "probe":
        # ONE SCRIPT, and the extract is NOT an argument.
        #
        # It is mounted at a fixed path (`EXTRACT_DEST`), so the script knows
        # where to look without being told. A path passed as an argument is a
        # path something has to validate twice, and the second validator would be
        # inside the untrusted code.
        #
        # ABSOLUTE, not relative. A relative path resolves against the image's
        # WORKDIR, which is a property of a Dockerfile in another repository, so
        # a probe would break if that WORKDIR ever moved -- and break with
        # `can't open file` rather than with anything about mounts. The worktree
        # root is mounted at SRC_DEST, and that is knowledge this module has.
        return [VENV_PYTHON, SRC_DEST + "/" + effective["args"]["path"]]
    if kind == "selftest":
        # NC13 from inside the sandbox, read from the trusted checkout only.
        return ["/bin/sh", f"{TRUSTED_MOUNT_PREFIX}nc13-inside.sh"]
    raise SandboxError(f"no entrypoint for kind {kind!r}")
