#!/usr/bin/env python3
"""qfd -- the trusted experiment dispatcher.

The only stateful piece, and the only one holding privilege. Everything it does
is arranged around three facts that earlier revisions of the design each got
wrong at least once:

  * **Contention never produces a state transition.** Everything contended is
    acquired between `peek` and `dequeue`, so there is no `LEASED -> QUEUED`
    edge and no defer state (design §4.2).
  * **A release is a claim about reality.** The training mutex is closed only
    after Docker positively reports every recorded container stopped. A
    subprocess timeout on `docker kill` proves the CLI stopped waiting, not that
    the workload died. A skipped nightly run is recoverable; a released lock
    over live work is not.
  * **Confirmation over an empty inventory is not confirmation.** Every
    "everything is stopped" check first asserts there was something to inspect.

Two sockets, deliberately: the client socket's group contains the untrusted
`research` user, so the operator escape hatch lives on a second one whose group
does not. `SO_PEERCRED` records who called; it does not authorise them.
"""
from __future__ import annotations

import calendar
import contextlib
import errno
import fcntl
import grp
import hashlib
import json
import logging
import os
import queue
import re
import socket
import struct
import subprocess
import sys
import threading
import time

import image as image_mod
import sandbox as sandbox_mod
import source as source_mod
import spec as spec_mod
import store as store_mod

log = logging.getLogger("qfd")

MAX_REQUEST_BYTES = 64 * 1024
# The bound on an ordinary Docker CLI call (inspect, stop, kill). Every such call
# is additionally capped by whatever is left of the job's hold budget, so this is
# a ceiling rather than a grant.
DOCKER_CALL_TIMEOUT_S = 60
SCHEMA_VERSION = spec_mod.SCHEMA_VERSION
FORCE_RELEASE_FLAG = "i_have_verified_nothing_is_running"

# The client socket's op table. `force-release` is deliberately ABSENT: the
# group on this socket contains `research` (revision 8's regression).
CLIENT_OPS = ("ping", "submit", "status", "list", "cancel", "verify-chain",
              "trusted-paths")
ADMIN_OPS = ("force-release",)

# Trusted files whose realpath and digest `trusted-paths` reports -- the live
# half of NC10.
TRUSTED_FILES = ("spec.py", "store.py", "source.py", "image.py", "sandbox.py",
                 "qfd.py", "trainer-env.Dockerfile", "nc13-inside.sh",
                 "handoff-inside.sh", "env/pyproject.toml", "env/uv.lock")

_MARKER_RE = re.compile(r"^nightly\.(\d+)\.(\d+)\.intent\Z")
_RUN_ID_SHA_LEN = 12


class ConfigError(Exception):
    """A startup precondition that is not met. Always fatal: every one of these
    is a control whose absence is silent."""


def utcnow():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _int_env(name, default=None):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        if default is None:
            raise ConfigError(f"{name} is unset")
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} is not an integer: {raw!r}")


class Config:
    """Every environment variable the unit sets, enumerated in one place.

    This list has twice claimed coverage the enumeration did not have, so
    `ENV_KEYS` is written to be CHECKED against `qf-dispatch.service` by a test
    rather than merely read.
    """

    ENV_KEYS = (
        "QFD_TRUSTED_DIR", "QFD_STATE_DIR", "QFD_RUNS_DIR", "QFD_SOCKET",
        "QFD_ADMIN_SOCKET", "QFD_ADMIN_UID", "QFD_REMOTE", "QFD_TOKEN_FILE",
        "QFD_LOCK_FILE", "QFD_INTENT_DIR", "QFD_BUILD_LOCK",
        "QFD_BUILD_TIMEOUT_S", "QFD_BUILD_LOCK_WAIT_S", "QFD_BUILD_SETTLE_S",
        "QFD_JOB_HOLD_DEADLINE_S", "QFD_KILL_CONFIRM_S", "QFD_STOP_TIMEOUT_S",
        "QFD_REAP_INTERVAL_S", "QFD_SETUP_TEARDOWN_ALLOWANCE_S",
        "QFD_MARKER_STALE_MARGIN_S", "QFD_LOCK_MIGRATED_MARKER",
        "QFD_ADMITTED_MEM_BUDGET_MB", "QFD_TIMEOUT_MAX_S", "QFD_LOCK_WAIT_S",
        "QFD_IMAGE_BUILD_MEM_MB", "QFD_LIGHT_WORKERS", "QFD_LOG_CAP_MB",
        "QFD_ARTIFACT_CAP_MB", "QFD_HANDOFF_TIMEOUT_S", "QFD_DISK_FLOOR_GB",
        "QFD_QUEUED_CAP_PER_UID", "QFD_LEASE_S",
    )

    def __init__(self, **kw):
        for key, value in kw.items():
            setattr(self, key, value)

    @classmethod
    def from_env(cls, env=None):
        env = env if env is not None else os.environ
        get = env.get
        cfg = cls(
            trusted_dir=get("QFD_TRUSTED_DIR", ""),
            state_dir=get("QFD_STATE_DIR", ""),
            runs_dir=get("QFD_RUNS_DIR", ""),
            socket_path=get("QFD_SOCKET", ""),
            admin_socket_path=get("QFD_ADMIN_SOCKET", ""),
            admin_uid=_int_env("QFD_ADMIN_UID"),
            remote=get("QFD_REMOTE", ""),
            token_file=get("QFD_TOKEN_FILE", ""),
            lock_file=get("QFD_LOCK_FILE", ""),
            intent_dir=get("QFD_INTENT_DIR", ""),
            build_lock=get("QFD_BUILD_LOCK", ""),
            build_timeout_s=_int_env("QFD_BUILD_TIMEOUT_S", 1800),
            build_lock_wait_s=_int_env("QFD_BUILD_LOCK_WAIT_S", 900),
            build_settle_s=_int_env("QFD_BUILD_SETTLE_S", 30),
            job_hold_deadline_s=_int_env("QFD_JOB_HOLD_DEADLINE_S", 7800),
            kill_confirm_s=_int_env("QFD_KILL_CONFIRM_S", 300),
            stop_timeout_s=_int_env("QFD_STOP_TIMEOUT_S", 10),
            reap_interval_s=_int_env("QFD_REAP_INTERVAL_S", 60),
            setup_teardown_allowance_s=_int_env(
                "QFD_SETUP_TEARDOWN_ALLOWANCE_S", 600),
            marker_stale_margin_s=_int_env("QFD_MARKER_STALE_MARGIN_S", 900),
            lock_migrated_marker=get("QFD_LOCK_MIGRATED_MARKER", ""),
            mem_budget_mb=_int_env("QFD_ADMITTED_MEM_BUDGET_MB", 22528),
            timeout_max_s=_int_env("QFD_TIMEOUT_MAX_S", 3600),
            lock_wait_s=_int_env("QFD_LOCK_WAIT_S", 9000),
            image_build_mem_mb=_int_env("QFD_IMAGE_BUILD_MEM_MB", 2048),
            light_workers=_int_env("QFD_LIGHT_WORKERS", 2),
            log_cap_mb=_int_env("QFD_LOG_CAP_MB", 16),
            artifact_cap_mb=_int_env("QFD_ARTIFACT_CAP_MB", 2048),
            handoff_timeout_s=_int_env("QFD_HANDOFF_TIMEOUT_S", 120),
            disk_floor_gb=_int_env("QFD_DISK_FLOOR_GB", 20),
            queued_cap_per_uid=_int_env("QFD_QUEUED_CAP_PER_UID", 20),
            lease_s=_int_env("QFD_LEASE_S", 300),
        )
        cfg.check_deadline_chain()
        return cfg

    def check_deadline_chain(self):
        """The one arithmetic invariant that ties every timeout together.

        `TIMEOUT_MAX + BUILD_TIMEOUT_S + BUILD_LOCK_WAIT_S + HANDOFF_TIMEOUT_S +
        setup/teardown < JOB_HOLD_DEADLINE_S < LOCK_WAIT_S`, and the nightly
        side's wait must also exceed the deadline plus the kill-confirmation
        window, because the dispatcher holds the lock past its deadline rather
        than release it over a kill it could not confirm. These numbers move
        together or not at all.
        """
        chain = (self.timeout_max_s + self.build_timeout_s
                 + self.build_lock_wait_s + self.handoff_timeout_s
                 + self.setup_teardown_allowance_s)
        if chain >= self.job_hold_deadline_s:
            raise ConfigError(
                f"phase budget {chain}s does not fit inside"
                f" QFD_JOB_HOLD_DEADLINE_S={self.job_hold_deadline_s}s")
        if self.job_hold_deadline_s + self.kill_confirm_s >= self.lock_wait_s:
            raise ConfigError(
                f"QFD_JOB_HOLD_DEADLINE_S + QFD_KILL_CONFIRM_S ="
                f" {self.job_hold_deadline_s + self.kill_confirm_s}s must be"
                f" below QFD_LOCK_WAIT_S={self.lock_wait_s}s, or the nightly run"
                " gives up while the dispatcher is still confirming a kill")
        if self.timeout_max_s != spec_mod.TIMEOUT_MAX:
            raise ConfigError(
                f"QFD_TIMEOUT_MAX_S={self.timeout_max_s} disagrees with"
                f" spec.TIMEOUT_MAX={spec_mod.TIMEOUT_MAX}")

    # --- startup preconditions -------------------------------------------
    def trusted_path(self, *parts):
        """Resolve a path INSIDE the trusted checkout, or raise (NC10).

        Applied at startup as well as per job: a symlink planted in the trusted
        directory must not become a trusted path just because nobody looked.
        """
        root = os.path.realpath(self.trusted_dir)
        real = os.path.realpath(os.path.join(root, *parts))
        if real != root and not real.startswith(root + os.sep):
            raise ConfigError(
                f"{os.path.join(*parts)} resolves to {real}, outside the"
                f" trusted directory {root}")
        return real

    def check_startup(self, *, group_of=None, my_groups=None, stat=os.stat):
        """Every precondition, fail-closed. Revision 5 validated only the
        training lock, so a missing or mis-permissioned intent directory would
        have silently restored starvation."""
        problems = []
        group_of = group_of or (lambda gid: grp.getgrgid(gid).gr_name)
        my_groups = my_groups if my_groups is not None else os.getgroups()

        for name in ("trusted_dir", "state_dir", "runs_dir", "lock_file",
                     "intent_dir", "lock_migrated_marker"):
            if not getattr(self, name):
                problems.append(f"{name} is unset")
        if problems:
            return problems

        try:
            self.trusted_path("qfd.py")
        except ConfigError as e:
            problems.append(str(e))

        # The cron-migration marker. An un-migrated cron entry locks a DIFFERENT
        # inode, which is no mutex at all (design D5).
        if not os.path.isfile(self.lock_migrated_marker):
            problems.append(
                f"the cron-migration marker {self.lock_migrated_marker} is"
                " absent; run `phase2-setup.sh cron-lock-path` first. An"
                " un-migrated cron entry locks a different inode.")

        try:
            st = stat(self.lock_file)
        except OSError as e:
            problems.append(f"{self.lock_file} is not stat-able: {e}")
        else:
            # daily_walk_forward.sh:213 opens it with `exec 9>` -- a WRITE open --
            # so a shared namespace is not enough; the inode must be
            # group-writable by a group qfd belongs to.
            if not (st.st_mode & 0o020):
                problems.append(
                    f"{self.lock_file} is not group-writable (mode"
                    f" {st.st_mode & 0o777:04o}); the nightly script's"
                    " `exec 9>` would fail fatally")
            if st.st_gid not in my_groups:
                problems.append(
                    f"{self.lock_file} is group {st.st_gid}, which qfd is not in")

        try:
            st = stat(self.intent_dir)
        except OSError as e:
            problems.append(f"{self.intent_dir} is not stat-able: {e}")
        else:
            if not (st.st_mode & 0o2000):
                # Without setgid, a marker's group comes from the deploy user's
                # primary group and its mode from that user's umask, so under
                # umask 077 qfd could not read the declaration and would admit
                # straight through it.
                problems.append(
                    f"{self.intent_dir} is not setgid (mode"
                    f" {st.st_mode & 0o7777:04o}); marker groups and modes would"
                    " be inherited from the deploy user's umask")
            if st.st_gid not in my_groups:
                problems.append(
                    f"{self.intent_dir} is group {st.st_gid}, which qfd is not in")
            if not os.access(self.intent_dir, os.R_OK | os.W_OK | os.X_OK):
                problems.append(
                    f"{self.intent_dir} is not readable and writable by qfd,"
                    " which must be able to unlink a stale marker")

        if not os.access(self.runs_dir, os.R_OK | os.X_OK):
            problems.append(f"{self.runs_dir} is not traversable")
        return problems


# --- the DB owner thread -------------------------------------------------
class DbOwner(threading.Thread):
    """One thread owns the SQLite connection; everything else calls through it.

    `sqlite3.connect()` binds a connection to its creating thread, so sharing a
    Store between the socket-accept thread and three workers would raise
    ProgrammingError. Serialising through one thread also serialises the hash
    chain for free, which is what makes `_append`'s read-then-insert of `seq`
    safe.
    """

    def __init__(self, db_path, **store_kw):
        super().__init__(name="db-owner", daemon=True)
        self._path = db_path
        self._store_kw = store_kw
        self._q = queue.Queue()
        self._ready = threading.Event()
        self._error = None
        self.store = None

    def run(self):
        try:
            self.store = store_mod.Store(self._path, **self._store_kw)
        except Exception as e:                       # pragma: no cover
            self._error = e
            self._ready.set()
            return
        self._ready.set()
        while True:
            item = self._q.get()
            if item is None:
                break
            method, args, kwargs, slot, done = item
            try:
                slot.append(("ok", getattr(self.store, method)(*args, **kwargs)))
            except BaseException as e:               # noqa: BLE001 - relayed
                slot.append(("err", e))
            finally:
                done.set()
        self.store.close()

    def start(self):
        super().start()
        self._ready.wait()
        if self._error:
            raise self._error
        return self

    def call(self, method, *args, **kwargs):
        slot, done = [], threading.Event()
        self._q.put((method, args, kwargs, slot, done))
        done.wait()
        status, value = slot[0]
        if status == "err":
            raise value
        return value

    def stop(self):
        self._q.put(None)
        self.join(timeout=10)


