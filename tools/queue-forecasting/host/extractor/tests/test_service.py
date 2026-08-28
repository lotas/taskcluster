"""Phase 2b-1 Task 4: the service, its startup gate, and the socket protocol.

Written before `service.py`. The startup gate is pure and the protocol runs over
a real unix socket in a temporary directory, so both are exercised here without
systemd, without a database and without privileges.

THE GATE IS THE POINT OF THIS TASK. D15's boundary -- `qfd` never holds the
database credential -- is a claim about a host, and a claim about a host that
nothing checks is a claim about a host somebody once configured correctly. So
every precondition is checked at start, fail-closed, and each problem names the
setting that fixes it. That is the same rule `Config.check_startup` follows in
the dispatcher, and the reason is the same: an invariant of the environment
belongs in the startup gate, not in the first request that trips over it.
"""
import datetime
import json
import os
import shutil
import re
import socket
import sys
import tempfile
import threading
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(_HERE)), "shared"))
# The fake session, the recording writer and the sample request are defined once,
# in the extractor's own tests, and reused rather than copied: two fakes that
# drift apart is how the watermark type bug survived 44 passing tests.
sys.path.insert(0, _HERE)

import extract_spec                                            # noqa: E402
import extractor                                               # noqa: E402
import service                                                 # noqa: E402

# `main()` registers these once the extractor modules are importable; the tests
# import them directly, so they register them too. Asserted separately by
# `test_the_safe_list_is_registered_by_main_not_guessed`, which runs against the
# module default rather than this.
_REAL_SAFE_ERRORS = (extract_spec.ExtractSpecError, extractor.ExtractError)

from test_extractor import (FakeSession, RecordingWriter, raw,   # noqa: E402
                            NOW)

UTC = datetime.timezone.utc


def a_config(tmp, **over):
    """A config that would pass the gate, so each test breaks exactly one thing."""
    dsn = os.path.join(tmp, "dsn")
    # ONLY IF ABSENT. This helper is called again by `problems()`, and a version
    # that rewrote and re-chmod'd the file undid the very permission each test
    # had just set -- five tests failing against correct code because the
    # fixture repaired what they broke.
    if not os.path.exists(dsn):
        with open(dsn, "w") as fh:
            fh.write("postgresql://forecast_experiment@127.0.0.1/forecasting\n")
        os.chmod(dsn, 0o600)
    root = os.path.join(tmp, "extracts")
    os.makedirs(root, exist_ok=True)
    env = {
        "QFX_EXTRACTS_DIR": root,
        "QFX_SOCKET": os.path.join(tmp, "sock"),
        "QFX_DSN_FILE": dsn,
        "QFX_CLIENT_UID": str(os.getuid() + 1),
        "QFX_SETTLEMENT_LAG_S": "172800",
    }
    env.update(over)
    return service.Config.from_env(env)


class GateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def problems(self, **over):
        cfg = a_config(self.tmp, **over)
        return cfg.check_startup(my_groups=[os.getgid()],
                                 group_name=lambda gid: f"g{gid}")


class TestAGoodHostPasses(GateCase):
    def test_no_problems_when_everything_is_in_place(self):
        # The POSITIVE canary for this whole class: if a correct configuration
        # cannot pass, every refusal below proves nothing about the refusals and
        # everything about the gate.
        self.assertEqual(self.problems(), [])


