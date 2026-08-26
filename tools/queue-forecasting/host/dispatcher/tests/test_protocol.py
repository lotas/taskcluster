# Tests for the socket protocol and admission gates. The server runs in-process
# against a socket in a temp directory with no Docker and no git: the boundary
# under test is the protocol, and a real daemon would only add flakiness.
#
# The store, however, is real and on disk, and several of these drive it from
# many threads at once -- a fake runner alone would hide the thread-bound
# connection defect the DB-owner thread exists to prevent.
import json
import os
import socket
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import qfd
import spec
import store

SHA = "3f1c" + "0" * 36
DEPLOY_UID = 4242


def base_spec(**over):
    d = {"schema": 1, "kind": "test", "source_sha": SHA}
    d.update(over)
    return d


class ProtocolCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.runs = os.path.join(root, "runs")
        self.intent = os.path.join(root, "intent.d")
        self.trusted = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        os.makedirs(self.runs)
        os.makedirs(self.intent)

        self.cfg = qfd.Config(
            trusted_dir=self.trusted, state_dir=root, runs_dir=self.runs,
            socket_path=os.path.join(root, "client.sock"),
            admin_socket_path=os.path.join(root, "admin.sock"),
            admin_uid=DEPLOY_UID, remote="https://example.invalid/x",
            token_file=os.path.join(root, "token"),
            lock_file=os.path.join(root, "lock"), intent_dir=self.intent,
            build_lock=os.path.join(root, "build.lock"),
            build_timeout_s=1800, build_lock_wait_s=900, build_settle_s=30,
            job_hold_deadline_s=7800, kill_confirm_s=300, stop_timeout_s=10,
            reap_interval_s=60, setup_teardown_allowance_s=600,
            marker_stale_margin_s=900,
            lock_migrated_marker=os.path.join(root, "migrated"),
            mem_budget_mb=22528, timeout_max_s=3600, lock_wait_s=9000,
            image_build_mem_mb=2048, light_workers=2, log_cap_mb=16,
            artifact_cap_mb=2048, handoff_timeout_s=120, disk_floor_gb=1,
            queued_cap_per_uid=5)
        self.cfg.check_deadline_chain()

        self.db = qfd.DbOwner(os.path.join(root, "state.db"),
                              mem_budget_mb=self.cfg.mem_budget_mb).start()
        self.addCleanup(self.db.stop)
        self.disp = qfd.Dispatcher(self.cfg, self.db)

    # --- direct dispatch (no socket) ---------------------------------------
    def do(self, op, payload=None, uid=1000, admin=False):
        return self.disp.handle(op, payload or {}, uid, admin=admin)

    def submit(self, uid=1000, **over):
        return self.do("submit", {"spec": base_spec(**over)}, uid=uid)

    def _raw_conn(self):
        """A connection of our own. The Store belongs to the DB-owner thread --
        reaching into it from here is exactly the ProgrammingError that thread
        exists to prevent."""
        import sqlite3
        return sqlite3.connect(os.path.join(self.tmp.name, "state.db"),
                               isolation_level=None)

    def raw_exec(self, sql, params=()):
        db = self._raw_conn()
        try:
            db.execute(sql, params)
        finally:
            db.close()

    def raw_query(self, sql, params=()):
        db = self._raw_conn()
        try:
            return db.execute(sql, params).fetchall()
        finally:
            db.close()


class TestSubmit(ProtocolCase):
    def test_a_valid_submit_returns_a_run_id_and_reaches_queued(self):
        resp = self.submit()
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["state"], "QUEUED")
        job = self.db.call("get", resp["run_id"])
        self.assertEqual(job["state"], "QUEUED")

    def test_the_run_id_format_sorts_chronologically(self):
        resp = self.submit()
        run_id = resp["run_id"]
        parts = run_id.split("-")
        self.assertEqual(parts[0], "test")
        self.assertRegex(parts[1], r"^\d{8}T\d{6}Z$")
        self.assertEqual(parts[2], SHA[:12])
        self.assertEqual(parts[3], "1")

    def test_an_invalid_spec_is_refused_with_the_spec_error_message(self):
        resp = self.do("submit", {"spec": base_spec(dockerfile="/evil")})
        self.assertFalse(resp["ok"])
        self.assertIn("unknown key", resp["error"])

    def test_a_submit_with_no_spec_object_is_refused(self):
        for payload in [{}, {"spec": "test"}, {"spec": []}]:
            with self.subTest(payload=payload):
                self.assertFalse(self.do("submit", payload)["ok"])

    def test_beyond_the_per_uid_queued_cap_submit_is_refused(self):
        # One caller must not be able to fill the queue.
        for _ in range(self.cfg.queued_cap_per_uid):
            self.assertTrue(self.submit(uid=7777)["ok"])
        refused = self.submit(uid=7777)
        self.assertFalse(refused["ok"])
        self.assertIn("cap", refused["error"])
        # A different caller is unaffected.
        self.assertTrue(self.submit(uid=8888)["ok"])

    def test_a_job_over_the_remaining_budget_is_queued_not_refused(self):
        # Contention is not invalidity: it will fit later.
        big = self.do("submit", {"spec": base_spec(mem_limit="22g")})
        self.assertTrue(big["ok"], big)
        self.assertEqual(big["state"], "QUEUED")
        self.assertEqual(self.db.call("get", big["run_id"])["lane"], "heavy")

    def test_two_concurrent_submits_get_distinct_run_ids(self):
        # A run-id collision would clobber another run's directory.
        got, lock = [], threading.Lock()

        def worker(i):
            resp = self.submit(uid=1000 + i)
            if resp.get("ok"):
                with lock:
                    got.append(resp["run_id"])

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(got), 12)
        self.assertEqual(len(set(got)), 12, got)
        ok, problems = self.db.call("verify_chain")
        self.assertTrue(ok, problems)


