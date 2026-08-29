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
import inventory                                               # noqa: E402
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


class TestTheCredentialIsReadableOnlyByThisService(GateCase):
    """The rule is "nothing outside this service can read it", NOT "mode 0600
    owned by us" -- which is what the first version asserted, and which
    `LoadCredential=` does not produce.

    systemd mounts the credential directory as a root-owned ramfs and writes the
    file **0440 root:<service group>**: the service reads it through its group
    and root stays the owner. The gate reported two precondition failures on a
    correctly configured host and the service crash-looped fifteen times.

    The mistake underneath was in the FIXTURE: it created the credential as the
    test user at 0600, which is the development path, so the production
    arrangement was never exercised. Fourth time this phase that a fake has been
    shaped more conveniently than the real thing.
    """

    def access(self, mode, uid, gid, *, from_systemd, my_uid=997, my_gid=986):
        st = os.stat_result((mode, 0, 0, 1, uid, gid, 0, 0, 0, 0))
        return service.Config._check_credential_access(
            "/run/credentials/qf-extract.service/dsn", st,
            from_systemd=from_systemd, uid=my_uid, gid=my_gid)

    def test_what_systemd_actually_produces_passes(self):
        # THE OBSERVED SHAPE, from the host: 0440 root:root, gid 0, readable by
        # uid 997 anyway. Two earlier versions of this check refused it -- first
        # by demanding 0600-owned-by-us, then by demanding our own group.
        #
        # A uid-997 process reading a 0440 root:root file is not something the
        # mode and owner explain; the likely mechanism is an ACL. So for a
        # systemd credential the DAC bits are not the access control, and this
        # test exists to stop the assertion being reinvented a third time.
        self.assertEqual(self.access(0o440, 0, 0, from_systemd=True), [])
        self.assertEqual(self.access(0o400, 0, 0, from_systemd=True), [])
        self.assertEqual(self.access(0o440, 0, 986, from_systemd=True), [])

    def test_other_bits_are_refused_even_from_systemd(self):
        # The one assertion that survives every systemd version: whatever the
        # ACL says, world-readable is world-readable.
        for mode in (0o444, 0o644, 0o777):
            with self.subTest(mode=oct(mode)):
                problems = self.access(mode, 0, 0, from_systemd=True)
                self.assertTrue(any("other" in p for p in problems), problems)

    def test_the_development_path_keeps_the_strict_rule(self):
        # No systemd, so the DAC bits ARE the control.
        self.assertEqual(self.access(0o600, 997, 986, from_systemd=False), [])
        self.assertEqual(self.access(0o400, 0, 0, from_systemd=False), [])

    def test_a_development_credential_readable_by_another_group_is_refused(self):
        # 0640 root:qfd would pass "no other bits" and hand the DSN to the one
        # process D15 excludes.
        problems = self.access(0o640, 0, 999, from_systemd=False)
        self.assertTrue(any("group" in p for p in problems), problems)

    def test_a_development_credential_with_a_stranger_owner_is_refused(self):
        problems = self.access(0o400, 1234, 986, from_systemd=False)
        self.assertTrue(any("owned by uid 1234" in p for p in problems),
                        problems)

    def test_the_source_file_is_the_setup_scripts_job_not_ours(self):
        # We cannot see /etc/qf-extract/dsn from inside the service --
        # ProtectSystem=strict, and it is 0600 root:root. The split is
        # deliberate: the script checks the source, the gate checks what arrived.
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(os.path.dirname(here),
                               "phase2b-setup.sh")) as fh:
            setup = fh.read()
        self.assertIn("root:root", setup)

    def test_a_missing_credential_is_refused(self):
        problems = self.problems(QFX_DSN_FILE=os.path.join(self.tmp, "nope"))
        self.assertTrue(any("nope" in p for p in problems), problems)

    def test_an_empty_credential_is_refused(self):
        a_config(self.tmp)
        with open(os.path.join(self.tmp, "dsn"), "w"):
            pass
        os.chmod(os.path.join(self.tmp, "dsn"), 0o600)
        self.assertTrue(any("empty" in p.lower() for p in self.problems()))

    def test_something_that_is_not_a_dsn_is_refused(self):
        a_config(self.tmp)
        with open(os.path.join(self.tmp, "dsn"), "w") as fh:
            fh.write("ghp_notadsn\n")
        os.chmod(os.path.join(self.tmp, "dsn"), 0o600)
        self.assertTrue(any("postgres" in p.lower() for p in self.problems()))

    def test_the_whole_gate_passes_with_a_systemd_shaped_credential(self):
        # THE INTEGRATION FORM, not just the helper. Every round of this bug was
        # a fixture that built the credential the convenient way, so the gate's
        # real path never met the real shape. This test drives `check_startup`
        # end to end with the mode/owner/group the host actually reports.
        creds = os.path.join(self.tmp, "creds")
        os.makedirs(creds)
        with open(os.path.join(creds, "dsn"), "w") as fh:
            fh.write("postgresql://forecast_experiment@127.0.0.1/forecasting\n")
        cfg = a_config(self.tmp, CREDENTIALS_DIRECTORY=creds)

        # 0440 root:root, as observed, without needing to be root to set it.
        real = os.stat(os.path.join(creds, "dsn"))
        faked = os.stat_result((0o100440, real.st_ino, real.st_dev, 1, 0, 0,
                                real.st_size, 0, 0, 0))

        problems = cfg.check_startup(
            my_groups=[os.getgid()], group_name=lambda gid: f"g{gid}",
            stat=lambda p: faked)
        self.assertEqual(problems, [], problems)

    def test_the_systemd_credentials_directory_wins_when_present(self):
        creds = os.path.join(self.tmp, "creds")
        os.makedirs(creds)
        with open(os.path.join(creds, "dsn"), "w") as fh:
            fh.write("postgresql://x@127.0.0.1/forecasting\n")
        os.chmod(os.path.join(creds, "dsn"), 0o440)
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


