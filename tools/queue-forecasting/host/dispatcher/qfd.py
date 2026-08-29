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
import datetime
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

import baseline as baseline_mod
import contract as contract_mod
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

# The smallest reservation any kind can ask for. `ping` reports the resource
# gate at THIS size, so "resource" answers the strongest available form of the
# question: if even the cheapest job cannot be admitted, nothing can.
# An `error_class` is written into a column that operators grep and the NC suite
# asserts on. When one arrives from ANOTHER privilege domain it is constrained to
# this shape rather than trusted: a reply could otherwise put a sentence, or an
# empty string, where every consumer expects a token.
_ERROR_CLASS_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}\Z")

# Kinds whose work happens in ANOTHER privilege domain, so the dispatcher relays
# a request and records a reply rather than running a container. Named as a set
# because three separate places branch on it -- which lock to take, which relay
# to call, and whether a run directory is prepared at all -- and three copies of
# `kind == "extract" or kind == "evaluate"` is how the fourth one gets forgotten.
RELAYED_KINDS = ("extract", "evaluate")

SMALLEST_MEM_LIMIT = min((k["mem_limit"] for k in spec_mod.KINDS.values()),
                         key=spec_mod.mem_mb)

# The shape of every frozen-input identity: an extract's request hash, an
# extract_hash, a baseline's content hash. Module level because two classes read
# it now -- `Runner` validates a relay's reply, `Dispatcher` filters the baseline
# store -- and a second copy is a second thing that can drift.
HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")

# The client socket's op table. `force-release` is deliberately ABSENT: the
# group on this socket contains `research` (revision 8's regression).
CLIENT_OPS = ("ping", "submit", "status", "list", "cancel", "verify-chain",
              "trusted-paths", "extracts", "baselines", "contracts")
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