# --- the drain gate ------------------------------------------------------
class IntentGate:
    """The nightly run's declaration of intent, as marker FILES, not a lock.

    A shared flock barges past a queued exclusive waiter -- verified on this
    host -- so a lock-based gate inherits exactly the starvation it exists to
    stop. A file's existence is visible to every reader in every order, so
    there is nothing to contend for and nothing to barge (design D10a).
    """

    def __init__(self, intent_dir, *, lock_wait_s, stale_margin_s,
                 pid_exists=None):
        self.dir = intent_dir
        self.lock_wait_s = lock_wait_s
        self.stale_margin_s = stale_margin_s
        self._pid_exists = pid_exists or _pid_exists

    def scan(self, now=None):
        """Return (blocked, notes). `blocked` means admit nothing this round."""
        now = now if now is not None else time.time()
        notes, blocked = [], False
        try:
            names = sorted(os.listdir(self.dir))
        except OSError as e:
            # Fail closed: a gate we cannot read is a gate we must assume is shut.
            return True, [f"intent dir {self.dir} unreadable ({e}); failing closed"]

        for name in names:
            path = os.path.join(self.dir, name)
            if name.endswith(".tmp"):
                continue                     # a publish in progress; not yet live
            m = _MARKER_RE.match(name)
            if not m:
                blocked = True
                notes.append(f"unparseable marker name {name!r}; failing closed")
                self._maybe_escape(path, now, notes)
                continue
            parsed = self._read(path)
            if parsed is None:
                # A file that cannot be parsed cannot be shown to be stale.
                blocked = True
                notes.append(f"unreadable or malformed marker {name!r};"
                             " failing closed")
                self._maybe_escape(path, now, notes)
                continue
            pid, deadline = parsed
            if self._pid_exists(pid) and deadline > now:
                blocked = True
                notes.append(f"live nightly intent {name} (pid {pid},"
                             f" {int(deadline - now)}s remaining)")
            else:
                # Loudly, because a stale marker means a nightly run died.
                notes.append(f"STALE intent marker {name} (pid {pid} alive="
                             f"{self._pid_exists(pid)}, deadline passed="
                             f"{deadline <= now}); unlinking")
                self._unlink(path, notes)
        return blocked, notes

    def _maybe_escape(self, path, now, notes):
        """The escape hatch for a corrupt marker: age beyond LOCK_WAIT_S plus
        the margin. Corruption delays the loop instead of ending it."""
        try:
            age = now - os.stat(path).st_mtime
        except OSError:
            return
        if age > self.lock_wait_s + self.stale_margin_s:
            notes.append(f"corrupt marker {os.path.basename(path)} is"
                         f" {int(age)}s old, beyond the escape hatch; unlinking")
            self._unlink(path, notes)

    @staticmethod
    def _read(path):
        try:
            with open(path) as fh:
                body = fh.read(4096)
        except OSError:
            return None
        fields = {}
        for line in body.splitlines():
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()
        try:
            return int(fields["pid"]), int(fields["deadline"])
        except (KeyError, ValueError):
            return None

    @staticmethod
    def _unlink(path, notes):
        try:
            os.unlink(path)
        except OSError as e:
            notes.append(f"could not unlink {path}: {e}")


