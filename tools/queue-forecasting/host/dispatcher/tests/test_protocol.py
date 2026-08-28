# Tests for the socket protocol and admission gates. The server runs in-process
# against a socket in a temp directory with no Docker and no git: the boundary
# under test is the protocol, and a real daemon would only add flakiness.
#
# The store, however, is real and on disk, and several of these drive it from
# many threads at once -- a fake runner alone would hide the thread-bound
# connection defect the DB-owner thread exists to prevent.
import json
import os
import re
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
            extract_socket="/nonexistent/extract.sock",
            settlement_lag_s=48 * 3600,
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
        # SCOPED TO `QFD_*`. The unit also sets PYTHONPATH, which is an
        # interpreter setting rather than dispatcher configuration -- it points at
        # `host/shared`, where the extraction validator both domains use lives.
        # `Config.ENV_KEYS` is the enumeration of what the DISPATCHER reads, and
        # widening it to hold PYTHONPATH would make it a list of two different
        # kinds of thing. Non-QFD_ variables are asserted separately, by
        # `TestBothUnitsCanFindTheSharedValidator`.
        in_unit = set()
        for line in unit.splitlines():
            line = line.strip()
            if line.startswith("Environment=") and "=" in line[12:]:
                key = line[len("Environment="):].split("=", 1)[0]
                if key.startswith("QFD_"):
                    in_unit.add(key)
        self.assertEqual(in_unit, set(qfd.Config.ENV_KEYS),
                         "Config.ENV_KEYS and the unit file disagree:"
                         f" unit-only={sorted(in_unit - set(qfd.Config.ENV_KEYS))}"
                         f" config-only={sorted(set(qfd.Config.ENV_KEYS) - in_unit)}")

    def test_the_deadline_chain_must_fit(self):
        kw = dict(timeout_max_s=3600, build_timeout_s=1800,
                  build_lock_wait_s=900, handoff_timeout_s=120,
                  setup_teardown_allowance_s=600, job_hold_deadline_s=7800,
                  kill_confirm_s=300, lock_wait_s=9000)
        qfd.Config(extract_socket="/nonexistent/extract.sock", settlement_lag_s=48 * 3600, **kw).check_deadline_chain()          # the shipped figures
        with self.assertRaises(qfd.ConfigError):
            qfd.Config(extract_socket="/nonexistent/extract.sock", settlement_lag_s=48 * 3600, **{**kw, "job_hold_deadline_s": 7000}).check_deadline_chain()
        with self.assertRaises(qfd.ConfigError):
            qfd.Config(extract_socket="/nonexistent/extract.sock", settlement_lag_s=48 * 3600, **{**kw, "lock_wait_s": 8000}).check_deadline_chain()

    def test_the_timeout_ceiling_must_agree_with_spec(self):
        kw = dict(timeout_max_s=1800, build_timeout_s=1800,
                  build_lock_wait_s=900, handoff_timeout_s=120,
                  setup_teardown_allowance_s=600, job_hold_deadline_s=7800,
                  kill_confirm_s=300, lock_wait_s=9000)
        with self.assertRaises(qfd.ConfigError):
            qfd.Config(extract_socket="/nonexistent/extract.sock", settlement_lag_s=48 * 3600, **kw).check_deadline_chain()


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
            extract_socket="/nonexistent/extract.sock",
            settlement_lag_s=48 * 3600,
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
        self.cfg = qfd.Config(extract_socket="/nonexistent/extract.sock", settlement_lag_s=48 * 3600, trusted_dir=self.trusted)

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