def _int_env(name, default=None, env=None):
    """An integer from the environment. `env` is threaded through from
    `Config.from_env` rather than read off `os.environ` here, because a
    parameter nothing consults is a claim nothing backs -- `from_env` took an
    `env` argument that its integer reads and three of its string reads ignored,
    so it could not be exercised without mutating the process."""
    raw = (os.environ if env is None else env).get(name)
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
        "QFD_EXTRACT_SOCKET", "QFD_SETTLEMENT_LAG_S", "QFD_EXTRACTS_DIR",
        "QFD_BASELINES_DIR", "QFD_CONTRACTS_DIR", "QFD_EVAL_SOCKET",
        "QFD_EVAL_DIR",
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

        def _env_int(name, default=None):
            return _int_env(name, default, env=env)

        cfg = cls(
            trusted_dir=get("QFD_TRUSTED_DIR", ""),
            state_dir=get("QFD_STATE_DIR", ""),
            runs_dir=get("QFD_RUNS_DIR", ""),
            socket_path=get("QFD_SOCKET", ""),
            admin_socket_path=get("QFD_ADMIN_SOCKET", ""),
            admin_uid=_env_int("QFD_ADMIN_UID"),
            remote=get("QFD_REMOTE", ""),
            token_file=get("QFD_TOKEN_FILE", ""),
            lock_file=get("QFD_LOCK_FILE", ""),
            intent_dir=get("QFD_INTENT_DIR", ""),
            build_lock=get("QFD_BUILD_LOCK", ""),
            build_timeout_s=_env_int("QFD_BUILD_TIMEOUT_S", 1800),
            build_lock_wait_s=_env_int("QFD_BUILD_LOCK_WAIT_S", 900),
            build_settle_s=_env_int("QFD_BUILD_SETTLE_S", 30),
            job_hold_deadline_s=_env_int("QFD_JOB_HOLD_DEADLINE_S", 7800),
            kill_confirm_s=_env_int("QFD_KILL_CONFIRM_S", 300),
            stop_timeout_s=_env_int("QFD_STOP_TIMEOUT_S", 10),
            reap_interval_s=_env_int("QFD_REAP_INTERVAL_S", 60),
            setup_teardown_allowance_s=_int_env(
                "QFD_SETUP_TEARDOWN_ALLOWANCE_S", 600),
            marker_stale_margin_s=_env_int("QFD_MARKER_STALE_MARGIN_S", 900),
            lock_migrated_marker=get("QFD_LOCK_MIGRATED_MARKER", ""),
            mem_budget_mb=_env_int("QFD_ADMITTED_MEM_BUDGET_MB", 22528),
            timeout_max_s=_env_int("QFD_TIMEOUT_MAX_S", 3600),
            lock_wait_s=_env_int("QFD_LOCK_WAIT_S", 9000),
            image_build_mem_mb=_env_int("QFD_IMAGE_BUILD_MEM_MB", 2048),
            light_workers=_env_int("QFD_LIGHT_WORKERS", 2),
            log_cap_mb=_env_int("QFD_LOG_CAP_MB", 16),
            extract_socket=get("QFD_EXTRACT_SOCKET",
                                          "/run/qf-extract/sock"),
            # READ-ONLY to the dispatcher, and only to resolve a probe's mount:
            # the directory belongs to qfextract, and `qf extracts` is relayed
            # rather than walked for exactly that reason. Mounting requires a
            # path, so this is the one place qfd needs to know the layout.
            extracts_dir=get("QFD_EXTRACTS_DIR",
                                        "/var/lib/qf-extracts"),
            # Likewise read-only, and for a stronger reason: the store is owned
            # by root and written only by `promote-baseline.sh`, run by a human.
            # qfd resolves a hash to a path so it can mount it, and does nothing
            # else with the directory -- it cannot publish, and must not.
            baselines_dir=get("QFD_BASELINES_DIR", "/var/lib/qf-baselines"),
            # Phase 2c. The contracts directory is READ here only to resolve a
            # submitted contract hash into a legible refusal at submit time; the
            # EVALUATOR resolves it again and its answer is authoritative -- the
            # same relationship as the settlement lag (D17). qfd cannot write
            # here: the directory is in the trusted checkout, root-owned.
            contracts_dir=get("QFD_CONTRACTS_DIR", ""),
            eval_socket=get("QFD_EVAL_SOCKET", "/run/qf-eval/sock"),
            # Where the untrusted prediction set is STAGED for the evaluator
            # (D28). qfd writes `<run_id>/in/`; the evaluator writes
            # `<run_id>/out/` and nothing else.
            eval_dir=get("QFD_EVAL_DIR", "/var/lib/qf-eval"),
            settlement_lag_s=_env_int("QFD_SETTLEMENT_LAG_S", 48 * 3600),
            artifact_cap_mb=_env_int("QFD_ARTIFACT_CAP_MB", 2048),
            handoff_timeout_s=_env_int("QFD_HANDOFF_TIMEOUT_S", 120),
            disk_floor_gb=_env_int("QFD_DISK_FLOOR_GB", 20),
            queued_cap_per_uid=_env_int("QFD_QUEUED_CAP_PER_UID", 20),
            lease_s=_env_int("QFD_LEASE_S", 300),
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

    def check_startup(self, *, group_of=None, my_groups=None, stat=os.stat,
                      client_gid=None):
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

        # CAN THIS PROCESS SET A SETGID BIT AT ALL? Asked here, once, because
        # the answer is a property of the process and not of any job: with
        # RestrictSUIDSGID=yes in the unit, systemd installs a seccomp filter
        # that fails every chmod carrying S_ISGID with EPERM, and `out/` is 2770
        # by design -- so EVERY run died at its first chmod, reported as
        # `error_class=internal`, which reads as a dispatcher bug and cost a live
        # investigation to trace back to one line of unit hardening.
        #
        # An invariant of the environment belongs in the startup gate, not in
        # the first job that trips over it. Fail-closed, and NAME THE CAUSE:
        # a refusal an operator cannot act on is only marginally better than a
        # crash.
        problems.extend(self._check_setgid_allowed())
        problems.extend(self._ensure_runs_dir_reachable(client_gid))
        return problems

    def _ensure_runs_dir_reachable(self, client_gid=None):
        """`qf logs` and artifact reads are FILE reads -- design D9 keeps the
        socket control-only -- so `runs_dir` ITSELF has to be traversable by
        qfclient.

        systemd's `StateDirectory` creates it as User:Group, i.e. qfd:qfd 0750,
        which excludes `research` outright. Every per-run directory underneath was
        correctly grouped qfclient, under a parent nobody in qfclient could
        enter, so `qf logs` could never work for the one account it exists for --
        and it did not FAIL loudly either: `os.path.exists` answers False for a
        path it cannot reach, so the client blamed a missing log file.

        Fixed rather than merely reported, for the same reason `prepare_run_dir`
        sets its own modes: the daemon owns this directory and belongs to the
        group, so it needs no privilege, and refusing to start over something
        nothing else is going to fix would be a self-inflicted outage. Verified
        afterwards, because a chown that silently did not take is worse than one
        that failed -- and only then reported as a problem.
        """
        gid = client_gid
        if gid is None:
            try:
                gid = grp.getgrnam("qfclient").gr_gid
            except KeyError:
                return ["the qfclient group does not exist, so no client can"
                        " ever read a log or an artifact; run"
                        " `phase2-setup.sh dispatch-user`."]
        try:
            os.chown(self.runs_dir, -1, gid)
            os.chmod(self.runs_dir, 0o750)
            st = os.stat(self.runs_dir)
        except OSError as e:
            return [f"cannot make {self.runs_dir} reachable by qfclient: {e}."
                    " Until it is, `qf logs` and artifact reads fail for every"
                    " client, and they fail as 'no such file'."]
        if st.st_gid != gid or not st.st_mode & 0o010:
            return [f"{self.runs_dir} is still not traversable by qfclient"
                    f" (gid={st.st_gid}, mode={oct(st.st_mode & 0o7777)});"
                    " `qf logs` and artifact reads cannot work."]
        return []

    def _check_setgid_allowed(self):
        """Prove `chmod 2770` works under the state dir, or say why not."""
        probe = os.path.join(self.state_dir, ".setgid-probe")
        try:
            os.makedirs(probe, exist_ok=True)
            os.chmod(probe, 0o2770)
            mode = os.stat(probe).st_mode
        except OSError as e:
            return [f"cannot set the setgid bit under {self.state_dir}: {e}."
                    " If the unit carries RestrictSUIDSGID=yes, that seccomp"
                    " filter is the cause -- it fails every chmod carrying"
                    " S_ISGID, and each run's out/ directory needs it so the"
                    " handoff container can read what the sandbox wrote."]
        finally:
            try:
                os.rmdir(probe)
            except OSError:
                pass
        if not (mode & 0o2000):
            # No error, no bit: the silent-clear path. POSIX drops S_ISGID
            # without complaint when the caller is not in the file's group, so
            # an unchecked chmod would leave `out/` looking correct.
            return [f"the setgid bit did not stick under {self.state_dir};"
                    " artifacts written by the sandbox would not be readable by"
                    " the handoff container"]
        return []


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


class MutexUnusable(Exception):
    """The training lock file cannot be OPENED -- not "someone else has it".

    Deliberately not a subclass of `LockHeld`. Contention is normal, transient
    and self-clearing; an inode qfd cannot open is a configuration fault that
    clears only when a human fixes it, and the two need different words and
    different log levels. Conflating them would have the daemon report a broken
    mutex as ordinary contention and wait for it forever.
    """


class LockHeld(Exception):
    """The lock could not be taken without blocking. Never waited on: a worker
    that blocks while holding anything turns a momentary hold into a long one."""


def probe_mutex(path):
    """"free", "held_exclusive", or "unknown". A DIAGNOSTIC, never a decision.

    Admission already answers this per lane inside `Runner.try_one`, where a
    failed `LOCK_SH` is logged and the job stays QUEUED. What was missing was any
    way to ASK: `may_admit` covers the cleanup stall and the nightly intent gate
    but not the mutex, so a queue frozen by an incumbent heavy run -- the
    nightly, most often -- looked identical to an idle one from outside. The
    fault gates then submitted sixteen jobs into it and reported sixteen
    unrelated voids.

    A shared, non-blocking probe: it succeeds unless something holds the lock
    EXCLUSIVELY, which is exactly the condition that stops the light lane. It
    holds the shared lock for microseconds, and the nightly waits with a timeout
    rather than `-n`, so the probe cannot cost it the lock.
    """
    fd = None
    try:
        fd = os.open(path, os.O_WRONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return "held_exclusive"
        return "free"
    except OSError:
        # No lock file, or unreadable. `check_startup` refuses to start without
        # one, so this is "cannot tell", not "free".
        return "unknown"
    finally:
        if fd is not None:
            os.close(fd)


class ExtractSlot:
    """What an EXTRACT job holds instead of the training mutex.

    THE BUG THIS FIXES. Every job took `TrainingLock` before its kind was
    inspected, so a minutes-long extraction held the shared training lock -- and
    a shared holder is exactly what blocks the nightly's exclusive acquisition.
    An extraction reads a replica of the same database the nightly trains from;
    it has no business making the nightly wait, and the plan said so.

    It is still a slot rather than nothing, for two reasons:

      * Two light workers would otherwise relay two extractions at once, and the
        second would be refused by the extractor's NON-BLOCKING mutex -- turning
        "wait your turn" into "failed". A slot here means the second job simply
        stays QUEUED, which is what backpressure should look like.
      * The dispatcher's own bookkeeping stays honest: something is occupied
        while an extraction is in flight.

    Same shape as `TrainingLock` (`lane`, `held`, `release`) so every release
    path treats them alike.
    """

    def __init__(self, semaphore, lane):
        self._sem = semaphore
        self.lane = lane
        self.held = False

    def acquire(self):
        if not self._sem.acquire(blocking=False):
            raise LockHeld("another extraction is in flight")
        self.held = True
        return self

    def release(self):
        if self.held:
            self.held = False
            self._sem.release()


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
        #
        # The open is guarded because it CAN fail, and did: on 2026-08-26 the NC8
        # suite deleted this inode, something recreated it, and the heavy worker
        # then raised a bare PermissionError out of here every two seconds. The
        # worker loop caught it and carried on -- so the lane was not lost -- but
        # what an operator saw was a traceback storm, with the one actionable
        # fact (the mutex inode is unusable, and the remedy is one command) buried
        # in a stack trace instead of stated.
        try:
            fd = os.open(self.path, os.O_WRONLY)
        except OSError as e:
            raise MutexUnusable(
                f"cannot open the training mutex {self.path} for write: {e}."
                " Nothing can be admitted without it. If the inode was replaced,"
                " restore it with `systemd-tmpfiles --create"
                " /etc/tmpfiles.d/qf-locks.conf` (0660 root:qfheavy) and check"
                " that qfd is still in qfheavy.") from None
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


def _one_line(text, limit=300):
    """Collapse a subprocess's stderr to a single bounded log line. Journald
    treats each line as a record, so an untrimmed multi-line error is
    interleaved with everything else and reads as several unrelated events."""
    return " ".join((text or "").split())[:limit]


# --- docker ---------------------------------------------------------------
class Docker:
    """Every call under its own subprocess timeout, so a hung daemon cannot
    extend a hold past the outer deadline."""

    def __init__(self, runner=None):
        self._runner = runner or self._subprocess_runner
        # One log line per (container, reason), not one per probe. The
        # confirmation loops poll every couple of seconds for up to
        # KILL_CONFIRM_S, so logging each unknown unconditionally buries the one
        # fact an operator needs; logging none at all is worse, and was: a
        # 300-second stall reported "state unknown" 150 times without once
        # saying what Docker actually said.
        self._unknown_seen = set()
        self._unknown_lock = threading.Lock()

    def _log_unknown(self, container_id, reason):
        """Say WHY the answer was unknown, once. An operator who cannot see the
        exit status and stderr has to reproduce the probe by hand to learn
        anything, and the probe is the part that is failing."""
        key = (container_id, reason)
        with self._unknown_lock:
            if key in self._unknown_seen:
                return
            self._unknown_seen.add(key)
        log.warning("docker: cannot determine the state of %s: %s",
                    container_id, reason)

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

    def _exists(self, container_id, timeout=15):
        """Is this container in the daemon's COMPLETE container list? Tri-state,
        same convention as `is_running`.

        This exists because `is_running` establishes absence by matching a
        SENTENCE, and a sentence is the weakest evidence in the system. Docker
        29 answers a missing container with wording that does not contain "No
        such object", so every `--rm` container that had already been removed
        read as unknown -- for ever, since the sentinel logic correctly refuses
        to age an unknown into a stop. Every job froze admissions on its way
        out.

        The fix is not a longer list of sentences to match. `docker ps -a` with
        a zero exit is a COMPLETE enumeration, so a name absent from it is
        absent from the daemon -- a positive answer rather than a reading of
        prose. Anything else is unknown: a non-zero exit is the daemon failing
        to answer, and "the list I could not obtain did not contain it" is
        exactly the inference that would release a mutex over live work when the
        socket is missing.

        Prefix matching on the id is deliberate and one-sided: a false PRESENCE
        only costs another poll, while a false ABSENCE releases the mutex.
        """
        try:
            p = self.run(["docker", "ps", "-a", "--no-trunc", "--format",
                          "{{.ID}}\t{{.Names}}"], timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        if p.returncode != 0:
            return None
        for line in (p.stdout or "").splitlines():
            cid, _, names = line.partition("\t")
            cid = cid.strip()
            if cid and len(container_id) >= 12 and cid.startswith(container_id):
                return True
            if container_id in (n.strip() for n in names.split(",")):
                return True
        return False

    def is_running(self, container_id, timeout=15):
        """POSITIVE confirmation only. An error, a timeout or an unparseable
        answer is 'unknown', and unknown is never treated as stopped."""
        try:
            p = self.run(["docker", "inspect", "-f", "{{.State.Status}}",
                          container_id], timeout=timeout)
        except subprocess.TimeoutExpired:
            self._log_unknown(container_id,
                              f"docker inspect did not answer in {timeout}s")
            return None
        out = (p.stdout or "").strip()
        if p.returncode != 0:
            # "No such object" IS a positive answer: the container is gone.
            if "No such object" in (p.stderr or ""):
                return False
            # The wording did not match, which says nothing about whether the
            # container is there. Ask a question with a positive answer instead
            # of matching more prose -- see `_exists`.
            if self._exists(container_id, timeout=timeout) is False:
                return False
            self._log_unknown(
                container_id,
                f"docker inspect exited {p.returncode} and the container is"
                f" still listed (or the list was unavailable); inspect said:"
                f" {_one_line(p.stderr)!r}")
            return None
        if out in self.LIVE_STATUSES:
            return True
        if out in self.STOPPED_STATUSES:
            return False
        self._log_unknown(container_id,
                          f"docker inspect exited 0 but reported the"
                          f" unrecognised status {out!r}")
        return None


# --- run ids -------------------------------------------------------------
class ProbeInputMissing(Exception):
    """A probe named a frozen input that is not published.

    Its own class per input, and its own `error_class`, because "the extract is
    not there" and "the extraction failed" send an operator to different places
    -- one is a request to fix, the other is a subsystem to look at. A baseline
    is a third place again: nothing automated publishes one.
    """

    error_class = "probe_input_not_published"


class ProbeExtractMissing(ProbeInputMissing):
    error_class = "extract_not_published"


class ProbeBaselineMissing(ProbeInputMissing):
    error_class = "baseline_not_published"


class EvalRelayError(Exception):
    """The evaluator could not be asked, or would not answer.

    Its own class rather than a reuse of `ExtractRelayError`, because the two
    name different subsystems and an error class is a routing decision: an
    operator sent to the extractor for a fault in the evaluator loses an
    afternoon.
    """

    def __init__(self, message, error_class="evaluator_unreachable"):
        super().__init__(message)
        self.error_class = error_class


class EvaluateInputMissing(ProbeInputMissing):
    """The run an evaluation names cannot be judged: it does not exist, it is not
    a probe, it did not succeed, or it produced no predictions.

    A subclass of `ProbeInputMissing` so the one handler that records
    `e.error_class` covers it, and so "a frozen input this job named is not
    there" stays one concept across kinds.
    """

    error_class = "evaluate_input_missing"


class EvaluateStagingDenied(EvaluateInputMissing):
    """The staged inbox cannot be handed to the evaluator.

    A HOST fault rather than the job's, and it is a subclass so that the relay's
    one guard covers it -- but with its own class, because the remedy is an
    install step and not a resubmission. `evaluate_input_missing` would send an
    operator to look for a prediction set that is present and correct.
    """

    error_class = "eval_staging_denied"


class ExtractRelayError(Exception):
    """The extractor could not be asked, or would not answer.

    Carries an `error_class` so the job's record names the subsystem: an
    operator sent to the dispatcher for a fault in the extractor loses an
    afternoon, which is the whole reason error classes are routing decisions.
    """

    def __init__(self, message, error_class):
        super().__init__(message)
        self.error_class = error_class


def eval_request(socket_path, payload, timeout):
    """One request, one reply, over the evaluator's socket.

    Deliberately a SEPARATE function from `extract_request` rather than a shared
    one parameterised by socket: the two carry different error classes, and the
    single thing this function exists to get right is that a fault in the
    evaluator is reported as a fault in the evaluator. A shared helper would
    either lose that or grow a flag that decides it.
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(socket_path)
            sock.sendall(json.dumps(payload).encode() + b"\n")
            buf = bytearray()
            while b"\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf.extend(chunk)
    except FileNotFoundError:
        raise EvalRelayError(
            f"no evaluator socket at {socket_path}: qf-eval.socket is not"
            f" listening") from None
    except PermissionError:
        raise EvalRelayError(
            f"permission denied on {socket_path}: the dispatcher must be able to"
            f" connect (the socket is 0660 root:qfd)") from None
    except (ConnectionRefusedError, socket.timeout, OSError) as e:
        raise EvalRelayError(f"cannot talk to the evaluator: {e}") from None
    if not buf:
        raise EvalRelayError(
            "the evaluator closed the connection without replying",
            "evaluator_no_reply")
    try:
        reply = json.loads(bytes(buf).split(b"\n")[0])
    except ValueError as e:
        raise EvalRelayError(f"the evaluator's reply is not JSON: {e}",
                             "evaluator_bad_reply") from None
    if not isinstance(reply, dict):
        raise EvalRelayError("the evaluator's reply is not an object",
                             "evaluator_bad_reply")
    return reply


def extract_request(socket_path, payload, timeout):
    """One request, one reply, over the extractor's socket.

    THE DISPATCHER HOLDS NO CREDENTIAL AND THIS IS WHY IT DOES NOT NEED ONE
    (D15): it asks a service that holds one. Nothing here reads a DSN, and the
    reply is a manifest.

    The timeout is generous by design. A real extraction took 688 seconds, so a
    short timeout would abandon work the extractor completed and then publish --
    leaving a SUCCEEDED extract on disk that no job row points at.
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(socket_path)
            sock.sendall(json.dumps(payload).encode() + b"\n")
            buf = bytearray()
            while b"\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf.extend(chunk)
    except FileNotFoundError:
        raise ExtractRelayError(
            f"no extractor socket at {socket_path}: qf-extract.socket is not"
            f" listening", "extractor_unreachable") from None
    except PermissionError:
        raise ExtractRelayError(
            f"permission denied on {socket_path}: the dispatcher's uid must"
            f" match QFX_CLIENT_UID in qf-extract.service",
            "extractor_unreachable") from None
    except socket.timeout:
        raise ExtractRelayError(
            f"the extractor did not answer within {timeout}s. It may still be"
            f" extracting; check `journalctl -u qf-extract`, and note that a"
            f" published extract is immutable so a retry will reuse it rather"
            f" than duplicate it", "extract_timeout") from None
    except OSError as e:
        raise ExtractRelayError(f"cannot talk to {socket_path}: {e}",
                                "extractor_unreachable") from None
    if not buf:
        raise ExtractRelayError(
            "the extractor closed the connection without replying",
            "extractor_unreachable")
    try:
        return json.loads(bytes(buf).split(b"\n")[0])
    except ValueError as e:
        raise ExtractRelayError(f"the extractor's reply is not JSON: {e}",
                                "extractor_protocol") from None


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
        # `admit` and `queued`, because "why is my job still QUEUED" was
        # unanswerable from here. The reasons were logged at INFO once per poll,
        # so they sit in journald surrounded by hundreds of copies of themselves,
        # and `stall` alone covers only one of the several ways admission stops.
        # A queue that is not moving is the likeliest question to ask this
        # endpoint, and it could not answer it.
        may, reason = self.may_admit()
        # The RESOURCE gate, separately from `admit`. `may_admit` covers the
        # cleanup stall and the nightly intent gate only -- the aggregate memory
        # budget and the disk floor are read inside `try_one`, one step later --
        # so the two commonest answers to "why is my job still QUEUED" were the
        # two this endpoint could not give. It reported `free_disk_mb` without
        # the floor to compare it against, which reads like an answer and is not
        # one.
        #
        # Asked at SMALLEST_MEM_LIMIT so a false "ok" is impossible: a bigger
        # reservation could be refused while a small one is admitted, and this
        # field must not say "resources are fine" when the queue is frozen.
        res_ok, res_why = self.db.call(
            "admit", SMALLEST_MEM_LIMIT,
            free_disk_mb=free_disk_mb(self.cfg.runs_dir))
        return {
            "admit": "ok" if may else reason,
            "resource": "ok" if res_ok else res_why,
            "resource_at": SMALLEST_MEM_LIMIT,
            "disk_floor_mb": self.cfg.disk_floor_gb * 1024,
            "mutex": probe_mutex(self.cfg.lock_file),
            "queued": len(self.db.call("list", state="QUEUED", limit=500)),
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
        # The CLOCK and the SETTLEMENT LAG, because the `extract` kind needs
        # both: `as_of_date` must be a completed UTC boundary past the lag (D20),
        # and a validator with no clock cannot check that.
        #
        # This is qfd's OWN copy of the lag, and the duplication is deliberate
        # per D17: the extractor holds the authoritative value and validates
        # again, so if the two disagree the extractor wins. qfd's copy exists
        # only so a bad request is refused at submit time with a legible message
        # rather than after a job has been queued, leased and relayed.
        effective = spec_mod.normalize(
            raw,
            now=datetime.datetime.fromtimestamp(self.clock(),
                                                datetime.timezone.utc),
            settlement_lag_s=self.cfg.settlement_lag_s)

        # NC9's LEGIBLE HALF. Refused at submit, before a job exists, when the
        # named contract is not in the trusted checkout -- so a typo costs a
        # message rather than a queued job that fails minutes later. The
        # EVALUATOR refuses authoritatively; this cannot be the only check,
        # because qfd is in the `docker` group and a control enforced only by the
        # most privileged process in the loop is not a control.
        if effective["kind"] == "evaluate":
            contracts = self.available_contracts()
            if effective["args"]["contract"] not in contracts:
                raise Refused(
                    f"no contract {effective['args']['contract'][:12]} in the"
                    f" trusted checkout. Available: "
                    + (", ".join(f"{h[:12]} ({n})"
                                 for h, n in sorted(contracts.items()))
                       or "none")
                    + ". `qf contracts` lists them.")

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
            # AS A PIN, because that is where `source_ref` actually lives: the
            # same-named `jobs` column stays NULL for every job (the runner
            # writes the pin mid-run for a test job), so a value put only in the
            # spec would appear nowhere a reader looks. For an extract this
            # matters more than usual -- it is the literal that stops
            # `source_sha` being mistaken for a commit.
            if effective.get("source_ref"):
                self.db.call("set_pin", run_id, "source_ref",
                             effective["source_ref"], now=now)
        return {"run_id": run_id, "state": "QUEUED",
                "spec_hash": spec_mod.spec_hash(effective)}

    def _op_status(self, payload, uid):
        run_id = payload.get("run_id")
        job = self.db.call("get", run_id) if isinstance(run_id, str) else None
        if job is None:
            raise Refused(f"no such run {run_id!r}")
        job["spec"] = json.loads(job["spec_json"])
        # The PINS, because that is where several things a reader looks for
        # actually live. `source_ref` is the clearest case: it is written as a pin
        # mid-run, so the same-named `jobs` column stays null for every job, and
        # a status that showed only the row reported "source_ref": null while the
        # ref -- the whole point of which is that a human can open it at a URL --
        # sat one table away.
        job["pins"] = self.db.call("pins_for", run_id)
        return {"job": job, "stall": self.cleanup_stall()}

    def _op_extracts(self, payload, uid):
        """What the extractor has published, with hashes and watermarks.

        RELAYED, not read. The extracts directory belongs to `qfextract`, and
        having the dispatcher walk it would put the layout in two places -- which
        is exactly how the publication path came to have a side index that could
        disagree with the artifacts. The extractor knows what it published.
        """
        try:
            reply = extract_request(self.cfg.extract_socket,
                                    {"op": "extracts"}, timeout=30)
        except ExtractRelayError as e:
            raise Refused(str(e)) from None
        if not reply.get("ok"):
            raise Refused(reply.get("error", "the extractor refused"))
        return {"extracts": reply.get("extracts", [])}

    # A listing cap. Not silent: `truncated` is reported, because a listing that
    # quietly stops is how a prefix resolves to "no match" for a baseline that is
    # right there.
    BASELINES_LIMIT = 200

    def available_contracts(self):
        """`{contract_hash: name}` from the trusted checkout.

        THE SAME RESOLUTION THE EVALUATOR DOES, and deliberately duplicated
        rather than relayed -- the same decision as the settlement lag (D17).
        Both read the same root-owned directory, so they cannot disagree unless
        the checkout moved between submit and run; the EVALUATOR's answer is
        authoritative, and this copy exists so a job naming an unknown contract
        is refused at submit time with a legible message instead of after being
        queued, leased and relayed.

        A file that does not validate is omitted, not offered: `contract.load`
        rehashes, so a contract edited since it was written resolves to nothing
        and the job is refused for naming an unknown rule -- which is the honest
        answer, because a rule whose content no longer matches its identity is
        not the rule the caller asked for.
        """
        out = {}
        root = self.cfg.contracts_dir
        if not root:
            return out
        try:
            names = sorted(os.listdir(root))
        except OSError as e:
            log.error("cannot list the contracts directory %s: %s", root, e)
            return out
        for name in names:
            # `.json.in` templates carry an unpinned baseline; the validator
            # refuses them, and offering one would be offering a rule that
            # judges against nothing.
            if not name.endswith(".json"):
                continue
            try:
                _body, digest = contract_mod.load(os.path.join(root, name))
            except contract_mod.ContractError as e:
                log.error("ignoring contract %s: %s", name, e)
                continue
            out.setdefault(digest, name)
        return out

    def _op_contracts(self, payload, uid):
        """What the trusted checkout carries. Read here, like `baselines`: the
        directory is root-owned and qfd cannot write it, so reading it is not a
        second writer."""
        contracts = self.available_contracts()
        return {"contracts": [{"contract_hash": h, "file": n}
                              for h, n in sorted(contracts.items(),
                                                 key=lambda kv: kv[1])],
                "dir": self.cfg.contracts_dir}

    def _op_baselines(self, payload, uid):
        """What has been promoted to the baseline store.

        READ HERE, not relayed, and the asymmetry with `extracts` is deliberate.
        The extracts directory belongs to another privilege domain, so walking it
        from here would put the layout in two places. The baseline store has no
        service: it is root-owned and written by a human running
        `promote-baseline.sh`. qfd is already the only process that reads it --
        it resolves a hash to a mount -- so this is not a second reader.

        A directory whose manifest is missing, unreadable or does not hash to its
        own name is reported as `broken` rather than omitted. Omitting it would
        make a half-promoted directory invisible to the one command an operator
        would use to find out why a probe was refused.
        """
        root = self.cfg.baselines_dir
        try:
            names = sorted(os.listdir(root))
        except FileNotFoundError:
            return {"baselines": [], "store": root, "truncated": False}
        except OSError as e:
            raise Refused(f"the baseline store at {root} is unreadable: {e}") \
                from None

        rows, seen = [], 0
        for name in names:
            if not HEX64_RE.match(name):
                # `.staging.*` and anything else: the store's own scratch, or
                # something that is not a baseline. Not an error, not a row.
                continue
            seen += 1
            if len(rows) >= self.BASELINES_LIMIT:
                continue
            path = os.path.join(root, name)
            row = {"baseline_hash": name}
            try:
                with open(os.path.join(path, "MANIFEST.json")) as fh:
                    manifest = json.load(fh)
                if not isinstance(manifest, dict):
                    raise ValueError("manifest is not an object")
                row["days"] = len(manifest.get("days") or [])
                row["ndjson_rows"] = manifest.get("ndjson_rows")
                row["pending_at_min"] = manifest.get("pending_at_min")
                row["pending_at_max"] = manifest.get("pending_at_max")
                row["exclude_dates"] = manifest.get("exclude_dates") or []
                if baseline_mod.baseline_hash(manifest) != name:
                    row["broken"] = "the manifest does not hash to its own name"
            except (OSError, ValueError, TypeError) as e:
                row["broken"] = f"the manifest is unreadable ({e})"
            row["promoted_at"] = self._promoted_at(path)
            rows.append(row)
        rows.sort(key=lambda r: (r.get("promoted_at") or "", r["baseline_hash"]))
        return {"baselines": rows, "store": root,
                "truncated": seen > len(rows), "published": seen}

    @staticmethod
    def _promoted_at(path):
        """When this baseline was published, from the sidecar the promoter wrote.

        A SIDECAR rather than a manifest field, because the manifest IS the
        identity: a `promoted_at` inside it would make every promotion of the
        same bytes a different artifact, which is what a content key exists to
        prevent. And a sidecar rather than the directory mtime, because an mtime
        survives a filesystem copy as a confident wrong answer.
        """
        try:
            with open(os.path.join(path, "PROMOTED_AT")) as fh:
                value = fh.read(64).strip()
        except OSError:
            return None
        return value or None

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
        # 0.5s, LOWERED FROM 2s BY MEASUREMENT (2026-08-28).
        #
        # A sampled bound cannot be exact, and at 2s it was not close: NC15's
        # flood fixture writes 1 MiB blocks in a tight loop, and the run
        # directory finished between 1.9x and 3.7x the 2048 MiB quota across five
        # runs (3845, 4205, 5963, 7042, 7605 MiB). The worst overshoot implies
        # ~2.7 GB/s of buffered writes, so a 2s window is ~5 GiB of rope.
        #
        # 0.5s brings the worst case to roughly 1.7x. That is better and still
        # approximate, which is why **the disk floor is the real protection** --
        # 20 GiB reserved for the dispatcher, and the worst observed flood was
        # 37% of it. OUT_QUOTA stops a runaway; the floor is what keeps the host
        # alive while it is being stopped.
        #
        # The cost is a `du` of one run directory twice a second. It holds a
        # handful of files, so this is a stat, not a walk.
        self.out_sample_interval_s = 0.5
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
        # Mutex state, logged at TRANSITIONS rather than per poll. Two light
        # workers polling every 2s produced 900 identical "light lock
        # unavailable" lines across a 15-minute nightly run -- which is both
        # unreadable and, worse, hides the one thing worth knowing: HOW LONG.
        # Logging the start and the end makes the duration a fact in the journal
        # instead of something to be reconstructed by counting lines.
        self._lock_wait = {}
        self._mutex_fault = set()
        self._refusals = {}
        self._lock_state_lock = threading.Lock()
        # ONE extraction at a time, and NOT via the training mutex. The extractor
        # enforces this too, with a non-blocking flock -- but its refusal would
        # fail a job that should simply have waited, so the queue holds the
        # second one here instead.
        self._extract_slot = threading.Semaphore(1)
        self.qfrun_gid = _gid("qfrun")
        self.qfclient_gid = _gid("qfclient")
        # The evaluator's group, for the staged inbox (D28). `None` on a host
        # where 2c is not installed, and the staging step tolerates that: a
        # dispatcher that refused to start because a LATER phase's group is
        # absent would make installing 2c a prerequisite for running 2a.
        self.qfeval_gid = _gid("qfeval")
        # Injectable so the whole execute path is testable without a docker
        # binary. It was a bare `subprocess.Popen` call, which is a large part of
        # why this path shipped both unwired and with a None image reference.
        self.spawn = subprocess.Popen

    def _note_refusal(self, lane, kind, detail):
        """Log an admission refusal when its KIND changes, not once per poll.

        Two light workers polling every two seconds produce ~3600 lines an hour,
        and the ones that matter -- it started, it stopped -- are indistinguishable
        from the ones that do not. The kind is the key rather than the whole
        message because the message carries a moving number ("12043m free"), and
        keying on that would log every poll again.
        """
        with self._lock_state_lock:
            changed = self._refusals.get(lane) != kind
            self._refusals[lane] = kind
        if changed:
            log.info("lane %s: not admitting (%s): %s", lane, kind, detail)

    def _note_admitted(self, lane):
        with self._lock_state_lock:
            previous = self._refusals.pop(lane, None)
        if previous is not None:
            log.info("lane %s: admitting again; %s cleared", lane, previous)

    def _note_mutex_contended(self, lane):
        """Log the START of a wait, not each poll of it."""
        with self._lock_state_lock:
            first = lane not in self._lock_wait
            if first:
                self._lock_wait[lane] = time.monotonic()
        if first:
            log.info("lane %s: waiting for the training mutex; it is held"
                     " elsewhere (the nightly walk-forward, most often). No job"
                     " in this lane is admitted until it is free.", lane)

    def _note_mutex_acquired(self, lane):
        """Log the END of a wait, with its duration."""
        with self._lock_state_lock:
            started = self._lock_wait.pop(lane, None)
            self._mutex_fault.discard(lane)
        if started is not None:
            log.info("lane %s: training mutex acquired after %ds of waiting",
                     lane, int(time.monotonic() - started))

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
            self._note_refusal(lane, reason, reason)
            return False

        # THE KIND IS KNOWN HERE -- `peek` returned the row -- and it decides
        # WHICH lock is taken. Work that happens in ANOTHER privilege domain must
        # not hold the training mutex: see `ExtractSlot`. `evaluate` joins
        # `extract` for the same reason and one more -- scoring an experiment
        # that has already finished has no business making the nightly wait.
        is_relayed = head["kind"] in RELAYED_KINDS
        try:                                                    # 3
            if is_relayed:
                lock = ExtractSlot(self._extract_slot, lane).acquire()
            else:
                lock = TrainingLock(self.cfg.lock_file, lane).acquire()
        except MutexUnusable as e:
            # ERROR, and once: this does not clear on its own, and repeating it
            # every two seconds would bury it in itself.
            with self._lock_state_lock:
                first = lane not in self._mutex_fault
                self._mutex_fault.add(lane)
            if first:
                log.error("lane %s: %s", lane, e)
            return False
        except LockHeld:
            self._note_mutex_contended(lane)
            return False
        self._note_mutex_acquired(lane)

        effective = json.loads(head["spec_json"])
        try:
            ok, why = self.db.call(                             # 4
                "admit", effective["mem_limit"],
                free_disk_mb=free_disk_mb(self.cfg.runs_dir))
            if not ok:
                # The kind is the text before the colon ("disk floor", "memory
                # budget"); the detail carries the numbers.
                self._note_refusal(lane, why.split(":", 1)[0], why)
                lock.release()
                return False
            self._note_admitted(lane)

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
        # Phase 2b-2: the one writable hole in the read-only source tree. The
        # trainer computes CACHE_DIR and its model output path relative to its
        # own module, so `/app/trainer/data` must be writable even though
        # `/app/trainer` is mounted read-only -- and it must be RUN-PRIVATE, or
        # one job's cache would decide another job's input, which is exactly the
        # contamination a frozen extract exists to remove.
        #
        # Same shape as `out/` deliberately. Nothing reads this directory today,
        # so the setgid bit is not load-bearing here as it is there; a second
        # shape would just be something a reader has to have explained.
        ("data",      "qfrun",    0o2770),
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
        # TWO lines per run in the journal, here and in `finish`. The event store
        # is the audit trail and always was, but an operator reads `journalctl`,
        # and a healthy run used to log NOTHING there -- so silence meant either
        # "it worked" or "nothing was even picked up", with no way to tell them
        # apart. A subsystem that only speaks when it is unhappy cannot be
        # watched.
        log.info("%s: starting in lane %s: kind=%s sha=%s mem=%s",
                 run_id, hold.lock.lane, hold.job["kind"],
                 effective["source_sha"][:12], effective.get("mem_limit"))
        renewer = self._start_renewer(hold)
        outcome = ("FAILED", {"error_class": "internal"})
        try:
            # Setup is inside the outer deadline too. It is git and filesystem
            # work on an agent-authored repository, so it is not free, and an
            # earlier version measured the deadline only from the container.
            if hold.expired():
                raise DeadlineExpired("the hold deadline passed before setup")

            # AN EXTRACTION RUNS NO CODE, so none of the setup below applies to
            # it: no run directory, no worktree, no image, no container. It is a
            # job so that it gets the state machine, the event chain and
            # `qf status`; the work itself happens in another privilege domain
            # (D15), and all this side does is ask and record what came back.
            if effective["kind"] == "probe":
                # BEFORE the worktree and the image build. A bad extract or
                # baseline reference then costs seconds rather than minutes, and
                # the provenance is recorded even if the run fails later.
                self._pin_probe_extract(hold, effective)
                self._pin_probe_baseline(hold, effective)
            if effective["kind"] in RELAYED_KINDS:
                # LEASED -> RUNNING FIRST, and this is not bookkeeping.
                #
                # `finish` returns whatever the relay returned, and the state
                # machine has no LEASED -> SUCCEEDED edge (store.ALLOWED): a
                # successful extraction persisted "cannot move LEASED ->
                # SUCCEEDED" and the row stayed LEASED with no exit code. Every
                # test of this path called `_relay_extract` and inspected its
                # return tuple, so none of them ever reached `finish`.
                #
                # RUNNING is also the honest answer while a relay is in flight:
                # the extraction genuinely is running, in another process.
                self.db.call("transition", hold.run_id, "RUNNING",
                             now=utcnow())
                outcome = (self._relay_extract(hold, effective)
                           if effective["kind"] == "extract"
                           else self._relay_evaluate(hold, effective))
                return
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
        except ProbeInputMissing as e:
            log.error("%s: %s", run_id, e)
            outcome = ("FAILED", {"error_class": e.error_class,
                                  "finished_at": utcnow()})
        except source_mod.NotPublished as e:
            log.error("%s: %s", run_id, e)
            outcome = ("FAILED", {"error_class": "source_not_published"})
        except source_mod.SourceError as e:
            # The BASE class, after its two specific subclasses. Everything else
            # git can fail at -- a token that cannot read the remote, DNS, a
            # remote that refuses, a corrupt mirror -- used to fall through to
            # the generic handler and be reported as `internal`, which points the
            # operator at a dispatcher bug when the fault is in the source or the
            # credential. An error class is a routing decision, so a class that
            # names the wrong subsystem costs an investigation.
            log.error("%s: source: %s", run_id, e)
            outcome = ("FAILED", {"error_class": "source_unavailable",
                                  "finished_at": utcnow()})
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

    # Injectable so the relay is testable without a listening socket. The
    # default is the real transport.
    extract_client = staticmethod(extract_request)
    eval_client = staticmethod(eval_request)

    PREDICTIONS_NAME = "predictions.parquet"

    def _evaluate_source(self, effective):
        """The run being judged, checked. Returns `(job, pins, predictions path)`.

        EVERY IDENTITY COMES FROM THE JUDGED RUN'S OWN PINS, never from the
        evaluate job's args. A caller that could name the extract would be able
        to claim it had evaluated cohort A's predictions against cohort B's data,
        and the verdict would look exactly like a real one.
        """
        run_id = effective["args"]["run"]
        job = self.db.call("get", run_id)
        if job is None:
            raise EvaluateInputMissing(
                f"no run {run_id}: an evaluation judges a probe that has already"
                f" finished. `qf list` shows what exists.")
        if job["kind"] != "probe":
            raise EvaluateInputMissing(
                f"{run_id} is a {job['kind']} job, not a probe. Only a probe"
                f" produces a prediction set to judge.")
        if job["state"] != "SUCCEEDED":
            raise EvaluateInputMissing(
                f"{run_id} is {job['state']}, not SUCCEEDED: judging a run that"
                f" did not finish would score a partial prediction set as though"
                f" it were the whole one")
        pins = self.db.call("pins_for", run_id)
        if not pins.get("request_hash"):
            raise EvaluateInputMissing(
                f"{run_id} records no request_hash, so there is no way to know"
                f" which extract its predictions were made against. A verdict"
                f" without that is not attributable to any data.")
        # THE RECORDED ARTIFACT, NOT `out/`. The first version staged
        # `<run>/out/predictions.parquet`, which is the candidate's own output
        # directory: nothing has a digest for it, `out/` is pruned once the
        # handoff has copied what the allowlist names (D9), and the bytes can
        # change after the probe succeeds. So a verdict could be attributed to a
        # probe while judging bytes that probe never produced -- and this
        # project's own NC11 clauses demonstrated it, mutating `out/` after a
        # SUCCEEDED run and getting a scored result. A negative control that
        # works because of a defect is worse than no control.
        #
        # `artifacts/predictions.parquet` is the copy the handoff made, whose
        # sha256 `add_artifact` recorded in the event store at the moment the run
        # finished. That digest is the only thing that ties bytes to a run.
        artifact = self.db.call("artifact", run_id, self.PREDICTIONS_NAME)
        if not artifact:
            raise EvaluateInputMissing(
                f"{run_id} recorded no {self.PREDICTIONS_NAME} artifact: it"
                f" succeeded without producing the prediction set the contract"
                f" judges, or the handoff did not collect it")
        recorded = artifact.get("sha256")
        if not isinstance(recorded, str) or not HEX64_RE.match(recorded):
            raise EvaluateInputMissing(
                f"{run_id}'s {self.PREDICTIONS_NAME} artifact records"
                f" {str(recorded)[:16]!r} as its digest, which is not a sha256."
                f" Without one there is nothing tying these bytes to this run.")
        path = artifact.get("path") or os.path.join(
            self.run_dir(run_id), "artifacts", self.PREDICTIONS_NAME)
        if not os.path.isfile(path):
            raise EvaluateInputMissing(
                f"{run_id} records a {self.PREDICTIONS_NAME} artifact at {path},"
                f" which is not there. The store has been pruned or the run"
                f" directory was removed; an evaluation cannot be attributed to"
                f" bytes nobody has.")
        if os.path.getsize(path) == 0:
            raise EvaluateInputMissing(
                f"{run_id}'s {self.PREDICTIONS_NAME} is empty. An empty"
                f" prediction set joins to nothing, which produces a verdict"
                f" over zero rows rather than an error.")
        # CHECKED NOW, BEFORE STAGING, so the failure names the artifact rather
        # than the copy. `_stage_predictions` checks the staged bytes against the
        # same recorded digest, which is what closes the window between the two.
        current = file_digest(path)
        if current != recorded:
            raise EvaluateInputMissing(
                f"{run_id}'s {self.PREDICTIONS_NAME} now digests to"
                f" {current[:12]} but the run recorded {recorded[:12]} when it"
                f" finished. The artifact has changed since the probe produced"
                f" it, so judging it would attribute a verdict to a run that did"
                f" not emit these bytes.")
        return job, pins, path, recorded

    def _give_to_the_evaluator(self, path):
        """Put `path` in the `qfeval` group, or refuse and name the fix.

        WHY THIS IS NOT A BARE `os.chown`. `qfd` is not in the `qfeval` group and
        must not need to be: it is already root-equivalent through `docker`, so
        the membership would not widen anything, but a domain that has to be
        JOINED to hand over a file is one more thing an install step can forget
        and no test can see. Instead `/var/lib/qf-eval` is `2770 qfd:qfeval`
        (phase2c-setup.sh's tmpfiles config), so everything created under it is
        already in the evaluator's group and there is nothing to change.

        Linux permits `chown(-1, gid)` only for a member of `gid`, or root. So
        the check is on the STATE rather than on the call: if the group is already
        right there is nothing to do, and if it is not, this host cannot give the
        evaluator its input and must say so here. Staging anyway would surface
        one privilege domain away from the cause, as "the evaluator cannot read
        the prediction set".
        """
        if self.qfeval_gid is None:
            # 2c is not installed on this host. The relay will fail at the
            # socket, which is the honest place for it -- refusing here would
            # make installing 2c a prerequisite for running 2a.
            return
        if os.stat(path).st_gid == self.qfeval_gid:
            return
        try:
            os.chown(path, -1, self.qfeval_gid)
        except OSError as e:
            raise EvaluateStagingDenied(
                f"{path} is not in the qfeval group and this dispatcher cannot"
                f" put it there ({e}). {self.cfg.eval_dir} must be 2770"
                f" qfd:qfeval, so that everything staged under it is in the"
                f" evaluator's group by inheritance: `sudo ./phase2c-setup.sh"
                f" install` provisions exactly that. Refusing rather than"
                f" staging a file the evaluator cannot open.") from None

    def _stage_predictions(self, run_id, source, recorded_sha256):
        """Copy the untrusted prediction set where the evaluator can read it.

        WHY A COPY AT ALL (D28). The base run directory is `0750 qfd:qfclient`,
        so reaching `out/` needs `qfclient` -- the group that lets `research`
        read logs and artifacts. Putting the evaluator in it to reach one file
        would hand the narrowest domain in the system the client surface too, and
        a directory carries one group, which is already taken.

        So the ONE untrusted input is copied to a place the candidate cannot
        reach, and the immutable content-hashed stores are read where they lie.

        Published by RENAME, and the digest is taken from the STAGED bytes rather
        than from the source: what the evaluator will read is what must be
        described, and a digest of the original would still verify if the copy
        were truncated.
        """
        base = os.path.join(self.cfg.eval_dir, run_id)
        inbox = os.path.join(base, "in")
        outbox = os.path.join(base, "out")
        os.makedirs(inbox, exist_ok=True)
        os.makedirs(outbox, exist_ok=True)
        # THE GROUP FIRST, THE MODE SECOND. The mode's setgid bit governs what
        # CHILDREN inherit, so it is only worth anything once the group it would
        # propagate is the right one.
        for path in (base, inbox, outbox):
            self._give_to_the_evaluator(path)
        # SETGID ON THE INBOX IS THE WHOLE MECHANISM, and the version this
        # replaces did not have it. It chmodded the three directories to
        # 0750/0750/0770 and chowned their GROUP to qfeval, and then created
        # `predictions.parquet` inside the inbox -- a file whose group comes from
        # the parent directory's setgid bit, which had just been cleared. So the
        # staged prediction set would have been `0640 qfd:qfd` inside a directory
        # the evaluator could traverse: the one file this whole staging path
        # exists to hand over would have been the one thing it could not read,
        # and `qfd` never chowns the file itself. Nothing in the suite could see
        # it, because every test runs as one uid.
        #
        # 2750 on the base and the inbox: the evaluator's group traverses and
        # reads, nobody else sees anything. 2770 on the outbox, which is the only
        # thing it writes.
        os.chmod(base, 0o2750)
        os.chmod(inbox, 0o2750)
        os.chmod(outbox, 0o2770)
        target = os.path.join(inbox, self.PREDICTIONS_NAME)
        tmp = target + ".partial"
        with open(source, "rb") as src, open(tmp, "wb") as dst:
            for chunk in iter(lambda: src.read(1 << 20), b""):
                dst.write(chunk)
        os.chmod(tmp, 0o640)
        os.replace(tmp, target)
        digest = file_digest(target)
        # THE STAGED BYTES MUST BE THE RECORDED BYTES. The digest is taken from
        # the copy -- what the evaluator will read is what must be described, and
        # a digest of the source would still verify if the copy were truncated --
        # and it is then compared against what the RUN recorded, which is what
        # makes the evaluator's input attributable to the probe rather than
        # merely internally consistent.
        if digest != recorded_sha256:
            raise EvaluateInputMissing(
                f"the staged copy of {run_id}'s {self.PREDICTIONS_NAME} digests"
                f" to {digest[:12]}, not the {recorded_sha256[:12]} the run"
                f" recorded. The artifact changed while it was being copied.")
        return target, digest

    def _unstage_predictions(self, staged):
        """Remove the staged copy once the relay is over, whatever it returned.

        WHY THE COPY GOES AND THE VERDICT STAYS. The staged prediction set is a
        second copy of bytes that already exist, digest-recorded, in the run's
        `artifacts/` -- the one thing under /var/lib/qf-eval that can be deleted
        without losing anything, and also the largest. NOTHING ELSE WAS GOING TO:
        `qf-runs-prune` is scoped to /var/lib/qf-runs, its unit's
        `ReadWritePaths=` says so, and no timer touches this tree at all.

        The verdict and `eval.parquet` in `out/` STAY. They are the record, and a
        record deleted by a timer while the job row still cites it is the dangling
        reference this system spends its effort not creating. They accumulate --
        see the plan's open item, with the arithmetic -- and the answer to that is
        a retention policy somebody chooses, not deleting evidence here.

        A failure to remove is logged, not raised: the evaluation has already
        happened and its verdict is already recorded, so failing the job over
        cleanup would misattribute an outcome that is already known.
        """
        try:
            os.unlink(staged)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("could not remove the staged %s: %s", staged, e)

    def _relay_evaluate(self, hold, effective):
        """Ask the evaluator, record the verdict. Returns an outcome tuple.

        Same shape as `_relay_extract`, including the part that took a P1 to
        learn: the state machine has no `LEASED -> SUCCEEDED` edge, so the caller
        transitions to RUNNING before this is reached.
        """
        run_id = hold.run_id
        try:
            _job, pins, source, recorded = self._evaluate_source(effective)
            staged, digest = self._stage_predictions(run_id, source, recorded)
        except EvaluateInputMissing as e:
            # STAGING IS INSIDE THE SAME GUARD as resolution: its digest check is
            # the second half of one property, and a mismatch there is the same
            # kind of failure as a missing artifact, not an internal fault.
            log.error("%s: %s", run_id, e)
            return ("FAILED", {"error_class": e.error_class,
                               "finished_at": utcnow()})
        try:
            now = utcnow()
            # PINNED BEFORE THE RELAY, so a failed evaluation still says
            # what it was judging and what it was judging by. Provenance that
            # exists only on the happy path is provenance a reader cannot rely
            # on -- the same rule as a probe's `baseline: none`.
            for key, value in (
                    ("judged_run", effective["args"]["run"]),
                    ("contract_hash", effective["args"]["contract"]),
                    ("request_hash", pins.get("request_hash")),
                    ("baseline_hash", pins.get("baseline_hash")),
                    ("baseline", pins.get("baseline")),
                    ("predictions_sha256", digest),
            ):
                if value is not None:
                    self.db.call("set_pin", run_id, key, str(value), now=now)

            request = {
                "op": "evaluate",
                "run_id": run_id,
                "contract": effective["args"]["contract"],
                "request_hash": pins["request_hash"],
                "predictions_sha256": digest,
            }
            if pins.get("baseline_hash"):
                request["baseline_hash"] = pins["baseline_hash"]
            budget = min(effective["timeout_s"], max(1, int(hold.remaining())))
            log.info("%s: asking the evaluator to judge %s by contract %s"
                     " (timeout %ss)", run_id, effective["args"]["run"],
                     effective["args"]["contract"][:12], budget)
            try:
                reply = self.eval_client(self.cfg.eval_socket, request, budget)
            except EvalRelayError as e:
                log.error("%s: evaluator: %s", run_id, e)
                return ("FAILED", {"error_class": e.error_class,
                                   "finished_at": utcnow()})

            if not reply.get("ok"):
                # The evaluator's own refusal text: it wrote it to be read,
                # and its unexpected failures already arrive as an opaque
                # journal reference, so nothing a dependency produced is passed
                # through. The CLASS is taken from the reply when it names one
                # -- `contract_not_trusted` is the NC9 outcome and must not be
                # flattened into "refused".
                klass = reply.get("error_class")
                if (not isinstance(klass, str)
                        or not _ERROR_CLASS_RE.match(klass)):
                    klass = "evaluate_refused"
                log.error("%s: evaluator refused (%s): %s", run_id, klass,
                          reply.get("error"))
                return ("FAILED", {"error_class": klass,
                                   "finished_at": utcnow()})

            verdict = reply.get("verdict")
            if verdict not in ("go", "no-go"):
                # A REPLY THAT SAYS `ok` IS NOT A VERDICT. The extract relay
                # learned this: `{"ok": true, "manifest": {}}` was recorded as
                # a successful extract, and a test enshrined it as "does not
                # crash".
                log.error("%s: the evaluator's reply carries no verdict (%r)",
                          run_id, verdict)
                return ("FAILED", {"error_class": "evaluate_reply_invalid",
                                   "finished_at": utcnow()})
            now = utcnow()
            self.db.call("set_pin", run_id, "verdict", verdict, now=now)
            if isinstance(reply.get("eval_hash"), str):
                self.db.call("set_pin", run_id, "eval_hash",
                             reply["eval_hash"], now=now)
            log.info("%s: verdict %s", run_id, verdict)
            return ("SUCCEEDED", {"exit_code": 0, "finished_at": utcnow()})
        finally:
            # WHATEVER HAPPENED. A refusal, a relay fault and an unexpected
            # exception all leave a staged copy behind, and the one that
            # would accumulate fastest is the failure that repeats.
            self._unstage_predictions(staged)

    def _relay_extract(self, hold, effective):
        """Ask the extractor, record what came back. Returns an outcome tuple.

        A CANCEL CANNOT INTERRUPT THIS, and that is a property rather than an
        oversight: the work is happening in another privilege domain, which the
        dispatcher has no authority over and deliberately cannot signal. A cancel
        requested during a relay takes effect when the reply arrives. Since a
        published extract is immutable, the extraction is not wasted either way.
        """
        run_id = hold.run_id
        import extract_spec

        # The INPUT fields, not the normalised ones: the extractor validates
        # again and refuses derived fields by name, so forwarding what we
        # validated would be refused for carrying `target_column`.
        request = extract_spec.to_raw(effective["args"])
        budget = min(effective["timeout_s"], max(1, int(hold.remaining())))
        log.info("%s: asking the extractor for %s %s..%s (timeout %ss)",
                 run_id, request["target"], request["train_start"],
                 request["as_of_date"], budget)
        try:
            reply = self.extract_client(
                self.cfg.extract_socket,
                {"op": "extract", "request": request}, budget)
        except ExtractRelayError as e:
            log.error("%s: extractor: %s", run_id, e)
            return ("FAILED", {"error_class": e.error_class,
                               "finished_at": utcnow()})

        if not reply.get("ok"):
            # The extractor's own refusal text, which it wrote to be read. Its
            # unexpected failures already arrive here as an opaque reference, so
            # nothing a dependency produced is passed through.
            log.error("%s: extractor refused: %s", run_id,
                      reply.get("error"))
            return ("FAILED", {"error_class": "extract_refused",
                               "finished_at": utcnow()})

        manifest = reply.get("manifest") or {}
        problem = self._extract_reply_problem(reply, manifest, request,
                                              effective)
        if problem is not None:
            # A REPLY THAT SAYS `ok` IS NOT AN EXTRACT.
            #
            # The first version accepted `{"ok": true, "manifest": {}}`, skipped
            # every missing pin, and recorded SUCCEEDED -- and a test enshrined
            # it as "does not crash". A job row saying an extract exists, with no
            # hash and no directory, is worse than a failure: 2b-2 would look for
            # an extract this row claims to have.
            log.error("%s: the extractor's reply is not usable: %s",
                      run_id, problem)
            return ("FAILED", {"error_class": "extract_reply_invalid",
                               "finished_at": utcnow()})
        now = utcnow()
        # PINS, never new columns (design 4.6). These are the identities 2b-2 and
        # 2c need: `request_hash` is what reuse is keyed on, `extract_hash` is
        # what every member of a comparison must share, and the watermark is the
        # provenance that says what this extract actually contained.
        for key, value in (
                ("request_hash", manifest.get("request_hash")),
                ("extract_hash", manifest.get("extract_hash")),
                ("extract_dir", reply.get("dir")),
                ("extract_watermark",
                 json.dumps(manifest.get("watermark") or {}, sort_keys=True)),
                ("extract_rows", json.dumps(
                    {name: entry.get("rows")
                     for name, entry in (manifest.get("files") or {}).items()},
                    sort_keys=True)),
        ):
            if value is not None:
                self.db.call("set_pin", run_id, key, value, now=now)
        log.info("%s: extract %s ready at %s", run_id,
                 (manifest.get("extract_hash") or "?")[:12], reply.get("dir"))

        # THE CANCEL IS CHECKED AFTER THE PINS ARE WRITTEN, and the order is the
        # whole point. A cancel cannot stop an extraction -- the work is in
        # another privilege domain this process has no authority to signal -- so
        # by the time the reply arrives the extract is published and immutable.
        #
        # Recording the pins first means the artifact is discoverable whatever
        # the job's state; reporting CANCELLED then means the JOB says what
        # actually happened to it. Reporting SUCCEEDED for something an operator
        # cancelled would make `qf list --state CANCELLED` stop meaning anything,
        # which is the same rule the container path follows.
        if hold.cancel_requested.is_set():
            log.info("%s: cancelled while the extractor was working; the"
                     " extract is published and its pins are recorded", run_id)
            return ("CANCELLED", {"error_class": "cancelled",
                                  "finished_at": now})
        return ("SUCCEEDED", {"exit_code": 0, "finished_at": now})

    def _extract_reply_problem(self, reply, manifest, request, effective):
        """Why this reply cannot be recorded as an extract, or None.

        Checked because the reply crosses a trust boundary in the direction
        nobody usually looks: the extractor is trusted, but "trusted" is not
        "incapable of a bug", and the failure mode of believing it is a job row
        that claims an extract which is not there.
        """
        import extract_spec

        for field in ("request_hash", "extract_hash"):
            value = manifest.get(field)
            if not isinstance(value, str) or not HEX64_RE.match(value):
                return f"{field} is {value!r}, not a sha256"

        # THE REQUEST HASH MUST BE THE ONE WE ASKED FOR. Otherwise a reply about
        # a different window would be recorded against this job -- and since
        # reuse is keyed on `request_hash` (D20), that is the one field capable
        # of pointing this row at somebody else's extract.
        expected = extract_spec.request_hash(
            extract_spec.validate(request, now=self.clock_dt(),
                                  settlement_lag_s=self.cfg.settlement_lag_s))
        if manifest["request_hash"] != expected:
            return (f"request_hash {manifest['request_hash'][:12]} is not the"
                    f" request we sent ({expected[:12]})")
        if effective["source_sha"] != expected:
            return (f"the job's identity {effective['source_sha'][:12]} does not"
                    f" match the request we sent ({expected[:12]})")

        directory = reply.get("dir")
        if not isinstance(directory, str) or not directory:
            return "the reply names no directory"
        if os.path.basename(directory.rstrip("/")) != manifest["request_hash"]:
            # D20 publishes under `<request_hash>`; a directory anywhere else is
            # either a different layout or a different extract.
            return (f"the directory {directory} is not the canonical location"
                    f" for {manifest['request_hash'][:12]}")

        files = manifest.get("files")
        if not isinstance(files, dict) or not files:
            return "the manifest lists no files"
        for name, entry in files.items():
            if not isinstance(entry, dict) or not entry.get("sha256"):
                return f"the manifest entry for {name} has no digest"
            if not entry.get("rows"):
                return f"the manifest says {name} has no rows"
        if not manifest.get("watermark"):
            return "the manifest carries no watermark"
        return None

    def clock_dt(self):
        """The DISPATCHER's clock as an aware datetime, for the shared validator.

        `self.disp.clock`, not a clock of the runner's own: the request was
        validated at submit time against that clock, and re-validating against a
        different one could disagree about the settlement boundary and reject a
        reply for a request the same process had accepted.
        """
        return datetime.datetime.fromtimestamp(self.disp.clock(),
                                               datetime.timezone.utc)

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
            extra_ro_mounts=(self._selftest_mounts(effective)
                             + self._probe_ro_mounts(effective)),
            extra_rw_mounts=self._probe_rw_mounts(effective, paths))

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
                       "error_class": None if exit_code == 0
                       else self._exit_class(exit_code)}

    # pytest's own exit codes for "you asked for the wrong thing". The
    # entrypoint IS pytest -- the runner builds `pytest -q <paths>` itself -- so
    # reading its exit table is making an existing coupling explicit, not adding
    # one.
    #
    # The distinction is a ROUTING decision, which is why it is worth the two
    # lines. `nonzero_exit` says "the experiment failed", and an auto-research
    # loop reading that will go looking at the code. 4 and 5 mean the JOB was
    # misconfigured -- a path that does not exist in the worktree, a `-k` that
    # selects nothing -- and the fix is the submission, not the repository. On a
    # loop that will make this mistake repeatedly, conflating them sends every
    # layout mistake off to debug an experiment that never ran.
    EXIT_CLASSES = {4: "bad_invocation", 5: "no_tests_collected"}

    def _exit_class(self, exit_code):
        return self.EXIT_CLASSES.get(exit_code, "nonzero_exit")

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

    def _probe_extract(self, effective):
        """Resolve, VALIDATE and return `(path, manifest)` for a probe's extract.

        RESOLVED from trusted config plus a validated request hash -- never from a
        path the spec supplied. `args.extract` is 64 hex characters and nothing
        else, so the only path this can produce is a direct child of the extracts
        directory.

        The extract MUST ALREADY EXIST. A probe that triggered an eleven-minute
        extraction would put a surprise inside a job somebody expected to be
        quick, and reuse already makes "extract once, probe often" cheap.

        AND THE MANIFEST MUST DESCRIBE THE EXTRACT WE ASKED FOR. An earlier
        version checked only that `MANIFEST.json` existed, which is the same
        mistake the relay made: a file saying nothing is not an extract, and a
        probe mounted against one would produce predictions whose provenance
        cannot be established -- which is the entire point of pinning it.
        """
        request_hash = effective["args"]["extract"]
        path = os.path.join(self.cfg.extracts_dir, request_hash)
        manifest_path = os.path.join(path, "MANIFEST.json")
        try:
            with open(manifest_path) as fh:
                manifest = json.load(fh)
        except FileNotFoundError:
            raise ProbeExtractMissing(
                f"no published extract {request_hash[:12]}: a probe reads an"
                f" extract that already exists. `qf extracts` lists what is"
                f" published; `qf extract` publishes one."
            ) from None
        except (OSError, ValueError) as e:
            raise ProbeExtractMissing(
                f"the manifest for {request_hash[:12]} is unreadable ({e}):"
                f" refusing rather than mounting an extract whose provenance"
                f" cannot be read"
            ) from None

        if manifest.get("request_hash") != request_hash:
            # The one field capable of pointing this run at somebody else's data.
            raise ProbeExtractMissing(
                f"the manifest in {path} says request_hash"
                f" {str(manifest.get('request_hash'))[:12]}, not"
                f" {request_hash[:12]}: the directory and its manifest disagree"
                f" about which extract this is")
        extract_hash = manifest.get("extract_hash")
        if not isinstance(extract_hash, str) or not HEX64_RE.match(
                extract_hash):
            raise ProbeExtractMissing(
                f"the manifest for {request_hash[:12]} carries no valid"
                f" extract_hash ({extract_hash!r}): that is the identity every"
                f" member of a comparison must share (design 4.6)")
        if not manifest.get("files"):
            raise ProbeExtractMissing(
                f"the manifest for {request_hash[:12]} lists no files")
        return path, manifest

    def _probe_baseline(self, effective):
        """Resolve, VALIDATE and return `(path, manifest)` for a probe's
        baseline, or `(None, None)` when the probe declared none.

        ABSENT IS LEGITIMATE. A non-residual cohort reads no baseline, so this is
        the one probe input with a "not asked for" case -- and it is distinct
        from "asked for and missing", which is a refusal. Collapsing them would
        make a typo in a hash silently produce a run with no comparison.

        Otherwise the same shape as `_probe_extract`, for the same reasons: the
        path is DERIVED from trusted config plus a 64-hex hash, so no spec can
        name a directory; and the manifest is READ AND CHECKED rather than merely
        found, because `MANIFEST.json` existing is not evidence that the
        directory holds the baseline whose hash this run is about to record.

        The hash is RECOMPUTED from the manifest body. It is a content key, so
        this is the one input whose declared identity can be verified rather than
        trusted -- and a promoted directory whose manifest does not hash to its
        own name is either corrupt or hand-edited. Skipping the check would mean
        the pinned `baseline_hash` proves only that somebody wrote it down.
        """
        baseline_hash = effective["args"].get("baseline")
        if baseline_hash is None:
            return None, None
        path = os.path.join(self.cfg.baselines_dir, baseline_hash)
        manifest_path = os.path.join(path, "MANIFEST.json")
        try:
            with open(manifest_path) as fh:
                manifest = json.load(fh)
        except FileNotFoundError:
            raise ProbeBaselineMissing(
                f"no promoted baseline {baseline_hash[:12]}: a probe reads a"
                f" baseline that already exists. `qf baselines` lists what is"
                f" promoted; promote-baseline.sh promotes one."
            ) from None
        except (OSError, ValueError) as e:
            raise ProbeBaselineMissing(
                f"the manifest for baseline {baseline_hash[:12]} is unreadable"
                f" ({e}): refusing rather than mounting a baseline whose"
                f" provenance cannot be read"
            ) from None

        if not isinstance(manifest, dict):
            raise ProbeBaselineMissing(
                f"the manifest for baseline {baseline_hash[:12]} is not an"
                f" object")
        if manifest.get("baseline_hash") != baseline_hash:
            raise ProbeBaselineMissing(
                f"the manifest in {path} says baseline_hash"
                f" {str(manifest.get('baseline_hash'))[:12]}, not"
                f" {baseline_hash[:12]}: the directory and its manifest disagree"
                f" about which baseline this is")
        try:
            recomputed = baseline_mod.baseline_hash(manifest)
        except Exception as e:                       # a malformed manifest body
            raise ProbeBaselineMissing(
                f"the manifest for baseline {baseline_hash[:12]} cannot be"
                f" hashed ({e}): its identity is a content key, so a body that"
                f" does not hash is not an identity") from None
        if recomputed != baseline_hash:
            raise ProbeBaselineMissing(
                f"baseline {baseline_hash[:12]} does not hash to its own name"
                f" (its body hashes to {recomputed[:12]}): the directory has"
                f" been edited since promotion, and a content key that does not"
                f" match its content records nothing")
        if not manifest.get("files"):
            raise ProbeBaselineMissing(
                f"the manifest for baseline {baseline_hash[:12]} lists no files")
        return path, manifest

    def _pin_probe_baseline(self, hold, effective):
        """Record WHICH BASELINE this probe saw -- or that it declared none.

        `baseline: none` is PINNED, not left absent. An absent pin is two
        different facts at once: this probe read no baseline, or this probe ran
        before baselines were pinned at all. A comparison across a version
        boundary cannot tell those apart, and the whole reason to record
        provenance is so a later reader does not have to guess.
        """
        path, manifest = self._probe_baseline(effective)
        now = utcnow()
        if manifest is None:
            self.db.call("set_pin", hold.run_id, "baseline", "none", now=now)
            return None
        for key, value in (
                ("baseline_hash", manifest.get("baseline_hash")),
                ("baseline_dir", path),
                ("baseline_days", str(len(manifest.get("days") or []))),
                ("baseline_pending_at_min", manifest.get("pending_at_min")),
                ("baseline_pending_at_max", manifest.get("pending_at_max")),
        ):
            if value is not None:
                self.db.call("set_pin", hold.run_id, key, str(value), now=now)
        log.info("%s: probing baseline %s (%d days) at %s", hold.run_id,
                 manifest["baseline_hash"][:12],
                 len(manifest.get("days") or []), path)
        return manifest

    def _pin_probe_extract(self, hold, effective):
        """Record WHICH DATA this probe saw, before any expensive setup.

        Early on purpose, twice over: validation happens before the worktree and
        the image build, so a bad reference costs seconds rather than minutes;
        and the pins exist even if the run fails later, so a failed probe still
        says what it was reading.

        Pins, never columns (design 4.6). Without these a probe's predictions
        have no provenance, and "every member of a comparison shares an
        extract_hash" becomes unverifiable -- which is the whole reason the
        identity is recorded rather than inferred.
        """
        path, manifest = self._probe_extract(effective)
        now = utcnow()
        for key, value in (
                ("request_hash", manifest.get("request_hash")),
                ("extract_hash", manifest.get("extract_hash")),
                ("extract_dir", path),
                ("extract_watermark",
                 json.dumps(manifest.get("watermark") or {}, sort_keys=True)),
        ):
            if value is not None:
                self.db.call("set_pin", hold.run_id, key, value, now=now)
        log.info("%s: probing extract %s (%s) at %s", hold.run_id,
                 manifest["extract_hash"][:12],
                 manifest.get("watermark"), path)
        return manifest

    def _probe_ro_mounts(self, effective):
        """The frozen extract and, when declared, the promoted baseline --
        read-only, at fixed paths."""
        if effective["kind"] != "probe":
            return ()
        path, _manifest = self._probe_extract(effective)
        mounts = [(path, sandbox_mod.EXTRACT_DEST)]
        baseline_path, _bm = self._probe_baseline(effective)
        if baseline_path is not None:
            mounts.append((baseline_path, sandbox_mod.BASELINE_DEST))
        return tuple(mounts)

    def _ensure_probe_mountpoint(self, paths):
        """Create the nested mount's directory IN THE BIND SOURCE, before launch.

        WHY THIS IS NEEDED AT ALL. `--read-only` makes the container's rootfs
        read-only, and a bind mount needs its mountpoint to exist. For a NESTED
        mount the parent is itself a read-only bind, so runc cannot create it
        either -- it fails with, verbatim:

            create mountpoint for /app/trainer/trainer/data mount:
            mkdirat ...: read-only file system

        `trainer/data` has no tracked files in `qf-research` (git does not track
        empty directories), so a fresh worktree does not contain it. The fix is to
        create it on the HOST side, where the worktree is writable, before the
        container that will see it read-only starts.

        THE PATH IS DERIVED FROM `DATA_DEST`, not written out a second time. Two
        literals for one location is how the mount and the directory come to
        disagree -- which is precisely the failure above, in its other half.
        """
        relative = sandbox_mod.DATA_DEST[len(sandbox_mod.SRC_DEST):].lstrip("/")
        target = os.path.join(paths["src"], relative)
        os.makedirs(target, exist_ok=True)
        return target

    def _probe_rw_mounts(self, effective, paths):
        """The run-private writable directory, nested inside the read-only tree.

        Only for a probe: a `test` job's pytest run has no business writing into
        `trainer/data`, and mounting it for every kind would hand every job a
        writable hole it did not ask for.
        """
        if effective["kind"] != "probe":
            return ()
        # The mountpoint must exist in the worktree before the container starts.
        self._ensure_probe_mountpoint(paths)
        return ((paths["data"], sandbox_mod.DATA_DEST),)

    def _pump(self, proc, out_w, err_w):
        def pump(stream, writer):
            # NEVER BREAK, however far past the cap this goes. The writer stops
            # WRITING at the cap (`write` returns 0 immediately once
            # `overflowed`), but this loop must keep READING to EOF.
            #
            # It used to `break` on overflow, and that turned a bounded log into
            # a wedged job: `docker start --attach` streams the container's
            # output into this pipe, so a full pipe with no reader blocks the
            # CLI in write() -- and then `proc.wait(timeout=budget)` cannot
            # return no matter how promptly the watcher kills the container.
            # NC15's log-flooding job sat there for its whole 1800s timeout and
            # was reported with a NULL error_class instead of `log_overflow`
            # within a sampling interval. The disk-flood twin passed throughout,
            # because it writes to /out and leaves its pipe drained.
            #
            # Killing a process does not help when what it is blocked on is a
            # pipe nobody is reading.
            for chunk in iter(lambda: stream.read(65536), b""):
                writer.write(chunk)

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

    # The prediction file's columns, frozen by design §4.6 and recorded HERE so
    # 2c's evaluator implements them rather than inventing them:
    #
    #   task_id   string  non-null      run_id   int32  non-null
    #   row_id    string  non-null      -- the canonical f"{task_id}:{run_id}"
    #   p50       double  non-null, finite
    #   p90_raw   double  non-null, finite
    #
    # Duplicated `row_id` is a REFUSAL, not a dedup: silently keeping one of two
    # is how a candidate would drop rows it scores badly on.
    #
    # NONE OF WHICH IS CHECKED HERE, and that is not an omission. `qfd` is
    # stdlib-only (D6) and reading Parquet needs `pyarrow`, so this side collects
    # the file and 2c's evaluator -- which has the dependency, and is where §8.5
    # puts scoring -- validates it. A "predictions-only contract" in 2b-2 is a
    # contract DECLARED, not a contract ENFORCED, and a reader should not have to
    # work that out.
    PREDICTION_COLUMNS = ("task_id", "run_id", "row_id", "p50", "p90_raw")

    @staticmethod
    def _artifact_allowlist(out_dir):
        """Only what trusted code NAMES, never a glob over what the candidate
        happened to write.

        2b-2 adds `predictions.parquet` -- one name, because the allowlist is the
        thing that keeps the artifacts directory from becoming a place where
        model files and core dumps accumulate.
        """
        candidates = ("result.json", "predictions.parquet")
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
                log.info("%s: %s exit_code=%s error_class=%s", run_id, state,
                         fields.get("exit_code"),
                         fields.get("error_class") or "-")
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
        self.sweep_worktrees()
        return decided

    # How many worktrees one sweep may reclaim. Bounded so a backlog is worked
    # off over several sweeps rather than making one of them long.
    WORKTREE_SWEEP_LIMIT = 50

    def sweep_worktrees(self):
        """Remove the `src/` worktree of every job that has reached a terminal
        state, whatever path took it there.

        `Runner.finish` already does this -- and for a long time it was the ONLY
        place that did, which meant the worktree was cleaned on the happy path
        and leaked on every other one: a lease reclaimed by this thread, a
        CLEANUP_BLOCKED job resolved by `resolve_blocked`, an operator
        force-release, a startup recovery that goes straight to FAILED. Those are
        precisely the paths the fault gates exercise, so a gate run left one full
        checkout of qf-research per hard kill, and `qf-runs-prune` does not touch
        them for ninety days. On a host whose admission floor is 20 GiB of the
        same filesystem, that is not housekeeping -- it is the loop stopping
        itself.

        A SWEEP rather than a call added to each terminal transition, for the
        reason the sentinel needed the same treatment: enumerating the ways a job
        can end is a list that goes stale, while "terminal and still has a
        worktree" is a condition. Driven from the filesystem, so a run directory
        whose job row is gone entirely is covered too.
        """
        try:
            entries = sorted(os.listdir(self.cfg.runs_dir))
        except OSError as e:
            log.warning("worktree sweep: cannot list %s: %s",
                        self.cfg.runs_dir, e)
            return 0
        swept = 0
        for run_id in entries:
            if swept >= self.WORKTREE_SWEEP_LIMIT:
                break
            src = os.path.join(self.cfg.runs_dir, run_id, "src")
            if not os.path.isdir(src):
                continue
            job = self.db.call("get", run_id)
            if job is not None and job["state"] not in store_mod.TERMINAL:
                continue
            try:
                self.runner.src.remove_worktree(src)
            except Exception:                          # noqa: BLE001
                log.exception("worktree sweep: %s", run_id)
                continue
            swept += 1
        if swept:
            log.info("worktree sweep: reclaimed %d worktree(s) from terminal"
                     " runs", swept)
        return swept

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