class TestTheCredentialIsOwnerOnly(GateCase):
    """The filesystem is what enforces D15, not a comment. If the DSN file has
    any group or other permission bit, someone other than the extractor can read
    it -- and the whole reason this is a separate privilege domain is that `qfd`
    cannot."""

    def test_a_group_readable_credential_is_refused(self):
        dsn = os.path.join(self.tmp, "dsn")
        a_config(self.tmp)
        os.chmod(dsn, 0o640)
        problems = self.problems()
        self.assertTrue(any("0640" in p or "group" in p.lower()
                            for p in problems), problems)

    def test_a_world_readable_credential_is_refused(self):
        a_config(self.tmp)
        os.chmod(os.path.join(self.tmp, "dsn"), 0o644)
        self.assertTrue(self.problems())

    def test_the_message_names_the_file_and_the_required_mode(self):
        a_config(self.tmp)
        os.chmod(os.path.join(self.tmp, "dsn"), 0o644)
        blob = " ".join(self.problems())
        self.assertIn("dsn", blob)
        self.assertIn("0600", blob)

    def test_a_missing_credential_is_refused(self):
        os.unlink(os.path.join(self.tmp, "dsn")) if os.path.exists(
            os.path.join(self.tmp, "dsn")) else None
        problems = self.problems(QFX_DSN_FILE=os.path.join(self.tmp, "nope"))
        self.assertTrue(any("nope" in p for p in problems), problems)

    def test_an_empty_credential_is_refused(self):
        a_config(self.tmp)
        with open(os.path.join(self.tmp, "dsn"), "w"):
            pass
        os.chmod(os.path.join(self.tmp, "dsn"), 0o600)
        self.assertTrue(any("empty" in p.lower() for p in self.problems()))

    def test_something_that_is_not_a_dsn_is_refused(self):
        # A path, a token, or a stray log line in the credential file would
        # otherwise surface as a connection error at the first request.
        a_config(self.tmp)
        with open(os.path.join(self.tmp, "dsn"), "w") as fh:
            fh.write("ghp_notadsn\n")
        os.chmod(os.path.join(self.tmp, "dsn"), 0o600)
        self.assertTrue(any("postgres" in p.lower() for p in self.problems()))

    def test_the_systemd_credentials_directory_wins_when_present(self):
        # `LoadCredential=` puts the credential in $CREDENTIALS_DIRECTORY at
        # 0400, owned by the service user. That is the production path, and it
        # must take precedence over any environment override.
        creds = os.path.join(self.tmp, "creds")
        os.makedirs(creds)
        with open(os.path.join(creds, "dsn"), "w") as fh:
            fh.write("postgresql://x@127.0.0.1/forecasting\n")
        os.chmod(os.path.join(creds, "dsn"), 0o400)
        cfg = a_config(self.tmp, CREDENTIALS_DIRECTORY=creds)
        self.assertEqual(cfg.dsn_file, os.path.join(creds, "dsn"))


class TestTheServiceRefusesPrivilegesItMustNotHave(GateCase):
    """The inverse of a normal permission check, and the most important clause
    in the gate: this service asserts that it has NOT been given membership of
    the groups that would let it run a container, hold the training mutex, or
    talk to the dispatcher's client socket.

    A future operator adding `SupplementaryGroups=docker` for convenience would
    silently make the extractor root-equivalent, which is exactly the property
    D15 exists to keep out of this domain."""

    def test_membership_of_docker_is_a_refusal(self):
        cfg = a_config(self.tmp)
        problems = cfg.check_startup(
            my_groups=[os.getgid(), 999],
            group_name=lambda gid: "docker" if gid == 999 else f"g{gid}")
        self.assertTrue(any("docker" in p for p in problems), problems)

    def test_membership_of_qfheavy_is_a_refusal(self):
        # It could otherwise hold the mutex and stop nightly training, with no
        # job involved and therefore no timeout to save it.
        cfg = a_config(self.tmp)
        problems = cfg.check_startup(
            my_groups=[os.getgid(), 998],
            group_name=lambda gid: "qfheavy" if gid == 998 else f"g{gid}")
        self.assertTrue(any("qfheavy" in p for p in problems), problems)

    def test_membership_of_qfclient_is_a_refusal(self):
        cfg = a_config(self.tmp)
        problems = cfg.check_startup(
            my_groups=[os.getgid(), 997],
            group_name=lambda gid: "qfclient" if gid == 997 else f"g{gid}")
        self.assertTrue(any("qfclient" in p for p in problems), problems)

    def test_the_refusal_names_the_unit_directive_that_causes_it(self):
        cfg = a_config(self.tmp)
        problems = cfg.check_startup(
            my_groups=[os.getgid(), 999],
            group_name=lambda gid: "docker" if gid == 999 else f"g{gid}")
        self.assertTrue(any("SupplementaryGroups" in p for p in problems),
                        problems)

    def test_the_forbidden_set_is_the_documented_three(self):
        self.assertEqual(set(service.FORBIDDEN_GROUPS),
                         {"docker", "qfheavy", "qfclient"})