class TestThePruneUnitAndItsScriptAgree(unittest.TestCase):
    """The unit ExecStarts a script in the trusted checkout, and nothing else
    checks that the two still match. A timer that fails at every firing does so
    quietly."""

    def setUp(self):
        self.here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(self.here, "qf-runs-prune.service")) as fh:
            self.unit = fh.read()

    def test_the_execstart_names_a_file_that_exists(self):
        line = [l for l in self.unit.splitlines() if l.startswith("ExecStart=")]
        self.assertEqual(len(line), 1, self.unit)
        path = line[0].split("=", 1)[1].split()[0]
        self.assertEqual(os.path.basename(path), "qf-runs-prune.sh")
        self.assertTrue(os.path.isfile(os.path.join(self.here,
                                                    "qf-runs-prune.sh")))

    def test_the_script_is_executable(self):
        # 0644 here means the timer fails at every firing with "Permission
        # denied", in a unit nobody is watching.
        mode = os.stat(os.path.join(self.here, "qf-runs-prune.sh")).st_mode
        self.assertTrue(mode & 0o111, oct(mode))

    def test_every_knob_the_script_reads_is_set_by_the_unit(self):
        # A default that only exists in the script is a default nobody reviewing
        # the unit can see.
        with open(os.path.join(self.here, "qf-runs-prune.sh")) as fh:
            script = fh.read()
        knobs = set(re.findall(r"\$\{(QF_PRUNE_[A-Z_]+):-", script))
        knobs.discard("QF_PRUNE_DRY_RUN")     # operator-only, never in the unit
        for knob in sorted(knobs):
            with self.subTest(knob=knob):
                self.assertIn(f"Environment={knob}=", self.unit)


