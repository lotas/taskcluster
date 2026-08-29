"""The trusted evaluator's service. Phase 2c Task 19.

THE NARROWEST DOMAIN IN THE SYSTEM. `qfd` is in `docker`, which is
root-equivalent (D5). `qfextract` holds the only database credential (D15). This
holds neither, and needs neither: its entire authority is "read two immutable
stores and one staged file, write one directory". It has no network, no
credential, no docker, and no membership of any group that grants anything.

"ROOT-OWNED EVALUATOR" MEANS ROOT-OWNED CODE, UNITS, CONTRACTS AND POLICY -- not
a process running as root. A judge running as root would be the most privileged
process in the loop, which is the wrong shape for the one component whose whole
job is to be trustworthy about somebody else's output. The separation that
matters is from the DEPLOYMENT domain, which produces the baselines: a judge
inside the domain that produces what it judges against is not independent, and no
amount of care inside it makes it so.

WHY IT DOES NOT READ THE RUN DIRECTORY (D28). The base run directory is
`0750 qfd:qfclient`, so traversal needs `qfclient` -- and `qfclient` is the group
that lets `research` read logs and artifacts, so joining it to reach one file
would hand this domain the client surface as well. A directory carries one group,
and that one is taken. So `qfd` STAGES the untrusted input into
`<eval_dir>/<run_id>/in/`, and the immutable, world-readable, content-hashed
stores are read in place.

`main()` SERVES REFUSALS RATHER THAN EXITING, which 2b-1 learned the hard way:
under socket activation, exiting non-zero is not a failure a client sees, it is a
hang plus a restart counter climbing to 15 while somebody holds Ctrl-C.
"""
from __future__ import annotations

import contextlib
import grp
import json
import logging
import os
import socket
import struct
import sys
import threading

import request as request_mod

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "shared"))
import contract as contract_mod                                # noqa: E402

log = logging.getLogger("qf-eval")

MAX_REQUEST_BYTES = 64 * 1024

# Not `docker`, not `qfheavy`, not `qfclient`, and the third is the one this
# domain adds: `qfclient` is what would have let it read a run directory, which
# is exactly the access D28 exists to avoid needing. A group whose absence is the
# reason for a design decision has to be refused, or the decision decays into a
# comment.
FORBIDDEN_GROUPS = ("docker", "qfheavy", "qfclient", "qfrun", "qfextract")

# Only these reach a client verbatim. Anything else becomes an opaque reference
# into the journal: an exception's text is written by whatever library raised it,
# and a dependency's future error prose is not a control.
SAFE_ERRORS = (ValueError,)