class TestUidCannotBeSpoofed(ProtocolCase):
    def test_a_payload_claiming_another_uid_is_ignored(self):
        # SO_PEERCRED wins. The payload's own idea of the caller is not read.
        resp = self.disp.handle("submit",
                                {"spec": base_spec(), "uid": 0,
                                 "submitted_by_uid": 0}, 1000)
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(self.db.call("get", resp["run_id"])["submitted_by_uid"],
                         1000)

    def test_the_recorded_uid_is_the_peer_uid(self):
        resp = self.submit(uid=31337)
        self.assertEqual(self.db.call("get", resp["run_id"])["submitted_by_uid"],
                         31337)


class TestOpTable(ProtocolCase):
    def test_an_unknown_op_is_refused_by_name(self):
        resp = self.do("frobnicate")
        self.assertFalse(resp["ok"])
        self.assertIn("frobnicate", resp["error"])
        self.assertIn("ops", resp)

    def test_force_release_is_absent_from_the_client_socket_entirely(self):
        # The revision-8 regression: an escape hatch reachable by the untrusted
        # user, whose group is on this socket.
        self.assertNotIn("force-release", qfd.CLIENT_OPS)
        resp = self.do("force-release", {"run_id": "x"}, uid=0)
        self.assertFalse(resp["ok"])
        self.assertIn("unknown op", resp["error"])

    def test_the_client_ops_are_not_discoverable_from_the_admin_socket(self):
        for op in qfd.CLIENT_OPS:
            with self.subTest(op=op):
                resp = self.do(op, uid=0, admin=True)
                self.assertFalse(resp["ok"])
                self.assertIn("unknown op", resp["error"])

    def test_every_client_op_has_a_handler(self):
        for op in qfd.CLIENT_OPS + qfd.ADMIN_OPS:
            with self.subTest(op=op):
                self.assertTrue(
                    hasattr(self.disp, "_op_" + op.replace("-", "_")))


class TestForceRelease(ProtocolCase):
    def blocked_job(self):
        resp = self.submit()
        run_id = resp["run_id"]
        self.db.call("dequeue", "light", owner="w1", now=qfd.utcnow(),
                     lease_expires_at="2026-08-25T11:00:00Z",
                     hold_deadline_at="2026-08-25T12:00:00Z", max_running=2)
        self.db.call("transition", run_id, "RUNNING", now=qfd.utcnow())
        self.db.call("transition", run_id, "CLEANUP_BLOCKED", now=qfd.utcnow(),
                     fields={"error_class": "kill_unconfirmed"})
        return run_id

    def test_an_unauthorised_peer_uid_is_refused_on_the_admin_socket(self):
        run_id = self.blocked_job()
        for uid in (1000, 65534, DEPLOY_UID + 1):
            with self.subTest(uid=uid):
                resp = self.do("force-release",
                               {"run_id": run_id,
                                qfd.FORCE_RELEASE_FLAG: True},
                               uid=uid, admin=True)
                self.assertFalse(resp["ok"])
                self.assertIn("not authorised", resp["error"])
        self.assertEqual(self.db.call("get", run_id)["state"], "CLEANUP_BLOCKED")

    def test_root_and_the_deploy_uid_are_authorised(self):
        for uid in (0, DEPLOY_UID):
            with self.subTest(uid=uid):
                run_id = self.blocked_job()
                resp = self.do("force-release",
                               {"run_id": run_id,
                                qfd.FORCE_RELEASE_FLAG: True},
                               uid=uid, admin=True)
                self.assertTrue(resp["ok"], resp)
                self.assertEqual(self.db.call("get", run_id)["state"], "FAILED")

    def test_without_the_long_flag_it_is_refused(self):
        run_id = self.blocked_job()
        resp = self.do("force-release", {"run_id": run_id}, uid=0, admin=True)
        self.assertFalse(resp["ok"])
        self.assertIn(qfd.FORCE_RELEASE_FLAG.replace("_", "-"), resp["error"])
        self.assertEqual(self.db.call("get", run_id)["state"], "CLEANUP_BLOCKED")

    def test_it_records_an_event_carrying_the_callers_uid(self):
        # Audit and authorisation are both needed, separately.
        run_id = self.blocked_job()
        self.do("force-release",
                {"run_id": run_id, qfd.FORCE_RELEASE_FLAG: True},
                uid=DEPLOY_UID, admin=True)
        pin = self.db.call("get", run_id)
        self.assertEqual(pin["error_class"], "force_released")
        rows = self.raw_query(
            "SELECT value FROM pins WHERE run_id=? AND"
            " key='force_released_by_uid'", (run_id,))
        self.assertEqual(rows[0][0], str(DEPLOY_UID))
        ok, problems = self.db.call("verify_chain")
        self.assertTrue(ok, problems)

    def test_a_job_that_is_not_cleanup_blocked_is_refused(self):
        resp = self.submit()
        out = self.do("force-release",
                      {"run_id": resp["run_id"], qfd.FORCE_RELEASE_FLAG: True},
                      uid=0, admin=True)
        self.assertFalse(out["ok"])
        self.assertIn("CLEANUP_BLOCKED", out["error"])