class TestTheClientUidMustBeConfiguredAndNotOurs(GateCase):
    def test_a_missing_client_uid_is_refused(self):
        self.assertTrue(any("QFX_CLIENT_UID" in p
                            for p in self.problems(QFX_CLIENT_UID="")))

    def test_our_own_uid_is_refused(self):
        # A client uid equal to ours would make the peer check vacuous: the only
        # process it admits would be this service.
        problems = self.problems(QFX_CLIENT_UID=str(os.getuid()))
        self.assertTrue(any("own uid" in p.lower() for p in problems), problems)

    def test_a_non_numeric_client_uid_is_refused(self):
        self.assertTrue(self.problems(QFX_CLIENT_UID="qfd"))


class TestTheExtractsDirectoryMustBeUsable(GateCase):
    def test_a_missing_directory_is_refused(self):
        problems = self.problems(
            QFX_EXTRACTS_DIR=os.path.join(self.tmp, "absent"))
        self.assertTrue(any("absent" in p for p in problems), problems)

    def test_an_unwritable_directory_is_refused(self):
        if os.getuid() == 0:
            self.skipTest("root ignores the write bit, so this proves nothing")
        root = os.path.join(self.tmp, "ro")
        os.makedirs(root)
        os.chmod(root, 0o500)
        self.addCleanup(os.chmod, root, 0o700)
        problems = self.problems(QFX_EXTRACTS_DIR=root)
        self.assertTrue(any("writ" in p.lower() for p in problems), problems)


class TestTheSettlementLagIsSaneBeforeAnyRequest(GateCase):
    def test_a_negative_lag_is_refused(self):
        self.assertTrue(self.problems(QFX_SETTLEMENT_LAG_S="-1"))

    def test_a_non_numeric_lag_is_refused(self):
        self.assertTrue(self.problems(QFX_SETTLEMENT_LAG_S="two days"))

    def test_a_zero_lag_is_allowed_but_named_in_the_log(self):
        # Zero is legal -- it is an operational choice -- but it removes the
        # completed-boundary protection D20 relies on, so it must not be silent.
        cfg = a_config(self.tmp, QFX_SETTLEMENT_LAG_S="0")
        self.assertEqual(cfg.check_startup(my_groups=[os.getgid()],
                                          group_name=lambda g: f"g{g}"), [])
        self.assertTrue(cfg.warnings())