class Config:
    def __init__(self, *, eval_dir, extracts_dir, baselines_dir, contracts_dir,
                 socket_path, client_uid):
        self.eval_dir = eval_dir
        self.extracts_dir = extracts_dir
        self.baselines_dir = baselines_dir
        self.contracts_dir = contracts_dir
        self.socket_path = socket_path
        self.client_uid = client_uid

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
            eval_dir=env.get("QFE_EVAL_DIR", "/var/lib/qf-eval"),
            extracts_dir=env.get("QFE_EXTRACTS_DIR", "/var/lib/qf-extracts"),
            baselines_dir=env.get("QFE_BASELINES_DIR", "/var/lib/qf-baselines"),
            contracts_dir=env.get("QFE_CONTRACTS_DIR", ""),
            socket_path=env.get("QFE_SOCKET", "/run/qf-eval/sock"),
            client_uid=num("QFE_CLIENT_UID", None),
        )

    # --- the gate ---------------------------------------------------------
    def check_startup(self, *, my_groups=None, access=os.access):
        """Every precondition, fail-closed. Returns a list of problems.

        `my_groups` and `access` are injected so the clauses can be tested
        without provisioning groups or running as the service user.
        """
        problems = []

        # 1. GROUP MEMBERSHIP. Named with the directive that grants it, because
        # the remedy is a unit edit and an error that does not say so sends
        # somebody to `usermod`.
        if my_groups is None:
            my_groups = set()
            with contextlib.suppress(OSError):
                my_groups = {grp.getgrgid(g).gr_name
                             for g in os.getgroups()
                             if _group_name_exists(g)}
        for forbidden in FORBIDDEN_GROUPS:
            if forbidden in my_groups:
                problems.append(
                    f"this service is in the {forbidden!r} group. The evaluator"
                    f" holds no privilege by design: remove it from"
                    f" SupplementaryGroups= in qf-eval.service."
                    + (f" {forbidden!r} would also let it read run directories"
                       f" directly, which is the access design D28 exists to"
                       f" avoid needing." if forbidden == "qfclient" else ""))

        # 2. THE CLIENT. A peer check against our own uid admits exactly one
        # process -- this one -- and reads as though it admits the dispatcher.
        if not isinstance(self.client_uid, int):
            problems.append(
                f"QFE_CLIENT_UID is {self.client_uid!r}: set it to the"
                f" dispatcher's uid, which is the only uid allowed to connect")
        elif self.client_uid == os.getuid():
            problems.append(
                f"QFE_CLIENT_UID is our own uid ({self.client_uid}), which makes"
                f" the peer check vacuous -- the only process it would admit is"
                f" this one")
        elif self.client_uid == 0:
            problems.append(
                "QFE_CLIENT_UID is 0. root can reach this socket regardless, so"
                " setting it here does not add access -- it REPLACES the"
                " dispatcher's uid with one that tells us nothing, and every"
                " request would then be attributed to root in the log")

        # 3. THE INPUTS ARE READABLE AND THE OUTPUT IS WRITABLE, checked
        # separately so "cannot evaluate" and "cannot publish" are different
        # messages.
        for label, path in (("extracts", self.extracts_dir),
                            ("baselines", self.baselines_dir),
                            ("contracts", self.contracts_dir)):
            if not path:
                problems.append(
                    f"the {label} directory is unset (QFE_{label.upper()}_DIR)")
            elif not os.path.isdir(path):
                problems.append(f"the {label} directory {path} does not exist")
            elif not access(path, os.R_OK | os.X_OK):
                problems.append(
                    f"the {label} directory {path} is not readable by this"
                    f" service (uid {os.getuid()})")
            elif access(path, os.W_OK):
                # NOT a warning. An evaluator that can write to the store it
                # judges against can change what a recorded comparison was
                # measured against, and the only reason it would not is that
                # nobody chose to.
                problems.append(
                    f"the {label} directory {path} is WRITABLE by this service."
                    f" The evaluator must not be able to alter an input it"
                    f" judges by; check the store's mode and this unit's"
                    f" ReadWritePaths=.")

        if not os.path.isdir(self.eval_dir):
            problems.append(
                f"{self.eval_dir} does not exist: it is created by"
                f" StateDirectory=qf-eval in the unit")
        elif not access(self.eval_dir, os.W_OK | os.X_OK):
            problems.append(f"{self.eval_dir} is not writable by this service"
                            f" (uid {os.getuid()})")

        # 4. NO CREDENTIAL, ASSERTED. This domain has no database credential and
        # is never given one, so a credential appearing in its environment means
        # a unit has been edited into something this code was not reviewed for.
        for name in ("DATABASE_URL", "QFX_DSN_FILE", "PGPASSWORD"):
            if os.environ.get(name):
                problems.append(
                    f"{name} is set in this service's environment. The"
                    f" evaluator reads files and nothing else; a credential"
                    f" here means the unit grants more than this code expects.")
        if os.environ.get("CREDENTIALS_DIRECTORY"):
            problems.append(
                "CREDENTIALS_DIRECTORY is set: qf-eval.service must carry no"
                " LoadCredential=")
        return problems


def _group_name_exists(gid):
    try:
        grp.getgrgid(gid)
    except KeyError:
        return False
    return True


def peer_uid(conn):
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                          struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