class TestCleanupBlockedStall(ProtocolCase):
    def setUp(self):
        super().setUp()
        resp = self.submit()
        self.run_id = resp["run_id"]
        self.db.call("dequeue", "light", owner="w1", now=qfd.utcnow(),
                     lease_expires_at="2026-08-25T11:00:00Z",
                     hold_deadline_at="2026-08-25T12:00:00Z", max_running=2)
        self.db.call("transition", self.run_id, "RUNNING", now=qfd.utcnow())
        self.db.call("transition", self.run_id, "CLEANUP_BLOCKED",
                     now=qfd.utcnow(),
                     fields={"error_class": "kill_unconfirmed"})

    def test_submit_still_succeeds_but_nothing_is_admitted(self):
        # A stall must not look like a refusal to the caller, and must not admit.
        resp = self.submit()
        self.assertTrue(resp["ok"], resp)
        ok, reason = self.disp.may_admit()
        self.assertFalse(ok)
        self.assertEqual(reason, "cleanup_blocked")

    def test_ping_names_the_stall_and_the_run_holding_it(self):
        # A silent stall looks exactly like an idle dispatcher.
        resp = self.do("ping")
        self.assertTrue(resp["ok"], resp)
        self.assertIsNotNone(resp["stall"])
        self.assertEqual(resp["stall"]["reason"], "cleanup_blocked")
        self.assertIn(self.run_id, resp["stall"]["run_ids"])
        self.assertIn("force-release", resp["stall"]["detail"])

    def test_status_names_the_stall_too(self):
        resp = self.do("status", {"run_id": self.run_id})
        self.assertIsNotNone(resp["stall"])

    def test_a_cleanup_blocked_job_still_holds_its_reservation(self):
        self.assertGreater(self.db.call("admitted_mem_mb"), 0)
        self.assertEqual(self.db.call("lane_busy", "light"), 1)

    def test_resolving_it_resumes_admissions_by_itself(self):
        self.db.call("transition", self.run_id, "FAILED", now=qfd.utcnow(),
                     fields={"finished_at": qfd.utcnow()})
        ok, reason = self.disp.may_admit()
        self.assertTrue(ok, reason)
        self.assertEqual(self.db.call("admitted_mem_mb"), 0)


class TestIntentGate(ProtocolCase):
    def marker(self, name, pid, deadline):
        path = os.path.join(self.intent, name)
        with open(path, "w") as fh:
            fh.write(f"pid={pid}\ndeadline={deadline}\n")
        return path

    def test_a_live_marker_blocks_admission(self):
        self.marker("nightly.1.100.intent", os.getpid(), 10 ** 10)
        blocked, notes = self.disp.gate.scan(1000)
        self.assertTrue(blocked)
        self.assertIn("live nightly intent", " ".join(notes))

    def test_a_marker_with_a_dead_pid_is_unlinked_and_ignored(self):
        path = self.marker("nightly.999999.100.intent", 999999, 10 ** 10)
        blocked, notes = self.disp.gate.scan(1000)
        self.assertFalse(blocked)
        self.assertIn("STALE", " ".join(notes))
        self.assertFalse(os.path.exists(path))

    def test_an_expired_deadline_is_stale_even_with_a_live_pid(self):
        path = self.marker("nightly.1.100.intent", os.getpid(), 500)
        blocked, _ = self.disp.gate.scan(1000)
        self.assertFalse(blocked)
        self.assertFalse(os.path.exists(path))

    def test_a_malformed_marker_fails_closed(self):
        # A file that cannot be parsed cannot be shown to be stale.
        path = os.path.join(self.intent, "nightly.1.100.intent")
        with open(path, "w") as fh:
            fh.write("garbage\n")
        blocked, notes = self.disp.gate.scan(1000)
        self.assertTrue(blocked)
        self.assertIn("failing closed", " ".join(notes))
        self.assertTrue(os.path.exists(path))

    def test_an_unparseable_name_fails_closed(self):
        with open(os.path.join(self.intent, "whatever"), "w") as fh:
            fh.write("pid=1\ndeadline=99\n")
        blocked, notes = self.disp.gate.scan(1000)
        self.assertTrue(blocked)
        self.assertIn("unparseable marker name", " ".join(notes))

    def test_a_corrupt_marker_older_than_the_escape_hatch_is_unlinked(self):
        # Corruption delays the loop instead of ending it.
        path = os.path.join(self.intent, "nightly.1.100.intent")
        with open(path, "w") as fh:
            fh.write("garbage\n")
        old = 1000
        os.utime(path, (old, old))
        horizon = old + self.cfg.lock_wait_s + self.cfg.marker_stale_margin_s + 1
        blocked, notes = self.disp.gate.scan(horizon)
        self.assertIn("escape hatch", " ".join(notes))
        self.assertFalse(os.path.exists(path))

    def test_a_publish_in_progress_tmp_file_is_not_yet_live(self):
        with open(os.path.join(self.intent, "nightly.1.100.intent.tmp"), "w") as fh:
            fh.write("pid=1\n")
        blocked, _ = self.disp.gate.scan(1000)
        self.assertFalse(blocked)

    def test_an_unreadable_intent_dir_fails_closed(self):
        gate = qfd.IntentGate(os.path.join(self.tmp.name, "gone"),
                              lock_wait_s=9000, stale_margin_s=900)
        blocked, notes = gate.scan(1000)
        self.assertTrue(blocked)
        self.assertIn("failing closed", " ".join(notes))

    def test_a_live_marker_blocks_may_admit(self):
        self.marker("nightly.1.100.intent", os.getpid(), 10 ** 10)
        ok, reason = self.disp.may_admit()
        self.assertFalse(ok)
        self.assertEqual(reason, "nightly_intent")