class TestMutexProbe(unittest.TestCase):
    """The gap that made a frozen queue indistinguishable from an idle one.

    `may_admit` covers the cleanup stall and the nightly intent gate; it does NOT
    cover the mutex, because that is decided per lane inside `try_one`. From
    outside, a light lane blocked by an incumbent heavy holder looked exactly
    like an idle host -- and the fault gates submitted sixteen jobs into one and
    reported sixteen unrelated voids.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock = os.path.join(self.tmp.name, "heavy.lock")
        open(self.lock, "wb").close()

    def test_an_unheld_lock_reads_free(self):
        self.assertEqual(qfd.probe_mutex(self.lock), "free")

    def test_a_shared_holder_still_reads_free(self):
        # The probe answers the question the LIGHT lane asks. A shared holder is
        # another light job, which does not block one.
        import fcntl as f
        fd = os.open(self.lock, os.O_WRONLY)
        self.addCleanup(os.close, fd)
        f.flock(fd, f.LOCK_SH)
        self.assertEqual(qfd.probe_mutex(self.lock), "free")

    def test_an_exclusive_holder_reads_held(self):
        import fcntl as f
        fd = os.open(self.lock, os.O_WRONLY)
        self.addCleanup(os.close, fd)
        f.flock(fd, f.LOCK_EX)
        self.assertEqual(qfd.probe_mutex(self.lock), "held_exclusive")

    def test_a_missing_lock_is_unknown_not_free(self):
        # "Cannot tell" must not read as "nothing is holding it".
        self.assertEqual(qfd.probe_mutex(self.lock + ".absent"), "unknown")

    def test_the_probe_does_not_keep_the_lock(self):
        # It holds a shared lock for microseconds; if it leaked one, an exclusive
        # acquire afterwards would fail and the probe would have cost the nightly
        # its mutex.
        import fcntl as f
        qfd.probe_mutex(self.lock)
        fd = os.open(self.lock, os.O_WRONLY)
        self.addCleanup(os.close, fd)
        f.flock(fd, f.LOCK_EX | f.LOCK_NB)   # raises if the probe leaked


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


class TestTheNcSuiteCanTellBlindnessFromAnAnswer(unittest.TestCase):
    """The NC suite's own instrument, guarded here because the suite cannot
    guard it: reaching the bad path needs a dispatcher that will not answer, and
    on a healthy host that path never runs.

    A run reported pass=49 fail=24 where `state_of` returned "" for every job.
    The 24 failures were noise; the danger was that three clauses printed `ok`
    having observed nothing -- including NC8's mutual-exclusion and memory-budget
    properties, which are the whole reason NC8 exists."""

    def setUp(self):
        # tests/ -> dispatcher/ -> host/. The suite is a sibling of the
        # dispatcher directory, not of this file.
        host = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))
        self.host = host
        with open(os.path.join(host, "nc-suite-phase2.sh")) as fh:
            self.suite = fh.read()

    def test_state_of_does_not_discard_the_reason_it_could_not_ask(self):
        # The original: `qf status ... 2>/dev/null | python3 -c ... 2>/dev/null`
        # -- two discarded error streams and a single empty-string outcome
        # covering "no socket", "refused", "unparseable" and "no such job".
        body = self.suite[self.suite.index("state_of() {"):]
        body = body[:body.index("\nfield_of() {")]
        self.assertNotIn("2>/dev/null", body,
                         "state_of is discarding the reason again:\n" + body)
        self.assertIn("UNREADABLE", body)
        self.assertIn("note_blind", body)

    def test_the_blindness_counter_is_not_a_shell_variable(self):
        # state_of is called almost only inside $(...), and a subshell's
        # increment to a shell variable is discarded when it exits -- so a
        # counter kept in a variable reads 0 however blind the run was.
        self.assertRegex(self.suite, r'BLIND_FILE="\$\(mktemp')

    def test_the_concurrency_clauses_require_positive_observation(self):
        # "They were never both RUNNING" is satisfied both by a working mutex and
        # by an observer that sees nothing. Only the first is evidence.
        self.assertNotRegex(
            self.suite,
            r'\[ "\$\(state_of "\$\w+"\)" = RUNNING \] && \[ "\$\(state_of',
            "a concurrency clause is comparing raw state_of output again; use"
            " never_concurrent, which demands each job be seen RUNNING")
        self.assertIn("never_concurrent ", self.suite)
        block = self.suite[self.suite.index("never_concurrent() {"):]
        block = block[:block.index("\n}\n")]
        self.assertIn("seen_a", block)
        self.assertIn("exclusion unproven", block)

    def test_the_preflight_proves_the_status_round_trip_not_just_ping(self):
        # `qf ping` answering proves the socket, sudo, the login shell and group
        # membership. It does not prove `qf status <run_id>` answers, which is
        # what every state assertion in the suite is built on.
        self.assertIn("preflight_instrument", self.suite)
        pf = self.suite[self.suite.index("preflight_instrument() {"):]
        pf = pf[:pf.index("\n}\n")]
        self.assertIn("qf submit", pf.replace("submit_as", "qf submit"))
        self.assertIn("state_of", pf)
        self.assertIn("exit 2", pf)

    def test_a_blind_run_does_not_report_totals_as_a_result(self):
        self.assertIn("THE INSTRUMENT WAS BLIND", self.suite)
        self.assertIn("VOID RUN:", self.suite)
        # Blindness fails the run even if every clause happened to pass.
        self.assertRegex(self.suite,
                         r'\[ "\$fail" -eq 0 \] && \[ "\$\{blind:-0\}" -eq 0 \]')

    def test_the_instrument_harness_passes(self):
        script = os.path.join(self.host, "test-nc-instrument.sh")
        self.assertTrue(os.path.isfile(script), script)
        self.assertTrue(os.stat(script).st_mode & 0o111, "not executable")
        # The stand-in clauses use real flock against real background processes,
        # so this takes ~25s of wall clock rather than being instant.
        p = subprocess.run([script], capture_output=True, text=True,
                           timeout=300)
        self.assertEqual(p.returncode, 0,
                         f"harness failed:\n{p.stdout}\n{p.stderr}")
        self.assertIn("harness: pass=16 fail=0", p.stdout)


class TestJsonIsAcceptedOnEitherSideOfTheSubcommand(unittest.TestCase):
    """`--json` was defined on the top-level parser only, so
    `qf status <run_id> --json` -- the order everyone types -- exited 2 with
    "unrecognized arguments: --json".

    Nothing noticed for the length of a phase, because the one caller that used
    that order discarded stderr: every state read in the NC suite came back
    empty, and NC8's mutual-exclusion and memory-budget clauses passed having
    observed nothing. The invocation was never valid; it only looked like a host
    problem."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.host = os.path.dirname(here)
        self.mod = {}
        with open(os.path.join(here, "qf")) as fh:
            exec(compile(fh.read(), "qf", "exec"), self.mod)  # noqa: S102

    def _json(self, argv, admin=False):
        return self.mod["build_parser"](admin).parse_args(argv).json

    def test_the_global_form_still_works(self):
        # The form the README and existing scripts use. A fix that broke this
        # would trade one silent failure for another.
        for argv in (["--json", "status", "r1"], ["--json", "ping"],
                     ["--json", "list", "--limit", "5"],
                     ["--json", "trusted-paths"]):
            self.assertTrue(self._json(argv), argv)

    def test_the_trailing_form_now_works(self):
        for argv in (["status", "r1", "--json"], ["ping", "--json"],
                     ["list", "--limit", "5", "--json"],
                     ["trusted-paths", "--json"]):
            self.assertTrue(self._json(argv), argv)

    def test_the_subparser_flag_does_not_clobber_the_global_one(self):
        # store_true's default of False would OVERWRITE the namespace, so
        # `qf --json status x` would come back json=False -- a fix that silently
        # disables JSON output everywhere. argparse.SUPPRESS is what prevents it.
        self.assertTrue(self._json(["--json", "status", "r1"]))

    def test_absent_means_absent(self):
        for argv in (["status", "r1"], ["ping"]):
            self.assertFalse(self._json(argv), argv)

    def test_the_admin_client_agrees(self):
        self.assertTrue(self._json(["force-release", "r1", "--json"], admin=True))
        self.assertTrue(self._json(["--json", "force-release", "r1"], admin=True))

    def test_no_caller_uses_a_form_that_only_works_on_the_new_client(self):
        # The trailing form is accepted now, but a script in the trusted checkout
        # may run against a client that predates this fix. The global form works
        # against both, so that is the one the callers use.
        for name in ("nc-suite-phase2.sh", "fault-gates-phase2.sh",
                     "phase2-setup.sh"):
            path = os.path.join(self.host, name)
            if not os.path.isfile(path):
                continue
            # Comment lines are skipped: the fixed call site carries a comment
            # naming the broken form, and matching that is how this test first
            # failed against a correct script.
            with open(path) as fh:
                text = "\n".join(l for l in fh.read().splitlines()
                                 if not l.lstrip().startswith("#"))
            bad = re.findall(r"qf(?:admin)? (?!--json)[a-z-]+[^\"'\n]*--json",
                             text)
            self.assertEqual(bad, [], f"{name} uses the trailing form: {bad}")