class Handler:
    """One request in, one reply dict out. Never raises to the socket layer."""

    def __init__(self, cfg, *, evaluate=None):
        self.cfg = cfg
        # Injected: 2c-2 supplies the real one. `None` means "the domain is up
        # but cannot evaluate yet", which is answered as a refusal rather than
        # by pretending -- a service that returned a plausible empty verdict
        # would be the worst possible stub.
        self._evaluate = evaluate

    # --- ops -------------------------------------------------------------
    def handle(self, raw):
        op = raw.get("op") if isinstance(raw, dict) else None
        if op == "ping":
            return {"ok": True, **self.ping()}
        if op == "evaluate":
            return self.evaluate(raw)
        return {"ok": False,
                "error": f"unknown op {op!r}; known: ping, evaluate"}

    def ping(self):
        """Readiness, and the three facts that decide whether a request could
        succeed: the contracts it can resolve, and that the stores are readable
        and not writable. `qf-extract`'s `ping` reports whether the credential is
        reachable for the same reason -- a channel that answers but cannot do its
        job looks identical to a healthy one from outside."""
        contracts = self.available_contracts()
        return {
            "uid": os.getuid(),
            "contracts": sorted(contracts),
            "stores": {
                "extracts": self._store_state(self.cfg.extracts_dir),
                "baselines": self._store_state(self.cfg.baselines_dir),
            },
            "can_evaluate": self._evaluate is not None,
        }

    def _store_state(self, path):
        if not os.path.isdir(path):
            return "missing"
        if not os.access(path, os.R_OK | os.X_OK):
            return "unreadable"
        if os.access(path, os.W_OK):
            # Reported, not hidden: this is a startup refusal, so seeing it here
            # means the mode changed under a running service.
            return "WRITABLE"
        return "read-only"

    def available_contracts(self):
        """`{contract_hash: name}` for every contract that VALIDATES.

        Resolved by hash, so the caller names a rule and cannot supply one. A
        file that does not validate, or whose declared hash disagrees with its
        body, is omitted from the map and logged -- so a job citing it is refused
        for "unknown contract", and the journal says which file was wrong.
        """
        out = {}
        try:
            names = sorted(os.listdir(self.cfg.contracts_dir))
        except OSError as e:
            log.error("cannot list the contracts directory: %s", e)
            return out
        for name in names:
            if not name.endswith(".json"):
                # `.json.in` templates are deliberately not contracts: they
                # carry an unpinned baseline and the validator refuses them.
                continue
            path = os.path.join(self.cfg.contracts_dir, name)
            try:
                _body, digest = contract_mod.load(path)
            except contract_mod.ContractError as e:
                log.error("ignoring contract %s: %s", name, e)
                continue
            if digest in out:
                log.error("two contract files hash to %s: %s and %s",
                          digest[:12], out[digest], name)
                continue
            out[digest] = name
        return out

    def evaluate(self, raw):
        try:
            req = request_mod.validate(raw)
        except request_mod.RequestError as e:
            return {"ok": False, "error": str(e),
                    "error_class": "bad_request"}

        contracts = self.available_contracts()
        if req["contract"] not in contracts:
            # NC9. The refusal lists what IS available, because "unknown
            # contract" with no list is unactionable -- and the list is hashes
            # with names, which is what the operator needs to correct the job.
            return {"ok": False,
                    "error": f"no contract {req['contract'][:12]} in the trusted"
                             f" checkout. Available: "
                             + (", ".join(f"{h[:12]} ({n})"
                                          for h, n in sorted(contracts.items()))
                                or "none"),
                    "error_class": "contract_not_trusted"}

        if self._evaluate is None:
            return {"ok": False,
                    "error": "this evaluator can resolve contracts but cannot"
                             " evaluate yet (2c-2 supplies the implementation)",
                    "error_class": "not_implemented"}
        try:
            return {"ok": True, **self._evaluate(self.cfg, req,
                                                 contracts[req["contract"]])}
        except SAFE_ERRORS as e:
            return {"ok": False, "error": str(e), "error_class": "refused"}
        except Exception:                                      # noqa: BLE001
            # An opaque reference, never the exception's own words: a
            # dependency's future error prose is not a control, and this reply
            # crosses a trust boundary in the direction that matters.
            ref = os.urandom(4).hex()
            log.exception("evaluate failed [%s]", ref)
            return {"ok": False,
                    "error": f"the evaluator failed; journal reference {ref}",
                    "error_class": "internal"}