class TestCancel(ProtocolCase):
    def test_cancel_on_a_queued_job_is_honoured(self):
        run_id = self.submit()["run_id"]
        resp = self.do("cancel", {"run_id": run_id})
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(self.db.call("get", run_id)["state"], "CANCELLED")

    def test_cancel_on_a_terminal_job_is_refused(self):
        # A cancel that pretends is worse than one that refuses.
        run_id = self.submit()["run_id"]
        self.do("cancel", {"run_id": run_id})
        resp = self.do("cancel", {"run_id": run_id})
        self.assertFalse(resp["ok"])
        self.assertIn("already CANCELLED", resp["error"])

    def test_cancel_on_an_unknown_run_is_refused(self):
        self.assertFalse(self.do("cancel", {"run_id": "nope"})["ok"])
        self.assertFalse(self.do("cancel", {})["ok"])


class TestTrustedPaths(ProtocolCase):
    def test_it_reports_realpaths_under_the_trusted_root_with_digests(self):
        # NC10's live half.
        resp = self.do("trusted-paths")
        self.assertTrue(resp["ok"], resp)
        root = os.path.realpath(self.trusted)
        self.assertEqual(resp["trusted_dir"], root)
        by_name = {e["name"]: e for e in resp["paths"]}
        for name in ("spec.py", "store.py", "qfd.py", "sandbox.py"):
            with self.subTest(name=name):
                entry = by_name[name]
                self.assertTrue(entry["realpath"].startswith(root + os.sep))
                self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")

    def test_every_trusted_file_is_listed(self):
        resp = self.do("trusted-paths")
        self.assertEqual({e["name"] for e in resp["paths"]},
                         set(qfd.TRUSTED_FILES))

    def test_a_path_escaping_the_trusted_root_is_reported_not_hashed(self):
        cfg = self.cfg
        original = cfg.trusted_dir
        cfg.trusted_dir = os.path.join(self.tmp.name, "elsewhere")
        os.makedirs(cfg.trusted_dir, exist_ok=True)
        try:
            resp = self.do("trusted-paths")
            self.assertTrue(all("error" in e for e in resp["paths"]), resp)
        finally:
            cfg.trusted_dir = original


class TestVerifyChainOp(ProtocolCase):
    def test_it_reports_ok_on_a_clean_store(self):
        self.submit()
        resp = self.do("verify-chain")
        self.assertTrue(resp["chain_ok"], resp)

    def test_it_reports_problems_after_a_direct_edit(self):
        run_id = self.submit()["run_id"]
        self.raw_exec("UPDATE jobs SET source_sha='x' WHERE run_id=?", (run_id,))
        resp = self.do("verify-chain")
        self.assertFalse(resp["chain_ok"])
        self.assertTrue(resp["problems"])


class TestListOp(ProtocolCase):
    def test_a_bad_limit_or_state_is_refused(self):
        for payload in [{"limit": 0}, {"limit": 501}, {"limit": "5"},
                        {"limit": True}, {"state": "NONSENSE"}]:
            with self.subTest(payload=payload):
                self.assertFalse(self.do("list", payload)["ok"])

    def test_it_filters_by_state(self):
        self.submit()
        run_id = self.submit()["run_id"]
        self.do("cancel", {"run_id": run_id})
        queued = self.do("list", {"state": "QUEUED"})["jobs"]
        self.assertEqual(len(queued), 1)


class TestSocketLayer(ProtocolCase):
    """The wire, not the dispatch table: framing, size caps and survival."""

    def setUp(self):
        super().setUp()
        self.server = qfd.SocketServer(self.cfg.socket_path, self.disp,
                                       admin=False).bind()
        self.server.start()
        self.addCleanup(self.server.stop)

    def send(self, raw, expect_reply=True, half_close=False):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(self.cfg.socket_path)
            s.sendall(raw)
            if half_close:
                # Signal "that is all I am sending". Without this the server is
                # right to keep waiting for the newline until its own timeout.
                s.shutdown(socket.SHUT_WR)
            if not expect_reply:
                return None
            buf = bytearray()
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf.extend(chunk)
        return json.loads(bytes(buf).split(b"\n")[0]) if buf else None

    def test_a_well_formed_request_round_trips(self):
        resp = self.send(json.dumps({"op": "ping"}).encode() + b"\n")
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["schema"], spec.SCHEMA_VERSION)

    def test_the_peer_uid_is_this_process_uid(self):
        resp = self.send(json.dumps(
            {"op": "submit", "payload": {"spec": base_spec()}}).encode() + b"\n")
        self.assertTrue(resp["ok"], resp)
        job = self.db.call("get", resp["run_id"])
        self.assertEqual(job["submitted_by_uid"], os.getuid())

    def test_malformed_json_gets_an_error_and_the_daemon_survives(self):
        # A one-line denial of service is the failure.
        resp = self.send(b"{not json\n")
        self.assertFalse(resp["ok"])
        self.assertIn("bad JSON", resp["error"])
        self.assertTrue(self.send(json.dumps({"op": "ping"}).encode()
                                  + b"\n")["ok"])

    def test_a_non_object_request_is_refused(self):
        resp = self.send(b"[1,2,3]\n")
        self.assertFalse(resp["ok"])

    def test_a_non_object_payload_is_refused(self):
        resp = self.send(json.dumps({"op": "ping", "payload": 5}).encode()
                         + b"\n")
        self.assertFalse(resp["ok"])

    def test_a_request_over_the_cap_is_rejected(self):
        # An unbounded read on a socket the untrusted user can reach.
        resp = self.send(b"{" + b"x" * (qfd.MAX_REQUEST_BYTES + 100) + b"\n")
        self.assertFalse(resp["ok"])
        self.assertIn("too large", resp["error"])
        self.assertTrue(self.send(json.dumps({"op": "ping"}).encode()
                                  + b"\n")["ok"])

    def test_a_request_without_a_newline_is_rejected(self):
        resp = self.send(json.dumps({"op": "ping"}).encode(), half_close=True)
        self.assertFalse(resp["ok"])
        self.assertIn("unterminated", resp["error"])

    def test_the_daemon_survives_a_client_that_hangs_up_immediately(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(self.cfg.socket_path)
        self.assertTrue(self.send(json.dumps({"op": "ping"}).encode()
                                  + b"\n")["ok"])


class TestConcurrentStoreAccess(ProtocolCase):
    def test_many_threads_submit_while_workers_dequeue(self):
        # The thread-bound-connection defect, which a fake runner alone would
        # hide. Every call goes through the DB-owner thread.
        errors = []

        def submitter(i):
            try:
                for _ in range(4):
                    self.disp.handle("submit", {"spec": base_spec()}, 5000 + i)
            except Exception as e:                     # pragma: no cover
                errors.append(repr(e))

        def worker(i):
            try:
                for _ in range(8):
                    self.db.call("dequeue", "light", owner=f"w{i}",
                                 now=qfd.utcnow(),
                                 lease_expires_at="2026-08-25T23:00:00Z",
                                 hold_deadline_at="2026-08-25T23:30:00Z",
                                 max_running=99)
            except Exception as e:                     # pragma: no cover
                errors.append(repr(e))

        threads = ([threading.Thread(target=submitter, args=(i,))
                    for i in range(6)]
                   + [threading.Thread(target=worker, args=(i,))
                      for i in range(3)])
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.db.call("list", limit=1000)), 24)
        ok, problems = self.db.call("verify_chain")
        self.assertTrue(ok, problems)