class TestTheStandInNightlyIsWaitableAndDoesNotBlockItsCaller(unittest.TestCase):
    """`standin_nightly` used to be `( ... ) & echo $!`, read through a command
    substitution. That made it both unreturnable and unwaitable, and produced
    four of NC8's protocol FAILs on a correctly behaving host."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(os.path.dirname(here),
                               "nc-suite-phase2.sh")) as fh:
            self.suite = fh.read()

    def test_it_does_not_echo_its_pid(self):
        # Echoing the pid forces a command substitution at the call site, and the
        # backgrounded subshell inherits that pipe as stdout -- so `$(...)` waits
        # for the stand-in to die instead of for the function to return.
        body = self.suite[self.suite.index("standin_nightly() {"):]
        body = body[:body.index("\n}\n")]
        self.assertNotIn("echo $!", body, body)
        self.assertIn("STANDIN_PID=$!", body)

    def test_its_output_cannot_hold_a_callers_pipe_open(self):
        body = self.suite[self.suite.index("standin_nightly() {"):]
        body = body[:body.index("\n}\n")]
        self.assertIn(">/dev/null 2>&1 &", body)

    def test_no_call_site_uses_a_command_substitution(self):
        # Comments stripped: the fixed helper documents the broken call form, and
        # matching that is how this test first failed against a correct script.
        # Second time in one sitting -- a static scan over source must decide
        # what counts as code before it decides what counts as wrong.
        code = "\n".join(l for l in self.suite.splitlines()
                         if not l.lstrip().startswith("#"))
        self.assertNotRegex(code, r'\w+="\$\(standin_nightly')

    def test_acquisition_is_observed_not_inferred_from_liveness(self):
        # A stand-in that WRONGLY acquired the mutex is also alive, holding it,
        # for its whole hold window -- so `kill -0` alone reports `ok` for the
        # failure clause (c) exists to detect.
        c = self.suite[self.suite.index("# (c) PER-DESCRIPTOR ownership"):]
        c = c[:c.index("# (f)")]
        self.assertIn("standin_acquired", c)
        self.assertIn("released another's shared lock", c)


class TestTheAdminCanaryDoesNotDependOnPath(unittest.TestCase):
    """`(g4) deploy reaches the admin socket` VOIDed with "qfadmin: command not
    found": qfadmin is installed in /usr/local/sbin, which is not on a non-root
    user's PATH. That is a report about $PATH wearing the costume of a report
    about the admin socket."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.host = os.path.dirname(here)
        with open(os.path.join(self.host, "nc-suite-phase2.sh")) as fh:
            self.suite = fh.read()

    def test_the_canary_uses_an_absolute_path(self):
        self.assertRegex(self.suite, r'QFADMIN="\$\{QFADMIN:-/usr/local/sbin/qfadmin\}"')
        g4 = self.suite[self.suite.index("# (g4) force-release authorisation"):]
        g4 = g4[:g4.index("# (g6)")]
        code = "\n".join(l for l in g4.splitlines()
                         if not l.lstrip().startswith("#"))
        self.assertIn("$QFADMIN --help", code)
        self.assertNotIn('"qfadmin --help"', code)

    def test_the_path_matches_where_setup_installs_it(self):
        # A suite that hardcodes a location the installer does not use fails for
        # a reason that has nothing to do with the control.
        with open(os.path.join(self.host, "phase2-setup.sh")) as fh:
            setup = fh.read()
        self.assertIn("/usr/local/sbin/qfadmin", setup)

    def test_the_admin_socket_refusals_do_not_invoke_a_binary(self):
        # A missing binary exits 127, and refuse_as would score that as a
        # refusal it had not earned. These clauses must talk to the socket.
        g4 = self.suite[self.suite.index("# (g4) force-release authorisation"):]
        g4 = g4[:g4.index("# (g6)")]
        for name in ("research cannot reach the admin socket",
                     "force-release does not exist on the client socket"):
            i = g4.index(name)
            self.assertIn("python3 -c", g4[i:i + 400], name)