class TestAnUnreadyServiceAnswersInsteadOfHanging(unittest.TestCase):
    """With socket activation, exiting non-zero at startup is a HANG: systemd
    accepts the client's connection, starts the service, the service exits, and
    the client blocks on `recv` for ever while `Restart=on-failure` loops.
    Observed on the host at restart counter 15, with a `ping` that had to be
    interrupted by hand.

    "Refuses to start" and "fail-closed" are not the same thing. Nothing can be
    extracted either way; this way the caller is TOLD."""

    def test_main_serves_rather_than_returning_nonzero(self):
        # Asserted on the source, because running `main()` would block. The
        # behavioural half is covered by the Handler tests: every op refuses
        # while `db_problems` is non-empty.
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "service.py")) as fh:
            source = fh.read()
        body = source[source.index("def main("):]
        code = "\n".join(l for l in body.splitlines()
                         if not l.lstrip().startswith("#"))
        self.assertNotIn("return 2", code,
                         "main() exits non-zero on an unmet precondition, which"
                         " with socket activation is a hang for the client")
        self.assertIn("serving refusals", body)

    def test_the_unready_path_still_refuses_every_op(self):
        cfg_problems = ["the credential is unreadable"]
        handler = service.Handler(None, extractor_factory=None,
                                  db_problems=cfg_problems)
        self.assertFalse(handler.ready)
        reply = handler.handle({"op": "extract", "request": {}})
        self.assertFalse(reply["ok"])
        self.assertIn("unreadable", reply["error"])


class TestListingPublishedExtracts(ProtocolCase):
    """`qf extracts` needs hashes and watermarks, and the answer comes from HERE
    because this directory belongs to this service. Having the dispatcher walk it
    would put the layout in two places, and the publication path has already been
    bitten once by a second thing that could disagree with the artifacts."""

    def test_an_empty_directory_lists_nothing(self):
        reply = self.call({"op": "extracts"})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["extracts"], [])

    def test_a_published_extract_appears_with_its_identity(self):
        made = self.call({"op": "extract", "request": raw()})["manifest"]
        listed = self.call({"op": "extracts"})["extracts"]
        self.assertEqual(len(listed), 1)
        row = listed[0]
        self.assertEqual(row["request_hash"], made["request_hash"])
        self.assertEqual(row["extract_hash"], made["extract_hash"])
        self.assertEqual(row["target"], "wait_time")
        self.assertEqual(row["watermark"], made["watermark"])
        self.assertTrue(row["dir"].endswith(made["request_hash"]))

    def test_row_counts_are_reported_per_file(self):
        self.call({"op": "extract", "request": raw()})
        rows = self.call({"op": "extracts"})["extracts"][0]["rows"]
        self.assertEqual(set(rows), set(inventory.DATASETS))
        for name, count in rows.items():
            with self.subTest(name=name):
                self.assertGreater(count, 0)

    def test_a_directory_without_a_manifest_is_skipped_not_reported(self):
        # Only a complete artifact counts -- the same rule `published_dir` uses.
        # Reporting a bare directory would advertise an extract with no files.
        os.makedirs(os.path.join(self.cfg.extracts_dir, "f" * 64))
        self.assertEqual(self.call({"op": "extracts"})["extracts"], [])

    def test_an_unreadable_manifest_is_skipped(self):
        made = self.call({"op": "extract", "request": raw()})["manifest"]
        path = os.path.join(self.cfg.extracts_dir, made["request_hash"],
                            "MANIFEST.json")
        with open(path, "w") as fh:
            fh.write("{not json")
        self.assertEqual(self.call({"op": "extracts"})["extracts"], [])

    def test_two_generations_both_appear(self):
        first = self.call({"op": "extract", "request": raw()})["manifest"]
        second = self.call({"op": "extract",
                            "request": raw(generation=2)})["manifest"]
        listed = {row["request_hash"]
                  for row in self.call({"op": "extracts"})["extracts"]}
        self.assertEqual(listed, {first["request_hash"],
                                  second["request_hash"]})

    def test_the_dsn_does_not_appear_in_a_listing(self):
        self.call({"op": "extract", "request": raw()})
        blob = json.dumps(self.call({"op": "extracts"}))
        self.assertNotIn("postgresql://", blob)
        self.assertNotIn("forecast_experiment", blob)

if __name__ == "__main__":
    # AT THE END. This guard had drifted into the middle of the file as classes
    # were appended below it, so running the file directly executed only the
    # classes above it and reported OK. `discover` imports the whole module, so
    # the suite was green and the gap invisible.
    unittest.main()