class TestConfig(unittest.TestCase):
    def test_the_env_key_list_matches_the_unit_file(self):
        # This list has twice claimed coverage the enumeration did not have, so
        # it is CHECKED against the unit rather than read.
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "qf-dispatch.service")) as fh:
            unit = fh.read()
        in_unit = set()
        for line in unit.splitlines():
            line = line.strip()
            if line.startswith("Environment=") and "=" in line[12:]:
                in_unit.add(line[len("Environment="):].split("=", 1)[0])
        self.assertEqual(in_unit, set(qfd.Config.ENV_KEYS),
                         "Config.ENV_KEYS and the unit file disagree:"
                         f" unit-only={sorted(in_unit - set(qfd.Config.ENV_KEYS))}"
                         f" config-only={sorted(set(qfd.Config.ENV_KEYS) - in_unit)}")

    def test_the_deadline_chain_must_fit(self):
        kw = dict(timeout_max_s=3600, build_timeout_s=1800,
                  build_lock_wait_s=900, handoff_timeout_s=120,
                  setup_teardown_allowance_s=600, job_hold_deadline_s=7800,
                  kill_confirm_s=300, lock_wait_s=9000)
        qfd.Config(**kw).check_deadline_chain()          # the shipped figures
        with self.assertRaises(qfd.ConfigError):
            qfd.Config(**{**kw, "job_hold_deadline_s": 7000}).check_deadline_chain()
        with self.assertRaises(qfd.ConfigError):
            qfd.Config(**{**kw, "lock_wait_s": 8000}).check_deadline_chain()

    def test_the_timeout_ceiling_must_agree_with_spec(self):
        kw = dict(timeout_max_s=1800, build_timeout_s=1800,
                  build_lock_wait_s=900, handoff_timeout_s=120,
                  setup_teardown_allowance_s=600, job_hold_deadline_s=7800,
                  kill_confirm_s=300, lock_wait_s=9000)
        with self.assertRaises(qfd.ConfigError):
            qfd.Config(**kw).check_deadline_chain()