class TestNc16AssertsTheClassTheClassifierActuallyProduces(unittest.TestCase):
    """NC16's probe names a path that does not exist -- pytest's usage error,
    exit 4 -- and the clause asserted `nonzero_exit` after exit 4 was split out
    as `bad_invocation`. It was asserting the previous behaviour of the very
    thing it tests."""

    def test_the_clause_expects_bad_invocation(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(os.path.dirname(here),
                               "nc-suite-phase2.sh")) as fh:
            suite = fh.read()
        nc16 = suite[suite.index("NC16 the probe is FAILED"):]
        nc16 = nc16[:nc16.index("exit status was relayed")]
        self.assertIn('"bad_invocation"', nc16)
        self.assertNotIn('"nonzero_exit" "$klass"', nc16)

    def test_and_the_classifier_agrees_with_it(self):
        # Pinned together so the two cannot drift again in either direction.
        self.assertEqual(qfd.Runner.EXIT_CLASSES.get(4), "bad_invocation")


class TestBothUnitsCanFindTheSharedValidator(unittest.TestCase):
    """`spec.normalize` delegates the `extract` kind to
    `host/shared/extract_spec.py`, and the extractor's service imports the same
    module. If either unit's `PYTHONPATH` misses it, that domain cannot start --
    and the failure is an ImportError in a journal, days after the change.

    The extractor learned this the expensive way: its `ExecStart` shipped as
    `/usr/bin/python3 service.py` with nothing on the path, and the tests hid it
    by inserting `sys.path` themselves."""

    def setUp(self):
        self.here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.host = os.path.dirname(self.here)

    def _pythonpath(self, unit_path):
        with open(unit_path) as fh:
            lines = [l for l in fh.read().splitlines()
                     if l.startswith("Environment=PYTHONPATH=")]
        self.assertEqual(len(lines), 1, f"{unit_path}: {lines}")
        return lines[0].split("=", 2)[2].split(":")

    def test_the_shared_module_exists_where_both_units_point(self):
        module = os.path.join(self.host, "shared", "extract_spec.py")
        self.assertTrue(os.path.isfile(module), module)

    def test_the_dispatcher_unit_has_shared_on_its_path(self):
        paths = self._pythonpath(os.path.join(self.here, "qf-dispatch.service"))
        self.assertTrue(any(p.endswith("/host/shared") for p in paths), paths)

    def test_the_extractor_unit_has_shared_on_its_path(self):
        paths = self._pythonpath(
            os.path.join(self.host, "extractor", "qf-extract.service"))
        self.assertTrue(any(p.endswith("/host/shared") for p in paths), paths)

    def test_the_shared_module_imports_nothing_outside_the_stdlib(self):
        # It is importable by qfd, which is stdlib-only by D6. An import of
        # anything else here would break the dispatcher, not just the extractor.
        with open(os.path.join(self.host, "shared", "extract_spec.py")) as fh:
            source = fh.read()
        imports = re.findall(r"^\s*(?:import|from)\s+([a-zA-Z_][\w.]*)",
                             source, re.M)
        for name in imports:
            with self.subTest(module=name):
                self.assertIn(name.split(".")[0],
                              {"datetime", "hashlib", "json", "re", "types",
                               "__future__"})

    def test_shared_does_not_import_from_either_domain(self):
        # The dependency runs one way. `shared` is the bottom, which is why
        # `ExtractSpecError` does not subclass `spec.SpecError`.
        with open(os.path.join(self.host, "shared", "extract_spec.py")) as fh:
            source = fh.read()
        for forbidden in ("import spec", "import qfd", "import store",
                          "import extractor", "import inventory"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


class TestTheExtractCommandInTheClient(unittest.TestCase):
    """Task 5's client half. The one thing worth pinning beyond argument
    parsing: `extract` must NOT take a `--sha`, because an extraction runs no
    code and a commit would record a dependency it does not have."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.mod = {}
        with open(os.path.join(here, "qf")) as fh:
            exec(compile(fh.read(), "qf", "exec"), self.mod)  # noqa: S102

    def parse(self, argv):
        return self.mod["build_parser"](False).parse_args(argv)

    def _base(self, *extra):
        return ["extract", "--target", "wait_time",
                "--train-start", "2026-07-01T00:00:00Z",
                "--as-of", "2026-08-01T00:00:00Z", *extra]

    def test_it_takes_no_sha(self):
        with self.assertRaises(SystemExit):
            self.parse(self._base("--sha", "a" * 40))

    def test_the_target_is_a_closed_choice_in_the_client_too(self):
        # Refused before a socket is opened. The dispatcher and the extractor
        # both refuse it as well; this one just saves a round trip and gives the
        # caller the list.
        with self.assertRaises(SystemExit):
            self.parse(["extract", "--target", "p90",
                        "--train-start", "2026-07-01T00:00:00Z",
                        "--as-of", "2026-08-01T00:00:00Z"])

    def test_the_window_is_required(self):
        for missing in (["--train-start", "2026-07-01T00:00:00Z"],
                        ["--as-of", "2026-08-01T00:00:00Z"]):
            with self.subTest(given=missing):
                with self.assertRaises(SystemExit):
                    self.parse(["extract", "--target", "wait_time", *missing])

    def test_lookback_days_has_the_configs_default(self):
        # 30 is what every promoted config uses.
        self.assertEqual(self.parse(self._base()).lookback_days, 30)

    def test_generation_defaults_to_absent_not_one(self):
        # Absent, so the dispatcher's normalisation supplies the default and
        # there is one place that decides it.
        self.assertIsNone(self.parse(self._base()).generation)
        self.assertEqual(self.parse(self._base("--generation", "3")).generation,
                         3)

    def test_the_command_builds_a_spec_with_no_source_sha(self):
        source = self.mod["cmd_extract"].__doc__ or ""
        self.assertIn("runs no code", source)
        # And structurally: the spec body it sends.
        import inspect
        body = inspect.getsource(self.mod["cmd_extract"])
        self.assertNotIn("source_sha", body)
        self.assertIn('"kind": "extract"', body)

    def test_wait_reports_the_extracts_identity(self):
        import inspect
        body = inspect.getsource(self.mod["cmd_extract"])
        for key in ("extract_hash", "extract_dir", "extract_watermark"):
            with self.subTest(key=key):
                self.assertIn(key, body)


class TestSubmittingAnExtractThroughTheOp(unittest.TestCase):
    """The gap this exists to close: every test of the `extract` kind called
    `spec.normalize` DIRECTLY, so none of them noticed that `_op_submit` passed
    it neither a clock nor a settlement lag -- which would have refused every
    extract submit with "kind extract needs a clock and a settlement lag".

    A unit test of a function is not a test of its caller.
    """

    def test_the_op_passes_a_clock_and_a_lag(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "qfd.py")) as fh:
            source = fh.read()
        call = source[source.index("effective = spec_mod.normalize("):]
        call = call[:call.index(")\n")]
        self.assertIn("now=", call)
        self.assertIn("settlement_lag_s=", call)

    def test_the_lag_is_configured_and_matches_the_extractors(self):
        # Two copies on purpose (D17), so a test keeps them equal: a dispatcher
        # that refused a window the extractor would accept, or accepted one the
        # extractor refuses, turns a policy into a coin toss.
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        host = os.path.dirname(here)
        with open(os.path.join(here, "qf-dispatch.service")) as fh:
            disp = fh.read()
        with open(os.path.join(host, "extractor",
                               "qf-extract.service")) as fh:
            extractor_unit = fh.read()
        mine = re.search(r"QFD_SETTLEMENT_LAG_S=(\d+)", disp)
        theirs = re.search(r"QFX_SETTLEMENT_LAG_S=(\d+)", extractor_unit)
        self.assertIsNotNone(mine, "the dispatcher unit does not set the lag")
        self.assertIsNotNone(theirs, "the extractor unit does not set the lag")
        self.assertEqual(mine.group(1), theirs.group(1))

    def test_the_config_key_is_enumerated(self):
        self.assertIn("QFD_SETTLEMENT_LAG_S", qfd.Config.ENV_KEYS)
        self.assertIn("QFD_EXTRACT_SOCKET", qfd.Config.ENV_KEYS)


class TestSourceRefIsActuallyRecorded(unittest.TestCase):
    """`normalize` produced `source_ref` for an extract job and `submit` never
    passed it anywhere, so the job row kept `source_ref = NULL` and the literal
    that stops `source_sha` being mistaken for a commit appeared nowhere a reader
    looks.

    It goes in as a PIN, because that is where `source_ref` actually lives: the
    same-named `jobs` column stays NULL for every job -- the runner writes the pin
    mid-run for a test job -- so a value put only in the column would be as
    invisible as one put nowhere."""

    def test_the_submit_op_writes_the_pin(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "qfd.py")) as fh:
            source = fh.read()
        body = source[source.index('self.db.call("submit", effective'):]
        body = body[:body.index("return {")]
        self.assertIn('"source_ref"', body)
        self.assertIn("set_pin", body)

    def test_the_literal_says_it_is_not_a_commit(self):
        # The whole purpose: a 64-hex value in a field called source_sha will be
        # read as a commit unless the record says otherwise itself.
        self.assertIn("not a commit", spec.EXTRACT_SOURCE_REF)

    def test_only_kinds_that_have_one_get_the_pin(self):
        # A test job's source_ref is the remote-tracking ref the runner resolves;
        # writing a placeholder at submit time would overwrite it later or race.
        effective = spec.normalize({"schema": 1, "kind": "test",
                                    "source_sha": "a" * 40})
        self.assertIsNone(effective.get("source_ref"))


class TestTheExtractsListingIsRelayedNotWalked(unittest.TestCase):
    """The extracts directory belongs to `qfextract`. Having the dispatcher walk
    it would put the layout in two places, which is how the publication path came
    to have a side index that could disagree with the artifacts."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "qfd.py")) as fh:
            self.source = fh.read()

    def test_the_op_exists_and_is_a_client_op(self):
        self.assertIn("extracts", qfd.CLIENT_OPS)
        self.assertNotIn("extracts", qfd.ADMIN_OPS)

    def test_it_relays_rather_than_reading_the_directory(self):
        body = self.source[self.source.index("def _op_extracts"):]
        body = body[:body.index("\n    def ")]
        self.assertIn("extract_request", body)
        for walking in ("listdir", "MANIFEST", "glob"):
            with self.subTest(walking=walking):
                self.assertNotIn(walking, body)

    def test_the_dispatcher_has_no_extracts_directory_setting(self):
        # Nothing here should know where they live. If a config key for it ever
        # appears, the layout is in two places again.
        self.assertNotIn("QFD_EXTRACTS_DIR", qfd.Config.ENV_KEYS)

    def test_an_unreachable_extractor_is_a_refusal_not_a_traceback(self):
        body = self.source[self.source.index("def _op_extracts"):]
        body = body[:body.index("\n    def ")]
        self.assertIn("ExtractRelayError", body)
        self.assertIn("Refused", body)