class Listener:
    """One request per connection, thread per connection."""

    enforces_peer_uid = True

    def __init__(self, cfg, handler):
        self.cfg = cfg
        self.handler = handler
        self.sock = None
        self._stop = threading.Event()

    def bind(self):
        fds = int(os.environ.get("LISTEN_FDS", "0") or 0)
        pid = os.environ.get("LISTEN_PID")
        if fds > 0 and (pid is None or pid == str(os.getpid())):
            self.sock = socket.socket(fileno=3)
            self.sock.setblocking(True)
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

    def stop(self):
        self._stop.set()

    def _serve(self, conn):
        try:
            uid = peer_uid(conn)
            # root is admitted alongside the dispatcher, and that is honesty
            # rather than latitude: root can reach any socket and can become any
            # user, so a check that refused root would be one root bypasses by
            # running `sudo -u qfd`. What it must never admit is `research`.
            if uid not in (self.cfg.client_uid, 0):
                log.warning("refused a connection from uid %s", uid)
                self._reply(conn, {
                    "ok": False,
                    "error": f"uid {uid} may not request an evaluation; only the"
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
                parsed = json.loads(line)
            except ValueError as e:
                self._reply(conn, {"ok": False, "error": f"bad JSON: {e}"})
                return
            if not isinstance(parsed, dict):
                self._reply(conn, {"ok": False,
                                   "error": "a request must be a JSON object"})
                return
            self._reply(conn, self.handler.handle(parsed))
        except Exception:                                      # noqa: BLE001
            log.exception("connection handler failed")
            with contextlib.suppress(Exception):
                self._reply(conn, {"ok": False, "error": "internal error"})
        finally:
            with contextlib.suppress(Exception):
                conn.close()

    def _read_line(self, conn):
        buf = bytearray()
        while b"\n" not in buf:
            if len(buf) > MAX_REQUEST_BYTES:
                return None
            try:
                chunk = conn.recv(65536)
            except OSError:
                return None
            if not chunk:
                # A half-close is a complete request: the client may shut down
                # its write side instead of sending a newline.
                break
            buf.extend(chunk)
        if not buf:
            return None
        return bytes(buf).split(b"\n")[0]

    def _reply(self, conn, payload):
        with contextlib.suppress(OSError):
            conn.sendall(json.dumps(payload).encode() + b"\n")


def main(argv=None):
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    problems = cfg.check_startup()
    handler = Handler(cfg)
    if problems:
        for problem in problems:
            log.error("startup: %s", problem)
        # SERVE THE REFUSAL. Exiting here is a HANG under socket activation:
        # systemd accepts the connection, starts us, we exit, the client is
        # holding an open socket with nothing on the other end, and the restart
        # counter climbs. 2b-1 discovered this at 15 restarts with somebody
        # holding Ctrl-C. A refusal that names the problem is the only reply
        # that helps.
        detail = "; ".join(problems)
        handler = _RefusingHandler(detail)
    Listener(cfg, handler).bind().serve_forever()
    return 0


class _RefusingHandler:
    """Answers everything with the same startup refusal, including `ping` --
    which is the point: `ping` is what an operator runs, so it has to be the
    thing that tells them."""

    def __init__(self, detail):
        self.detail = detail

    def handle(self, raw):
        return {"ok": False, "error": f"qf-eval refuses to serve: {self.detail}",
                "error_class": "startup_refused"}


if __name__ == "__main__":
    sys.exit(main())