class TestStartupPreconditions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.trusted = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.lock = os.path.join(self.root, "lock")
        self.intent = os.path.join(self.root, "intent.d")
        self.marker = os.path.join(self.root, "migrated")
        open(self.lock, "w").close()
        os.chmod(self.lock, 0o660)
        os.makedirs(self.intent)
        os.chmod(self.intent, 0o2770)
        open(self.marker, "w").close()
        self.cfg = qfd.Config(
            trusted_dir=self.trusted, state_dir=self.root, runs_dir=self.root,
            lock_file=self.lock, intent_dir=self.intent,
            lock_migrated_marker=self.marker)

    def check(self, **over):
        for k, v in over.items():
            setattr(self.cfg, k, v)
        gid = os.stat(self.lock).st_gid
        # `client_gid` is injected because there is no qfclient group in a test
        # environment; the daemon resolves it by name.
        return self.cfg.check_startup(
            my_groups=[gid, os.stat(self.intent).st_gid],
            client_gid=os.stat(self.root).st_gid)

    def test_a_wellformed_host_passes(self):
        self.assertEqual(self.check(), [])

    def test_a_missing_migration_marker_is_fatal(self):
        # An un-migrated cron entry locks a DIFFERENT inode, which is no mutex.
        os.unlink(self.marker)
        problems = self.check()
        self.assertTrue(any("cron-migration marker" in p for p in problems),
                        problems)

    def test_a_lock_that_is_not_group_writable_is_fatal(self):
        # daily_walk_forward.sh's `exec 9>` is a WRITE open and would fail
        # fatally.
        os.chmod(self.lock, 0o640)
        problems = self.check()
        self.assertTrue(any("group-writable" in p for p in problems), problems)

    def test_an_intent_dir_without_setgid_is_fatal(self):
        # Without it, marker modes come from the deploy user's umask and qfd
        # could not read the declaration.
        os.chmod(self.intent, 0o770)
        problems = self.check()
        self.assertTrue(any("setgid" in p for p in problems), problems)

    def test_a_missing_intent_dir_is_fatal(self):
        problems = self.check(intent_dir=os.path.join(self.root, "absent"))
        self.assertTrue(any("stat-able" in p for p in problems), problems)

    def test_a_missing_lock_file_is_fatal(self):
        problems = self.check(lock_file=os.path.join(self.root, "absent"))
        self.assertTrue(any("stat-able" in p for p in problems), problems)

    def test_an_unset_required_path_is_fatal(self):
        problems = self.check(runs_dir="")
        self.assertTrue(any("runs_dir is unset" in p for p in problems),
                        problems)

    def test_a_host_that_forbids_setgid_is_fatal_at_startup(self):
        # The live failure this was written for: RestrictSUIDSGID=yes in the unit
        # made every `chmod 2770` on a run's out/ raise EPERM, so every job died
        # as `error_class=internal` -- a per-job symptom for a per-PROCESS fault.
        # An invariant of the environment belongs in the startup gate.
        def refuse(path, mode):
            raise PermissionError(1, "Operation not permitted")

        with mock.patch("qfd.os.chmod", refuse):
            problems = self.check()
        self.assertTrue(any("setgid bit" in p for p in problems), problems)
        self.assertTrue(any("RestrictSUIDSGID" in p for p in problems),
                        "the refusal must NAME the cause; an operator cannot act"
                        " on 'Operation not permitted'")

    def test_a_setgid_bit_that_silently_does_not_stick_is_fatal(self):
        # POSIX drops S_ISGID without an error when the caller is not in the
        # file's group, so a chmod that "succeeded" proves nothing on its own.
        real = os.chmod

        def drop(path, mode):
            return real(path, mode & ~0o2000)

        with mock.patch("qfd.os.chmod", drop):
            problems = self.check()
        self.assertTrue(any("did not stick" in p for p in problems), problems)

    def test_the_runs_dir_is_made_reachable_by_clients(self):
        # The live failure: systemd's StateDirectory creates runs_dir as
        # qfd:qfd 0750, so `research` could not TRAVERSE it -- and every per-run
        # directory underneath was correctly grouped qfclient beneath a parent
        # nobody in qfclient could enter. `qf logs` could never work for the one
        # account it exists for.
        os.chmod(self.root, 0o700)
        self.assertEqual(self.check(), [])
        st = os.stat(self.root)
        self.assertEqual(st.st_gid, os.stat(self.root).st_gid)
        self.assertTrue(st.st_mode & 0o010,
                        "clients must be able to traverse the runs dir")

    def test_a_runs_dir_that_cannot_be_made_reachable_is_fatal(self):
        # Silence here is the worst outcome: the client's own error for an
        # unreachable path is "no such file", so nothing would name the cause.
        def refuse(path, gid_or_mode, *rest):
            raise PermissionError(1, "Operation not permitted")

        with mock.patch("qfd.os.chown", refuse):
            problems = self.check()
        self.assertTrue(any("reachable by qfclient" in p for p in problems),
                        problems)

    def test_the_probe_leaves_nothing_behind(self):
        self.assertEqual(self.check(), [])
        self.assertFalse(os.path.exists(os.path.join(self.root,
                                                     ".setgid-probe")))


class TestTrustedPathResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        self.trusted = os.path.join(self.root, "trusted")
        os.makedirs(self.trusted)
        self.cfg = qfd.Config(trusted_dir=self.trusted)

    def test_a_path_inside_resolves(self):
        target = os.path.join(self.trusted, "spec.py")
        open(target, "w").close()
        self.assertEqual(self.cfg.trusted_path("spec.py"), target)

    def test_a_traversal_is_refused(self):
        with self.assertRaises(qfd.ConfigError):
            self.cfg.trusted_path("../outside.py")

    def test_a_symlink_escaping_the_root_is_refused(self):
        # NC10 at startup as well as per job: a planted symlink must not become
        # a trusted path because nobody looked.
        outside = os.path.join(self.root, "outside.py")
        open(outside, "w").close()
        os.symlink(outside, os.path.join(self.trusted, "spec.py"))
        with self.assertRaises(qfd.ConfigError):
            self.cfg.trusted_path("spec.py")


class TestBoundedWriter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "out.log")

    def test_it_stops_at_the_cap_and_marks_the_truncation(self):
        # Docker's driver is `none`, so this is the only place the bytes land.
        w = qfd.BoundedWriter(self.path, 10)
        self.assertEqual(w.write(b"12345"), 5)
        self.assertEqual(w.write(b"6789012345"), 5)
        self.assertTrue(w.overflowed)
        w.close()
        with open(self.path, "rb") as fh:
            body = fh.read()
        self.assertTrue(body.startswith(b"1234567890"))
        self.assertIn(b"log truncated", body)

    def test_writes_after_overflow_are_dropped(self):
        w = qfd.BoundedWriter(self.path, 4)
        w.write(b"aaaaaaaa")
        before = os.path.getsize(self.path)
        self.assertEqual(w.write(b"more"), 0)
        w.close()
        self.assertEqual(os.path.getsize(self.path), before)

    def test_an_exact_fit_does_not_overflow(self):
        w = qfd.BoundedWriter(self.path, 4)
        w.write(b"abcd")
        self.assertFalse(w.overflowed)
        w.close()