def _pid_exists(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM
    return True


# --- the training mutex --------------------------------------------------
class Revoked(Exception):
    """This hold's lock was taken away; the phase must not start."""


class DeadlineExpired(Exception):
    """The outer hold budget ran out. Never a reason to release the mutex on its
    own -- forced cleanup and confirmation still have to run."""


class Cancelled(Exception):
    """An operator asked for this run to stop."""


class StartFailed(Exception):
    """`docker create` answered no, and a probe then found the name unbound: as
    far as anything can tell, nothing was created and nothing was started.

    Classification only. The resource row is RETAINED like any other, because a
    probe is a reading rather than a proof -- see `_create_then_start`.
    """

    error_class = "container_start_failed"


class StartUnconfirmed(Exception):
    """`docker create` did not answer, so whether the name is bound is UNKNOWN.

    Fail closed: the resource row stays live, which keeps the descriptor and the
    reservation held until a confirmation path can positively account for the
    container. An unknown is not an absence.
    """

    error_class = "start_unconfirmed"


class LockHeld(Exception):
    """The lock could not be taken without blocking. Never waited on: a worker
    that blocks while holding anything turns a momentary hold into a long one."""


class TrainingLock:
    """One open file description per job.

    `flock` ownership is per open file description -- confirmed by experiment --
    so a descriptor shared between workers loses the lock the moment its first
    user closes it. Each job therefore opens the file afresh.
    """

    def __init__(self, path, lane):
        self.path = path
        self.lane = lane
        self.fd = None

    def acquire(self):
        # Opened for WRITE, matching daily_walk_forward.sh:213's `exec 9>`, so
        # both sides contend on the same inode with the same access.
        fd = os.open(self.path, os.O_WRONLY)
        mode = fcntl.LOCK_SH if self.lane == "light" else fcntl.LOCK_EX
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise LockHeld(f"{self.lane} lock unavailable on {self.path}")
        self.fd = fd
        return self

    def release(self):
        """Closing the descriptor releases the lock. Called ONLY after every
        recorded container is confirmed stopped."""
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    @property
    def held(self):
        return self.fd is not None


# --- bounded log capture -------------------------------------------------
class BoundedWriter:
    """Stops at the cap, appends a marker, and reports overflow.

    Docker's own driver is `none` (Task 5), so this is the only place the bytes
    land -- and the only place a runaway log can be stopped.
    """

    MARKER = b"\n[qfd] log truncated at cap; container killed\n"

    def __init__(self, path, cap_bytes):
        self.path = path
        self.cap = cap_bytes
        self.written = 0
        self.overflowed = False
        self._fh = open(path, "wb")

    def write(self, chunk):
        if self.overflowed:
            return 0
        room = self.cap - self.written
        if len(chunk) <= room:
            self._fh.write(chunk)
            self._fh.flush()
            self.written += len(chunk)
            return len(chunk)
        if room > 0:
            self._fh.write(chunk[:room])
            self.written += room
        self._fh.write(self.MARKER)
        self._fh.flush()
        self.overflowed = True
        return room

    def close(self):
        self._fh.close()

    # A context manager, because the alternative is remembering to close on
    # every exit path -- and the raising paths are exactly the ones that got
    # forgotten. `close()` is idempotent on a file object, so the explicit
    # close() in the normal path and this one do not conflict.
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# --- docker ---------------------------------------------------------------
class Docker:
    """Every call under its own subprocess timeout, so a hung daemon cannot
    extend a hold past the outer deadline."""

    def __init__(self, runner=None):
        self._runner = runner or self._subprocess_runner

    @staticmethod
    def _subprocess_runner(argv, env, timeout):
        merged = dict(os.environ)
        merged.update(env or {})
        return subprocess.run(argv, env=merged, capture_output=True, text=True,
                              timeout=timeout)

    def run(self, argv, env=None, timeout=60):
        return self._runner(argv, env, timeout)

    # Statuses that are NOT a confirmed stop, and `created` is the one that
    # matters. `{{.State.Running}}` was the original probe and it answers
    # "false" for a container that has been created but not started -- which
    # the confirmation paths then read as "stopped" and release the mutex over.
    # Under the create-then-start protocol that window is deliberately
    # reachable (the name is bound first, on purpose), so the probe has to
    # distinguish "has not run yet" from "has finished".
    #
    # `removing` counts as live for the same fail-closed reason: removal in
    # progress is not removal completed.
    LIVE_STATUSES = frozenset({"created", "running", "restarting", "paused",
                               "removing"})
    STOPPED_STATUSES = frozenset({"exited", "dead"})

    def is_running(self, container_id, timeout=15):
        """POSITIVE confirmation only. An error, a timeout or an unparseable
        answer is 'unknown', and unknown is never treated as stopped."""
        try:
            p = self.run(["docker", "inspect", "-f", "{{.State.Status}}",
                          container_id], timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        out = (p.stdout or "").strip()
        if p.returncode != 0:
            # "No such object" IS a positive answer: the container is gone.
            if "No such object" in (p.stderr or ""):
                return False
            return None
        if out in self.LIVE_STATUSES:
            return True
        if out in self.STOPPED_STATUSES:
            return False
        return None


# --- run ids -------------------------------------------------------------
def make_run_id(kind, sha, seq, now=None):
    """`<kind>-<YYYYmmddTHHMMSSZ>-<sha[:12]>-<seq>`.

    `seq` comes from the event chain, so ids sort chronologically and cannot
    collide -- a collision would clobber another run's directory.
    """
    stamp = time.strftime("%Y%m%dT%H%M%SZ",
                          time.gmtime(now if now is not None else time.time()))
    return f"{kind}-{stamp}-{sha[:_RUN_ID_SHA_LEN]}-{seq}"


def peer_uid(conn):
    """SO_PEERCRED. Two purposes, kept separate: it AUTHORISES on the admin
    socket and it is AUDITED on either. Revision 8 conflated them."""
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                          struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


def file_digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def free_disk_mb(path):
    st = os.statvfs(path)
    return (st.f_bavail * st.f_frsize) // (1024 * 1024)


def checkout_commit(start, _depth=8):
    """The commit HEAD points at in the checkout containing `start`, or
    "unknown".

    Reads `.git` DIRECTLY rather than shelling out to git, for two reasons that
    are both about who qfd is. It runs as a nologin system user against a
    checkout owned by someone else, and modern git REFUSES such a repository
    ("detected dubious ownership") unless it is listed in safe.directory -- so
    the subprocess would report "unknown" on a perfectly good checkout. And this
    runs during start-up, where a subprocess against a wedged filesystem is a
    hang rather than an answer.

    Total by construction: anything unreadable, unexpected or not a hex sha is
    "unknown", because this is a diagnostic and must never be the reason the
    daemon fails to start.
    """
    path = os.path.realpath(start or ".")
    for _ in range(_depth):
        git = os.path.join(path, ".git")
        if os.path.exists(git):
            break
        parent = os.path.dirname(path)
        if parent == path:
            return "unknown"
        path = parent
    else:
        return "unknown"
    try:
        if os.path.isfile(git):
            # A worktree or submodule: `.git` is a file naming the real dir.
            with open(git) as fh:
                line = fh.read().strip()
            if not line.startswith("gitdir:"):
                return "unknown"
            git = os.path.realpath(
                os.path.join(path, line.split(":", 1)[1].strip()))
        with open(os.path.join(git, "HEAD")) as fh:
            head = fh.read().strip()
        if not head.startswith("ref:"):
            return head if _is_sha(head) else "unknown"
        ref = head.split(":", 1)[1].strip()
        loose = os.path.join(git, ref)
        if os.path.isfile(loose):
            with open(loose) as fh:
                sha = fh.read().strip()
            return sha if _is_sha(sha) else "unknown"
        # Packed: a checkout that has been gc'd has no loose ref for its branch.
        with open(os.path.join(git, "packed-refs")) as fh:
            for row in fh:
                if row.startswith("#") or row.startswith("^"):
                    continue
                parts = row.split()
                if len(parts) == 2 and parts[1] == ref and _is_sha(parts[0]):
                    return parts[0]
    except OSError:
        return "unknown"
    return "unknown"


def _is_sha(value):
    return len(value) == 40 and all(c in "0123456789abcdef" for c in value)


def _gid(name):
    """The gid of a host group, or None when it does not exist.

    None means "leave ownership alone", which is right for the unit tests and in
    development. In production both groups exist -- `phase2-setup.sh
    dispatch-user` creates them and dies if `qfrun` is not gid 10001, because
    the trusted image bakes 10001 in.
    """
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        return None


def dir_size_mb(path):
    """Apparent size of a directory tree, in MiB. Used to sample `out/` against
    OUT_QUOTA: a sample is a bound, not a guarantee (design §4.5 measure 2), and
    the honest word for it is in that section."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total // (1024 * 1024)


def cgroup_current_bytes(container_id, docker):
    """The container's `memory.current`, for the high-water mark parent §15 asks
    for. Read through `docker inspect`-reported cgroup paths rather than guessed,
    and any failure is simply "no sample" -- this is telemetry, not a control."""
    try:
        p = docker.run(["docker", "inspect", "-f", "{{.State.Pid}}",
                        container_id], timeout=10)
        pid = (p.stdout or "").strip()
        if p.returncode != 0 or not pid.isdigit() or pid == "0":
            return None
        with open(f"/proc/{pid}/cgroup") as fh:
            rel = fh.read().strip().split(":")[-1]
        with open(f"/sys/fs/cgroup{rel}/memory.current") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError, IndexError):
        return None


class Refused(Exception):
    """A request that gets a reason, not a crash. A refusal is never a crash:
    the caller gets the reason and the record keeps it."""


class Dispatcher:
    """Protocol handling and admission. Docker and git are injected so the
    protocol suite can run with no daemon, no network and no privileges."""

    def __init__(self, cfg, db, *, docker=None, src=None, clock=None):
        self.cfg = cfg
        self.db = db
        self.docker = docker or Docker()
        self.src = src
        self.clock = clock or time.time
        self.gate = IntentGate(cfg.intent_dir,
                               lock_wait_s=cfg.lock_wait_s,
                               stale_margin_s=cfg.marker_stale_margin_s)
        self._id_lock = threading.Lock()
        self.started_at = utcnow()
        # Provenance, and the one field that answers "is the process running the
        # code that was reviewed?" (design §7 risk 4: qfd EXECUTES from the
        # trusted checkout, so the code and the process must move together).
        # READ FROM THE CHECKOUT, not from the unit or a file written at install
        # time: a value stamped by the installer goes stale the moment anyone
        # runs `git pull` and `systemctl restart` by hand, and a stale commit is
        # worse than no commit -- "unknown" prompts a question, a wrong sha ends
        # one. The env var still wins, for tests and for a deployment that has no
        # checkout at all.
        self.commit = (os.environ.get("QFD_COMMIT")
                       or checkout_commit(self.cfg.trusted_dir))
        # run_id -> Hold, for every job whose lock this process is holding.
        # Without it, `cancel` and `force-release` could only edit the database:
        # a RUNNING job would be marked CANCELLED while its container kept its
        # memory, and `force-release` would record a release while the retained
        # flock descriptor stayed open until the next restart -- leaking the very
        # mutex the operator invoked it to recover.
        self.holds = {}
        self._holds_lock = threading.Lock()

    # --- the runtime hold registry ---------------------------------------
    def register_hold(self, hold):
        with self._holds_lock:
            self.holds[hold.run_id] = hold

    def unregister_hold(self, run_id):
        """Called ONLY once the descriptor is actually closed. A CLEANUP_BLOCKED
        job stays registered on purpose: it still holds its lock, and
        `force-release` needs to be able to find it."""
        with self._holds_lock:
            return self.holds.pop(run_id, None)

    def get_hold(self, run_id):
        with self._holds_lock:
            return self.holds.get(run_id)

    # --- admission -------------------------------------------------------
    def cleanup_stall(self):
        """The reason admissions are frozen, or None.

        While any CLEANUP_BLOCKED job exists there are no admissions, and
        `ping`/`status` must NAME it: a silent stall looks exactly like an idle
        dispatcher, which is how an operator loses a night without knowing.
        """
        stuck = self.db.call("list", state="CLEANUP_BLOCKED", limit=10)
        if not stuck:
            return None
        return {
            "reason": "cleanup_blocked",
            "run_ids": [j["run_id"] for j in stuck],
            "detail": ("a job's workload could not be confirmed stopped; it"
                       " retains its lock and reservation. Resolve with"
                       " `qfadmin force-release <run-id>"
                       f" --{FORCE_RELEASE_FLAG.replace('_', '-')}` after"
                       " verifying by hand."),
        }

    def may_admit(self):
        """(ok, reason). The gate is READ, not held: step 5 of the admission
        sequence has nothing to release."""
        stall = self.cleanup_stall()
        if stall:
            return False, stall["reason"]
        blocked, notes = self.gate.scan(self.clock())
        for note in notes:
            log.warning("intent gate: %s", note)
        if blocked:
            return False, "nightly_intent"
        return True, ""

    # --- protocol --------------------------------------------------------
    def handle(self, op, payload, uid, *, admin=False):
        """One request in, one response dict out. Never raises to the socket
        layer: an unhandled exception there is a one-line denial of service."""
        table = ADMIN_OPS if admin else CLIENT_OPS
        if op not in table:
            # Refused BY NAME, so op growth cannot be silent -- and so a client
            # cannot discover the admin table by probing.
            return {"ok": False, "error": f"unknown op {op!r}",
                    "ops": list(table)}
        try:
            handler = getattr(self, "_op_" + op.replace("-", "_"))
            return {"ok": True, **handler(payload, uid)}
        except Refused as e:
            return {"ok": False, "error": str(e)}
        except spec_mod.SpecError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:                        # noqa: BLE001
            log.exception("op %s failed", op)
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _op_ping(self, payload, uid):
        stall = self.cleanup_stall()
        return {
            "commit": self.commit,
            "schema": SCHEMA_VERSION,
            "started_at": self.started_at,
            "lanes": {lane: self.db.call("lane_busy", lane)
                      for lane in ("light", "heavy")},
            "admitted_mem_mb": self.db.call("admitted_mem_mb"),
            "mem_budget_mb": self.cfg.mem_budget_mb,
            "free_disk_mb": free_disk_mb(self.cfg.runs_dir),
            "stall": stall,
        }

    def _op_submit(self, payload, uid):
        raw = payload.get("spec")
        if not isinstance(raw, dict):
            raise Refused("submit needs a 'spec' object")
        # The payload's own idea of who is calling is IGNORED. SO_PEERCRED wins.
        effective = spec_mod.normalize(raw)

        queued = self.db.call("queued_count_for_uid", uid)
        if queued >= self.cfg.queued_cap_per_uid:
            raise Refused(
                f"uid {uid} already has {queued} queued jobs, at the cap of"
                f" {self.cfg.queued_cap_per_uid}")

        now = utcnow()
        with self._id_lock:
            seq = self.db.call("head")[0] + 1
            run_id = make_run_id(effective["kind"], effective["source_sha"], seq,
                                 self.clock())
            # A job whose memory exceeds the CURRENT budget is QUEUED, not
            # refused: contention is not invalidity, and it will fit later.
            self.db.call("submit", effective, run_id=run_id, uid=uid, now=now)
        return {"run_id": run_id, "state": "QUEUED",
                "spec_hash": spec_mod.spec_hash(effective)}

    def _op_status(self, payload, uid):
        run_id = payload.get("run_id")
        job = self.db.call("get", run_id) if isinstance(run_id, str) else None
        if job is None:
            raise Refused(f"no such run {run_id!r}")
        job["spec"] = json.loads(job["spec_json"])
        return {"job": job, "stall": self.cleanup_stall()}

    def _op_list(self, payload, uid):
        limit = payload.get("limit", 20)
        if not isinstance(limit, int) or isinstance(limit, bool) \
                or not 1 <= limit <= 500:
            raise Refused("limit must be an int in [1,500]")
        state = payload.get("state")
        if state is not None and state not in store_mod.STATES:
            raise Refused(f"unknown state {state!r}")
        return {"jobs": self.db.call("list", state=state, limit=limit)}

    def _op_cancel(self, payload, uid):
        run_id = payload.get("run_id")
        job = self.db.call("get", run_id) if isinstance(run_id, str) else None
        if job is None:
            raise Refused(f"no such run {run_id!r}")
        if job["state"] in store_mod.TERMINAL:
            # A cancel that pretends is worse than one that refuses.
            raise Refused(f"{run_id} is already {job['state']}")
        if "CANCELLED" not in store_mod.ALLOWED.get(job["state"], set()):
            raise Refused(f"{run_id} cannot be cancelled from {job['state']}")
        now = utcnow()
        hold = self.get_hold(run_id)
        if hold is None:
            # Nothing of ours is running it, so the state change IS the whole
            # cancellation.
            self.db.call("transition", run_id, "CANCELLED", now=now,
                         fields={"finished_at": now,
                                 "error_class": "cancelled", "wall_s": None})
            return {"run_id": run_id, "state": "CANCELLED"}

        # A live job is signalled, not overwritten. Marking it CANCELLED here
        # would undercharge live work: the row leaves ADMITTED_STATES, its
        # reservation is freed, and the container keeps its memory anyway. The
        # runner owns the terminal transition, after confirmed shutdown.
        hold.cancel_requested.set()
        # Stop everything in the RECORDED inventory, not the runner's in-memory
        # list. `resources` is the inventory of record precisely because it
        # survives a crash and is written before a container can exist; reading
        # the in-memory list would miss a container recorded by a previous
        # process and re-adopted by this one.
        for res in self.db.call("resources_for", run_id, unreleased_only=True):
            self.stop_container(res["container_id"])
        return {"run_id": run_id, "state": "CANCELLING",
                "detail": ("the container is being stopped; the run reaches"
                           " CANCELLED once shutdown is confirmed")}

    def stop_container(self, cid):
        """Best-effort stop, each call under its own subprocess timeout."""
        try:
            self.docker.run(["docker", "stop", "-t",
                             str(self.cfg.stop_timeout_s), cid],
                            timeout=self.cfg.stop_timeout_s + 15)
        except subprocess.TimeoutExpired:
            log.warning("docker stop timed out for %s", cid)

    def _op_verify_chain(self, payload, uid):
        ok, problems = self.db.call("verify_chain")
        return {"chain_ok": ok, "problems": problems}

    def _op_trusted_paths(self, payload, uid):
        """The live half of NC10: each trusted path with its realpath and
        digest, so a reviewer can compare against the checkout."""
        out = []
        for rel in TRUSTED_FILES:
            entry = {"name": rel}
            try:
                real = self.cfg.trusted_path(rel)
                entry["realpath"] = real
                entry["sha256"] = file_digest(real)
            except (ConfigError, OSError) as e:
                entry["error"] = str(e)
            out.append(entry)
        return {"trusted_dir": os.path.realpath(self.cfg.trusted_dir),
                "paths": out}

    def _op_force_release(self, payload, uid):
        """Admin socket only, and only for root or the configured deploy uid.

        SO_PEERCRED records who called; it does not authorise them. Revision 8
        had this on the client socket, whose group contains `research` -- which
        let the untrusted agent assert it had verified shutdown and release the
        mutex over live work.
        """
        if uid not in (0, self.cfg.admin_uid):
            raise Refused(f"uid {uid} is not authorised to force-release")
        if payload.get(FORCE_RELEASE_FLAG) is not True:
            raise Refused(
                "force-release requires"
                f" --{FORCE_RELEASE_FLAG.replace('_', '-')}")
        run_id = payload.get("run_id")
        job = self.db.call("get", run_id) if isinstance(run_id, str) else None
        if job is None:
            raise Refused(f"no such run {run_id!r}")
        if job["state"] != "CLEANUP_BLOCKED":
            raise Refused(
                f"{run_id} is {job['state']}, not CLEANUP_BLOCKED; force-release"
                " is for a job whose shutdown could not be confirmed")
        # THE POINT OF THE OPERATION is closing the RETAINED DESCRIPTOR.
        # Recording the release without closing it leaves the mutex held until
        # the next restart -- the operator would see FAILED, believe the lock was
        # recovered, and the nightly run would still wait out its full
        # LOCK_WAIT_S.
        hold = self.get_hold(run_id)
        released = False
        if hold is not None:
            with hold.guard:
                # REVOKE FIRST, THEN VERIFY, and in that order for a reason.
                # Revocation is what stops the NEXT phase; it says nothing about
                # a phase that already won the gate. This request may have
                # WAITED on that guard while such a phase created and started a
                # container, and the operator's "nothing is running" was
                # observed before the request was even sent. The flag records
                # that they checked, not WHEN -- so the assertion is about a
                # past that may no longer hold.
                #
                # Whether the hold was ALREADY revoked is therefore read before
                # revoking it, because that is the difference between an
                # assertion about a moving inventory and one about a frozen one.
                frozen = hold.revoked.is_set()
                hold.revoke_under_guard()
                live, unknown = self._inventory_states(run_id)
                if live:
                    log.error("%s: force-release REFUSED: Docker reports %s"
                              " live; the hold has been revoked so no further"
                              " phase can start", run_id, live)
                    raise Refused(
                        f"{run_id}: Docker positively reports {live} still"
                        " live, so nothing was released and the run was left"
                        f" {job['state']}. The hold is now revoked, so no"
                        " further phase can start and shutdown is being"
                        " confirmed; admissions resume by themselves once it"
                        " is. Retrying refuses again for as long as Docker"
                        " still reports it live -- evidence is not overridable"
                        " by assertion.")
                if unknown and not frozen:
                    # THE TWO-PHASE CASE, and the reason the operation is not
                    # idempotent-on-the-first-call. An unknown is exactly what
                    # the flag exists to override -- but overriding it while a
                    # phase could still have started something behind Docker's
                    # silence would release over work nobody can see. So the
                    # first request FREEZES (the revoke above) and refuses; the
                    # second one can trust its own inventory, because nothing
                    # was able to change it in between.
                    log.error("%s: force-release REFUSED (first pass): Docker"
                              " will not answer about %s; the hold is now"
                              " revoked and the inventory frozen", run_id,
                              unknown)
                    raise Refused(
                        f"{run_id}: the hold has now been REVOKED, so no"
                        " further container can start -- but Docker will not"
                        f" say whether {unknown} is gone, and until this call"
                        " the inventory could still have grown. Nothing was"
                        " released. Verify those containers by hand and issue"
                        " the SAME command again: the second pass answers from"
                        " a frozen inventory, so its answer cannot go stale.")
                self._record_force_release(run_id, uid)
                self.unregister_hold(run_id)
                if hold.lock is not None and hold.lock.held:
                    hold.lock.release()
                    released = True
        else:
            # No hold means no descriptor of ours to close, but the transition
            # still frees the memory reservation, so the live check still
            # applies: freeing a reservation over a live container overcommits
            # the host just as surely as freeing the mutex does.
            #
            # No two-phase pass here, and not as a shortcut: with no registered
            # hold there is no phase gate to win, so nothing can add to this
            # inventory. It is already frozen.
            live, _unknown = self._inventory_states(run_id)
            if live:
                raise Refused(
                    f"{run_id}: Docker positively reports {live} still live;"
                    " nothing was released.")
            self._record_force_release(run_id, uid)
        log.warning("%s: force-released by uid %s; descriptor closed=%s",
                    run_id, uid, released)
        return {"run_id": run_id, "state": "FAILED", "released_by_uid": uid,
                "descriptor_closed": released}

    def _inventory_states(self, run_id):
        """(live, unknown) over the recorded inventory.

        Two lists rather than one, because the two answers are overridden by
        different things. A POSITIVE "live" is evidence, and evidence beats an
        operator's assertion outright. An UNKNOWN is the situation the assertion
        exists for -- but it can only be trusted once the inventory it was made
        about can no longer change.
        """
        live, unknown = [], []
        for res in self.db.call("resources_for", run_id, unreleased_only=True):
            cid = res["container_id"]
            state = self.docker.is_running(cid)
            if state is True:
                live.append(cid)
            elif state is None:
                unknown.append(cid)
        return live, unknown

    def _record_force_release(self, run_id, uid):
        now = utcnow()
        # Recorded as an event carrying the caller's uid: audit and
        # authorisation are both needed, separately.
        self.db.call("set_pin", run_id, "force_released_by_uid", str(uid),
                     now=now)
        self.db.call("transition", run_id, "FAILED", now=now,
                     fields={"finished_at": now,
                             "error_class": "force_released"})


class SocketServer(threading.Thread):
    """One connection, one JSON request line, one JSON response line."""

    def __init__(self, path, dispatcher, *, admin, group=None, mode=0o660):
        super().__init__(name=f"sock-{'admin' if admin else 'client'}",
                         daemon=True)
        self.path = path
        self.dispatcher = dispatcher
        self.admin = admin
        self.group = group
        self.mode = mode
        self._stop = threading.Event()
        self.sock = None

    def bind(self):
        if os.path.exists(self.path):
            os.unlink(self.path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        if self.group is not None:
            os.chown(self.path, -1, self.group)
        os.chmod(self.path, self.mode)
        self.sock.listen(16)
        self.sock.settimeout(0.5)
        return self

    def run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn,),
                             daemon=True).start()

    def _serve(self, conn):
        try:
            conn.settimeout(30)
            uid = peer_uid(conn)
            line = self._read_line(conn)
            if line is None:
                self._reply(conn, {"ok": False,
                                   "error": "request too large or unterminated;"
                                            f" cap is {MAX_REQUEST_BYTES} bytes"
                                            " and it must end with a newline"})
                return
            try:
                req = json.loads(line)
                if not isinstance(req, dict):
                    raise ValueError("a request must be a JSON object")
            except ValueError as e:
                # Malformed JSON gets an error response and the daemon survives.
                self._reply(conn, {"ok": False, "error": f"bad JSON: {e}"})
                return
            op = req.get("op")
            payload = req.get("payload") or {}
            if not isinstance(payload, dict):
                self._reply(conn, {"ok": False,
                                   "error": "payload must be an object"})
                return
            self._reply(conn, self.dispatcher.handle(op, payload, uid,
                                                     admin=self.admin))
        except Exception:                             # noqa: BLE001
            log.exception("connection handler failed")
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _read_line(conn):
        """Bounded read. An unbounded one is a trivial memory denial of
        service on a socket the untrusted user can reach."""
        buf = bytearray()
        while True:
            nl = buf.find(b"\n")
            if nl != -1:
                # The cap is checked against the LINE, not against how much has
                # arrived: a single recv can overshoot the cap and carry the
                # newline with it, and returning that line would hand an
                # over-cap request to json.loads anyway.
                return None if nl > MAX_REQUEST_BYTES else bytes(buf[:nl])
            if len(buf) > MAX_REQUEST_BYTES:
                return None
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                return None
            if not chunk:
                return None                     # closed with no newline
            buf.extend(chunk)

    @staticmethod
    def _reply(conn, obj):
        try:
            conn.sendall(json.dumps(obj).encode() + b"\n")
        except OSError:
            pass

    def stop(self):
        self._stop.set()
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        try:
            os.unlink(self.path)
        except OSError:
            pass


def parse_iso(stamp):
    """UTC ISO instant -> epoch seconds. The store keeps instants, not
    durations, so every deadline comparison goes through here."""
    return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))


def iso_at(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class BuildLock:
    """The dispatcher-private build mutex.

    Each attempt opens its OWN descriptor and waits at most
    `BUILD_LOCK_WAIT_S`; the build phase's deadline includes that wait,
    otherwise one timed-out build lets the next job wait 900 s and then build
    for another 1800.
    """

    def __init__(self, path, wait_s):
        self.path = path
        self.wait_s = wait_s
        self.fd = None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600)
        deadline = time.time() + self.wait_s
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.time() >= deadline:
                    os.close(self.fd)
                    self.fd = None
                    raise LockHeld(
                        f"build lock not acquired within {self.wait_s}s")
                time.sleep(1)

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        return False


class Hold:
    """One job's claim on everything contended: the training descriptor, the
    memory reservation (implied by its non-terminal state) and the persisted
    outer deadline."""

    def __init__(self, job, lock, deadline_epoch):
        self.job = job
        self.lock = lock
        self.deadline_epoch = deadline_epoch
        self.containers = []          # (role, container_id), as recorded
        # The image id is carried HERE, not re-read from `job`. `job` is the row
        # captured at dequeue, so `job["image_digest"]` is still None after
        # _ensure_image has written it to SQLite -- which made the handoff build
        # an argv with image_ref=None, raise SandboxError, and finish every
        # artifact-producing job as `internal`.
        self.image_id = None
        # Set by `cancel`/forced cleanup so a running phase can notice without
        # being killed from outside.
        self.cancel_requested = threading.Event()
        # Set when this hold's lock has been taken away from the worker that owns
        # it -- by the reaper after a lease reclaim, or by `force-release`.
        self.revoked = threading.Event()
        # Serialises PHASE START against REVOCATION, and it has to be a lock
        # rather than another flag. Checking `revoked` and then starting a phase
        # is a time-of-check/time-of-use race: revocation can land in the gap,
        # release the mutex, and the phase then starts a container anyway. No
        # number of additional checks closes that -- only holding something
        # across both the decision and the act does.
        #
        # The critical section is deliberately SHORT: decide, then record the
        # container. Starting it happens outside, which is safe because once the
        # row exists `Reaper.release_hold` can see it and will refuse to release
        # the descriptor. The two halves together give the real invariant: a
        # container is never started after the mutex is freed, and the mutex is
        # never freed while a container is recorded live.
        self.guard = threading.RLock()

    @contextlib.contextmanager
    def phase_gate(self, what):
        """Enter a phase, or refuse. Held across the decision AND the record."""
        with self.guard:
            if self.revoked.is_set():
                raise Revoked(f"{self.run_id}: hold revoked before {what}")
            if self.expired():
                raise DeadlineExpired(
                    f"{self.run_id}: hold deadline passed before {what}")
            yield

    def revoke_under_guard(self):
        """Mark revoked. The caller must already hold `guard`."""
        self.revoked.set()
        self.cancel_requested.set()

    @property
    def run_id(self):
        return self.job["run_id"]

    def remaining(self, now=None):
        return self.deadline_epoch - (now if now is not None else time.time())

    def expired(self, now=None):
        return self.remaining(now) <= 0


class Runner:
    """One job, start to terminal state.

    The ordering here is the whole point and is not negotiable: everything
    contended is acquired between `peek` and `dequeue`, the image is built only
    after the lease, and the training descriptor is closed only after Docker has
    positively confirmed every recorded container stopped.
    """

    def __init__(self, cfg, db, dispatcher, *, docker=None, src=None,
                 owner="qfd"):
        self.cfg = cfg
        self.db = db
        self.disp = dispatcher
        self.docker = docker or dispatcher.docker
        self.src = src if src is not None else dispatcher.src
        self.owner = owner
        # How often the confirmation loop re-asks Docker. An attribute rather
        # than a literal so a test can drive the loop without waiting out the
        # real kill-confirmation window.
        self.poll_interval_s = 2
        # Sampling cadences (design §4.5 and parent §15). Attributes for the
        # same reason.
        self.out_sample_interval_s = 2
        self.mem_sample_interval_s = 5
        # How long `docker create` may take to acknowledge that the container
        # exists. This is the only Docker call made while the phase gate is
        # held, so it is bounded tightly: a wedged daemon must stall the reaper
        # for seconds, not for a hold.
        self.create_timeout_s = 60
        # (run_id, role) pairs whose sentinel conversion FAILED, and the lock
        # that lets the reaper thread drain them while a worker thread adds to
        # them. In memory on purpose: the conversion failed because the store
        # was unwell, so the record of that failure cannot live in the store. It
        # does not need to -- the pin itself is durable, and if this process dies
        # the pin's other owner (`Recovery._settle_unissued`) is exactly the
        # startup path. What was missing was an owner for the process STAYING
        # ALIVE through a transient DB failure. See `_settle_if_unissued`.
        self.unsettled = set()
        self.unsettled_lock = threading.Lock()
        self.qfrun_gid = _gid("qfrun")
        self.qfclient_gid = _gid("qfclient")
        # Injectable so the whole execute path is testable without a docker
        # binary. It was a bare `subprocess.Popen` call, which is a large part of
        # why this path shipped both unwired and with a None image reference.
        self.spawn = subprocess.Popen

    # --- the admission sequence ------------------------------------------
    def try_one(self, lane):
        """Steps 1-7, in this order and no other. Returns True if a job ran.

        A lost race at step 6 releases the lock and re-peeks. Contention never
        produces a state transition, which is why there is no LEASED -> QUEUED
        edge to need.
        """
        head = self.db.call("peek", lane)                       # 1
        if head is None:
            return False

        ok, reason = self.disp.may_admit()                      # 2
        if not ok:
            log.info("not admitting into %s: %s", lane, reason)
            return False

        try:                                                    # 3
            lock = TrainingLock(self.cfg.lock_file, lane).acquire()
        except LockHeld as e:
            log.info("lane %s: %s", lane, e)
            return False

        effective = json.loads(head["spec_json"])
        try:
            ok, why = self.db.call(                             # 4
                "admit", effective["mem_limit"],
                free_disk_mb=free_disk_mb(self.cfg.runs_dir))
            if not ok:
                log.info("lane %s not admitted: %s", lane, why)
                lock.release()
                return False

            # 5 is deliberately empty: the gate was READ, not held.
            now_epoch = time.time()
            now = iso_at(now_epoch)
            max_running = self.cfg.light_workers if lane == "light" else 1
            job = self.db.call(                                 # 6
                "dequeue", lane, owner=self.owner, now=now,
                lease_expires_at=iso_at(now_epoch + self.cfg.lease_s),
                hold_deadline_at=iso_at(now_epoch
                                        + self.cfg.job_hold_deadline_s),
                max_running=max_running)
            if job is None:
                # The queue emptied under us, or the lane filled. Release
                # everything and re-peek; nothing transitioned.
                lock.release()
                return False
        except Exception:
            lock.release()
            raise

        hold = Hold(job, lock, parse_iso(job["hold_deadline_at"]))
        self.execute(hold)                                      # 7
        return True

    # --- the lifecycle ---------------------------------------------------
    def run_dir(self, run_id):
        return os.path.join(self.cfg.runs_dir, run_id)

    # Who each part of a run directory belongs to, and why. The service's
    # PRIMARY group is qfd, so anything not chowned here stays qfd:qfd 0750 --
    # which is invisible to both of the identities that need it. An earlier
    # version chowned only out/ and artifacts/, leaving:
    #   * src/  qfd:qfd, so the container (uid/gid 10001:10001) could not
    #     traverse the source worktree it is supposed to run;
    #   * logs/ qfd:qfd, so `qf logs` failed for every qfclient member;
    #   * the run directory itself qfd:qfd, so a client could not even traverse
    #     to artifacts/.
    OWNERSHIP = (
        # (subdir or None for the base, group, mode)
        (None,        "qfclient", 0o750),   # traversable by clients
        ("src",       "qfrun",    0o750),   # the container must read this
        ("out",       "qfrun",    0o2770),  # ...and write this; setgid keeps it
        ("artifacts", "qfclient", 0o750),   # the only thing a client reads
        ("logs",      "qfclient", 0o750),   # `qf logs` reads the file directly
    )

    def prepare_run_dir(self, run_id, *, qfrun_gid=None, qfclient_gid=None):
        """Create the run directory with the ownership OWNERSHIP states.

        chown clears the setgid bit on Linux, so every chmod comes AFTER its
        chown -- `out/` losing 2770 would mean artifacts created by the sandbox
        drifting out of the qfrun group.
        """
        base = self.run_dir(run_id)
        paths = {"base": base}
        gids = {"qfrun": qfrun_gid, "qfclient": qfclient_gid}
        for name, group, mode in self.OWNERSHIP:
            path = base if name is None else os.path.join(base, name)
            os.makedirs(path, exist_ok=True)
            gid = gids.get(group)
            if gid is not None:
                os.chown(path, -1, gid)
            os.chmod(path, mode)
            paths[name or "base"] = path
        return paths

    def execute(self, hold):
        """The body of a run. Every exit path goes through `finish`, which is
        the only place the training descriptor is closed."""
        run_id = hold.run_id
        effective = json.loads(hold.job["spec_json"])
        # Registered before any work: `cancel` and `force-release` must be able
        # to find this hold for as long as it exists.
        self.disp.register_hold(hold)
        renewer = self._start_renewer(hold)
        outcome = ("FAILED", {"error_class": "internal"})
        try:
            # Setup is inside the outer deadline too. It is git and filesystem
            # work on an agent-authored repository, so it is not free, and an
            # earlier version measured the deadline only from the container.
            if hold.expired():
                raise DeadlineExpired("the hold deadline passed before setup")
            paths = self.prepare_run_dir(run_id, qfrun_gid=self.qfrun_gid,
                                         qfclient_gid=self.qfclient_gid)
            # ONE absolute deadline covering ALL the setup git work, passed as
            # an argument. Assigning `src.timeout_s` was wrong twice over: it is
            # shared mutable state on an object both light workers hold, so they
            # overwrote each other's budget; and a per-command ceiling is not a
            # total bound, so `resolve`'s several commands could each honour it
            # and still spend many times the hold together.
            setup_deadline = self._setup_deadline(hold)
            ref = self.src.resolve(effective["source_sha"],
                                   deadline=setup_deadline)
            self.db.call("set_pin", run_id, "source_ref", ref, now=utcnow())
            self.src.add_worktree(effective["source_sha"], paths["src"],
                                  deadline=setup_deadline)
            if hold.expired():
                raise DeadlineExpired("the hold deadline passed during setup")
            if hold.cancel_requested.is_set():
                raise Cancelled("cancelled during setup")

            image_id = self._ensure_image(hold)
            outcome = self._launch(hold, effective, paths, image_id)
        except Revoked as e:
            log.error("%s: %s", run_id, e)
            outcome = ("FAILED", {"error_class": "hold_revoked",
                                  "finished_at": utcnow()})
        except DeadlineExpired as e:
            # FAILED, not TIMEOUT: these raises happen before any container
            # exists, so the job is still LEASED -- and the transition table has
            # no LEASED -> TIMEOUT edge. TIMEOUT is for a workload that ran too
            # long, which is the in-flight path in `_launch`.
            log.error("%s: %s", run_id, e)
            outcome = ("FAILED", {"error_class": "hold_deadline_expired",
                                  "finished_at": utcnow()})
        except Cancelled:
            outcome = ("CANCELLED", {"error_class": "cancelled",
                                     "finished_at": utcnow()})
        except (StartFailed, StartUnconfirmed) as e:
            # Neither releases its own row: `finish` runs next, and confirmation
            # is the only thing allowed to account for a name Docker has already
            # been asked about. The two classes differ in what the operator is
            # told, not in what is held.
            log.error("%s: %s", run_id, e)
            outcome = ("FAILED", {"error_class": e.error_class,
                                  "finished_at": utcnow()})
        except source_mod.Timeout as e:
            # A hung fetch is a deadline failure, not a retryable error: the
            # budget it consumed was the mutex's.
            log.error("%s: %s", run_id, e)
            outcome = ("FAILED", {"error_class": "source_timeout",
                                  "finished_at": utcnow()})
        except source_mod.NotPublished as e:
            log.error("%s: %s", run_id, e)
            outcome = ("FAILED", {"error_class": "source_not_published"})
        except LockHeld:
            outcome = ("FAILED", {"error_class": "image_build_lock_timeout"})
        except image_mod.ImageError as e:
            log.error("%s: image: %s", run_id, e)
            outcome = ("FAILED", {"error_class": "image_build_failed"})
        except Exception:
            log.exception("%s: run failed", run_id)
        finally:
            renewer.set()
            self.finish(hold, *outcome)

    def _setup_deadline(self, hold):
        """An ABSOLUTE instant by which all source work for this job must be
        done: the hold's own deadline, capped by the setup allowance so one
        phase cannot consume the whole budget.

        Absolute, not a duration, for the same reason the store keeps lease
        instants rather than lengths: every git command can then subtract the
        clock and get its own share, and the total is bounded no matter how many
        commands run.
        """
        remaining = int(hold.remaining())
        if remaining <= 0:
            raise DeadlineExpired("no budget left for source operations")
        return min(hold.deadline_epoch,
                   time.time() + self.cfg.setup_teardown_allowance_s)

    def _start_renewer(self, hold):
        """A job may run up to TIMEOUT_MAX; any sane lease is shorter, so
        without renewal the reclaimer eats live work."""
        stop = threading.Event()
        interval = max(5, self.cfg.lease_s // 3)

        def loop():
            while not stop.wait(interval):
                if not self.db.call("renew", hold.run_id, owner=self.owner,
                                    lease_expires_at=iso_at(time.time()
                                                            + self.cfg.lease_s),
                                    now=utcnow()):
                    log.warning("%s: lease lost; stopping renewal",
                                hold.run_id)
                    return

        threading.Thread(target=loop, name=f"renew-{hold.run_id}",
                         daemon=True).start()
        return stop

    def _ensure_image(self, hold):
        """After the lease, under its own lock, with a re-check and a timeout.

        Building for a QUEUED job would need a QUEUED -> FAILED edge the state
        table does not have -- which is why this happens at step 7 and the job
        sits in BUILDING throughout.
        """
        self.db.call("transition", hold.run_id, "BUILDING", now=utcnow())
        trusted = os.path.realpath(self.cfg.trusted_dir)
        if hold.expired():
            raise DeadlineExpired("the hold deadline passed before the build")
        budget = min(self.cfg.build_lock_wait_s, int(hold.remaining()))
        with BuildLock(self.cfg.build_lock, budget):
            # Re-check UNDER the lock: two light workers can miss the same key
            # at once, and both hold only LOCK_SH on the training lock, so
            # nothing else would stop them building twice.
            def runner(argv, env, timeout=None):
                # EVERY call here is bounded by what is left of the hold, not
                # just `docker build`. Two bugs lived in the old one line:
                # `docker image inspect` was given the caller's `timeout=None`
                # and so fell through to a flat 60s regardless of the budget,
                # and `timeout or 60` turned a correctly-computed budget of ZERO
                # back into a full 60 seconds -- a falsy-zero that restored
                # exactly the overrun the arithmetic had just ruled out.
                remaining = int(hold.remaining())
                if remaining <= 0:
                    raise DeadlineExpired(
                        "the hold deadline passed during image preparation")
                if argv[:2] == ["docker", "build"]:
                    argv = ["docker", "build",
                            "--memory", f"{self.cfg.image_build_mem_mb}m",
                            "--force-rm", *argv[2:]]
                    cap = self.cfg.build_timeout_s
                else:
                    cap = DOCKER_CALL_TIMEOUT_S
                return self.docker.run(argv, env, min(cap, remaining))

            tmpdir = os.path.join(self.cfg.state_dir, "build-context")
            tag, image_id = image_mod.ensure_image(
                trusted, lambda argv, env: runner(argv, env), tmpdir=tmpdir,
                trusted_root_dir=trusted)
        self.db.call("transition", hold.run_id, "RUNNING", now=utcnow(),
                     fields={"image_digest": image_id,
                             "started_at": utcnow()})
        self.db.call("set_pin", hold.run_id, "image_tag", tag, now=utcnow())
        hold.image_id = image_id
        return image_id

    def _launch(self, hold, effective, paths, image_id):
        """Run the candidate, then the handoff. Returns (state, fields)."""
        run_id = hold.run_id
        argv = sandbox_mod.docker_create_argv(
            image_ref=image_id, run_id=run_id,
            spec_hash=hold.job["spec_hash"], kind=effective["kind"],
            src_mount=paths["src"], out_mount=paths["out"],
            entrypoint_argv=sandbox_mod.entrypoint_for(effective),
            mem_limit=effective["mem_limit"], cpus=effective["cpus"],
            role="candidate",
            extra_ro_mounts=self._selftest_mounts(effective))

        # The effective timeout is the SMALLER of the job's own and what is left
        # of the outer hold budget (design D10a). `max(1, ...)` used to floor it,
        # which meant an image build that exhausted the budget still started a
        # candidate with a one-second grant -- work admitted past a deadline that
        # had already passed. If there is no budget, there is no run.
        #
        # This check comes BEFORE the writers are opened. It used to come after,
        # so the refusal path raised past two open file objects and leaked them
        # (visible as ResourceWarnings).
        remaining = int(hold.remaining())
        if remaining <= 0:
            raise DeadlineExpired(
                "the hold deadline passed during image preparation; refusing to"
                " start a candidate")
        budget = min(effective["timeout_s"], remaining)

        cap = self.cfg.log_cap_mb * 1024 * 1024
        # An ExitStack, not two bare constructor calls. Everything below can
        # raise -- the gate refuses a revoked hold, the post-record check refuses
        # a spent deadline -- and each of those paths used to unwind past two open
        # file objects. Moving the checks earlier only fixed the checks that
        # existed at the time; this fixes the shape, so a check added later
        # cannot reintroduce it.
        with contextlib.ExitStack() as stack:
            out_w = stack.enter_context(
                BoundedWriter(os.path.join(paths["logs"], "stdout.log"), cap))
            err_w = stack.enter_context(
                BoundedWriter(os.path.join(paths["logs"], "stderr.log"), cap))
            return self._run_candidate(hold, effective, paths, argv, out_w,
                                       err_w, budget)

    def _run_candidate(self, hold, effective, paths, argv, out_w, err_w, budget):
        """The body of the candidate run, with the log writers owned by the
        caller's ExitStack so every exit path closes them."""
        run_id = hold.run_id
        started = time.time()

        # RECORDED BEFORE IT CAN EXIST, and both the record and the spawn happen
        # INSIDE THE GATE. The name is deterministic (`--name qf-<run_id>-<role>`,
        # and run ids never collide) and Docker accepts a name anywhere it accepts
        # an id, so the inventory can be written before `Popen`.
        #
        # The gate is what makes the deadline real here. The check above happens
        # before the log writers are opened and before the synchronous DB call
        # that records the container -- both of which take time -- so a budget
        # that was positive at the check could be spent by the time we spawn.
        # Re-checking inside the same critical section as the spawn is the only
        # arrangement where "no candidate starts past its deadline" is a property
        # rather than a hope. `Popen` returns immediately, so the section stays
        # short.
        with hold.phase_gate("the candidate"), \
                self._unacked_create(run_id, "candidate"):
            container_id = self._record_container(hold, run_id, "candidate")
            # One last look, because the record above is a synchronous
            # round-trip to the DB-owner thread and therefore takes time. The
            # spawn is the irreversible act, so the check has to sit immediately
            # before it -- and if the budget went while we were recording, the
            # row is marked released, because nothing ever started under it. That
            # keeps both invariants at once: no container is ever started before
            # it is recorded, and no row ever claims a container that does not
            # exist.
            if hold.expired():
                self.db.call("release_resource", run_id, role="candidate",
                             container_id=container_id, now=utcnow())
                raise DeadlineExpired(
                    f"{run_id}: hold deadline passed while recording the"
                    " candidate; nothing was started")
            # Created (and PROVEN created) then started, both inside the gate.
            # See `_create_then_start`: a bare `Popen` of `docker run` leaves a
            # window in which the recorded container does not exist yet, and
            # absence is what every confirmation path treats as proof of a stop.
            proc = self._create_then_start(hold, "candidate", argv,
                                           stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE)
        # Re-measured for the same reason the gate re-checks expiry: the
        # argument was computed before the log writers, before the synchronous
        # record and before the synchronous create, and all three take time.
        # The watchers would catch an overrun within a sampling interval, but a
        # budget that is simply correct does not need catching.
        budget = max(0, min(budget, int(hold.remaining())))
        pump = self._pump(proc, out_w, err_w)
        watch = self._start_watchers(hold, paths, container_id, out_w, err_w)
        try:
            proc.wait(timeout=budget)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            self._stop_container(container_id)
            self._reap(proc, container_id)
        finally:
            watch["stop"].set()
        for t in pump:
            t.join(timeout=5)
        out_w.close()
        err_w.close()
        wall_s = time.time() - started
        exit_code = proc.returncode
        rss_kb = (watch["peak_bytes"][0] // 1024) or None

        base = {"exit_code": exit_code, "wall_s": wall_s,
                "finished_at": utcnow(), "rss_high_water_kb": rss_kb}

        # Order matters: a job killed for breaching a containment bound must be
        # reported as that breach, not as whatever exit code the kill produced.
        if hold.revoked.is_set():
            # Classify BEFORE the cancel branch. Revocation sets
            # `cancel_requested` too, so the watcher kills the container -- but a
            # reaper-reclaimed job is not something anybody asked to cancel, and
            # reporting CANCELLED for it would hide a lost lease behind an
            # operator action. (This is classification of a run that already
            # finished; whether the HANDOFF may start is the gate's job, not
            # this check's.)
            return "FAILED", {**base, "error_class": "hold_revoked"}
        if watch["killed_for"][0] == "cancelled" or hold.cancel_requested.is_set():
            # An operator cancel is not a failure, and the state has to say so
            # or `qf list --state FAILED` stops meaning anything.
            #
            # The flag is checked as well as the kill, because a cancel can land
            # in the instant between the workload exiting and this line. The
            # request wins that race deliberately: the handoff is skipped, so no
            # artifacts were collected, and reporting SUCCEEDED for a run whose
            # outputs were never gathered would be the misleading answer.
            return "CANCELLED", {**base, "error_class": "cancelled"}
        if watch["killed_for"][0]:
            return "FAILED", {**base, "error_class": watch["killed_for"][0]}
        if out_w.overflowed or err_w.overflowed:
            return "FAILED", {**base, "error_class": "log_overflow"}
        if timed_out:
            return "TIMEOUT", {**base, "error_class": "timeout"}
        if hold.expired():
            return "TIMEOUT", {**base, "error_class": "hold_deadline_expired"}

        try:
            handoff_class = self._handoff(hold, paths)
        except Revoked:
            # The gate inside `_handoff` refused. An `is_set()` check HERE was
            # the original defect: revocation could land between it and the
            # phase start.
            log.error("%s: hold revoked before the handoff; collecting nothing",
                      run_id)
            return "FAILED", {**base, "error_class": "hold_revoked"}
        except DeadlineExpired:
            return "TIMEOUT", {**base, "error_class": "hold_deadline_expired"}
        except (StartFailed, StartUnconfirmed) as e:
            # The candidate itself finished; it is the collection that did not
            # start. Reporting SUCCEEDED would claim artifacts nobody gathered.
            log.error("%s: %s", run_id, e)
            return "FAILED", {**base, "error_class": e.error_class}
        if handoff_class:
            # A candidate that exited 0 but whose artifacts trusted code could
            # not collect must not read SUCCEEDED.
            return "FAILED", {**base, "error_class": handoff_class}
        state = "SUCCEEDED" if exit_code == 0 else "FAILED"
        return state, {**base,
                       "error_class": None if exit_code == 0 else "nonzero_exit"}

    def _start_watchers(self, hold, paths, container_id, out_w, err_w):
        """The containment monitoring design §4.5 and parent §15 require.

        Three things, none of which can be done after the fact:
          * `out/` sampled against OUT_QUOTA, so a job writing endlessly is
            KILLED at the bound rather than discovered over it afterwards;
          * the log cap enforced by killing when the bounded writer overflows --
            checking `overflowed` only after `proc.wait()` bounds the FILE but
            lets the container run on forever producing bytes nobody reads;
          * `memory.current` sampled for the high-water mark.
        Returns a handle whose `stop` event ends the threads.
        """
        stop = threading.Event()
        peak_bytes = [0]
        killed_for = [None]
        quota_mb = self.cfg.artifact_cap_mb        # OUT_QUOTA, per design §4.5

        def kill(reason):
            if killed_for[0] is None:
                killed_for[0] = reason
                log.error("%s: killing container for %s", hold.run_id, reason)
                self._stop_container(container_id)

        def watch_disk():
            while not stop.wait(self.out_sample_interval_s):
                if dir_size_mb(paths["out"]) > quota_mb:
                    kill("out_quota_exceeded")
                    return
                if out_w.overflowed or err_w.overflowed:
                    kill("log_overflow")
                    return
                if hold.expired():
                    kill("hold_deadline_expired")
                    return
                if hold.cancel_requested.is_set():
                    kill("cancelled")
                    return

        def watch_mem():
            while not stop.wait(self.mem_sample_interval_s):
                current = cgroup_current_bytes(container_id, self.docker)
                if current and current > peak_bytes[0]:
                    peak_bytes[0] = current

        for fn, name in ((watch_disk, "disk"), (watch_mem, "mem")):
            threading.Thread(target=fn, name=f"{name}-{hold.run_id}",
                             daemon=True).start()
        return {"stop": stop, "peak_bytes": peak_bytes, "killed_for": killed_for}

    def _selftest_mounts(self, effective):
        if effective["kind"] != "selftest":
            return ()
        script = os.path.join(os.path.realpath(self.cfg.trusted_dir),
                              "nc13-inside.sh")
        return ((script, "/trusted/nc13-inside.sh"),)

    def _pump(self, proc, out_w, err_w):
        def pump(stream, writer):
            for chunk in iter(lambda: stream.read(65536), b""):
                if writer.write(chunk) == 0 and writer.overflowed:
                    break

        threads = []
        for stream, writer in ((proc.stdout, out_w), (proc.stderr, err_w)):
            t = threading.Thread(target=pump, args=(stream, writer),
                                 daemon=True)
            t.start()
            threads.append(t)
        return threads

    @contextlib.contextmanager
    def _unacked_create(self, run_id, role):
        """Wrap a phase's record-and-create so it CANNOT leave an ownerless
        sentinel behind, however it leaves.

        `ABSENCE_NOT_YET_ISSUED` is deliberately immune to elapsed time, on the
        grounds that the phase holding the gate is still going to ask. That makes
        the phase its owner -- and an owner that walks away without either an
        acknowledgement or a conversion leaves a pin nothing can ever resolve:
        every confirmation path refuses the absence for ever, the job parks in
        CLEANUP_BLOCKED, and the lock, the lane and the reservation stay held
        until a restart or an operator. `Recovery._settle_unissued` covers the
        process dying; this covers the phase merely giving up while the process
        lives.

        A FINALIZER, not a longer list of `except` clauses. `subprocess.run` can
        raise `OSError` (a fork or exec that never got as far as a Docker
        request), a DB call can raise, and the next exception this code learns
        about has not been written yet. Enumerating them means the next one is a
        stall; leaving on any path at all means it is not.

        Idempotent by construction: it converts the SENTINEL only, so an
        acknowledged create (pin cleared) and an answered-but-ambiguous one (pin
        already an instant, written where the answer arrived and the reason is
        legible) both pass through untouched.
        """
        try:
            yield
        finally:
            self._settle_if_unissued(run_id, role)

    def _settle_if_unissued(self, run_id, role):
        """Hand a surviving sentinel over to the bounded window.

        Swallows its own failure on purpose. This runs in a `finally`, usually
        with an exception in flight, and that exception is what the caller has to
        see: raising from here would replace a diagnosis ("docker: cannot fork")
        with a bookkeeping error.

        Swallowing it is not the same as accepting it, and calling startup
        recovery the fallback was wrong: nothing here forces a restart, so a
        transient DB failure -- one that has fully cleared a second later --
        would leave the pin standing with no owner at all for the lifetime of the
        daemon. A stall whose remedy is "wait for a restart nobody will perform"
        is a stall.

        So the failure is REMEMBERED and retried by the reaper
        (`retry_unsettled`), which is the thread that already exists to keep
        asking about things that were not resolved the first time. The queue is
        in memory because the store is what just failed; the pin it is about is
        durable, and a process that dies before the retry lands hands the pin to
        `Recovery._settle_unissued` on the way back up.
        """
        try:
            roles = self.db.call("settle_unissued_creates", run_id,
                                 settles_at=iso_at(time.time()
                                                   + self.cfg.build_settle_s),
                                 now=utcnow(), role=role)
        except Exception:
            with self.unsettled_lock:
                self.unsettled.add((run_id, role))
            log.exception("%s: could not settle the unissued %s create; queued"
                          " for retry, and its pin stands until one succeeds",
                          run_id, role)
            return
        if roles:
            log.warning("%s: the %s phase abandoned its create without an"
                        " acknowledgement; its name settles by removal instead",
                        run_id, role)

    def retry_unsettled(self):
        """Re-attempt every conversion that failed, once per reaper pass.

        Removed from the queue only on SUCCESS -- where success includes the
        store reporting nothing left to convert, since another owner getting
        there first is the same outcome. A failed retry stays queued, because the
        alternative is dropping the last live reference to a pin that nothing
        else will look at until a restart.

        Returns the (run_id, role) pairs it settled, for the log and the tests.
        """
        with self.unsettled_lock:
            pending = sorted(self.unsettled)
        settled = []
        for run_id, role in pending:
            try:
                self.db.call("settle_unissued_creates", run_id,
                             settles_at=iso_at(time.time()
                                               + self.cfg.build_settle_s),
                             now=utcnow(), role=role)
            except Exception:
                log.exception("%s: the %s create still cannot be settled;"
                              " admissions stay frozen and this will be retried",
                              run_id, role)
                continue
            with self.unsettled_lock:
                self.unsettled.discard((run_id, role))
            settled.append((run_id, role))
            log.warning("%s: settled the abandoned %s create on retry", run_id,
                        role)
        return settled

    def _record_container(self, hold, run_id, role):
        """Record the container BEFORE it can exist, and identify it by NAME.

        `jobs.container_id` is candidate-only, so forced cleanup and restart
        recovery inventory `resources` -- which only works if the row is there
        before the container is. The name is what makes that possible: it is
        deterministic (`--name qf-<run_id>-<role>`), run ids never collide, and
        Docker accepts a name anywhere it accepts an id, so `docker inspect` and
        `docker stop` work against it exactly as against the 64-hex id.

        An earlier version started the container and then inspected it for its
        real id. That inverted the ordering the table exists to guarantee: a
        crash in the window left a LIVE container with no `resources` row, and
        recovery would then see an empty inventory, take the build-settle path,
        and release the training mutex over running work.

        With `--rm`, the name disappears when the container is removed, and
        `docker inspect` then answers "No such object" -- a POSITIVE absence,
        which is exactly the confirmation `confirm_all_stopped` needs.

        THAT LAST STEP HAS A PRECONDITION, and forgetting it was the round-6
        defect: absence only means "gone" once the name has been BOUND. Before
        that it means "not yet", and the two are the same string. Which is why
        the row is written here, the name is bound by `_create_then_start` while
        the phase gate is still held, and only then is anything allowed to read
        an absence as a stop.
        """
        cid = sandbox_mod.container_name(run_id, role)
        # THE PIN COMES FIRST, before the row it protects. From the instant a row
        # exists, this name's absence must not be read as proof: the container
        # has not been created yet, so `docker inspect` says "No such object" --
        # and a confirmation pass running concurrently (`resolve_blocked` holds no
        # guard) would release the row as gone while the phase, still inside its
        # gate, goes on to create and start the container. Writing it after the
        # row would leave exactly that window open, and writing it at create time
        # leaves the whole record-to-create gap open.
        #
        # NOT an instant here, but `ABSENCE_NOT_YET_ISSUED`. An instant is a bet
        # that a request already in flight completes within a window; before the
        # request is issued there is nothing to bet on, and a phase stalled past
        # any window is still a phase that is going to issue its create.
        # `_create_then_start` converts this to an instant when it asks.
        #
        # A pin without a row is harmless -- there is nothing to confirm -- which
        # is why this is the safe order.
        self.db.call("set_pin", run_id, store_mod.absence_settles_pin(role),
                     store_mod.ABSENCE_NOT_YET_ISSUED, now=utcnow())
        try:
            self.db.call("add_resource", run_id, role=role, container_id=cid,
                         now=utcnow())
        except store_mod.WorkNotPermitted as e:
            # The run moved out from under this phase: `reclaim` can settle it to
            # FAILED, or move it to CLEANUP_BLOCKED where cleanup has already
            # begun. Either way this phase is no longer entitled to start
            # anything, which is precisely a revoked hold -- so it takes the
            # revoked path: nothing starts, nothing is collected, and no
            # container is ever created, because the record comes first.
            raise Revoked(str(e)) from None
        hold.containers.append((role, cid))
        if role == "candidate":
            self.db.call("renew", run_id, owner=self.owner,
                         lease_expires_at=iso_at(time.time()
                                                 + self.cfg.lease_s),
                         now=utcnow())
        return cid

    def _stop_container(self, cid):
        """`docker stop -t 10`, then `docker kill`, then `docker rm -f`, each
        under its own subprocess timeout so a hung daemon cannot extend the hold
        past the deadline.

        The `rm -f` is not belt-and-braces. A container that was CREATED and
        never started cannot reach a stopped state at all: `docker stop` on it
        succeeds without changing anything, AutoRemove only fires on a death
        that never happens, and `is_running` correctly keeps answering True --
        so without a removal the job would sit in CLEANUP_BLOCKED holding
        admissions shut forever. Removal is what turns the name into the
        positive absence every confirmation path is waiting for.
        """
        try:
            self.docker.run(["docker", "stop", "-t",
                             str(self.cfg.stop_timeout_s), cid],
                            timeout=self.cfg.stop_timeout_s + 15)
        except subprocess.TimeoutExpired:
            log.warning("docker stop timed out for %s", cid)
        try:
            self.docker.run(["docker", "kill", cid], timeout=30)
        except subprocess.TimeoutExpired:
            # This proves the CLI stopped waiting, NOT that the workload died.
            log.warning("docker kill timed out for %s", cid)
        try:
            self.docker.run(["docker", "rm", "-f", cid], timeout=30)
        except subprocess.TimeoutExpired:
            log.warning("docker rm timed out for %s", cid)

    def _remove_container(self, cid):
        """`docker rm -f` alone, for the settle window: the escalation's stop and
        kill are pointless against a name that does not exist yet, and this has
        to run on EVERY pass rather than once."""
        try:
            self.docker.run(["docker", "rm", "-f", cid], timeout=30)
        except subprocess.TimeoutExpired:
            log.warning("docker rm timed out for %s", cid)

    def _account_for(self, run_id, role, cid):
        """One container, one confirmation pass. True once its row is released.

        The ONLY place a resource row is released on the strength of an
        inspection, and deliberately so: there are two confirmation loops, and a
        rule stated in two places is a rule that eventually differs in one.

        The rule has two halves. Ordinarily an absence IS proof -- the container
        was created, ran, and `--rm` removed it. But for a name whose create was
        issued and never acknowledged, absence is only a reading: the daemon can
        complete a submitted request after the client that submitted it has died,
        so stop, kill and remove can all run BEFORE the create binds the name,
        and one negative probe would then release the row and the mutex just
        ahead of a container appearing. For those names the window keeps
        REMOVING as well as probing, and the absence has to hold for
        BUILD_SETTLE_S -- anything that sees the container pushes that instant
        forward, so what is being tested is stability, not a lucky sample.

        The residual is the same one design D10 already accepts for an abandoned
        build, and is worth stating rather than implying: a daemon that completes
        a create MORE than BUILD_SETTLE_S after the client that asked for it died
        would still slip through. The window rests on documented behaviour, not
        on a proof, which is why it is the same knob.
        """
        pins = self.db.call("pins_for", run_id)
        unacked = store_mod.create_unacked(pins, role)
        # OBSERVE, then act. What gets released is decided by what was observed;
        # the removal below is a safety net, and deliberately NOT evidence -- a
        # zero exit from `docker rm -f` does not reliably distinguish "removed
        # it" from "there was nothing there", and that distinction is not
        # something to bet a mutex on.
        alive = self.docker.is_running(cid)
        now = utcnow()
        if unacked:
            # Repeatedly, not once. A window that only samples cannot destroy
            # what lands between two samples.
            self._remove_container(cid)
        if alive is None:
            log.warning("%s: %s state unknown; not treating as stopped",
                        run_id, cid)
            return False
        if alive:
            if unacked:
                # Seen, so the delayed create DID land. Start the stability
                # window again from here; the removal above is what makes this
                # terminate rather than repeat.
                self.db.call("set_pin", run_id,
                             store_mod.absence_settles_pin(role),
                             iso_at(time.time() + self.cfg.build_settle_s),
                             now=now)
            return False
        if not store_mod.absence_believable(pins, role, now):
            log.info("%s: %s reads absent but its create was never"
                     " acknowledged; holding until %s", run_id, cid,
                     pins.get(store_mod.absence_settles_pin(role)))
            return False
        # Positively absent or stopped: a release record is a claim about
        # reality, and this is the evidence for it.
        self.db.call("release_resource", run_id, role=role, container_id=cid,
                     now=now)
        return True

    def _create_then_start(self, hold, role, create_argv, **popen_kw):
        """Bind the container name, PROVE it is bound, and only then start it.

        This is called with the phase gate HELD, and the proof is the reason the
        gate can be opened afterwards at all. `Popen(["docker", "run", ...])`
        proves nothing: it says the local CLI was spawned, not that the daemon
        created anything. Until the name is bound, `docker inspect` answers "No
        such object", every confirmation path here reads that as a POSITIVE
        absence, and so a sweep could release this run's resource row and the
        training descriptor -- and only afterwards would the CLI create and
        start the container. Live work, no mutex, which is the one outcome this
        subsystem exists to prevent.

        With the name bound first, a sweep that runs the instant the gate opens
        is harmless in both directions: it either sees the container (and
        refuses to release) or it stops and removes it (and then `docker start`
        has nothing to start).

        `docker create` is SYNCHRONOUS, so its exit status is the
        acknowledgement. Not answering is not the same as answering no:

          * 0        -> the name is bound; start it.
          * non-zero -> nothing was necessarily created, but nothing is proven
                        either. A non-zero exit covers both a refusal the daemon
                        answered (which did create nothing) and a transport
                        failure AFTER the request was submitted (where the
                        daemon may still be creating). The name is probed once
                        so the failure can be CLASSIFIED, and that is all the
                        probe is for.
          * timeout  -> the CLI was killed mid-request, and the daemon may
                        complete the create after it. "Absent now" would not
                        mean absent later.

        ONE RULE COVERS BOTH, and it is the round-6 lesson stated as an
        invariant: **a resource row may be released without confirmation only
        when Docker was never asked about that name at all.** Once a create has
        been issued, an absence read back is a reading, not a proof -- the
        daemon can complete a submitted request after the client that submitted
        it has died. So both failure paths RETAIN the row.

        Retaining it is not by itself enough, and this is where round 7 landed:
        the confirmation path would have converted the FIRST absence into a
        release, which is the same mistake one layer down. So the ambiguity is
        PERSISTED (`store.absence_settles_pin`) and every path that would
        release on an inspection consults it -- see `_account_for` and
        `Store.reclaim`. It is written by `_record_container`, BEFORE the row and
        therefore before the create, and it comes down HERE, on the answer: a
        recorded name whose container does not exist yet reads exactly like one
        whose container has been removed, so the cautious state has to cover the
        whole span from the row to the answer, not just the create.
        """
        run_id = hold.run_id
        cid = sandbox_mod.container_name(run_id, role)
        # Bounded by the HOLD too, not just by its own ceiling: this call is the
        # last thing between the deadline check above and a running container,
        # so an unbounded 60s here would be 60s of work admitted past a deadline
        # that had already passed.
        budget = min(self.create_timeout_s, hold.remaining())
        if budget <= 0:
            # The ONE release this function may make. Docker has not been asked
            # about this name yet, and now never will be, so the absence is not
            # a reading -- there is nothing that could still create it.
            self.db.call("release_resource", run_id, role=role,
                         container_id=cid, now=utcnow())
            # Row first, pin second. A pin with no row is inert; a row with no
            # pin is releasable on the first absence, which is only true once
            # this function has committed to never asking -- and a crash between
            # the two must not leave the permissive half standing alone.
            self.db.call("set_pin", run_id, store_mod.absence_settles_pin(role),
                         "", now=utcnow())
            raise DeadlineExpired(
                f"{run_id}: the hold deadline passed before the {role}"
                " container was created; nothing was started")
        # The sentinel written by `_record_container` STAYS UP ACROSS THIS CALL.
        # It is tempting to convert it to a settle instant just before the
        # request goes out -- but "just before" is still before, and a thread
        # descheduled between the two statements would leave an instant expiring
        # while nothing had been asked, which is the whole failure the sentinel
        # exists to prevent, in a narrower window. A window is not a fix.
        #
        # The answer to the request is what moves the pin, because the answer is
        # the first moment anything is known:
        #
        #   * 0        -> cleared: the name is bound, ordinary rules resume.
        #   * non-zero -> an instant: the request WAS issued, so a late create is
        #                 now the bounded residual, and time means something.
        #   * timeout  -> an instant, for the same reason.
        #
        # Leaving here by ANY other route -- `OSError` from a fork that never
        # reached the daemon, a DB call that raised, an exception this code has
        # not met yet -- also leaves the sentinel, and `_unacked_create` (the
        # finalizer around this call and the record before it) converts it. The
        # explicit conversions below are kept even so, because they say something
        # the finalizer cannot: the request WAS issued and answered. Same value,
        # different fact, and the log reads differently.
        #
        # Crashing here leaves it for the other owner: `Recovery._settle_unissued`
        # converts it at startup, where "no phase can issue this create" is
        # finally true.
        settles_pin = store_mod.absence_settles_pin(role)

        def issued():
            self.db.call("set_pin", run_id, settles_pin,
                         iso_at(time.time() + self.cfg.build_settle_s),
                         now=utcnow())

        try:
            p = self.docker.run(create_argv, timeout=budget)
        except subprocess.TimeoutExpired:
            issued()
            raise StartUnconfirmed(
                f"{run_id}: docker create for the {role} did not return within"
                f" {int(budget)}s, so whether {cid} exists is unknown;"
                " RETAINING its resource row") from None
        if p.returncode == 0:
            # ACKNOWLEDGED: the name is bound, so from here an absence means the
            # container was removed and the ordinary rule applies again.
            self.db.call("set_pin", run_id, settles_pin, "", now=utcnow())
            # The create is bounded by what was left of the hold, so it can
            # return exactly ON the deadline -- and starting then would be work
            # admitted past it. The row is deliberately NOT released: the
            # container EXISTS now, and only the confirmation path (which stops,
            # kills and removes) may account for something that exists.
            if hold.expired():
                raise DeadlineExpired(
                    f"{run_id}: the hold deadline passed while creating the"
                    f" {role} container; {cid} exists and was never started")
            return self.spawn(sandbox_mod.docker_start_argv(run_id, role),
                              **popen_kw)
        issued()
        why = (p.stderr or "").strip()[:200]
        if self.docker.is_running(cid) is False:
            raise StartFailed(f"{run_id}: docker create for the {role} failed"
                              f" and {cid} reads as absent: {why}")
        raise StartUnconfirmed(
            f"{run_id}: docker create for the {role} failed and {cid} could not"
            f" be accounted for: {why}")

    def _reap(self, proc, what):
        """Wait out the docker client after its container was stopped, and kill
        it if it still will not go.

        Returning from a phase with the child unwaited leaves a zombie for the
        lifetime of the daemon, and `docker stop` returning does not mean the
        attached client has exited.
        """
        try:
            proc.wait(timeout=self.cfg.stop_timeout_s + 5)
            return
        except subprocess.TimeoutExpired:
            pass
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.error("the docker client for %s will not exit even after"
                      " SIGKILL", what)

    def _handoff(self, hold, paths):
        """Four steps, because ownership is the whole difficulty. Returns an
        error_class on failure, or None."""
        run_id = hold.run_id
        wanted = self._artifact_allowlist(paths["out"])
        if not wanted:
            return None
        # 1. qfd PRE-CREATES each destination, empty, as its own.
        for name in wanted:
            dest = os.path.join(paths["artifacts"], name)
            with open(dest, "wb"):
                pass
            # Group-owned by qfclient, mode 0660. Both halves are needed: the
            # handoff container runs as 10001 with --group-add <qfclient gid>,
            # so it can only write these if the GROUP is qfclient. Leaving them
            # qfd:qfd made the --group-add ornamental and the copy fail.
            if self.qfclient_gid is not None:
                os.chown(dest, -1, self.qfclient_gid)
            os.chmod(dest, 0o660)

        argv = sandbox_mod.docker_create_argv(
            image_ref=hold.image_id, run_id=run_id,
            spec_hash=hold.job["spec_hash"], kind="handoff",
            src_mount=paths["src"], out_mount=paths["out"],
            entrypoint_argv=["/bin/sh", "/trusted/handoff-inside.sh", *wanted],
            mem_limit="512m", cpus=1.0, role="handoff",
            group_add=self._qfclient_gids(),
            extra_ro_mounts=((os.path.join(
                os.path.realpath(self.cfg.trusted_dir), "handoff-inside.sh"),
                "/trusted/handoff-inside.sh"),),
            extra_rw_mounts=((paths["artifacts"], "/artifacts"),))
        # The budget is checked BEFORE anything is recorded. Recording first and
        # checking second left a resource row for a container that was never
        # started -- a phantom the inventory then reports as live, so if Docker
        # later answered `None` about a container that had never existed, the run
        # would be forced to CLEANUP_BLOCKED and keep the training lock over
        # nothing at all.
        #
        # No `max(1, ...)` floor: that granted a one-second run to a handoff
        # whose budget was already spent, which is the same overrun, smaller.
        if int(hold.remaining()) <= 0:
            log.error("%s: no budget left for the handoff; artifacts are not"
                      " collected", run_id)
            return "handoff_timeout"

        # The gate covers DECISION, RECORD **and START** -- exactly as the
        # candidate path does. Covering only decision-and-record was the same
        # defect one level down: `docker.run` happened after the guard was
        # released, so `force-release` (which deliberately does not veto on live
        # rows) could take the guard, close the descriptor, and only then would
        # the handoff container start. And with no re-check after the
        # synchronous record, a handoff whose budget was consumed by that record
        # started anyway.
        #
        # The container is STARTED here and WAITED FOR outside, so the critical
        # section stays short: `Popen` returns at once, where `subprocess.run`
        # would have held the guard for the whole handoff and stalled the reaper.
        with hold.phase_gate("the handoff"), \
                self._unacked_create(run_id, "handoff"):
            self._record_container(hold, run_id, "handoff")
            # Recomputed INSIDE the gate, after the record: the record is a
            # round-trip to the DB-owner thread, so the budget measured before it
            # is not the budget that remains.
            remaining = int(hold.remaining())
            if remaining <= 0:
                self.db.call("release_resource", run_id, role="handoff",
                             container_id=sandbox_mod.container_name(
                                 run_id, "handoff"), now=utcnow())
                log.error("%s: budget spent while recording the handoff; nothing"
                          " was started", run_id)
                return "handoff_timeout"
            # DEVNULL, not PIPE. Nothing here reads those pipes: a handoff that
            # wrote more than one pipe buffer would block on the write and be
            # killed by its own timeout, and `communicate()` instead would
            # buffer an unbounded stream from a container whose `/bin/sh` comes
            # out of the candidate's own image. The handoff's diagnostic channel
            # is its EXIT CODE (2-5 below), which is why it has one.
            proc = self._create_then_start(hold, "handoff", argv,
                                           stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL)
        cid = sandbox_mod.container_name(run_id, "handoff")
        # MEASURED HERE, after the create, not before it. `docker create` is
        # synchronous, so a budget computed before it is a budget that has
        # already been partly spent -- and unlike the candidate the handoff has
        # no deadline watcher, so nothing else would ever notice it running past
        # the hold. A budget that has gone entirely means the container is
        # already up and must come straight back down.
        budget = min(self.cfg.handoff_timeout_s, int(hold.remaining()))
        timed_out = budget <= 0
        if not timed_out:
            try:
                proc.wait(timeout=budget)
            except subprocess.TimeoutExpired:
                # The FIFO case lands here: a blocking copy must terminate on
                # this timeout rather than wedge the worker (NC15).
                timed_out = True
        if timed_out:
            self._stop_container(cid)
            # And the client is REAPED, not abandoned: `docker stop` returning
            # does not mean the attached client has exited.
            self._reap(proc, cid)
            return "handoff_timeout"

        code = proc.returncode
        by_code = {0: None, 2: "handoff_bad_type",
                   3: "handoff_missing_artifact", 4: "handoff_oversize",
                   5: "handoff_missing_artifact"}
        klass = by_code.get(code, "handoff_missing_artifact")
        if klass:
            return klass

        # 4. qfd, still the owner, hashes and records, then drops the mode.
        for name in wanted:
            dest = os.path.join(paths["artifacts"], name)
            self.db.call("add_artifact", run_id, name=name, path=dest,
                         sha256=file_digest(dest),
                         bytes_=os.path.getsize(dest), now=utcnow())
            os.chmod(dest, 0o640)
        return None

    @staticmethod
    def _artifact_allowlist(out_dir):
        """2a collects only what trusted code names. `result.json` is the whole
        allowlist for now; 2b widens it with typed contracts, not with a glob
        over whatever the candidate happened to write."""
        candidates = ("result.json",)
        return [n for n in candidates
                if os.path.exists(os.path.join(out_dir, n))]

    @staticmethod
    def _qfclient_gids():
        try:
            return [grp.getgrnam("qfclient").gr_gid]
        except KeyError:
            return []

    # --- the only place the descriptor closes ----------------------------
    def finish(self, hold, state, fields):
        """Forced cleanup, then confirmation, then -- and only then -- release.

        On deadline expiry the runner starts forced cleanup; it does NOT release
        the lock. A skipped nightly run is recoverable; a released lock over live
        work is not.
        """
        run_id = hold.run_id
        confirmed = self.confirm_all_stopped(hold)
        if not confirmed:
            job = self.db.call("get", run_id)
            if "CLEANUP_BLOCKED" in store_mod.ALLOWED.get(job["state"], set()):
                self.db.call("transition", run_id, "CLEANUP_BLOCKED",
                             now=utcnow(),
                             fields={"error_class": "kill_unconfirmed"})
                log.error("%s: shutdown unconfirmed; RETAINING the training"
                          " descriptor and the reservation. Admissions are"
                          " frozen until this resolves.", run_id)
                # The descriptor is deliberately NOT released here.
                return
            log.error("%s: shutdown unconfirmed and no CLEANUP_BLOCKED edge"
                      " from %s; retaining the descriptor", run_id,
                      job["state"])
            return

        try:
            job = self.db.call("get", run_id)
            if state in store_mod.ALLOWED.get(job["state"], set()):
                self.db.call("transition", run_id, state, now=utcnow(),
                             fields=fields)
            else:
                log.error("%s: cannot move %s -> %s", run_id, job["state"],
                          state)
        finally:
            self._cleanup_worktree(run_id)
            hold.lock.release()
            # Only now, because the registry's meaning is "this process holds
            # this job's lock". A CLEANUP_BLOCKED job above returns WITHOUT
            # reaching here and stays registered on purpose.
            self.disp.unregister_hold(run_id)

    SETTLE_PIN = "settle_started_at"

    def settle_empty_inventory(self, run_id):
        """Resolve a job that has NO recorded containers, via elapsed time.

        This is the cancellation-settle procedure, and it is the one release not
        backed by inspection (design D10). It exists because
        `confirm_run_stopped` correctly refuses an empty inventory -- confirmation
        over an empty set is not confirmation -- which left a `BUILDING` job that
        owns no container of ours with NO path forward at all: `_mutex_lost` moved
        it to CLEANUP_BLOCKED, and the reaper then asked the same question and got
        the same refusal, forever. A permanent stall is not fail-closed; it is
        just failed.

        Under the classic builder there is no container to inspect, and after a
        crash or a lost mutex the `docker build` client was not even our child,
        so there is nothing to `waitpid`. What is left is the documented
        behaviour the design accepts: the daemon cancels a build when its client
        disconnects, `--force-rm` removes intermediates, and after
        `QFD_BUILD_SETTLE_S` the work is gone. So: record when settling started,
        and only once that window has elapsed -- AND nothing has appeared in the
        inventory meanwhile -- is the job releasable.
        """
        if self.db.call("resources_for", run_id, unreleased_only=True):
            # Something showed up; this is an inspection question after all.
            return False
        pins = self.db.call("pins_for", run_id)
        started = pins.get(self.SETTLE_PIN)
        now = time.time()
        if started is None:
            # Record when settling began, then fall through to the SAME elapsed
            # test as every later call. Returning False unconditionally here
            # would make the answer depend on how many times it had been asked
            # rather than on how long it had been, so a zero-length window took
            # two passes to clear.
            started = iso_at(now)
            self.db.call("set_pin", run_id, self.SETTLE_PIN, started,
                         now=utcnow())
            log.warning("%s: empty inventory; starting the %ds settle window",
                        run_id, self.cfg.build_settle_s)
        elapsed = now - parse_iso(started)
        if elapsed < self.cfg.build_settle_s:
            log.info("%s: settling, %ds of %ds elapsed", run_id, int(elapsed),
                     self.cfg.build_settle_s)
            return False
        log.warning("%s: settle window of %ds elapsed with an empty inventory;"
                    " releasing", run_id, self.cfg.build_settle_s)
        return True

    def confirm_run_stopped(self, run_id):
        """Confirmation for a run this process holds NO lock for.

        Needed by the startup `mutex_lost` path and by the reaper: both have to
        ask "is this run's recorded work definitely gone?" without a descriptor
        to release afterwards. Same rule as everywhere else -- an EMPTY inventory
        is not a confirmation, and `None` is never "stopped".
        """
        recorded = self.db.call("resources_for", run_id, unreleased_only=True)
        if not recorded:
            return False
        deadline = time.time() + self.cfg.kill_confirm_s
        pending = {r["container_id"]: r for r in recorded}
        for cid in list(pending):
            self._stop_container(cid)
        while pending and time.time() < deadline:
            for cid, res in list(pending.items()):
                if self._account_for(run_id, res["role"], cid):
                    pending.pop(cid, None)
            if pending:
                time.sleep(self.poll_interval_s)
        if pending:
            log.error("%s: %d container(s) unconfirmed: %s", run_id,
                      len(pending), sorted(pending))
            return False
        return True

    def confirm_all_stopped(self, hold, now=None):
        """POSITIVE confirmation for every recorded container, or False.

        Confirmation over an EMPTY inventory is not confirmation: a BUILDING job
        under the classic builder owns no container of ours at all, so an empty
        set takes the build-settle path instead of a free pass.
        """
        run_id = hold.run_id
        recorded = self.db.call("resources_for", run_id, unreleased_only=True)
        if not recorded:
            return self._settle_build(hold)

        deadline = time.time() + self.cfg.kill_confirm_s
        pending = {r["container_id"] for r in recorded}
        by_id = {r["container_id"]: r for r in recorded}
        for cid in list(pending):
            self._stop_container(cid)
        while pending and time.time() < deadline:
            for cid in list(pending):
                if self._account_for(run_id, by_id[cid]["role"], cid):
                    pending.discard(cid)
            if pending:
                time.sleep(self.poll_interval_s)
        if pending:
            log.error("%s: %d container(s) unconfirmed after %ds: %s", run_id,
                      len(pending), self.cfg.kill_confirm_s, sorted(pending))
            return False
        return True

    def _settle_build(self, hold):
        """The one release not backed by inspection (design D10).

        The `docker build` client is our own child, so its death is a waitpid;
        the daemon cancels a build when its client disconnects; --force-rm
        removes intermediates on failure as well as success. Daemon-side build
        work is not enumerable, so this rests on documented behaviour, bounded
        by --memory, and is why build abandonment does NOT route through
        CLEANUP_BLOCKED -- an operator gate on every abandoned build would stop
        the loop far more often than the residual justifies.
        """
        time.sleep(min(self.cfg.build_settle_s, max(0, hold.remaining() + 60)))
        return True

    def _cleanup_worktree(self, run_id):
        try:
            self.src.remove_worktree(os.path.join(self.run_dir(run_id), "src"))
        except Exception:
            log.exception("%s: worktree removal failed", run_id)


class Reaper(threading.Thread):
    """Two jobs, both of which the dispatcher is useless without.

    1. **Lease reclaim.** A crashed runner's job would otherwise hold its lane
       and its reservation forever. The probe is tri-state and `None` never
       counts as stopped (see `store.reclaim`).
    2. **Re-polling CLEANUP_BLOCKED.** The contract is explicit: while such a
       job exists there are NO admissions, and on confirmation it moves to
       FAILED and admissions resume *by themselves*. Without this thread that
       recovery never happens and the loop stops permanently, waiting for an
       operator who was never told to act.
    """

    def __init__(self, cfg, db, runner, dispatcher, docker):
        super().__init__(name="reaper", daemon=True)
        self.cfg = cfg
        self.db = db
        self.runner = runner
        self.disp = dispatcher
        self.docker = docker
        self.stop_event = threading.Event()

    def run(self):
        while not self.stop_event.wait(self.cfg.reap_interval_s):
            try:
                self.sweep()
            except Exception:                      # noqa: BLE001
                log.exception("reaper sweep failed")

    def sweep(self):
        now = utcnow()
        decided = self.db.call("reclaim", now, probe=self.docker.is_running,
                               owner=self.runner.owner,
                               lease_expires_at=iso_at(time.time()
                                                       + self.cfg.lease_s))
        for run_id, outcome in decided:
            log.info("reclaim: %s -> %s", run_id, outcome)
            if outcome == "unconfirmed":
                log.error("%s: Docker did not answer about a recorded"
                          " container; holding everything and retrying",
                          run_id)
            elif outcome == "deadline_expired":
                # A live container past its PERSISTED hold deadline, whose lease
                # lapsed -- so nothing is driving it and adoption would renew it
                # for ever. `reclaim` moved it to CLEANUP_BLOCKED because it has
                # only a probe; `resolve_blocked` below has a Docker client, so
                # the kill happens in this same pass.
                log.error("%s: past its hold deadline with a lapsed lease and"
                          " nothing confirmed gone (live, unknown, or an"
                          " unsettled absence); forcing cleanup rather than"
                          " renewing it or asking again", run_id)
            elif outcome == "reclaimed":
                # The DB half of the reclaim already released the resources and
                # moved the job to FAILED. The PROCESS half has to happen too:
                # the lease expired, so the runner that owned this hold is gone
                # or wedged, and nobody else will ever close its descriptor.
                # Logging the outcome and leaving the flock held meant admission
                # was released logically while the mutex leaked until restart --
                # the nightly run then waits out its full LOCK_WAIT_S for a job
                # that is already FAILED.
                self.release_hold(run_id, "reclaimed")
        # BEFORE `resolve_blocked`, not after: a sentinel that is still up makes
        # every absence unbelievable, so a confirmation pass over the same run in
        # this sweep would refuse to release its row and the pass would be wasted.
        # Converting first means one sweep can both settle and confirm.
        self.runner.retry_unsettled()
        self.resolve_blocked()
        return decided

    def release_hold(self, run_id, why):
        """Close and unregister a hold this process still owns, if any.

        Under the hold's OWN guard, so this cannot interleave with a phase
        start, and with a re-check of the inventory inside that guard. Two rules
        together:

          * revoke first -- the worker that owns this hold may be mid-run, since
            the lease expiring says its renewer stopped, not that its thread did;
          * REFUSE to release while any recorded container is unreleased. The
            reclaim decision that led here was made earlier, so a phase that
            recorded a container in between must veto it. Releasing anyway is how
            the mutex gets handed to the nightly run over live work -- which is
            the one failure this whole subsystem exists to prevent.
        """
        hold = self.disp.get_hold(run_id)
        if hold is None:
            return False
        with hold.guard:
            hold.revoke_under_guard()
            live = self.db.call("resources_for", run_id, unreleased_only=True)
            if live:
                # Registered and held on purpose: the next sweep re-examines it,
                # and the container it names is now inside the inventory that
                # every confirmation path reads.
                log.error("%s: NOT releasing the descriptor (%s): %d container(s)"
                          " are recorded live: %s", run_id, why, len(live),
                          [r["container_id"] for r in live])
                return False
            self.disp.unregister_hold(run_id)
            if hold.lock is not None and hold.lock.held:
                hold.lock.release()
                log.warning("%s: released the training descriptor (%s)", run_id,
                            why)
                return True
        return False

    def resolve_blocked(self):
        """On confirmation -> FAILED, descriptor closed, reservation released,
        admissions resume without operator action.

        This runs for jobs with AND without a registered hold. A job can reach
        CLEANUP_BLOCKED with no hold -- the startup `mutex_lost` path is exactly
        that case, since a nightly incumbent holds the lock and this process
        never got a descriptor. Skipping those (as an earlier version did, on the
        reasoning that "recovery owns that case") left them CLEANUP_BLOCKED
        forever, and the no-admissions rule then stopped the loop permanently
        with nobody scheduled to look again.
        """
        for job in self.db.call("list", state="CLEANUP_BLOCKED", limit=100):
            run_id = job["run_id"]
            hold = self.disp.get_hold(run_id)
            if hold is not None:
                confirmed = self.runner.confirm_all_stopped(hold)
            elif self.db.call("resources_for", run_id, unreleased_only=True):
                confirmed = self.runner.confirm_run_stopped(run_id)
            else:
                # No hold and nothing to inspect: the settle path is the only
                # thing that can ever resolve this, and without it the job sits
                # in CLEANUP_BLOCKED forever holding admissions shut.
                confirmed = self.runner.settle_empty_inventory(run_id)
            if not confirmed:
                continue
            log.warning("%s: shutdown now confirmed; releasing", run_id)
            # The CAUSE survives the resolution. Overwriting `error_class` with
            # `reclaimed_after_block` said how the job got unstuck and threw away
            # why it died -- `hold_deadline_expired`, `kill_unconfirmed`,
            # `mutex_lost` -- which is the half triage needs. The resolution goes
            # in a pin instead, which is what pins are for.
            now = utcnow()
            self.db.call("set_pin", run_id, "unblocked_at", now, now=now)
            self.db.call("transition", run_id, "FAILED", now=now,
                         fields={"finished_at": now,
                                 "error_class": (job["error_class"]
                                                 or "reclaimed_after_block")})
            self.release_hold(run_id, "confirmed after block")


class Worker(threading.Thread):
    """One lane slot. Two of these for `light`, one for `heavy`.

    Not one blocking thread per lane: that cannot deliver two light workers, and
    it would share a thread-bound SQLite connection (which is what the DB-owner
    thread exists to prevent).
    """

    def __init__(self, runner, lane, index, idle_sleep_s=2):
        super().__init__(name=f"worker-{lane}-{index}", daemon=True)
        self.runner = runner
        self.lane = lane
        self.idle_sleep_s = idle_sleep_s
        self.stop_event = threading.Event()

    def run(self):
        while not self.stop_event.is_set():
            try:
                if self.runner.try_one(self.lane):
                    continue          # a job ran; look for another immediately
            except Exception:                      # noqa: BLE001
                log.exception("worker %s failed", self.name)
            self.stop_event.wait(self.idle_sleep_s)


class Recovery:
    """Startup resource reconstruction, before any worker starts.

    Driven from SQLite, not from `docker ps`: revision 8 started from live
    containers, so a CLEANUP_BLOCKED job whose workload died while qfd was down
    was never discovered -- it stayed CLEANUP_BLOCKED, and the no-admissions
    rule stopped the loop PERMANENTLY.
    """

    # The named reconciliation phases, in order. Gate B of the fault suite
    # aborts after each one and asserts that the result is always either
    # "everything still held" or "verified cleanup completed" -- never an
    # intermediate release.
    PHASES = ("enumerate", "lock", "recharge", "deadline", "resolve_blocked")

    def __init__(self, cfg, db, runner, docker):
        self.cfg = cfg
        self.db = db
        self.runner = runner
        self.docker = docker

    @staticmethod
    def _fault(phase):
        """Crash immediately after `phase`, for Gate B only.

        Honoured ONLY when QFD_ALLOW_FAULT_INJECTION=1, which the unit never
        sets: an abort primitive reachable in production is a denial of service
        wearing a test harness.
        """
        if os.environ.get("QFD_ALLOW_FAULT_INJECTION") != "1":
            return
        if os.environ.get("QFD_FAULT_AFTER") == phase:
            log.error("fault injection: aborting after recovery phase %s", phase)
            os._exit(99)

    def reconstruct(self):
        pending = []
        for state in store_mod.ADMITTED_STATES:                       # step 0
            pending.extend(self.db.call("list", state=state, limit=1000))
        self._fault("enumerate")
        for job in pending:
            self._settle_unissued(job["run_id"])
        holds = []
        for job in pending:
            hold = self._readopt(job)
            if hold is not None:
                holds.append(hold)
        return holds

    def _settle_unissued(self, run_id):
        """Step 0.5, and it must precede every confirmation below.

        A create that was recorded but never acknowledged leaves an
        `ABSENCE_NOT_YET_ISSUED` pin, which means "the phase holding the gate has
        not asked Docker yet, so its absence proves nothing". No phase survived
        this process, so nothing will ever replace that pin -- and every path
        below (`finish`, `confirm_run_stopped`, the reaper's `resolve_blocked`,
        `Store.reclaim`) refuses to release a row while it stands. Left alone it
        is a permanent stall with a held mutex, escapable only by hand.

        Converting it to a settle instant HERE, before the first confirmation, is
        what lets those paths terminate: the ambiguity becomes the bounded kind
        that repeated removal resolves.
        """
        roles = self.db.call("settle_unissued_creates", run_id,
                             settles_at=iso_at(time.time()
                                               + self.cfg.build_settle_s),
                             now=utcnow(), role=None)
        if roles:
            log.warning("%s: create(s) for %s were recorded but never"
                        " acknowledged and no phase survived to issue them;"
                        " they settle by removal instead", run_id,
                        ", ".join(roles))

    def _readopt(self, job):
        run_id = job["run_id"]
        effective = json.loads(job["spec_json"])
        # Step 1: re-acquire the lane-appropriate lock BEFORE any cleanup, both
        # lanes -- revision 4 did heavy only, leaving an orphaned light
        # container running with no LOCK_SH at all while nightly could take
        # LOCK_EX.
        try:
            lock = TrainingLock(self.cfg.lock_file, job["lane"]).acquire()
        except LockHeld:
            log.error("%s: a nightly incumbent holds the lock; taking the"
                      " mutex_lost kill-and-confirm path", run_id)
            return self._mutex_lost(job)
        self._fault("lock")

        # Re-charge the job's ORIGINAL LOGICAL reservation from its stored spec,
        # not the live container's HostConfig.Memory: a 22 GB job whose only
        # live container is its 2 GB builder would come back charged 2 GB.
        logical = store_mod.reservation_mb(effective["mem_limit"],
                                           self.cfg.image_build_mem_mb)
        live = self._live_cap_mb(job)
        if live is not None and live > logical:
            # "Take the larger" has to mean CHARGED, not logged. Recording it as
            # a pin is what makes `admitted_mem_mb` include it; the previous
            # version logged the discrepancy and went on charging the smaller
            # figure, so the budget would admit work the real reservation
            # excludes -- the same arithmetic hole as trusting the container's
            # cap in the first place.
            log.warning("%s: live container cap %dm exceeds the stored"
                        " reservation %dm; charging the larger", run_id, live,
                        logical)
            self.db.call("set_pin", run_id, "reservation_override_mb",
                         str(live), now=utcnow())
        self._fault("recharge")

        # Step 2: restore the REMAINING budget, never a fresh one.
        deadline = job["hold_deadline_at"]
        hold = Hold(job, lock,
                    parse_iso(deadline) if deadline else time.time())
        self._fault("deadline")
        if hold.expired():
            log.warning("%s: hold deadline already passed; forced cleanup while"
                        " still holding the lock", run_id)
            self.runner.finish(hold, "FAILED",
                               {"error_class": "deadline_expired",
                                "finished_at": utcnow()})
            return None

        # Step 3: a recorded inventory means FORCED CLEANUP, whatever the state.
        #
        # Not adoption, and the distinction is the whole of this step. Nothing in
        # a restarted process can resume one of these runs: the `docker start
        # --attach` client died with the old process, so its exit status is gone,
        # its logs are no longer being pumped, its watchers are not sampling and
        # its handoff will never run. A hold handed back here would be driven by
        # NOBODY -- and an undriven hold is not merely useless, it is a permanent
        # stall: the lease lapses, `reclaim` finds a live container, renews, and
        # every later sweep does the same while the mutex, the lane and the
        # reservation stay held.
        #
        # Whether the container is `running` or merely `created` (the crash
        # window between `docker create` and `docker start`) makes no difference
        # HERE, which is why recovery does not need to tell them apart: the kill
        # escalation ends in `docker rm -f`, so both become a positive absence
        # and both confirm.
        recorded = self.db.call("resources_for", run_id, unreleased_only=True)
        if recorded:
            log.warning("%s: %s with %d recorded container(s) and no process to"
                        " resume it; forcing cleanup", run_id, job["state"],
                        len(recorded))
            self.runner.finish(hold, "FAILED",
                               {"error_class": "reclaimed_at_startup",
                                "finished_at": utcnow()})
            self._fault("resolve_blocked")
            # `finish` releases only on CONFIRMED shutdown; otherwise it leaves
            # the job CLEANUP_BLOCKED holding everything, and the hold has to go
            # back so `resolve_blocked` can keep asking. The descriptor is the
            # honest witness to which of the two happened.
            return hold if hold.lock.held else None
        if job["state"] in ("CLEANUP_BLOCKED", "BUILDING"):
            log.warning("%s: %s with no recorded containers; retaining its"
                        " lock and reservation through the build-settle"
                        " procedure", run_id, job["state"])
            self._fault("resolve_blocked")
            return hold
        # LEASED or RUNNING with nothing recorded: no container of ours exists,
        # so there is nothing to confirm and nothing to kill -- but there is
        # still nobody to drive it, so it is finished here rather than left for a
        # sweep to renew for ever.
        log.warning("%s: %s with no recorded containers and no process to"
                    " resume it; failing it at startup", run_id, job["state"])
        self.runner.finish(hold, "FAILED",
                           {"error_class": "reclaimed_at_startup",
                            "finished_at": utcnow()})
        return hold if hold.lock.held else None

    def _live_cap_mb(self, job):
        for res in self.db.call("resources_for", job["run_id"],
                                unreleased_only=True):
            p = self.docker.run(["docker", "inspect", "-f",
                                 "{{.HostConfig.Memory}}",
                                 res["container_id"]], timeout=15)
            if p.returncode == 0 and (p.stdout or "").strip().isdigit():
                return int(p.stdout.strip()) // (1024 * 1024)
        return None

    def _mutex_lost(self, job):
        """A nightly incumbent holds the lock, so this orphan cannot be adopted.

        Kill it, confirm, and REACH A TERMINAL STATE. An earlier version
        confirmed and then returned without transitioning, which left the job
        RUNNING with no hold, no lock and no reservation-holder: nothing would
        ever look at it again, and if it later drifted to CLEANUP_BLOCKED the
        reaper skipped it because no hold was registered.

        Confirmed gone -> FAILED with `error_class=mutex_lost`, which is what
        NC8(e) asserts. Not confirmed -> CLEANUP_BLOCKED where it is legal, so
        admissions freeze and the reaper keeps asking; the reaper now handles
        hold-less blocked jobs precisely for this case.
        """
        run_id = job["run_id"]
        inventory = self.db.call("resources_for", run_id, unreleased_only=True)
        if not inventory:
            # No container of ours to inspect. Take the settle path rather than
            # blocking on a question that can never be answered; if the window
            # has not elapsed yet, fall through to CLEANUP_BLOCKED and the reaper
            # finishes it.
            if self.runner.settle_empty_inventory(run_id):
                log.error("%s: mutex lost with an empty inventory and the settle"
                          " window elapsed; failing it", run_id)
                self.db.call("transition", run_id, "FAILED", now=utcnow(),
                             fields={"finished_at": utcnow(),
                                     "error_class": "mutex_lost"})
                return None
        elif self.runner.confirm_run_stopped(run_id):
            log.error("%s: mutex lost to a nightly incumbent and the workload is"
                      " confirmed gone; failing it", run_id)
            self.db.call("transition", run_id, "FAILED", now=utcnow(),
                         fields={"finished_at": utcnow(),
                                 "error_class": "mutex_lost"})
            return None
        if "CLEANUP_BLOCKED" in store_mod.ALLOWED.get(job["state"], set()):
            log.error("%s: mutex lost and shutdown UNCONFIRMED; blocking"
                      " admissions until the reaper can confirm", run_id)
            self.db.call("transition", run_id, "CLEANUP_BLOCKED", now=utcnow(),
                         fields={"error_class": "mutex_lost_unconfirmed"})
        else:
            # LEASED has no CLEANUP_BLOCKED edge, and a LEASED job started
            # nothing, so its own state is the confirmation.
            log.error("%s: mutex lost from %s with nothing confirmed running;"
                      " failing it", run_id, job["state"])
            self.db.call("transition", run_id, "FAILED", now=utcnow(),
                         fields={"finished_at": utcnow(),
                                 "error_class": "mutex_lost"})
        return None


def main(argv=None):                                  # pragma: no cover
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = Config.from_env()
    problems = cfg.check_startup()
    if problems:
        for p in problems:
            log.error("startup precondition: %s", p)
        return 2

    db = DbOwner(os.path.join(cfg.state_dir, "state.db"),
                 mem_budget_mb=cfg.mem_budget_mb,
                 image_build_mem_mb=cfg.image_build_mem_mb,
                 artifact_cap_mb=cfg.artifact_cap_mb,
                 disk_floor_mb=cfg.disk_floor_gb * 1024).start()
    ok, chain_problems = db.call("verify_chain")
    log.info("chain verify at startup: ok=%s problems=%d", ok,
             len(chain_problems))
    for problem in chain_problems[:20]:
        log.error("chain: %s", problem)

    docker = Docker()
    src = source_mod.Source(os.path.join(cfg.state_dir, "mirror.git"),
                            cfg.remote, cfg.token_file)
    src.ensure_mirror()

    disp = Dispatcher(cfg, db, docker=docker, src=src)
    runner = Runner(cfg, db, disp, docker=docker, src=src)

    # STARTUP RESOURCE RECONSTRUCTION, before any worker starts. Admitted memory
    # and the flock are both process-local, so a restart must rebuild them from
    # what is actually running -- and it must do so before anything new can be
    # admitted against a budget that does not yet know about the old work.
    recovery = Recovery(cfg, db, runner, docker)
    for hold in recovery.reconstruct():
        disp.register_hold(hold)
        log.warning("re-adopted %s in lane %s (deadline %s)", hold.run_id,
                    hold.lock.lane, hold.job["hold_deadline_at"])

    # Only then start the pool: two light, one heavy.
    workers = [Worker(runner, "light", i) for i in range(cfg.light_workers)]
    workers.append(Worker(runner, "heavy", 0))
    reaper = Reaper(cfg, db, runner, disp, docker)
    for thread in (*workers, reaper):
        thread.start()

    client = SocketServer(cfg.socket_path, disp, admin=False,
                          group=_gid("qfclient")).bind()
    admin = SocketServer(cfg.admin_socket_path, disp, admin=True,
                         group=_gid("qfheavy")).bind()
    client.start()
    admin.start()
    log.info("qfd ready: client=%s admin=%s workers=%d", cfg.socket_path,
             cfg.admin_socket_path, len(workers))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        for thread in (*workers, reaper):
            thread.stop_event.set()
        client.stop()
        admin.stop()
        db.stop()
    return 0


if __name__ == "__main__":                            # pragma: no cover
    sys.exit(main())
