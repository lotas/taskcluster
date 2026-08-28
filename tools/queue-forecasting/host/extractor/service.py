"""The extractor's service: a startup gate, a socket, one op. Task 4.

THIS FILE IS WHERE D15 STOPS BEING A DESIGN AND STARTS BEING A HOST.

The boundary -- `qfd` may request an extraction and never holds the database
credential -- is a claim about how a machine is configured. A claim about a
machine that nothing checks is a claim about a machine somebody once configured
correctly. So `Config.check_startup` refuses to start unless every precondition
holds, and each problem names the setting that fixes it, exactly as
`qfd`'s own gate does.

Two of its clauses are unusual and are the important ones:

  * **The credential must have no group or other permission bit at all.** Not
    "not readable by qfd" -- owner-only, which makes the question moot however
    the groups are arranged later.
  * **This service asserts it does NOT hold privileges.** Membership of
    `docker`, `qfheavy` or `qfclient` is a refusal to start. A future operator
    adding `SupplementaryGroups=docker` for convenience would make the extractor
    root-equivalent, which is the one property this domain exists to exclude.

Stdlib only. `psycopg` and `pyarrow` are reached through the extractor's seam and
are imported by `main()` alone, so the gate and the protocol are testable with
neither installed.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import socket
import struct
import sys
import threading
import traceback

log = logging.getLogger("qf-extract")

MAX_REQUEST_BYTES = 64 * 1024

# Membership of any of these would give the extractor a capability it has no use
# for and must not have: running a container (root-equivalent, D5), holding the
# training mutex (it could stop nightly training with no job involved and
# therefore no timeout to save it), or reaching the dispatcher's client socket.
FORBIDDEN_GROUPS = ("docker", "qfheavy", "qfclient")

_DSN_RE = re.compile(r"^postgres(ql)?://", re.I)

# Kept in step with `extractor.WRITE_REFUSAL_REASONS` by a test rather than by an
# import: `service.py` must stay importable with no extractor environment.
_WRITE_REFUSAL_REASONS = frozenset({"read_only", "insufficient_privilege"})

# Exceptions whose TEXT is safe to return: ones this codebase raises on purpose,
# with messages written to be read by the caller. Everything else is opaque.
# Populated by `main()` once the extractor modules are importable, so the module
# stays importable without them.
SAFE_ERRORS = (ValueError,)


class Config:
    def __init__(self, *, extracts_dir, socket_path, dsn_file, client_uid,
                 settlement_lag_s, floor_mb, temp_mb, output_mb,
                 credentials_dir=None):
        self.extracts_dir = extracts_dir
        self.socket_path = socket_path
        self._dsn_file = dsn_file
        self.credentials_dir = credentials_dir
        self.client_uid = client_uid
        self.settlement_lag_s = settlement_lag_s
        self.floor_mb = floor_mb
        self.temp_mb = temp_mb
        self.output_mb = output_mb

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env

        def num(name, default):
            raw = env.get(name, "")
            if raw == "":
                return default
            try:
                return int(raw)
            except ValueError:
                return raw            # kept, so the gate can name it

        return cls(
            extracts_dir=env.get("QFX_EXTRACTS_DIR", "/var/lib/qf-extracts"),
            socket_path=env.get("QFX_SOCKET", "/run/qf-extract/sock"),
            dsn_file=env.get("QFX_DSN_FILE", ""),
            credentials_dir=env.get("CREDENTIALS_DIRECTORY") or None,
            client_uid=num("QFX_CLIENT_UID", None),
            settlement_lag_s=num("QFX_SETTLEMENT_LAG_S", 48 * 3600),
            floor_mb=num("QFX_DISK_FLOOR_MB", 20 * 1024),
            temp_mb=num("QFX_PG_TEMP_MB", 20 * 1024),
            output_mb=num("QFX_OUTPUT_MB", 4 * 1024),
        )

    @property
    def dsn_file(self):
        """`$CREDENTIALS_DIRECTORY/dsn` wins when systemd provides it.

        `LoadCredential=` is the production path: systemd reads the source as
        root and places a 0400 copy owned by the service user, so the credential
        never passes through anything else. `QFX_DSN_FILE` exists for development
        and is deliberately not set by the unit -- if both were plausible in
        production, a reviewer could not tell which one was in force.
        """
        if self.credentials_dir:
            return os.path.join(self.credentials_dir, "dsn")
        return self._dsn_file

    def read_dsn(self):
        with open(self.dsn_file) as fh:
            return fh.read().strip()

    # --- the gate ---------------------------------------------------------
    def check_startup(self, *, my_groups=None, group_name=None, stat=os.stat):
        """Every precondition, fail-closed. Returns a list of problems.

        `my_groups` and `group_name` are injected so the group clauses can be
        tested without provisioning real groups.
        """
        problems = []
        my_groups = os.getgroups() if my_groups is None else my_groups
        if group_name is None:
            import grp

            def group_name(gid):
                try:
                    return grp.getgrgid(gid).gr_name
                except KeyError:
                    return str(gid)

        # 1. THE PRIVILEGES WE MUST NOT HAVE.
        held = set()
        for gid in my_groups:
            with contextlib.suppress(Exception):
                held.add(group_name(gid))
        for forbidden in FORBIDDEN_GROUPS:
            if forbidden in held:
                problems.append(
                    f"this service is a member of {forbidden!r} and must not be:"
                    f" remove it from SupplementaryGroups= in"
                    f" qf-extract.service. Membership of docker is"
                    f" root-equivalent; of qfheavy would let the extractor stop"
                    f" nightly training; of qfclient would let it reach the"
                    f" dispatcher's client socket. See D15."
                )

        # 2. THE CREDENTIAL.
        path = self.dsn_file
        if not path:
            problems.append(
                "no credential: set LoadCredential=dsn:<file> in"
                " qf-extract.service, or QFX_DSN_FILE for development")
        else:
            try:
                st = stat(path)
            except OSError as e:
                problems.append(f"cannot stat the credential {path}: {e}")
            else:
                problems.extend(self._check_credential_access(path, st))
                try:
                    dsn = self.read_dsn()
                except OSError as e:
                    problems.append(f"cannot read the credential {path}: {e}")
                else:
                    if not dsn:
                        problems.append(f"the credential {path} is empty")
                    elif not _DSN_RE.match(dsn):
                        problems.append(
                            f"the credential {path} does not look like a"
                            f" PostgreSQL DSN (expected postgresql://...);"
                            f" refusing rather than discovering this as a"
                            f" connection error at the first request")

        # 3. THE CLIENT.
        if not isinstance(self.client_uid, int):
            problems.append(
                f"QFX_CLIENT_UID is {self.client_uid!r}: set it to the"
                f" dispatcher's uid, which is the only uid allowed to connect")
        elif self.client_uid == os.getuid():
            problems.append(
                f"QFX_CLIENT_UID is our own uid ({self.client_uid}), which makes"
                f" the peer check vacuous -- the only process it would admit is"
                f" this one")

        # 4. THE OUTPUT DIRECTORY.
        if not os.path.isdir(self.extracts_dir):
            problems.append(
                f"{self.extracts_dir} does not exist: it is created by"
                f" StateDirectory=qf-extracts in the unit")
        elif not os.access(self.extracts_dir, os.W_OK | os.X_OK):
            problems.append(f"{self.extracts_dir} is not writable by this"
                            f" service (uid {os.getuid()})")

        # 5. THE NUMBERS.
        for name, value in (("QFX_SETTLEMENT_LAG_S", self.settlement_lag_s),
                            ("QFX_DISK_FLOOR_MB", self.floor_mb),
                            ("QFX_PG_TEMP_MB", self.temp_mb),
                            ("QFX_OUTPUT_MB", self.output_mb)):
            if not isinstance(value, int) or value < 0:
                problems.append(f"{name} is {value!r}: expected a"
                                f" non-negative integer")
        return problems

    @staticmethod
    def _check_credential_access(path, st, *, uid=None, gid=None):
        """Nothing outside this service may read the credential.

        THE RULE, AND WHY THE FIRST VERSION REFUSED A CORRECT HOST. It asserted
        "mode 0600 or stricter, owned by us", which is not what
        `LoadCredential=` produces: systemd mounts the credential directory as a
        root-owned ramfs and writes the file **0440 root:<service group>**, so
        the service reads it through its GROUP and root stays the owner. The gate
        therefore reported two precondition failures on a host that was
        configured exactly right, and the service crash-looped 15 times.

        A fail-closed check that fails on the good case is worse than no check:
        it blocks the working configuration and it teaches whoever is debugging
        it to loosen the gate. The mistake underneath was that the test built
        the credential as the TEST USER at 0600 -- the development path -- so the
        production arrangement was never exercised.

        What is actually required:

          * NO `other` bits. Anyone-can-read is the whole failure.
          * If group bits are set, the group must be OUR OWN primary group.
            `qfextract`'s group is created by `useradd --system` with exactly one
            member, so group-readable-to-our-group is readable by us alone. A
            credential group-readable to, say, `qfd` would pass a mode check and
            fail the point of D15 entirely.
          * The owner must be root (systemd's ramfs) or us. Nobody else.
        """
        uid = os.getuid() if uid is None else uid
        gid = os.getgid() if gid is None else gid
        mode = st.st_mode & 0o777
        problems = []
        if mode & 0o007:
            problems.append(
                f"the credential {path} is mode {mode:04o}: it has `other`"
                f" permission bits, so anyone on the host can read the DSN")
        if (mode & 0o070) and st.st_gid != gid:
            problems.append(
                f"the credential {path} is mode {mode:04o} and group-owned by"
                f" gid {st.st_gid}, which is not this service's group ({gid}):"
                f" some other group can read the DSN")
        if st.st_uid not in (0, uid):
            problems.append(
                f"the credential {path} is owned by uid {st.st_uid}, which is"
                f" neither root (systemd's credential store) nor this service"
                f" (uid {uid})")
        return problems

    def warnings(self):
        """Legal but worth saying out loud. Not failures -- an operator chose
        them -- but a choice nobody can see is a choice nobody reviews."""
        out = []
        if self.settlement_lag_s == 0:
            out.append(
                "QFX_SETTLEMENT_LAG_S is 0, so a window ending at the most"
                " recent midnight is extractable immediately. The collector runs"
                " a one-minute enrichment backfill, so such an extract will"
                " contain fewer late updates than one taken later -- which is"
                " legal under D20 (the extract is immutable and says so in its"
                " manifest) but is not what the default protects against.")
        return out


def probe_database(session_factory):
    """The DATABASE half of the startup gate. Returns a list of problems.

    The file half (`Config.check_startup`) cannot see any of this, so a host with
    a perfect credential file and an unreachable database passed the gate and
    failed at the first extraction -- which is precisely the shape Task 4 exists
    to prevent: an invariant of the environment belongs in the startup gate, not
    in the first request that trips over it.

    Takes a `session_factory` so it is exercised by the same fake the extractor's
    tests use, with no database present.
    """
    problems = []
    try:
        session = session_factory()
    except Exception as e:                                     # noqa: BLE001
        # The DSN may well be in this message -- psycopg quotes the conninfo --
        # so it goes to the journal and the caller is told only the class.
        log.error("cannot connect to the database: %s",
                  "".join(traceback.format_exception(e)).rstrip())
        return [f"cannot connect to the database ({type(e).__name__});"
                f" the detail is in the journal, not here, because a connection"
                f" error quotes the connection string"]
    try:
        parallel = session.setting("max_parallel_workers_per_gather")
        if str(parallel) != "0":
            problems.append(
                f"max_parallel_workers_per_gather is {parallel!r} on the live"
                f" role, not 0: apply"
                f" host/extractor/migrate-extractor-session.sql. Until then"
                f" temp_file_limit bounds roughly five times what it appears to,"
                f" because it is enforced per process and parallel workers are"
                f" separate processes (D23)")
        reason = session.attempt_write()
        if reason is None:
            problems.append(
                "a write SUCCEEDED as the extraction role: neither the"
                " SELECT-only grant nor default_transaction_read_only is in"
                " force on the live cluster")
        elif reason not in _WRITE_REFUSAL_REASONS:
            problems.append(
                f"the write canary failed for an unexpected reason ({reason});"
                f" refusing to read that as proof the role cannot write")
    except Exception as e:                                     # noqa: BLE001
        log.error("the database probe failed: %s",
                  "".join(traceback.format_exception(e)).rstrip())
        problems.append(f"the database probe failed ({type(e).__name__});"
                        f" see the journal")
    finally:
        with contextlib.suppress(Exception):
            session.close()
    return problems


def peer_uid(conn):
    """The connecting process's uid, from the kernel.

    SO_PEERCRED, not a name the caller supplies. The socket's mode is a
    configuration and this is a program; both are used, and only this one is
    unforgeable.
    """
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                            struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


class Handler:
    """The ops. Two, and the second is the only one that does anything."""

    def __init__(self, cfg, *, extractor_factory, db_problems=()):
        self.cfg = cfg
        self.extractor_factory = extractor_factory
        # The result of the STARTUP database probe. Kept rather than re-run per
        # request: a probe on every request would put a connection and a write
        # attempt in front of every `ping`, and `ping` exists to be cheap.
        self.db_problems = list(db_problems)

    @property
    def ready(self):
        return not self.db_problems

    @property
    def not_ready_because(self):
        return ("the extractor is not ready: "
                + "; ".join(self.db_problems)
                + ". Fixed on the database or in the unit, not by retrying.")

    def handle(self, request):
        op = request.get("op")
        if op == "ping":
            # READINESS IS PART OF THE ANSWER. A `ping` that says "ok" while the
            # credential, the connectivity or the role configuration is unusable
            # is the comfortable answer this project keeps having to remove: the
            # NC17 canary would pass and every extraction would fail.
            return {"ok": True,
                    "ready": self.ready,
                    "problems": list(self.db_problems),
                    "settlement_lag_s": self.cfg.settlement_lag_s,
                    "extracts_dir": self.cfg.extracts_dir}
        if op == "extract":
            return self._extract(request.get("request"))
        return {"ok": False,
                "error": f"unknown op {op!r}; known: ping, extract"}

    def _extract(self, raw_request):
        if not isinstance(raw_request, dict):
            return {"ok": False, "error": "request must be an object"}
        if not self.ready:
            return {"ok": False, "error": self.not_ready_because}
        try:
            manifest = self.extractor_factory().run(raw_request)
        except SAFE_ERRORS as e:
            # OUR OWN refusals, whose text we wrote and which are meant to be
            # read by the caller.
            log.warning("extract refused: %s: %s", type(e).__name__, e)
            return {"ok": False, "error": f"{e}"}
        except Exception as e:                                  # noqa: BLE001
            # ANYTHING ELSE IS OPAQUE ON THE WIRE.
            #
            # The previous version returned `str(e)` for every exception, on the
            # reasoning that nothing in this codebase puts a DSN in an exception.
            # That is true of this codebase and unenforceable about its
            # dependencies: psycopg's connection errors quote the conninfo, and
            # the one process that must never see the DSN is the one on the other
            # end of this socket. An assertion about every library's future error
            # text is not a control.
            #
            # The full detail goes to the journal, where only root and the
            # operator can read it, and the caller gets an id to quote.
            ref = secrets.token_hex(4)
            log.error("extract failed [ref %s]: %s", ref,
                      "".join(traceback.format_exception(e)).rstrip())
            return {"ok": False,
                    "error": f"the extractor failed unexpectedly."
                             f" Reference {ref}; the detail is in"
                             f" `journalctl -u qf-extract -g {ref}`."}
        return {"ok": True, "manifest": manifest}


class Listener:
    """One request per connection, thread per connection.

    Threads rather than a serial loop so `ping` answers while a long extraction
    runs; serialisation of the extractions themselves is the extractor's flock,
    which is where it belongs (D23).
    """

    enforces_peer_uid = True

    def __init__(self, cfg, handler):
        self.cfg = cfg
        self.handler = handler
        self.sock = None
        self._stop = threading.Event()
        self._inherited = False

    def bind(self):
        """Use systemd's socket if it gave us one, otherwise create our own.

        Socket activation means the socket exists whether or not this service is
        running, so a client never has to ask "is it up yet" -- the same problem
        2a solved with `wait_ready`, removed at the source instead.
        """
        fds = int(os.environ.get("LISTEN_FDS", "0") or 0)
        pid = os.environ.get("LISTEN_PID")
        if fds > 0 and (pid is None or pid == str(os.getpid())):
            self.sock = socket.socket(fileno=3)
            self.sock.setblocking(True)
            self._inherited = True
            log.info("listening on the socket systemd passed us (fd 3)")
        else:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.cfg.socket_path)
            os.makedirs(os.path.dirname(self.cfg.socket_path), exist_ok=True)
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.bind(self.cfg.socket_path)
            os.chmod(self.cfg.socket_path, 0o660)
            self.sock.listen(8)
            log.info("listening on %s", self.cfg.socket_path)
        self.sock.settimeout(0.5)
        return self

    def serve_forever(self):
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
            # No read timeout that could interrupt an extraction: the reply is
            # sent after the work finishes, and the work legitimately takes
            # minutes. The REQUEST read is bounded by size instead of by time.
            uid = peer_uid(conn)
            if uid != self.cfg.client_uid and uid != 0:
                log.warning("refused a connection from uid %s", uid)
                self._reply(conn, {
                    "ok": False,
                    "error": f"uid {uid} may not request an extraction; only the"
                             f" dispatcher (uid {self.cfg.client_uid}) may"})
                return
            line = self._read_line(conn)
            if line is None:
                self._reply(conn, {
                    "ok": False,
                    "error": f"no request, or larger than"
                             f" {MAX_REQUEST_BYTES} bytes"})
                return
            try:
                request = json.loads(line)
            except ValueError as e:
                self._reply(conn, {"ok": False, "error": f"bad JSON: {e}"})
                return
            if not isinstance(request, dict):
                self._reply(conn, {"ok": False,
                                   "error": "a request must be a JSON object"})
                return
            self._reply(conn, self.handler.handle(request))
        except Exception:                                      # noqa: BLE001
            log.exception("connection handler failed")
        finally:
            with contextlib.suppress(OSError):
                conn.close()

    @staticmethod
    def _read_line(conn):
        """Bounded. An unbounded read is a trivial memory denial of service, and
        the cap is checked against the LINE rather than against how much has
        arrived, because one recv can overshoot and carry the newline with it."""
        buf = bytearray()
        while True:
            nl = buf.find(b"\n")
            if nl != -1:
                return None if nl > MAX_REQUEST_BYTES else bytes(buf[:nl])
            if len(buf) > MAX_REQUEST_BYTES:
                return None
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                return None
            if not chunk:
                return None
            buf.extend(chunk)

    @staticmethod
    def _reply(conn, obj):
        with contextlib.suppress(OSError):
            conn.sendall(json.dumps(obj).encode() + b"\n")

    def stop(self):
        self._stop.set()
        with contextlib.suppress(Exception):
            self.sock.close()
        # Only if we made it. Unlinking a socket systemd owns would leave the
        # unit socket-activated on a path that no longer exists.
        if not self._inherited:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.cfg.socket_path)


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s")
    # umask 022 so the extract's files are readable by the container that will
    # mount them. The data is Taskcluster task metadata, not a secret; the
    # credential is the secret, and it is 0400 in a different directory.
    os.umask(0o022)

    cfg = Config.from_env()
    problems = cfg.check_startup()
    for warning in cfg.warnings():
        log.warning("%s", warning)
    for problem in problems:
        log.error("startup precondition: %s", problem)

    # SERVE EVEN WHEN UNREADY, AND THIS IS THE DELIBERATE PART.
    #
    # The first version returned 2 here. With socket activation that is a hang:
    # systemd accepts the client's connection, starts this service, the service
    # exits, and the client blocks on `recv` for ever while `Restart=on-failure`
    # loops. Observed on the host at restart counter 15, with the operator
    # having to Ctrl-C a `ping`.
    #
    # "Refuses to start" and "fail-closed" are not the same thing. Nothing can be
    # extracted either way -- every op refuses while `problems` is non-empty --
    # but this way the client is TOLD, in the reply, which is the difference
    # between a fixable message and a hang. It is the same lesson as 2a's "the
    # dispatcher closed the connection without replying".
    #
    # The cost is that `systemctl status` reads green on a misconfigured host.
    # The journal carries ERROR lines and `ping` reports `ready: false` with the
    # reasons, which is where an operator who is debugging this will actually be
    # looking.
    if problems:
        log.error("NOT READY: %d precondition(s) unmet; serving refusals so the"
                  " caller is told rather than left waiting", len(problems))
        listener = Listener(cfg, Handler(cfg, extractor_factory=None,
                                         db_problems=problems)).bind()
        try:
            listener.serve_forever()
        finally:
            listener.stop()
        return 0

    # Imported HERE and nowhere above, so the gate and the protocol stay
    # testable on a machine with neither installed.
    import extract_spec
    import extractor as extractor_mod
    import parquet_writer
    import pg

    dsn = cfg.read_dsn()

    def factory():
        return extractor_mod.Extractor(
            root=cfg.extracts_dir,
            session_factory=pg.session_factory(dsn),
            writer=parquet_writer.ParquetWriter(
                types_for=_types_for_columns),
            free_disk_mb=_free_disk_mb,
            clock=_utcnow,
            settlement_lag_s=cfg.settlement_lag_s,
            floor_mb=cfg.floor_mb, temp_mb=cfg.temp_mb,
            output_mb=cfg.output_mb)

    # The refusal types whose messages are meant for the caller. Registered
    # here, where the modules are importable, so the wire never carries a
    # dependency's exception text.
    global SAFE_ERRORS
    SAFE_ERRORS = (extract_spec.ExtractSpecError, extractor_mod.ExtractError)

    db_problems = probe_database(pg.session_factory(dsn))
    for problem in db_problems:
        log.error("startup precondition (database): %s", problem)

    listener = Listener(cfg, Handler(cfg, extractor_factory=factory,
                                     db_problems=db_problems)).bind()
    log.info("listening: extracts=%s client_uid=%s settlement_lag=%ss ready=%s",
             cfg.extracts_dir, cfg.client_uid, cfg.settlement_lag_s,
             not db_problems)
    try:
        listener.serve_forever()
    finally:
        listener.stop()
    return 0


def _types_for_columns(columns):
    """Find the dataset whose column list this is, and return its declared types.

    Matched on the column tuple rather than passed by name because the writer's
    seam is `open(path, columns)` -- keeping the name out of it means the writer
    cannot be handed a schema for a different dataset than the one it is writing.
    """
    import inventory
    wanted = tuple(columns)
    for dataset in inventory.DATASETS.values():
        if dataset.columns == wanted:
            return dataset.types
    raise ValueError(f"no dataset declares the columns {wanted}")


def _free_disk_mb(path):
    st = os.statvfs(path)
    return (st.f_bavail * st.f_frsize) // (1024 * 1024)


def _utcnow():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc)


if __name__ == "__main__":
    sys.exit(main())