class TestDockerConfirmation(unittest.TestCase):
    """`is_running` must never turn 'unknown' into 'stopped'."""

    def probe(self, returncode, stdout="", stderr="", raises=None):
        def runner(argv, env, timeout):
            if raises:
                raise raises
            return type("P", (), {"returncode": returncode, "stdout": stdout,
                                  "stderr": stderr})()
        return qfd.Docker(runner=runner)

    def test_a_running_container_is_live(self):
        self.assertIs(self.probe(0, "running\n").is_running("c"), True)

    def test_an_exited_container_is_stopped(self):
        self.assertIs(self.probe(0, "exited\n").is_running("c"), False)

    def test_a_dead_container_is_stopped(self):
        self.assertIs(self.probe(0, "dead\n").is_running("c"), False)

    def test_a_created_container_is_live(self):
        # The one the probe was rewritten for. `{{.State.Running}}` answered
        # "false" here, so a container that had been created and not yet
        # started read as STOPPED -- and the create-then-start protocol makes
        # that window reachable by design: the name is bound first so a sweep
        # can see it. Treating "has not run yet" as "has finished" would
        # release the descriptor over work about to begin.
        self.assertIs(self.probe(0, "created\n").is_running("c"), True)

    def test_paused_restarting_and_removing_are_live(self):
        for status in ("paused", "restarting", "removing"):
            with self.subTest(status=status):
                self.assertIs(self.probe(0, status + "\n").is_running("c"),
                              True)

    def test_a_missing_container_is_positively_absent(self):
        self.assertIs(self.probe(1, "", "Error: No such object: c")
                      .is_running("c"), False)

    def test_an_error_is_unknown_not_stopped(self):
        # Closing the mutex on an unknown answer is the failure the whole
        # confirm-before-release rule exists to prevent.
        self.assertIsNone(self.probe(1, "", "daemon unreachable").is_running("c"))

    def test_a_timeout_is_unknown_not_stopped(self):
        import subprocess as sp
        self.assertIsNone(
            self.probe(0, raises=sp.TimeoutExpired("docker", 1)).is_running("c"))

    def test_an_unparseable_answer_is_unknown(self):
        self.assertIsNone(self.probe(0, "maybe\n").is_running("c"))


class TestAbsenceDoesNotDependOnDockersWording(unittest.TestCase):
    """The live failure of 2026-08-26: every run froze admissions on its way out.

    `is_running` established absence by matching the string "No such object" in
    `docker inspect`'s stderr. Docker 29 words it differently, so a container
    that `--rm` had already removed came back as UNKNOWN -- and unknown is
    deliberately immune to time, so the confirmation loop polled it for
    KILL_CONFIRM_S, gave up, and left the job CLEANUP_BLOCKED with admissions
    shut. A restart re-adopted the cleanup and hit the same wall.

    The rule these tests pin down is that absence must come from an ANSWER, not
    from a sentence: a zero exit from `docker ps -a` is a complete enumeration.
    """

    def docker(self, inspect, ps):
        """`inspect` and `ps` are (returncode, stdout, stderr), or an exception."""
        def runner(argv, env, timeout):
            spec = ps if "ps" in argv else inspect
            if isinstance(spec, BaseException):
                raise spec
            rc, out, err = spec
            return type("P", (), {"returncode": rc, "stdout": out,
                                  "stderr": err})()
        return qfd.Docker(runner=runner)

    UNRECOGNISED = (1, "", "Error response from daemon: No such container: c")

    def test_a_removed_container_is_absent_whatever_the_message_says(self):
        # Deliberately NOT only the Docker 29 wording: the point is that the
        # decision no longer depends on any wording at all.
        for stderr in ("Error response from daemon: No such container: c",
                       "some wording nobody has shipped yet"):
            with self.subTest(stderr=stderr):
                d = self.docker((1, "", stderr), (0, "abc123\tother\n", ""))
                self.assertIs(d.is_running("c"), False)

    def test_a_container_still_listed_is_unknown_not_absent(self):
        # The dangerous direction. inspect failed, but the container IS there --
        # releasing the mutex here is the one failure the subsystem exists to
        # prevent, so this must stay unknown rather than become absent.
        d = self.docker(self.UNRECOGNISED,
                        (0, "abc123\tc\ndef456\tother\n", ""))
        self.assertIsNone(d.is_running("c"))

    def test_an_unobtainable_list_is_unknown_not_absent(self):
        # "The list I could not get did not contain it" is exactly the inference
        # that would release the mutex when the docker socket is missing.
        d = self.docker(self.UNRECOGNISED,
                        (1, "", "Cannot connect to the Docker daemon"))
        self.assertIsNone(d.is_running("c"))

    def test_a_hung_list_is_unknown_not_absent(self):
        d = self.docker(self.UNRECOGNISED,
                        subprocess.TimeoutExpired("docker ps", 1))
        self.assertIsNone(d.is_running("c"))

    def test_a_full_id_matches_by_prefix(self):
        # Callers pass the full id from `docker create` OR the name; a listing
        # must not read as an absence just because it was matched the other way.
        full = "a" * 64
        d = self.docker(self.UNRECOGNISED, (0, full + "\tqf-x-candidate\n", ""))
        self.assertIsNone(d.is_running(full))

    def test_a_recognised_message_still_short_circuits(self):
        # The old fast path stays: on a daemon that words it the old way there is
        # no reason to pay a second subprocess. `ps` is made to fail loudly so
        # this proves it was not consulted.
        d = self.docker((1, "", "Error: No such object: c"),
                        (1, "", "ps should not have been called"))
        self.assertIs(d.is_running("c"), False)


class TestTrainingLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "lock")
        open(self.path, "w").close()

    def test_two_shared_holders_coexist(self):
        a = qfd.TrainingLock(self.path, "light").acquire()
        b = qfd.TrainingLock(self.path, "light").acquire()
        self.addCleanup(a.release)
        self.addCleanup(b.release)
        self.assertTrue(a.held and b.held)

    def test_exclusive_excludes_shared(self):
        a = qfd.TrainingLock(self.path, "heavy").acquire()
        self.addCleanup(a.release)
        with self.assertRaises(qfd.LockHeld):
            qfd.TrainingLock(self.path, "light").acquire()

    def test_shared_excludes_exclusive(self):
        a = qfd.TrainingLock(self.path, "light").acquire()
        self.addCleanup(a.release)
        with self.assertRaises(qfd.LockHeld):
            qfd.TrainingLock(self.path, "heavy").acquire()

    def test_each_job_gets_its_own_descriptor(self):
        # flock ownership is per open file description, so a shared descriptor
        # loses the lock the moment its first user closes it.
        a = qfd.TrainingLock(self.path, "light").acquire()
        b = qfd.TrainingLock(self.path, "light").acquire()
        self.assertNotEqual(a.fd, b.fd)
        a.release()
        self.assertTrue(b.held)
        with self.assertRaises(qfd.LockHeld):
            qfd.TrainingLock(self.path, "heavy").acquire()
        b.release()
        qfd.TrainingLock(self.path, "heavy").acquire().release()

    def test_release_is_idempotent(self):
        a = qfd.TrainingLock(self.path, "light").acquire()
        a.release()
        a.release()
        self.assertFalse(a.held)


class TestRunId(unittest.TestCase):
    def test_the_format_is_kind_stamp_sha_seq(self):
        rid = qfd.make_run_id("test", "a" * 40, 12, now=0)
        self.assertEqual(rid, "test-19700101T000000Z-aaaaaaaaaaaa-12")

    def test_ids_sort_chronologically(self):
        early = qfd.make_run_id("test", "a" * 40, 1, now=0)
        late = qfd.make_run_id("test", "a" * 40, 2, now=10 ** 9)
        self.assertLess(early, late)

    def test_the_sequence_prevents_a_same_second_collision(self):
        a = qfd.make_run_id("test", "a" * 40, 1, now=5)
        b = qfd.make_run_id("test", "a" * 40, 2, now=5)
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()


class TestCheckoutCommit(unittest.TestCase):
    """Provenance has to be read from the checkout, not stamped at install time.

    A stamped value goes stale the moment someone pulls and restarts by hand,
    and a stale sha ends the question "is this the reviewed code?" with a wrong
    answer, where "unknown" at least prompts it.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = self.dir.name
        self.git = os.path.join(self.root, ".git")
        os.makedirs(os.path.join(self.git, "refs", "heads"))
        self.sha = "b" * 40

    def head(self, text):
        with open(os.path.join(self.git, "HEAD"), "w") as fh:
            fh.write(text)

    def branch(self, name, sha):
        path = os.path.join(self.git, "refs", "heads", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(sha + "\n")

    def test_a_loose_ref_is_resolved(self):
        self.head("ref: refs/heads/main\n")
        self.branch("main", self.sha)
        self.assertEqual(qfd.checkout_commit(self.root), self.sha)

    def test_it_searches_upwards_from_a_subdirectory(self):
        # qfd is given the DISPATCHER directory, several levels down.
        self.head("ref: refs/heads/main\n")
        self.branch("main", self.sha)
        deep = os.path.join(self.root, "tools", "qf", "host", "dispatcher")
        os.makedirs(deep)
        self.assertEqual(qfd.checkout_commit(deep), self.sha)

    def test_a_packed_ref_is_resolved(self):
        # A gc'd checkout has no loose ref for its own branch.
        self.head("ref: refs/heads/main\n")
        with open(os.path.join(self.git, "packed-refs"), "w") as fh:
            fh.write("# pack-refs with: peeled fully-peeled sorted\n")
            fh.write(f"{'c' * 40} refs/heads/other\n")
            fh.write(f"{self.sha} refs/heads/main\n")
            fh.write(f"^{'d' * 40}\n")
        self.assertEqual(qfd.checkout_commit(self.root), self.sha)

    def test_a_detached_head_is_the_sha_itself(self):
        self.head(self.sha + "\n")
        self.assertEqual(qfd.checkout_commit(self.root), self.sha)

    def test_a_gitdir_file_is_followed(self):
        # A worktree's `.git` is a file pointing at the real directory.
        real = os.path.join(self.dir.name, "real.git")
        os.makedirs(real)
        with open(os.path.join(real, "HEAD"), "w") as fh:
            fh.write(self.sha + "\n")
        work = os.path.join(self.dir.name, "work")
        os.makedirs(work)
        with open(os.path.join(work, ".git"), "w") as fh:
            fh.write(f"gitdir: {real}\n")
        self.assertEqual(qfd.checkout_commit(work), self.sha)

    def test_anything_unreadable_or_odd_is_unknown_not_an_exception(self):
        # It runs during start-up: a diagnostic must never be why qfd refuses to
        # come up. Every one of these is a shape seen in the wild.
        self.head("ref: refs/heads/main\n")            # ref with no file
        # Not "" -- an empty path means the CWD, which in a checkout resolves to
        # a real commit. That is correct behaviour, not a failure case.
        cases = {
            "missing ref": self.root,
            "no repository": tempfile.mkdtemp(dir=self.dir.name),
            "a path that does not exist": "/nonexistent/deeper/still",
        }
        for label, path in cases.items():
            with self.subTest(case=label):
                self.assertEqual(qfd.checkout_commit(path), "unknown")
        self.branch("main", "not-a-sha")
        self.assertEqual(qfd.checkout_commit(self.root), "unknown",
                         "a non-sha was reported as a commit")

    def test_the_environment_still_wins(self):
        # For tests, and for a deployment with no checkout at all.
        self.assertEqual(qfd.checkout_commit("/nonexistent"), "unknown")
