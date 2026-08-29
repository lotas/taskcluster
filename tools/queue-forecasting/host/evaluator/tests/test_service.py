"""Phase 2c Task 19. The `qfeval` domain: the startup gate, the peer check, the
contract resolution that NC9 rests on, and the units that grant its authority."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

EVALUATOR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST = os.path.dirname(EVALUATOR)
sys.path.insert(0, EVALUATOR)
sys.path.insert(0, os.path.join(HOST, "shared"))

import contract as contract_mod
import service
# THE TOKENISING SCAN, not the line-based helper this file used to carry. That
# helper strips `#` lines and structurally cannot see a docstring, which is how
# a scan came to match its own prose for the ninth time in this programme.
# `srcscan.code_only` blanks comments AND string literals, preserving line
# offsets -- and returns a file that does not tokenise unchanged rather than
# empty, because an empty result makes every assertion built on it pass.
from srcscan import code_only


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.eval_dir = os.path.join(root, "qf-eval")
        self.extracts = os.path.join(root, "qf-extracts")
        self.baselines = os.path.join(root, "qf-baselines")
        self.contracts = os.path.join(root, "contracts")
        for path in (self.eval_dir, self.extracts, self.baselines,
                     self.contracts):
            os.makedirs(path)
        self.cfg = service.Config(
            eval_dir=self.eval_dir, extracts_dir=self.extracts,
            baselines_dir=self.baselines, contracts_dir=self.contracts,
            socket_path=os.path.join(root, "sock"), client_uid=os.getuid() + 1)

    def write_contract(self, name="wait_time.v1.json", **over):
        body = {
            "schema": 1, "name": "wait_time_v1", "target": "wait_time",
            "baseline_hash": "a" * 64,
            "primary_slice": {"reason_resolved": ["completed"]},
            "metrics": {"mae": {"direction": "lower_is_better",
                                "bar": {"kind": "relative_improvement",
                                        "value": 0.15}}},
            "consistency": {"days_required": 3}, "holdout_days": 5,
        }
        body.update(over)
        body = contract_mod.validate(body)
        digest = contract_mod.contract_hash(body)
        body["contract_hash"] = digest
        with open(os.path.join(self.contracts, name), "w") as fh:
            json.dump(body, fh)
        return digest

    # The gate reads the real filesystem for writability; in a tmpdir owned by
    # the test user every directory is writable, so `access` is injected to
    # describe production rather than the test environment.
    def gate(self, *, writable=(), **kw):
        writable = set(writable) | {self.eval_dir}

        def access(path, mode):
            if mode & os.W_OK:
                return path in writable
            return True
        kw.setdefault("my_groups", set())
        return self.cfg.check_startup(access=access, **kw)


class TestTheStartupGate(ServiceCase):
    def test_a_correct_configuration_has_no_problems(self):
        # The canary. Without it every refusal below could be passing because
        # the gate refuses everything.
        self.assertEqual(self.gate(), [])

    def test_every_forbidden_group_is_refused_naming_the_directive(self):
        for group in ("docker", "qfheavy", "qfclient", "qfrun", "qfextract"):
            with self.subTest(group=group):
                problems = self.gate(my_groups={group})
                self.assertTrue(any(group in p for p in problems), problems)
                self.assertTrue(any("SupplementaryGroups=" in p
                                    for p in problems), problems)

    def test_qfclient_membership_says_why_it_matters_here(self):
        # It is the group that would let the evaluator read run directories
        # directly, which is the access D28 exists to avoid needing. A refusal
        # that did not say so reads like boilerplate.
        problems = self.gate(my_groups={"qfclient"})
        self.assertTrue(any("D28" in p for p in problems), problems)

    def test_a_writable_input_store_is_a_refusal_not_a_warning(self):
        # An evaluator that can write to the store it judges against can change
        # what a recorded comparison was measured against.
        for store in (self.extracts, self.baselines, self.contracts):
            with self.subTest(store=store):
                problems = self.gate(writable={store})
                self.assertTrue(any("WRITABLE" in p for p in problems),
                                problems)

    def test_a_missing_store_is_named_individually(self):
        for label, path in (("extracts", self.extracts),
                            ("baselines", self.baselines),
                            ("contracts", self.contracts)):
            with self.subTest(label=label):
                os.rename(path, path + ".moved")
                self.addCleanup(lambda p=path: os.path.exists(p + ".moved")
                                and os.rename(p + ".moved", p))
                problems = self.gate()
                self.assertTrue(any(label in p and "does not exist" in p
                                    for p in problems), problems)
                os.rename(path + ".moved", path)

    def test_an_unset_contracts_directory_is_refused(self):
        self.cfg.contracts_dir = ""
        self.assertTrue(any("QFE_CONTRACTS_DIR" in p for p in self.gate()))

    def test_a_peer_check_against_our_own_uid_is_refused_as_vacuous(self):
        self.cfg.client_uid = os.getuid()
        problems = self.gate()
        self.assertTrue(any("vacuous" in p for p in problems), problems)

    def test_a_peer_check_against_root_is_refused(self):
        # root reaches this socket regardless, so setting it here does not add
        # access -- it REPLACES the dispatcher's uid with one that says nothing.
        self.cfg.client_uid = 0
        problems = self.gate()
        self.assertTrue(any("QFE_CLIENT_UID is 0" in p for p in problems),
                        problems)

    def test_an_unset_client_uid_is_refused(self):
        self.cfg.client_uid = None
        self.assertTrue(any("QFE_CLIENT_UID" in p for p in self.gate()))

    def test_a_credential_in_the_environment_is_refused(self):
        for name in ("DATABASE_URL", "QFX_DSN_FILE", "PGPASSWORD",
                     "CREDENTIALS_DIRECTORY"):
            with self.subTest(name=name):
                with mock.patch.dict(os.environ, {name: "x"}):
                    problems = self.gate()
                self.assertTrue(any(name in p for p in problems), problems)


class TestPingAnswersWhatAnOperatorNeeds(ServiceCase):
    def test_it_reports_the_contracts_it_can_resolve(self):
        digest = self.write_contract()
        reply = service.Handler(self.cfg).handle({"op": "ping"})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["contracts"], [digest])

    def test_it_reports_the_store_states(self):
        reply = service.Handler(self.cfg).handle({"op": "ping"})
        # In a tmpdir the stores really are writable, and `ping` says so rather
        # than reporting what the design intends. A diagnostic that reports the
        # intent is the failure mode this whole programme keeps removing.
        self.assertIn(reply["stores"]["extracts"],
                      ("read-only", "WRITABLE", "missing", "unreadable"))

    def test_it_says_whether_it_can_evaluate_at_all(self):
        # 2c-1 ships the domain without the implementation. Saying so is the
        # difference between a stub and a service that returns a plausible
        # empty verdict.
        self.assertFalse(service.Handler(self.cfg).handle({"op": "ping"})
                         ["can_evaluate"])
        with_impl = service.Handler(self.cfg, evaluate=lambda *a: {})
        self.assertTrue(with_impl.handle({"op": "ping"})["can_evaluate"])

    def test_an_unknown_op_is_refused_by_name_and_lists_what_exists(self):
        reply = service.Handler(self.cfg).handle({"op": "extract"})
        self.assertFalse(reply["ok"])
        self.assertIn("ping", reply["error"])
        self.assertIn("evaluate", reply["error"])


class TestContractResolutionIsTheControlNc9Asserts(ServiceCase):
    def test_a_contract_not_in_the_checkout_is_refused_with_its_class(self):
        self.write_contract()
        reply = service.Handler(self.cfg).handle({
            "op": "evaluate",
            "run_id": "probe-20260829T123756Z-9d54e39271d7-1",
            "contract": "f" * 64, "request_hash": "e" * 64,
            "predictions_sha256": "d" * 64})
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error_class"], "contract_not_trusted")

    def test_the_refusal_lists_what_is_available(self):
        # "unknown contract" with no list is unactionable.
        digest = self.write_contract()
        reply = service.Handler(self.cfg).handle({
            "op": "evaluate",
            "run_id": "probe-20260829T123756Z-9d54e39271d7-1",
            "contract": "f" * 64, "request_hash": "e" * 64,
            "predictions_sha256": "d" * 64})
        self.assertIn(digest[:12], reply["error"])
        self.assertIn("wait_time.v1.json", reply["error"])

    def test_a_valid_contract_gets_past_the_gate(self):
        # THE CANARY for the two clauses above: a resolver that refused
        # everything would pass both of them.
        digest = self.write_contract()
        reply = service.Handler(self.cfg).handle({
            "op": "evaluate",
            "run_id": "probe-20260829T123756Z-9d54e39271d7-1",
            "contract": digest, "request_hash": "e" * 64,
            "predictions_sha256": "d" * 64})
        self.assertFalse(reply["ok"])
        # Refused for the RIGHT reason: this `Handler` was built with no
        # implementation injected. `main` injects `evaluate.evaluate`; a handler
        # without one answers a refusal rather than a plausible empty verdict,
        # which would be the worst possible stub.
        self.assertEqual(reply["error_class"], "not_implemented")

    def test_a_template_is_not_a_contract(self):
        # `.json.in` carries an unpinned baseline; the validator refuses it and
        # the resolver must not offer it.
        with open(os.path.join(self.contracts, "x.v1.json.in"), "w") as fh:
            json.dump({"schema": 1, "baseline_hash": "@BASELINE_HASH@"}, fh)
        self.assertEqual(service.Handler(self.cfg).available_contracts(), {})

    def test_a_contract_that_does_not_validate_is_omitted_not_offered(self):
        with open(os.path.join(self.contracts, "broken.json"), "w") as fh:
            fh.write("{not json")
        good = self.write_contract()
        self.assertEqual(list(service.Handler(self.cfg)
                              .available_contracts()), [good])

    def test_a_contract_edited_since_it_was_written_is_omitted(self):
        digest = self.write_contract()
        path = os.path.join(self.contracts, "wait_time.v1.json")
        with open(path) as fh:
            body = json.load(fh)
        body["metrics"]["mae"]["bar"]["value"] = 0.01   # leaves the hash alone
        with open(path, "w") as fh:
            json.dump(body, fh)
        self.assertNotIn(digest,
                         service.Handler(self.cfg).available_contracts())

    def test_two_files_hashing_to_one_contract_do_not_shadow_each_other(self):
        digest = self.write_contract("a.json")
        self.write_contract("b.json")
        resolved = service.Handler(self.cfg).available_contracts()
        # One entry, and which file won is logged rather than silently chosen.
        self.assertEqual(list(resolved), [digest])

    def test_a_malformed_request_is_refused_before_any_contract_lookup(self):
        reply = service.Handler(self.cfg).handle({"op": "evaluate"})
        self.assertEqual(reply["error_class"], "bad_request")


class TestThePeerCheck(ServiceCase):
    def _round_trip(self, listener):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5)
        client.connect(self.cfg.socket_path)
        client.sendall(json.dumps({"op": "ping"}).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = client.recv(65536)
            if not chunk:
                break
            buf += chunk
        client.close()
        return json.loads(buf.split(b"\n")[0])

    def test_a_peer_that_is_not_the_dispatcher_is_refused(self):
        # Our own uid is NOT client_uid (setUp sets it to uid+1), so this
        # connection is the refused case -- exercised for real over a socket
        # rather than by calling the check.
        listener = service.Listener(self.cfg, service.Handler(self.cfg)).bind()
        thread = threading.Thread(target=listener.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(listener.stop)
        reply = self._round_trip(listener)
        self.assertFalse(reply["ok"])
        self.assertIn("may not request an evaluation", reply["error"])

    def test_the_dispatcher_is_admitted(self):
        # THE CANARY: without it, the refusal above is satisfied by a listener
        # that refuses everyone.
        self.cfg.client_uid = os.getuid()
        listener = service.Listener(self.cfg, service.Handler(self.cfg)).bind()
        thread = threading.Thread(target=listener.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(listener.stop)
        self.assertTrue(self._round_trip(listener)["ok"])

    def test_the_listener_declares_that_it_enforces_the_peer_uid(self):
        # Read by the NC suite, the same way it reads the extractor's.
        self.assertTrue(service.Listener.enforces_peer_uid)


class TestStartupRefusalsAreServedNotExited(unittest.TestCase):
    """Under socket activation, exiting non-zero is not a failure a client sees
    -- it is a hang plus a restart counter. 2b-1 found this at 15 restarts."""

    def test_main_does_not_exit_when_the_gate_fails(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "service.py")) as fh:
            body = fh.read()
        main = code_only(body[body.index("def main(argv=None):"):])
        main = main[:main.index("\nclass ")]
        self.assertNotIn("sys.exit", main)
        self.assertNotIn("raise SystemExit", main)
        self.assertIn("_RefusingHandler", main)

    def test_the_refusing_handler_answers_ping_too(self):
        # `ping` is what an operator runs, so it has to be the thing that tells
        # them. A refusing handler that answered `ping` normally would report a
        # healthy service.
        handler = service._RefusingHandler("in the docker group")
        for op in ("ping", "evaluate", "nonsense"):
            with self.subTest(op=op):
                reply = handler.handle({"op": op})
                self.assertFalse(reply["ok"])
                self.assertIn("docker", reply["error"])
                self.assertEqual(reply["error_class"], "startup_refused")


class TestTheUnitsGrantOnlyWhatTheCodeExpects(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(cls.here, "qf-eval.service")) as fh:
            cls.svc = fh.read()
        with open(os.path.join(cls.here, "qf-eval.socket")) as fh:
            cls.sock = fh.read()

    def test_the_socket_group_is_the_dispatchers_so_qfd_can_connect(self):
        # An earlier plan draft said 0600 root:qfeval, which would have produced
        # a channel the only permitted client could not open: 0660 grants the
        # group read/write, 0600 grants it nothing, and qfd is not root.
        self.assertIn("SocketGroup=qfd", self.sock)
        self.assertIn("SocketMode=0660", self.sock)
        self.assertNotIn("SocketMode=0600", code_only(self.sock))

    def test_the_runtime_directory_is_traversable(self):
        self.assertIn("RuntimeDirectoryMode=0755", self.sock)

    def test_the_service_holds_no_supplementary_groups(self):
        self.assertNotIn("SupplementaryGroups=", code_only(self.svc))

    def test_the_service_loads_no_credential(self):
        self.assertNotIn("LoadCredential=", code_only(self.svc))

    def test_the_only_writable_path_is_the_evaluator_output(self):
        rw = [line.split("=", 1)[1].split()
              for line in self.svc.splitlines()
              if line.startswith("ReadWritePaths=")]
        self.assertEqual(rw, [["/var/lib/qf-eval"]])

    def test_neither_store_nor_the_contracts_are_writable(self):
        writable = {p for group in
                    [line.split("=", 1)[1].split()
                     for line in self.svc.splitlines()
                     if line.startswith("ReadWritePaths=")] for p in group}
        for path in ("/var/lib/qf-extracts", "/var/lib/qf-baselines"):
            self.assertNotIn(path, writable)
            for granted in writable:
                self.assertFalse(path.startswith(granted.rstrip("/") + "/"))
        self.assertTrue(any("contracts" in line
                            and line.startswith("Environment=QFE_CONTRACTS_DIR")
                            for line in self.svc.splitlines()))

    def test_the_network_is_gone_which_the_extractor_cannot_have(self):
        self.assertIn("PrivateNetwork=yes", self.svc)

    def test_the_hardening_the_extractor_has_is_here_too(self):
        for directive in ("ProtectSystem=strict", "ProtectHome=yes",
                          "NoNewPrivileges=yes", "RestrictSUIDSGID=yes",
                          "CapabilityBoundingSet=", "PrivateDevices=yes"):
            with self.subTest(directive=directive):
                self.assertIn(directive, self.svc)

    def test_the_client_uid_is_substituted_at_install_time(self):
        # A peer check against the wrong uid is worse than none: it reads as
        # though it names the dispatcher.
        self.assertIn("QFE_CLIENT_UID=%%QFD_UID%%", self.svc)

    def test_every_env_knob_the_code_reads_is_set_by_the_unit(self):
        # The enumeration `Config.from_env` reads, checked against the unit --
        # the same test 2a wrote for qfd after twice claiming coverage the
        # enumeration did not have.
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "service.py")) as fh:
            source = fh.read()
        block = source[source.index("def from_env(cls, env=None):"):]
        block = block[:block.index("# --- the gate")]
        import re as _re
        knobs = set(_re.findall(r'env\.get\("(QFE_[A-Z_]+)"', block))
        self.assertTrue(knobs)
        for knob in sorted(knobs):
            with self.subTest(knob=knob):
                self.assertIn(f"Environment={knob}=", self.svc)


class TestTheUnitNamesAnInterpreterThatWillExist(unittest.TestCase):
    """2b-1's P1: the unit named `/usr/bin/python3`, which has neither pyarrow
    nor psycopg, and the tests hid it by putting the packages on `sys.path`. The
    installed service could not start. This checks the three things that have to
    agree: the unit's interpreter, the directory the setup script syncs, and the
    manifest that declares the closure."""

    @classmethod
    def setUpClass(cls):
        cls.here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(cls.here, "qf-eval.service")) as fh:
            cls.svc = fh.read()

    def exec_start(self):
        lines = [l for l in self.svc.splitlines() if l.startswith("ExecStart=")]
        self.assertEqual(len(lines), 1, lines)
        return lines[0].split("=", 1)[1].split()

    def test_the_interpreter_is_the_venv_the_manifest_builds(self):
        interpreter, script = self.exec_start()
        self.assertTrue(interpreter.endswith("/evaluator/env/.venv/bin/python"),
                        interpreter)
        self.assertTrue(script.endswith("/evaluator/service.py"), script)

    def test_it_is_not_the_system_interpreter(self):
        # Which would work TODAY -- the service is stdlib-only so far -- and
        # break the moment 2c-2 imports pyarrow.
        interpreter, _script = self.exec_start()
        self.assertNotIn(interpreter, ("/usr/bin/python3", "/usr/bin/python"))

    def test_the_manifest_and_the_lock_both_exist(self):
        env = os.path.join(self.here, "env")
        self.assertTrue(os.path.isfile(os.path.join(env, "pyproject.toml")))
        # A range in a manifest with no lock is a wish. 2b-1 made this a
        # refusal in the setup script after the warning was ignored twice.
        self.assertTrue(os.path.isfile(os.path.join(env, "uv.lock")))

    def test_the_closure_declares_what_2c_2_will_import(self):
        with open(os.path.join(self.here, "env", "pyproject.toml")) as fh:
            manifest = fh.read()
        for package in ("pyarrow", "numpy"):
            self.assertIn(package, manifest)
        # NOT pandas: the trusted evaluator's per-row single pass is what makes
        # it an independent route rather than the trainer's path twice (D26),
        # and leaving it out keeps this closure off the trainer's bump cycle.
        self.assertNotIn("pandas", code_only(manifest))

    def test_the_lock_covers_the_declared_dependencies(self):
        with open(os.path.join(self.here, "env", "uv.lock")) as fh:
            lock = fh.read()
        for package in ("pyarrow", "numpy"):
            self.assertIn(f'name = "{package}"', lock)


class TestTheRefusalClassReachesTheDispatcher(ServiceCase):
    """`qfd` records an `error_class`, and folding every cause into `refused`
    makes a negative control's own signal invisible -- which is why
    `contract_not_trusted` is carried through rather than flattened."""

    class Refusal(ValueError):
        def __init__(self, message, error_class):
            super().__init__(message)
            self.error_class = error_class

    def _reply(self, raiser):
        digest = self.write_contract()
        handler = service.Handler(self.cfg, evaluate=raiser)
        return handler.handle({
            "op": "evaluate",
            "run_id": "probe-20260829T123756Z-9d54e39271d7-1",
            "contract": digest, "request_hash": "e" * 64,
            "predictions_sha256": "d" * 64})

    def test_a_named_class_is_carried_through(self):
        def raiser(_cfg, _req, _name):
            raise self.Refusal("the row set is not scorable", "row_set_rejected")
        reply = self._reply(raiser)
        self.assertEqual(reply["error_class"], "row_set_rejected")
        self.assertIn("not scorable", reply["error"])

    def test_a_refusal_with_no_class_still_gets_one(self):
        def raiser(_cfg, _req, _name):
            raise ValueError("something ordinary")
        self.assertEqual(self._reply(raiser)["error_class"], "refused")

    def test_a_non_string_class_does_not_reach_the_reply(self):
        # A column consumers grep must not be able to hold an object.
        def raiser(_cfg, _req, _name):
            raise self.Refusal("x", {"not": "a token"})
        self.assertEqual(self._reply(raiser)["error_class"], "refused")

    def test_an_empty_class_does_not_reach_the_reply(self):
        def raiser(_cfg, _req, _name):
            raise self.Refusal("x", "")
        self.assertEqual(self._reply(raiser)["error_class"], "refused")

    def test_an_unexpected_failure_is_still_opaque(self):
        # THE BOUNDARY THAT MUST NOT MOVE: a dependency's error prose is not a
        # control, and this reply crosses a trust boundary in the direction
        # that matters.
        def raiser(_cfg, _req, _name):
            raise RuntimeError("pyarrow internal detail with a path in it")
        reply = self._reply(raiser)
        self.assertEqual(reply["error_class"], "internal")
        self.assertNotIn("pyarrow", reply["error"])
        self.assertIn("journal reference", reply["error"])

    def test_a_verdict_reaches_the_reply(self):
        # THE CANARY: without it every clause above could hold because the
        # handler refuses everything.
        def works(_cfg, _req, _name):
            return {"verdict": "go", "eval_hash": "a" * 64}
        reply = self._reply(works)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["verdict"], "go")


class TestMainInjectsTheImplementation(unittest.TestCase):
    """2c-2 supplies `evaluate.evaluate`, and `main` is where it is wired."""

    def setUp(self):
        with open(os.path.join(EVALUATOR, "service.py")) as fh:
            body = fh.read()
        self.main = code_only(body[body.index("def main(argv=None):"):])

    def test_it_injects_the_real_evaluate(self):
        self.assertIn("import evaluate as evaluate_mod", self.main)
        self.assertIn("evaluate_mod.evaluate", self.main)
        self.assertIn("Handler(cfg, evaluate=evaluate", self.main)

    def test_the_import_is_inside_main_not_at_module_scope(self):
        # `service` must stay importable without pyarrow, which is what lets the
        # startup gate -- the part that enforces this domain's privilege
        # boundaries -- be tested in an environment without the closure.
        with open(os.path.join(EVALUATOR, "service.py")) as fh:
            module_scope = code_only(fh.read()).split("def main(")[0]
        for name in ("pyarrow", "numpy", "import evaluate"):
            self.assertNotIn(name, module_scope, name)

    def test_an_import_failure_becomes_a_startup_problem(self):
        # Not a crash: under socket activation, exiting is a hang plus a restart
        # counter, which 2b-1 discovered at 15 restarts.
        self.assertIn("problems.append", self.main)
        self.assertIn("evaluator/env/.venv", self.main)

    def test_service_imports_with_no_closure_installed(self):
        # Executed rather than asserted about: the claim is that THIS
        # interpreter, which has neither pyarrow nor numpy, can import it.
        p = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r); sys.path.insert(0, %r);"
             " import service; print(service.FORBIDDEN_GROUPS)"
             % (EVALUATOR, os.path.join(HOST, "shared"))],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("docker", p.stdout)



if __name__ == "__main__":
    # AT THE END, deliberately. It used to sit two thirds of the way up with 160
    # lines of test classes after it, so `python tests/test_service.py` ran none
    # of them and reported OK. `discover` imports the whole module, so the suite
    # was green either way and the gap was invisible.
    unittest.main()