class ProtocolCase(unittest.TestCase):
    """One request per connection over a real unix socket."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = a_config(self.tmp, QFX_CLIENT_UID=str(os.getuid()))
        self.session = FakeSession()
        self.handler = service.Handler(
            self.cfg,
            extractor_factory=lambda: extractor.Extractor(
                root=self.cfg.extracts_dir,
                session_factory=lambda: self.session,
                writer=RecordingWriter(),
                free_disk_mb=lambda p: 100_000,
                clock=lambda: NOW,
                settlement_lag_s=self.cfg.settlement_lag_s),
        )
        self.server = service.Listener(self.cfg, self.handler)
        self.server.bind()
        self.addCleanup(self.server.stop)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def call(self, payload, *, timeout=30):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(self.cfg.socket_path)
            s.sendall(json.dumps(payload).encode() + b"\n")
            buf = bytearray()
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf.extend(chunk)
        return json.loads(bytes(buf).split(b"\n")[0]) if buf else None


class TestTheProtocolIsClosedWorldToo(ProtocolCase):
    def test_ping_answers(self):
        # A positive canary NC17 can use: proof the socket, the unit and the
        # peer check all work, without extracting anything.
        reply = self.call({"op": "ping"})
        self.assertTrue(reply["ok"])
        self.assertIn("settlement_lag_s", reply)

    def test_an_unknown_op_is_refused_by_name(self):
        reply = self.call({"op": "drop-everything"})
        self.assertFalse(reply["ok"])
        self.assertIn("drop-everything", reply["error"])

    def test_a_missing_op_is_refused(self):
        self.assertFalse(self.call({})["ok"])

    def test_bad_json_is_refused_rather_than_crashing_the_server(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(self.cfg.socket_path)
            s.sendall(b"{not json\n")
            reply = json.loads(s.recv(65536).decode().split("\n")[0])
        self.assertFalse(reply["ok"])
        # And the server is still up afterwards.
        self.assertTrue(self.call({"op": "ping"})["ok"])

    def test_an_oversized_request_is_refused(self):
        reply = self.call({"op": "extract",
                           "request": {"note": "x" * (70 * 1024)}})
        self.assertFalse(reply["ok"])

    def test_an_extract_returns_a_manifest(self):
        reply = self.call({"op": "extract", "request": raw()})
        self.assertTrue(reply["ok"], reply)
        self.assertIn("manifest", reply)
        self.assertIn("extract_hash", reply["manifest"])

    def test_an_invalid_request_comes_back_as_a_refusal_not_a_traceback(self):
        reply = self.call({"op": "extract", "request": raw(target="p90")})
        self.assertFalse(reply["ok"])
        self.assertIn("p90", reply["error"])

    def test_the_dsn_never_appears_in_a_reply(self):
        # D15's whole point, asserted on the wire. A traceback that echoed the
        # connection string would hand the credential to the one process that
        # must not have it.
        for payload in ({"op": "ping"},
                        {"op": "extract", "request": raw()},
                        {"op": "extract", "request": raw(target="p90")},
                        {"op": "nope"}):
            with self.subTest(payload=payload):
                blob = json.dumps(self.call(payload))
                self.assertNotIn("postgresql://", blob)
                self.assertNotIn("forecast_experiment", blob)


class TestOnlyTheConfiguredClientMayConnect(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_peer_with_another_uid_is_refused(self):
        # Our own uid is deliberately NOT the configured client, so this process
        # stands in for `research`: it can reach the socket and is refused on the
        # peer credential rather than on the directory mode.
        cfg = a_config(self.tmp, QFX_CLIENT_UID=str(os.getuid() + 1))
        handler = service.Handler(cfg, extractor_factory=lambda: None)
        server = service.Listener(cfg, handler)
        server.bind()
        self.addCleanup(server.stop)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(cfg.socket_path)
            s.sendall(b'{"op": "ping"}\n')
            data = s.recv(65536)
        reply = json.loads(data.decode().split("\n")[0])
        self.assertFalse(reply["ok"])
        self.assertIn("uid", reply["error"].lower())

    def test_the_check_is_on_the_credential_not_the_socket_mode(self):
        # The directory mode is a configuration; SO_PEERCRED is a program. Both
        # are used, and the test proves the second one works on its own -- the
        # socket in this test is reachable by anyone who can see the tmpdir.
        cfg = a_config(self.tmp, QFX_CLIENT_UID=str(os.getuid() + 1))
        self.assertTrue(service.Listener(cfg, None).enforces_peer_uid)


class TestTheUnitsAndTheCodeAgree(unittest.TestCase):
    """Nothing else checks that the shipped units name the same paths, the same
    user and the same environment variables the code reads. A unit that fails at
    every start does so quietly."""

    @staticmethod
    def directives(text):
        """The unit's DIRECTIVES, with comments removed.

        Third time in this phase that a static scan of mine has matched its own
        explanatory comment and failed against correct configuration. A scan over
        source has to decide what counts as code before it decides what counts as
        wrong, so that decision now lives in one place.
        """
        return "\n".join(l for l in text.splitlines()
                          if not l.lstrip().startswith("#"))

    def setUp(self):
        self.here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(self.here, "qf-extract.service")) as fh:
            self.unit = fh.read()
        with open(os.path.join(self.here, "qf-extract.socket")) as fh:
            self.sock = fh.read()
        self.unit_directives = self.directives(self.unit)
        self.sock_directives = self.directives(self.sock)

    def test_the_execstart_runs_the_venv_python_not_the_host_one(self):
        # `/usr/bin/python3` has neither pyarrow nor psycopg -- Phase 2a needs
        # neither, since qfd is stdlib-only -- so the unit shipped once with an
        # ExecStart that could not have started. The tests hid it by inserting
        # sys.path themselves.
        line = [l for l in self.unit_directives.splitlines()
                if l.startswith("ExecStart=")]
        self.assertEqual(len(line), 1, self.unit)
        interpreter, script = line[0].split("=", 1)[1].split()
        self.assertNotEqual(interpreter, "/usr/bin/python3")
        self.assertIn("env/.venv/bin/python", interpreter)
        self.assertTrue(os.path.isfile(
            os.path.join(self.here, os.path.basename(script))), script)

    def test_the_pythonpath_covers_the_shared_module(self):
        # `extract_spec` lives in host/shared and is imported by both domains.
        line = [l for l in self.unit_directives.splitlines()
                if l.startswith("Environment=PYTHONPATH=")]
        self.assertEqual(len(line), 1, self.unit)
        paths = line[0].split("=", 2)[2].split(":")
        self.assertTrue(any(p.endswith("/host/shared") for p in paths), paths)
        shared = os.path.join(os.path.dirname(self.here), "shared",
                              "extract_spec.py")
        self.assertTrue(os.path.isfile(shared), shared)

    def test_the_environment_manifest_exists_and_pins_both_packages(self):
        manifest = os.path.join(self.here, "env", "pyproject.toml")
        self.assertTrue(os.path.isfile(manifest), manifest)
        with open(manifest) as fh:
            text = fh.read()
        self.assertIn("pyarrow", text)
        self.assertIn("psycopg", text)

    def test_the_missing_lock_is_documented_rather_than_silent(self):
        # `uv` is unavailable in development so the lock cannot be generated
        # here. That is a gap, and a gap nobody wrote down is a gap nobody fixes.
        readme = os.path.join(self.here, "env", "README.md")
        self.assertTrue(os.path.isfile(readme))
        with open(readme) as fh:
            self.assertIn("uv.lock", fh.read())

    def test_the_service_runs_as_qfextract(self):
        self.assertIn("User=qfextract", self.unit_directives)

    def test_the_unit_grants_none_of_the_forbidden_groups(self):
        # The startup gate refuses at run time; this refuses at review time.
        # Checked against the DIRECTIVES: the unit names all three in a comment
        # explaining why they are absent, and a scan that cannot tell the
        # difference fails on the documentation.
        self.assertNotIn("SupplementaryGroups", self.unit_directives)
        for group in service.FORBIDDEN_GROUPS:
            with self.subTest(group=group):
                self.assertNotIn(group, self.unit_directives)

    def test_the_unit_explains_why_those_groups_are_absent(self):
        # The inverse, and worth asserting separately: an absence with no
        # rationale is an absence somebody will helpfully correct.
        for group in service.FORBIDDEN_GROUPS:
            with self.subTest(group=group):
                self.assertIn(group, self.unit)

    def test_the_credential_is_delivered_by_systemd(self):
        self.assertIn("LoadCredential=", self.unit_directives)

    def test_every_env_var_the_code_reads_is_set_by_the_unit(self):
        with open(os.path.join(self.here, "service.py")) as fh:
            source = fh.read()
        import re
        read = set(re.findall(r'"(QFX_[A-Z_]+)"', source))
        set_by_unit = set(re.findall(r"Environment=(QFX_[A-Z_]+)=", self.unit))
        # QFX_DSN_FILE is the development override; production uses
        # LoadCredential, so the unit deliberately does not set it.
        missing = read - set_by_unit - {"QFX_DSN_FILE"}
        self.assertEqual(missing, set(),
                         f"the code reads {sorted(missing)} and the unit never"
                         f" sets them, so the default is invisible to a reviewer")

    def test_the_socket_and_the_service_agree_on_the_path(self):
        listen = [l for l in self.sock.splitlines()
                  if l.startswith("ListenStream=")]
        self.assertEqual(len(listen), 1, self.sock)
        path = listen[0].split("=", 1)[1]
        self.assertIn(f"QFX_SOCKET={path}", self.unit_directives)

    def test_the_socket_is_not_reachable_by_other(self):
        # The property is that the LAST digit is 0. My first version of this test
        # demanded 0600 or 0640, which a correct socket cannot be: `qfd` reaches
        # it through its group, so group rw is required. The test was asserting a
        # mode that would have broken the only client it has.
        modes = re.findall(r"SocketMode=(\d{4})", self.sock_directives)
        self.assertEqual(len(modes), 1, self.sock)
        self.assertEqual(modes[0][-1], "0",
                         f"SocketMode={modes[0]} is reachable by other")
        self.assertNotEqual(modes[0][2], "0",
                            f"SocketMode={modes[0]} gives the group nothing, so"
                            f" SocketGroup= buys nothing either")

    def test_the_socket_is_owned_by_a_named_group(self):
        # Defence in depth beneath SO_PEERCRED: the mode keeps `research` from
        # even connecting, and the peer check keeps anything else out.
        self.assertRegex(self.sock_directives, r"SocketGroup=\S+")


if __name__ == "__main__":
    unittest.main()


class TestTheDatabaseHalfOfTheGate(unittest.TestCase):
    """`Config.check_startup` cannot see the database, so a host with a perfect
    credential file and an unreachable cluster passed the gate and failed at the
    first extraction. That is the shape Task 4 exists to prevent."""

    def test_a_healthy_database_produces_no_problems(self):
        self.assertEqual(service.probe_database(lambda: FakeSession()), [])

    def test_a_connection_failure_is_a_problem_without_the_dsn(self):
        def boom():
            raise RuntimeError(
                'connection failed: "postgresql://user:hunter2@host/db"')
        with self.assertLogs(service.log, level="ERROR"):
            problems = service.probe_database(boom)
        self.assertEqual(len(problems), 1)
        # The exception text quotes the conninfo. psycopg does this too, which is
        # exactly why the caller gets the class and the journal gets the detail.
        self.assertNotIn("hunter2", problems[0])
        self.assertNotIn("postgresql://", problems[0])
        self.assertIn("RuntimeError", problems[0])

    def test_a_non_zero_parallel_setting_is_a_problem_naming_the_migration(self):
        problems = service.probe_database(lambda: FakeSession(parallel="4"))
        self.assertEqual(len(problems), 1)
        self.assertIn("migrate-extractor-session.sql", problems[0])

    def test_a_writable_role_is_a_problem(self):
        problems = service.probe_database(
            lambda: FakeSession(write_refused_by=None))
        self.assertTrue(any("SUCCEEDED" in p for p in problems), problems)

    def test_an_unexpected_canary_reason_is_a_problem(self):
        problems = service.probe_database(
            lambda: FakeSession(write_refused_by="unexpected sqlstate 08006"))
        self.assertTrue(any("unexpected" in p for p in problems), problems)

    def test_the_session_is_closed_even_when_the_probe_finds_problems(self):
        session = FakeSession(parallel="4")
        service.probe_database(lambda: session)
        self.assertTrue(session.closed)

    def test_the_refusal_reasons_agree_with_the_extractors(self):
        # Duplicated rather than imported, because `service.py` must stay
        # importable with no extractor environment -- so a test keeps the two in
        # step instead of an import.
        self.assertEqual(set(service._WRITE_REFUSAL_REASONS),
                         set(extractor.WRITE_REFUSAL_REASONS))


class TestAnUnreadyServiceSaysSoRatherThanFailingLater(ProtocolCase):
    def setUp(self):
        super().setUp()
        self.unready = service.Handler(
            self.cfg, extractor_factory=lambda: None,
            db_problems=["max_parallel_workers_per_gather is '4' on the live"
                         " role, not 0"])

    def test_ping_reports_not_ready_and_why(self):
        # A `ping` that answers "ok" while nothing can be extracted is the
        # comfortable answer: NC17's canary would pass and every extraction
        # would fail.
        reply = self.unready.handle({"op": "ping"})
        self.assertTrue(reply["ok"])
        self.assertFalse(reply["ready"])
        self.assertTrue(reply["problems"])

    def test_a_healthy_ping_says_ready(self):
        reply = self.call({"op": "ping"})
        self.assertTrue(reply["ready"])
        self.assertEqual(reply["problems"], [])

    def test_extract_refuses_while_unready(self):
        reply = self.unready.handle({"op": "extract", "request": raw()})
        self.assertFalse(reply["ok"])
        self.assertIn("not ready", reply["error"])

    def test_it_refuses_before_touching_the_database(self):
        # `extractor_factory` returns None above, so any attempt to use it would
        # raise AttributeError rather than refuse.
        reply = self.unready.handle({"op": "extract", "request": raw()})
        self.assertIn("max_parallel", reply["error"])


class TestUnexpectedFailuresAreOpaqueOnTheWire(ProtocolCase):
    """The previous version returned `str(e)` for every exception, reasoning that
    nothing in this codebase puts a DSN in an exception. True of this codebase,
    and unenforceable about psycopg -- whose connection errors quote the
    conninfo, to the one process that must never see it."""

    def setUp(self):
        super().setUp()
        self._saved = service.SAFE_ERRORS
        service.SAFE_ERRORS = _REAL_SAFE_ERRORS
        self.addCleanup(setattr, service, "SAFE_ERRORS", self._saved)

    def _handler(self, exc):
        class Boom:
            def run(self, _request):
                raise exc
        return service.Handler(self.cfg, extractor_factory=lambda: Boom())

    def test_a_dependency_exception_does_not_reach_the_caller(self):
        handler = self._handler(RuntimeError(
            'connection to server at "db" failed:'
            ' password authentication failed for user "forecast_experiment"'
            ' (dsn: postgresql://forecast_experiment:hunter2@db/forecasting)'))
        with self.assertLogs(service.log, level="ERROR"):
            reply = handler.handle({"op": "extract", "request": raw()})
        self.assertFalse(reply["ok"])
        for secret in ("hunter2", "postgresql://", "forecast_experiment",
                       "password authentication"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, reply["error"])

    def test_the_caller_gets_a_reference_that_appears_in_the_log(self):
        handler = self._handler(RuntimeError("secret detail"))
        with self.assertLogs(service.log, level="ERROR") as captured:
            reply = handler.handle({"op": "extract", "request": raw()})
        import re as _re
        match = _re.search(r"Reference ([0-9a-f]{8})", reply["error"])
        self.assertIsNotNone(match, reply["error"])
        self.assertIn(match.group(1), "\n".join(captured.output))

    def test_the_reference_is_actionable(self):
        handler = self._handler(RuntimeError("x"))
        with self.assertLogs(service.log, level="ERROR"):
            reply = handler.handle({"op": "extract", "request": raw()})
        self.assertIn("journalctl", reply["error"])

    def test_our_own_refusals_keep_their_text(self):
        # The other half: a refusal we wrote is meant to be read, and turning it
        # opaque would make every legitimate refusal require journal access.
        for exc in (extractor.ExtractError("runs returned no rows"),
                    extract_spec.ExtractSpecError("unknown target 'p90'")):
            with self.subTest(exc=type(exc).__name__):
                handler = self._handler(exc)
                reply = handler.handle({"op": "extract", "request": raw()})
                self.assertFalse(reply["ok"])
                self.assertIn(str(exc), reply["error"])

    def test_the_safe_list_is_registered_by_main_not_guessed(self):
        # Against the MODULE DEFAULT, not the patched value: until `main()` runs,
        # `SAFE_ERRORS` must not include anything that would let a dependency's
        # text through.
        self.assertNotIn(Exception, self._saved)
        self.assertNotIn(BaseException, self._saved)
