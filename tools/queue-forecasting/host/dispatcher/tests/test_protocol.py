# Tests for the socket protocol and admission gates. The server runs in-process
# against a socket in a temp directory with no Docker and no git: the boundary
# under test is the protocol, and a real daemon would only add flakiness.
#
# The store, however, is real and on disk, and several of these drive it from
# many threads at once -- a fake runner alone would hide the thread-bound
# connection defect the DB-owner thread exists to prevent.
import datetime
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

# `host/shared` on the path. This file imports `baseline` and `contract`, both of
# which live there -- and it used to get away with not saying so, because
# `test_runner.py` inserts the same path and `unittest discover` imports every
# test module into one process. That is an import-order dependency, not a
# bootstrap: `test_protocol` sorts BEFORE `test_runner`, so running this file
# alone failed while the suite passed. Inline rather than shared, for the reason
# `test_runner.py` records: a bootstrap that works under one invocation only is
# worse than two copies.
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "shared"))

import qfd                                                     # noqa: E402
import spec                                                    # noqa: E402
import store                                                   # noqa: E402

SHA = "3f1c" + "0" * 36
DEPLOY_UID = 4242


def assert_clause_runs(case, suite, name):
    """A negative-control group that nothing invokes is a group that passes for
    ever, so three test classes assert that each of theirs is wired.

    IT USED TO BE A BARE CALL LINE (`\n  nc9\n`) matched in `main`. The suite now
    takes group names on the command line -- the whole thing submits real jobs
    and waits on real deadlines, so re-running nineteen groups to look at one was
    the alternative, and what that produces is nobody re-running it -- and `main`
    dispatches through a loop. That refactor silently emptied this check on three
    clauses at once, which is why it is one function now: the wiring is TWO
    lists, and a clause has to be in both to be reachable.
    """
    default = re.search(r"groups=\(nc8[^)]*\)", suite)
    case.assertIsNotNone(default, "the default group set is not where it was")
    # `findall`, not `split`: splitting on whitespace leaves `groups=(nc8` and
    # `nc19)` as tokens, so the first and last group in the list would each read
    # as absent -- a check that fails on exactly the two entries it is least
    # likely to be doubted about.
    case.assertIn(name, re.findall(r"nc\d+", default.group(0)),
                  f"{name} is not in the set `main` runs by default")
    accepted = re.search(r"^ *nc8\|nc9\|[a-z0-9|]*\)", suite, re.M)
    case.assertIsNotNone(accepted, "the group-name validator is not where it was")
    case.assertIn(name, re.findall(r"nc\d+", accepted.group(0)),
                  f"{name} cannot be asked for by name on the command line")


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


class TestPingReportsTheResourceGate(ProtocolCase):
    """The second half of the gap TestMutexProbe names.

    `ping` exists to answer "why is my job still QUEUED". It reported the
    cleanup stall and the nightly intent gate (`admit`), and the mutex, and
    `free_disk_mb` -- but not the memory budget or the disk floor, which are
    read one step later inside `try_one`. So the two commonest reasons were the
    two it could not give, and it printed a free-space figure with no floor to
    compare it against, which reads like an answer without being one.

    This is not only a diagnostic. NC15 raises the floor by writing a drop-in
    and restarting, then asserts nothing is admitted -- and with no way to ask
    whether the floor took effect, "a job was admitted" was indistinguishable
    from "the drop-in never applied". The suite failed on that exact
    ambiguity, naming the control as broken on evidence that never touched it.
    """

    def test_a_blocking_floor_is_visible_from_outside(self):
        # A floor far above any real free space: every admission is refused.
        self.disp.db.store.disk_floor_mb = 1 << 40
        resp = self.do("ping")
        self.assertTrue(resp["ok"], resp)
        self.assertTrue(resp["resource"].startswith("disk floor"),
                        resp["resource"])
        # `admit` is UNCHANGED: the resource gate is a separate boundary, and
        # folding them together would lose which one is blocking.
        self.assertEqual(resp["admit"], "ok")

    def test_an_exhausted_memory_budget_is_visible_too(self):
        self.disp.db.store.mem_budget_mb = 0
        resp = self.do("ping")
        self.assertTrue(resp["resource"].startswith("memory budget"),
                        resp["resource"])

    def test_it_says_ok_when_nothing_is_blocking(self):
        resp = self.do("ping")
        self.assertEqual(resp["resource"], "ok", resp["resource"])

    def test_the_gate_is_asked_at_the_smallest_reservation_any_kind_can_ask(self):
        # A false "ok" is the dangerous direction: if this were asked at a large
        # reservation it could report blocked while small jobs flow, and if it
        # were asked at an arbitrary size it could report ok while the smallest
        # job is refused. The smallest is the only size for which "ok" means
        # "something can get in".
        smallest = min((k["mem_limit"] for k in spec.KINDS.values()),
                       key=spec.mem_mb)
        self.assertEqual(qfd.SMALLEST_MEM_LIMIT, smallest)
        self.assertEqual(self.do("ping")["resource_at"], smallest)

    def test_the_floor_itself_is_reported_so_free_space_can_be_read(self):
        resp = self.do("ping")
        self.assertEqual(resp["disk_floor_mb"], self.cfg.disk_floor_gb * 1024)
        self.assertIn("free_disk_mb", resp)


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
        # `fail=0` AND a floor, not an exact count -- the same correction the
        # promoter's wrapper needed. An exact figure makes every added shell
        # clause a two-place edit whose second place is a Python test with
        # nothing to say about the instrument. What this must catch is a harness
        # that stopped running or stopped asserting; a floor catches both, and
        # 0/0 passing catches neither.
        self.assertIn("fail=0", p.stdout)
        count = int(re.search(r"harness: pass=(\d+)", p.stdout).group(1))
        self.assertGreaterEqual(count, 16, p.stdout)


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


class TestNc15FloorClauseCannotClaimWhatItDidNotSee(unittest.TestCase):
    """The clause raised the floor with a drop-in and a restart, then reported
    any non-QUEUED state as "a job was admitted below the disk floor".

    Two different worlds produce that: the floor was in force and admission
    ignored it (the finding the clause exists to make), or the floor never
    applied and the job was admitted because there is plenty of disk (a setup
    failure that says nothing about the control). It also caught the terminal
    states, which mean the opposite of admitted-and-running.

    So the negative claim is now GATED on the daemon first agreeing the floor is
    blocking, and it names the state it saw.
    """

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(os.path.dirname(here),
                               "nc-suite-phase2.sh")) as fh:
            self.suite = fh.read()
        clause = self.suite[self.suite.index("# Admission floor: with the floor"):]
        self.clause = code_only(clause[:clause.index("\n}")])

    def test_the_claim_is_gated_on_the_floor_being_in_force(self):
        # The submit must sit INSIDE the "disk floor" branch, not before the
        # case that reads the gate.
        self.assertIn('qf --json ping', self.clause)
        gate_at = self.clause.index('case "$gate" in')
        self.assertGreater(self.clause.index("submit_as"), gate_at,
                           "the job is submitted before the gate is read")

    def test_a_floor_that_never_applied_is_void_not_a_failure(self):
        branch = self.clause[self.clause.index("    ok)"):]
        self.assertIn("void", branch[:branch.index(";;")])

    def test_an_unreadable_gate_is_void_too(self):
        # An empty `resource` -- an older dispatcher, a broken socket -- must not
        # be read as "the floor is not blocking" and must not be read as a pass.
        self.assertIn("could not read the resource gate", self.clause)

    def test_the_failure_names_the_state_it_observed(self):
        self.assertIn("$LAST_LEFT_FOR", self.clause)
        # SEVENTH instance, and this one is in THIS FILE: the comment above
        # quotes the accusation it is asserting is gone. `code_only` exists for
        # exactly this and I still did not reach for it on the first write.
        self.assertNotIn("a job was admitted below the disk floor",
                         code_only(self.suite))

    def test_being_unable_to_watch_is_separated_from_the_job_moving(self):
        # require_state_for returns 1 (moved) and 2 (blind); an `if` folds them.
        self.assertIn("case $? in", self.clause)

    def test_every_require_state_for_caller_distinguishes_blind_from_moved(self):
        # The whole point: two of the three callers used a bare `if`, and both
        # printed a specific accusation for a state they had not identified.
        code = code_only(self.suite)
        # `require_state_for()` -- the definition -- is not a caller, and its
        # body legitimately has no `case`. Split on the CALL form: a space, then
        # a dollar-quoted run id.
        for chunk in code.split('require_state_for "$')[1:]:
            head = chunk[:400]
            self.assertIn("case $?", head,
                          "a require_state_for caller folds blind into moved:"
                          f" {head.splitlines()[0]!r}")

    def test_the_field_the_gate_reads_is_the_one_ping_emits(self):
        # A scan of the suite proves the clause asks; this proves the daemon
        # answers, under that exact key. Pinned so neither can drift alone.
        self.assertIn('"resource": *"', self.clause.replace('\\', ''))
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "qfd.py")) as fh:
            self.assertIn('"resource": "ok" if res_ok else res_why', fh.read())


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


def code_only(text, comment="#"):
    # NINTH instance, and the shape changed: a `#`-line filter cannot see a
    # DOCSTRING, so the evaluator's `verdict.py` matched its own prohibition
    # ("nothing here writes to trainer/data/models/"). `shared/srcscan.py` now
    # tokenises for Python source. This stays line-based because almost every
    # caller here scans SHELL, which has no docstrings -- and delegating shell to
    # a Python tokeniser would be worse than the bug.
    #
    # For PYTHON source, prefer `srcscan.code_only`.
    """`text` with comment lines removed.

    SIXTH time in this phase that a static scan matched its own documentation:
    `extract_spec`, `standin_nightly`, the unit files, the `force` word, the
    `%%placeholder%%` keys, and now a mode glob quoted in the comment explaining
    why it was replaced.

    "Remember to strip comments" has demonstrably not stuck as a habit, so it is
    a function. A scan over source has to decide what counts as code before it
    decides what counts as wrong, and that decision now lives in one place.
    """
    return "\n".join(line for line in text.splitlines()
                      if not line.lstrip().startswith(comment))


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

    def test_the_dispatcher_knows_the_path_but_never_walks_it(self):
        # REFINED IN 2b-2, and the refinement is a sharpening rather than a
        # retreat. This asserted that `QFD_EXTRACTS_DIR` did not exist at all,
        # on the reasoning that the layout must live in one place. Then Task 9
        # needed to MOUNT an extract, and a bind mount takes a path -- so the
        # dispatcher does have to know where they are.
        #
        # The rule that actually matters is not "must not know the path", it is
        # **must not walk the directory**: `qf extracts` stays relayed to the
        # service that owns it, so there is still one thing that decides what is
        # published. Knowing a path and enumerating a directory are different
        # amounts of knowledge.
        self.assertIn("QFD_EXTRACTS_DIR", qfd.Config.ENV_KEYS)
        body = self.source[self.source.index("def _op_extracts"):]
        body = body[:body.index("\n    def ")]
        for walking in ("listdir", "MANIFEST", "glob", "extracts_dir"):
            with self.subTest(walking=walking):
                self.assertNotIn(walking, body)

    def test_the_only_use_of_the_path_is_a_probe_mount(self):
        uses = [line.strip() for line in self.source.splitlines()
                if "cfg.extracts_dir" in line]
        self.assertEqual(len(uses), 1, uses)
        # And it resolves a validated 64-hex hash, never a spec-supplied path.
        # In `_probe_extract`, which is where the resolution moved when manifest
        # VALIDATION was added -- the assertion is about what the resolution does,
        # not which function holds it.
        resolve = self.source[self.source.index("def _probe_extract"):]
        resolve = resolve[:resolve.index("\n    def ")]
        self.assertIn('effective["args"]["extract"]', resolve)
        self.assertIn("os.path.join(self.cfg.extracts_dir", resolve)
        # And the manifest is parsed, not merely found to exist.
        self.assertIn("json.load", resolve)
        self.assertIn("request_hash", resolve)

    def test_an_unreachable_extractor_is_a_refusal_not_a_traceback(self):
        body = self.source[self.source.index("def _op_extracts"):]
        body = body[:body.index("\n    def ")]
        self.assertIn("ExtractRelayError", body)
        self.assertIn("Refused", body)


class TestARefreshMovesUnitsWithTheCode(unittest.TestCase):
    """`mirror-refresh` reset the checkout and restarted the daemon while
    /etc/systemd/system held a unit from an older commit, so it ran new code
    under old configuration -- silently. The visible symptom was
    `ModuleNotFoundError: No module named 'extract_spec'`: an error about a
    module, for a cause that was a missing `Environment=PYTHONPATH` directive.

    And the extractor kept serving from a process three commits old, because it
    is socket-activated but LONG-LIVED once started -- it answered
    `unknown op 'extracts'` for an op the new code had."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.host = os.path.dirname(here)
        with open(os.path.join(self.host, "phase2-setup.sh")) as fh:
            self.setup = fh.read()
        self.refresh = self.setup[self.setup.index("cmd_mirror_refresh()"):]
        self.refresh = self.refresh[:self.refresh.index("\ncmd_verify")]

    def test_the_refresh_checks_the_installed_units(self):
        self.assertIn("assert_units_current", self.refresh)

    def test_it_refuses_rather_than_reinstalling(self):
        # The installed units carry substituted uids, and the substitution
        # belongs to each setup script's own install path. Duplicating it here
        # would give two places that decide what a unit says.
        check = self.setup[self.setup.index("assert_units_current() {"):]
        check = check[:check.index("\nunit_matches()")]
        self.assertIn("die ", check)
        self.assertIn("phase2b-setup.sh install", check)

    def test_every_shipped_unit_is_in_the_comparison(self):
        # A unit added later and left out of this list would drift unnoticed,
        # which is the whole failure being closed.
        check = self.setup[self.setup.index("assert_units_current() {"):]
        check = check[:check.index("\nunit_matches()")]
        shipped = []
        for sub in ("dispatcher", "extractor"):
            directory = os.path.join(self.host, sub)
            if not os.path.isdir(directory):
                continue
            shipped += [name for name in os.listdir(directory)
                        if name.endswith((".service", ".socket", ".timer"))]
        for unit in shipped:
            with self.subTest(unit=unit):
                self.assertIn(unit, check)

    def test_the_extractor_is_restarted_too(self):
        self.assertIn("qf-extract.service", self.refresh)
        # try-restart, not restart: with nothing having asked for an extract
        # there is no process to cycle, and starting one eagerly would open a
        # database connection nobody wanted.
        self.assertIn("try-restart", self.refresh)

    def test_the_drift_check_has_its_own_test(self):
        script = os.path.join(self.host, "tests", "test_unit_drift.sh")
        self.assertTrue(os.path.isfile(script), script)
        self.assertTrue(os.stat(script).st_mode & 0o111, "not executable")
        p = subprocess.run([script], capture_output=True, text=True,
                           timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        # A floor, not an exact count: the third instance of this pattern, so
        # it is applied everywhere rather than where it happened to bite.
        self.assertIn("fail=0", p.stdout)
        count = int(re.search(r"unit-drift: pass=(\d+)", p.stdout).group(1))
        self.assertGreaterEqual(count, 8, p.stdout)


class TestNc17AndNc18AreWiredAndHonest(unittest.TestCase):
    """The suite cannot be run from here -- it needs the host -- so what is
    checked is that the clauses exist, that they are wired into `main`, and that
    the two habits this project keeps having to relearn are present: a positive
    canary that gates, and no silently-dropped control."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(os.path.dirname(here),
                               "nc-suite-phase2.sh")) as fh:
            self.suite = fh.read()

    def _clause(self, name):
        body = self.suite[self.suite.index(f"{name}() {{"):]
        return body[:body.index("\n}\n")]

    def test_both_clauses_are_called_from_main(self):
        for name in ("nc17", "nc18"):
            with self.subTest(clause=name):
                assert_clause_runs(self, self.suite, name)

    def test_nc17_gates_on_its_canary(self):
        # A canary that reports without gating is decoration -- the lesson from
        # the 49/24 run, where three clauses printed `ok` having observed
        # nothing.
        body = self._clause("nc17")
        canary = body[body.index("NC17 canary"):]
        self.assertIn("return", canary[:600])

    def test_nc17_canary_is_the_service_working_not_a_file_read(self):
        # The plan called for "qfextract can read the credential file". It
        # cannot: the source is 0600 root:root and systemd hands the service a
        # copy. A canary asserting the impossible would void every refusal.
        body = self._clause("nc17")
        self.assertIn("ready", body)
        self.assertNotIn('canary_as "$QFEXTRACT_USER"', body)

    def test_nc17_refuses_for_all_three_identities(self):
        body = self._clause("nc17")
        for user in ("$QFD_USER", "$RESEARCH_USER", "$QFEXTRACT_USER"):
            with self.subTest(user=user):
                self.assertIn(user, body)

    def test_nc17_asserts_the_groups_d15_forbids(self):
        body = self._clause("nc17")
        for group in ("docker", "qfheavy", "qfclient"):
            with self.subTest(group=group):
                self.assertIn(group, body)

    def test_nc18_uses_a_published_window_so_the_canary_is_cheap(self):
        # An 11-minute canary is a canary nobody runs.
        self.assertIn("NC18_TRAIN_START", self.suite)
        self.assertIn("already-published", self.suite)

    def test_nc18_asserts_immutability_by_digest(self):
        body = self._clause("nc18")
        self.assertIn("sha256sum", body)
        self.assertIn("serves the same bytes", body)

    def test_nc18_asserts_the_protocol_cannot_force_a_re_extraction(self):
        # Stronger than the planned clause: `force` exists in the extractor's own
        # API and is deliberately unreachable from the wire.
        body = self._clause("nc18")
        self.assertIn("no way to force a re-extraction", body)

    def test_the_slow_clauses_are_opt_in_and_say_so(self):
        # NO SILENT CAPS. A suite that drops a control quietly reads as coverage.
        body = self._clause("nc18")
        self.assertIn("NC_SLOW", body)
        self.assertIn("OPT-IN", body)
        self.assertIn("covered by unit tests", body)

    def test_the_probe_script_is_valid_python(self):
        # It was not: the first version nested python inside `bash -lc` inside
        # `sudo` and produced `b'...' + b chr(10)`. A canary that cannot run is
        # worse than none, because its failure reads as the thing it checked.
        marker = "cat > \"$prober\" <<'PROBE'\n"
        body = self.suite[self.suite.index(marker) + len(marker):]
        body = body[:body.index("\nPROBE\n")].replace("\\\\n", "\\n")
        compile(body, "<probe>", "exec")


class TestAStaticScanMatchesSyntaxNotProse(unittest.TestCase):
    """FIFTH time in this phase that a static scan of mine matched its own
    documentation. `nc18` searched `service.py` for the word `force` to prove the
    protocol cannot force a re-extraction, and matched five times -- "in force on
    the live cluster", "enforced per process", "unenforceable",
    "enforces_peer_uid" -- reporting a correct service as broken.

    The durable fix is not "remember to strip comments". It is to search for the
    SYNTAX a caller would have to write, which prose cannot contain by accident."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.host = os.path.dirname(here)
        with open(os.path.join(self.host, "nc-suite-phase2.sh")) as fh:
            self.suite = fh.read()

    def test_the_force_scan_looks_for_an_assignment(self):
        i = self.suite.index("no way to force a re-extraction")
        clause = self.suite[i - 1200:i + 400]
        self.assertIn("'force='", clause)

    def test_the_word_alone_would_still_match_the_service(self):
        # The premise, asserted: if this stops being true, the scan could be
        # loosened again without anyone recalling why it was tightened.
        with open(os.path.join(self.host, "extractor", "service.py")) as fh:
            source = fh.read()
        self.assertGreater(source.count("force"), 1)
        self.assertEqual(source.count("force="), 0)


class TestClauseCChecksItsSubjectBeforeConcluding(unittest.TestCase):
    """It reported a dispatcher failure for a race in its own setup: it needs l2
    to still hold a shared lock when l1 finishes, and both are ordinary test jobs
    of similar duration -- `--timeout 600` is a ceiling, not a length."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(os.path.dirname(here),
                               "nc-suite-phase2.sh")) as fh:
            suite = fh.read()
        start = suite.index("# (c) PER-DESCRIPTOR ownership")
        self.clause = suite[start:suite.index("# (f)", start)]

    def test_it_rechecks_l2_at_the_moment_of_measurement(self):
        self.assertIn('state_of "$l2"', self.clause)
        self.assertIn("could not observe its subject", self.clause)

    def test_an_unobservable_run_is_VOID_not_FAIL(self):
        # A precondition that did not hold is not evidence of a defect.
        void_at = self.clause.index('void "(c)')
        fail_at = self.clause.index('bad "(c) one job')
        self.assertLess(void_at, fail_at)

    def test_the_durable_signal_is_checked_before_the_transient_one(self):
        # The marker file outlives the process. Testing liveness first made a
        # stand-in that acquired, held and finished indistinguishable from one
        # that never ran -- which is what hid the real answer.
        acquired_at = self.clause.index("standin_acquired")
        alive_at = self.clause.index('kill -0 "$STANDIN_PID"')
        self.assertLess(acquired_at, alive_at)


class TestNc15AssertsTheFloorNotAToleranceOnTheQuota(unittest.TestCase):
    """The clause asserted `used <= cap * 3` and failed at 3.7x -- on a run where
    containment had worked exactly as designed. Raising the multiple to fit would
    have been fitting the test to the data.

    What matters is that the host survives: the quota stops a runaway, and the
    dispatcher's floor keeps the filesystem usable while it is being stopped. A
    sampled quota cannot be exact; the floor does not depend on sampling."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(os.path.dirname(here),
                               "nc-suite-phase2.sh")) as fh:
            suite = fh.read()
        start = suite.index("disk flood finished")
        self.clause = suite[start:suite.index("NC15 a 0600 artifact", start)]

    def test_it_no_longer_asserts_a_multiple_of_the_quota(self):
        self.assertNotIn("* 3 ))", self.clause)

    def test_it_asserts_against_the_disk_floor(self):
        self.assertIn("QFD_DISK_FLOOR_GB", self.clause)
        self.assertIn("disk floor", self.clause)

    def test_the_overshoot_is_reported_rather_than_hidden(self):
        # Behind a tolerance, the quota's real meaning disappears.
        self.assertIn("overshoot:", self.clause)

    def test_the_kill_itself_is_still_asserted(self):
        # The floor check alone would pass for a job that was never stopped, as
        # long as it happened to stay small.
        self.assertIn("out_quota_exceeded", self.clause)


class TestClauseCSchedulesTheExitItMeasures(unittest.TestCase):
    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(os.path.dirname(here),
                               "nc-suite-phase2.sh")) as fh:
            suite = fh.read()
        start = suite.index("# (c) PER-DESCRIPTOR ownership")
        self.clause = suite[start:suite.index("# (f)", start)]

    def test_l1_is_cancelled_rather_than_waited_for(self):
        # Two test suites of similar duration make "wait for l1, hope l2 is still
        # running" a coin flip. Cancelling makes the exit something the clause
        # schedules.
        self.assertIn('qf cancel $l1', self.clause)

    def test_the_precondition_is_still_rechecked_afterwards(self):
        # Belt and braces: cancelling makes the race unlikely, not impossible.
        self.assertIn('state_of "$l2"', self.clause)


class TestTheProbeCommandAndFixture(unittest.TestCase):
    """Task 9's client half and Task 12's fixture. The distinction that matters:
    `probe` takes a `--sha` because it runs code, and `extract` does not because
    it does not."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.host = os.path.dirname(here)
        self.mod = {}
        with open(os.path.join(here, "qf")) as fh:
            exec(compile(fh.read(), "qf", "exec"), self.mod)  # noqa: S102

    def parse(self, argv):
        return self.mod["build_parser"](False).parse_args(argv)

    def test_probe_requires_a_commit_and_extract_does_not(self):
        base = ["probe", "--path", "research/experiments/x.py",
                "--extract", "c" * 64]
        with self.assertRaises(SystemExit):
            self.parse(base)                      # no --sha
        self.assertEqual(self.parse(base + ["--sha", "b" * 40]).sha, "b" * 40)
        with self.assertRaises(SystemExit):
            self.parse(["extract", "--target", "wait_time",
                        "--train-start", "2026-07-01T00:00:00Z",
                        "--as-of", "2026-08-01T00:00:00Z",
                        "--sha", "b" * 40])

    def test_the_extract_is_required(self):
        # A probe with no extract fails inside the container with a
        # FileNotFoundError about /extract; refusing here says what is wrong.
        with self.assertRaises(SystemExit):
            self.parse(["probe", "--sha", "b" * 40,
                        "--path", "research/experiments/x.py"])

    def test_the_help_points_at_where_to_find_one(self):
        # `qf extracts` is the answer to "what can I probe", so the flag says so.
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.suppress(SystemExit), \
                contextlib.redirect_stdout(buf):
            self.parse(["probe", "--help"])
        self.assertIn("qf extracts", buf.getvalue())

    def test_the_fixture_generator_exists_and_is_executable(self):
        script = os.path.join(self.host, "nc-fixtures-phase2b.sh")
        self.assertTrue(os.path.isfile(script), script)
        self.assertTrue(os.stat(script).st_mode & 0o111)

    def test_the_embedded_fixture_is_valid_python(self):
        # The NC17 probe shipped as `b'...' + b chr(10)`. A fixture that cannot
        # run is worse than none, because its failure reads as the thing it was
        # checking.
        with open(os.path.join(self.host, "nc-fixtures-phase2b.sh")) as fh:
            text = fh.read()
        marker = "cat > \"$EXP/extract_contract.py\" <<'EOF'\n"
        body = text[text.index(marker) + len(marker):]
        body = body[:body.index("\nEOF\n")]
        compile(body, "extract_contract.py", "exec")

    def test_the_fixture_asserts_the_clauses_nc13_cannot(self):
        # NC13 runs as a selftest, which has no extract -- so it asserts the
        # ABSENCE of one, and the presence clauses live in the probe fixture.
        with open(os.path.join(self.host, "nc-fixtures-phase2b.sh")) as fh:
            text = fh.read()
        for claim in ("is not writable", "DATABASE_URL is unset",
                      "no outbound network", "predictions.parquet"):
            with self.subTest(claim=claim):
                self.assertIn(claim, text)

    def test_the_generator_never_commits_or_pushes(self):
        # The branch is the operator's to publish with the agent's credential;
        # the dispatcher's token is read-only and must stay so.
        with open(os.path.join(self.host, "nc-fixtures-phase2b.sh")) as fh:
            lines = [l for l in fh.read().splitlines()
                     if not l.lstrip().startswith("#")]
        code = "\n".join(lines)
        # `git` appears only inside the printed instructions heredoc.
        for verb in ("git commit", "git push"):
            with self.subTest(verb=verb):
                self.assertNotRegex(code, rf"^\s*{verb}", )

    def test_nc13_asserts_the_data_plane_is_not_ambient(self):
        with open(os.path.join(self.host, "dispatcher",
                               "nc13-inside.sh")) as fh:
            inside = fh.read()
        self.assertIn("/extract is absent in a job that requested none", inside)
        self.assertIn("the data plane is ambient", inside)


class TestTheExtractorEnvironmentMustBeLocked(unittest.TestCase):
    """The installer generated a lock, printed "now commit it", and the lock is
    still not in the repository -- so that install path produced exactly the
    situation it was warning about, twice, while reporting success.

    A warning that has been ignored once is documentation; a refusal is a
    control."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.host = os.path.dirname(here)
        with open(os.path.join(self.host, "phase2b-setup.sh")) as fh:
            self.setup = fh.read()

    def test_a_missing_lock_is_a_refusal_not_a_warning(self):
        block = self.setup[self.setup.index("uv.lock"):]
        block = block[:block.index("# THE ONE SUBSTITUTION")]
        self.assertIn("die ", block)

    def test_the_escape_hatch_exists_so_a_first_lock_can_be_made(self):
        # Refusing outright would make the first lock impossible to create.
        self.assertIn("ALLOW_UNLOCKED_ENV", self.setup)

    def test_the_refusal_says_how_to_get_the_lock_into_the_repo(self):
        block = self.setup[self.setup.index("uv.lock is missing"):]
        block = block[:block.index('"\n')]
        for step in ("ALLOW_UNLOCKED_ENV=1", "git add", "cp "):
            with self.subTest(step=step):
                self.assertIn(step, block)

    def test_the_readme_no_longer_describes_the_old_warning(self):
        with open(os.path.join(self.host, "extractor", "env",
                               "README.md")) as fh:
            readme = fh.read()
        self.assertIn("REFUSES", readme)
        self.assertNotIn("prints a reminder to commit", readme)

    def test_the_lock_is_still_absent_so_this_test_still_matters(self):
        # When the lock lands, this flips -- and the flip is the signal that the
        # refusal can be relied on rather than merely present.
        lock = os.path.join(self.host, "extractor", "env", "uv.lock")
        if os.path.isfile(lock):
            self.skipTest("uv.lock is committed; the refusal is now the"
                          " uncommon path")
        self.assertFalse(os.path.isfile(lock))


class TestAnExtractPrefixResolvesInTheClient(unittest.TestCase):
    """`qf extracts` printed the truncated hash prominently and the full one only
    inside `dir=`, so the natural copy-paste was the value the validator refuses:

        $ qf probe --extract 8e94d833d4c6 ...
        qf: args.extract must be an extract request hash (64 lowercase hex)

    Fixed in two ways at once, and the split is the point. The LISTING now prints
    the full hash on its own line labelled with the flag it goes to; and a unique
    PREFIX resolves -- in the client. Ergonomics belong in the client, strictness
    belongs at the boundary: the dispatcher still receives 64 hex and still does
    not enumerate the extracts directory."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.mod = {}
        with open(os.path.join(here, "qf")) as fh:
            exec(compile(fh.read(), "qf", "exec"), self.mod)  # noqa: S102
        self.calls = []

        def fake_call(op, payload=None, **kw):
            self.calls.append(op)
            return {"ok": True, "extracts": [
                {"request_hash": "8e94d833d4c6" + "0" * 52},
                {"request_hash": "c179c7f5b961" + "1" * 52},
                {"request_hash": "c179c7f5b9ff" + "2" * 52},
            ]}

        self.mod["call"] = fake_call

    def test_a_full_hash_needs_no_lookup(self):
        full = "a" * 64
        self.assertEqual(self.mod["resolve_extract"](full), full)
        self.assertEqual(self.calls, [], "a full hash opened a socket")

    def test_a_unique_prefix_resolves(self):
        self.assertEqual(self.mod["resolve_extract"]("8e94d833"),
                         "8e94d833d4c6" + "0" * 52)
        self.assertEqual(self.calls, ["extracts"])

    def test_an_ambiguous_prefix_is_refused_and_lists_the_candidates(self):
        # Picking the first match would mean a two-character-shorter prefix
        # silently probing different data.
        with self.assertRaises(SystemExit):
            self.mod["resolve_extract"]("c179c7f5")

    def test_a_prefix_matching_nothing_is_refused(self):
        with self.assertRaises(SystemExit):
            self.mod["resolve_extract"]("deadbeef")

    def test_something_that_is_not_hex_is_refused_before_any_call(self):
        for bad in ("8e94", "", "ZZZZZZZZ", "g" * 12, "8e94-d833"):
            with self.subTest(given=bad):
                with self.assertRaises(SystemExit):
                    self.mod["resolve_extract"](bad)
        self.assertEqual(self.calls, [])

    def test_the_listing_prints_the_flag_and_the_full_value(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "qf")) as fh:
            source = fh.read()
        body = source[source.index("def cmd_extracts"):]
        body = body[:body.index("\ndef ")]
        self.assertIn("--extract {row.get('request_hash')}", body)

    def test_the_dispatcher_still_receives_a_full_hash(self):
        # The strictness did not move. `spec._check_probe_args` refuses anything
        # that is not 64 hex, and this is what keeps that true after the client
        # got friendlier.
        import spec
        with self.assertRaises(spec.SpecError):
            spec.normalize({"schema": 1, "kind": "probe",
                            "source_sha": "b" * 40,
                            "args": {"path": "research/experiments/x.py",
                                     "extract": "8e94d833"}})


class TestTheFixtureAgreesWithTheMountConstants(unittest.TestCase):
    """The fixture asserts `/app/trainer/trainer/data` is writable, and the
    dispatcher mounts it there. Two literals for one location is how they came to
    disagree the first time, so a test holds them together."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.host = os.path.dirname(here)
        with open(os.path.join(self.host, "nc-fixtures-phase2b.sh")) as fh:
            self.gen = fh.read()

    def test_the_fixtures_data_path_is_the_declared_destination(self):
        import sandbox
        self.assertIn(f'DATA = "{sandbox.DATA_DEST}"', self.gen)

    def test_the_fixtures_extract_path_is_the_declared_destination(self):
        import sandbox
        self.assertIn(f'EXTRACT = "{sandbox.EXTRACT_DEST}"', self.gen)

    def test_the_generator_says_an_earlier_push_must_be_replaced(self):
        # A fixture already pushed with the wrong path would keep failing, and
        # the failure would look like a dispatcher bug.
        self.assertIn("RE-RUN THIS", self.gen)


class TestTheBaselinePromoterIsWiredAndItsCheckIsReal(unittest.TestCase):
    """Task 13. The store must sit outside the deployment domain's write access,
    or the domain that PRODUCES baselines can rewrite PUBLISHED ones and
    "immutable" rests on nobody choosing to."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.host = os.path.dirname(here)
        with open(os.path.join(self.host, "promote-baseline.sh")) as fh:
            self.script = fh.read()

    def test_the_promoter_exists_and_is_executable(self):
        path = os.path.join(self.host, "promote-baseline.sh")
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(os.stat(path).st_mode & 0o111)

    def test_the_write_check_is_bitwise_not_a_glob(self):
        # The first version was `case "$mode" in *[2367])`, which matches only
        # the LAST digit: it caught other-writable and let 0775 through.
        self.assertIn("8#022", code_only(self.script))
        # Against the CODE: the glob appears in the comment explaining why it was
        # replaced, and asserting over prose would fail on the fix.
        self.assertNotIn("*[2367]", code_only(self.script))

    def test_it_refuses_a_store_it_does_not_expect_the_owner_of(self):
        self.assertIn("QF_BASELINE_STORE_OWNER", self.script)
        self.assertIn("outside the deployment domain's write access",
                      self.script)

    def test_publication_is_one_atomic_rename(self):
        self.assertIn("mv -T", self.script)
        self.assertIn(".staging", self.script)

    def test_it_copies_only_the_files_the_manifest_names(self):
        # Copying the directory wholesale would publish whatever else was in it,
        # and the validation exists precisely to decide what belongs.
        self.assertIn('json.load(sys.stdin)["files"]', self.script)

    def test_a_second_promotion_is_a_no_op(self):
        self.assertIn("already published", self.script)

    def test_it_is_fail_closed(self):
        self.assertIn("set -Eeuo pipefail", self.script)
        self.assertIn("trap ", self.script)

    def test_baseline_py_is_stdlib_only(self):
        # The promoter runs it with the SYSTEM python: no venv, no dependency on
        # the extractor's environment, so promotion works on a host where the
        # extractor was never installed.
        with open(os.path.join(self.host, "shared", "baseline.py")) as fh:
            source = fh.read()
        imports = re.findall(r"^\s*(?:import|from)\s+([a-zA-Z_][\w.]*)",
                             source, re.M)
        for name in imports:
            with self.subTest(module=name):
                self.assertIn(name.split(".")[0],
                              {"hashlib", "json", "os", "re", "__future__"})

    def test_the_promoter_has_its_own_test_suite(self):
        script = os.path.join(self.host, "tests", "test_promote_baseline.sh")
        self.assertTrue(os.path.isfile(script), script)
        self.assertTrue(os.stat(script).st_mode & 0o111)
        p = subprocess.run([script], capture_output=True, text=True,
                           timeout=180)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        # `fail=0` AND a floor on the pass count, rather than an exact figure.
        # The exact form made every added clause a two-place edit, and the second
        # place is a Python test that has nothing to say about the promoter. What
        # this needs to catch is a suite that stopped running or stopped
        # asserting -- a floor catches both, and 0/0 passing catches neither.
        self.assertIn("fail=0", p.stdout)
        count = int(re.search(r"promote-baseline: pass=(\d+)", p.stdout).group(1))
        self.assertGreaterEqual(count, 17, p.stdout)


class TestTheBaselinesListing(ProtocolCase):
    """Task 15. Read here rather than relayed, and the asymmetry with `extracts`
    is deliberate: the extracts directory belongs to another privilege domain,
    while the baseline store has no service at all -- it is root-owned and
    written by a human running promote-baseline.sh. qfd already reads it to
    resolve a mount, so this is not a second reader."""

    def setUp(self):
        super().setUp()
        self.store = os.path.join(self.tmp.name, "qf-baselines")
        os.makedirs(self.store)
        self.cfg.baselines_dir = self.store

    def promote(self, *, promoted_at="2026-08-29T00:00:00Z", **over):
        import baseline as baseline_mod
        manifest = {
            "schema": 1,
            "files": {"baseline_predictions.ndjson":
                      {"sha256": "a" * 64, "bytes": 12}},
            "days": ["2026-08-01", "2026-08-02"],
            "ndjson_rows": 11,
            "pending_at_min": "2026-08-01T00:00:00+00:00",
            "pending_at_max": "2026-08-02T23:59:59+00:00",
            "exclude_dates": [],
            "exclude_dates_provenance": "declared by the promoter",
        }
        manifest.update(over)
        digest = baseline_mod.baseline_hash(manifest)
        manifest["baseline_hash"] = digest
        path = os.path.join(self.store, digest)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "MANIFEST.json"), "w") as fh:
            json.dump(manifest, fh)
        if promoted_at:
            with open(os.path.join(path, "PROMOTED_AT"), "w") as fh:
                fh.write(promoted_at + "\n")
        return digest, path

    def test_an_empty_store_is_an_empty_list_not_an_error(self):
        resp = self.do("baselines")
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["baselines"], [])

    def test_a_store_that_does_not_exist_yet_is_not_an_error_either(self):
        # Before the first promotion the directory may simply not be there, and
        # `qf baselines` is exactly the command someone runs to find that out.
        self.cfg.baselines_dir = os.path.join(self.tmp.name, "nope")
        resp = self.do("baselines")
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["baselines"], [])

    def test_a_promoted_baseline_is_reported_with_its_coverage(self):
        digest, _path = self.promote()
        rows = self.do("baselines")["baselines"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["baseline_hash"], digest)
        self.assertEqual(row["days"], 2)
        self.assertEqual(row["ndjson_rows"], 11)
        self.assertEqual(row["promoted_at"], "2026-08-29T00:00:00Z")
        self.assertNotIn("broken", row)

    def test_a_manifest_that_no_longer_hashes_to_its_name_is_reported_broken(self):
        # NOT omitted. A half-promoted or edited directory has to be visible to
        # the one command an operator runs to find out why a probe refused; an
        # omitted row makes it look like the hash was never promoted at all.
        digest, path = self.promote()
        with open(os.path.join(path, "MANIFEST.json")) as fh:
            manifest = json.load(fh)
        manifest["ndjson_rows"] = 99
        with open(os.path.join(path, "MANIFEST.json"), "w") as fh:
            json.dump(manifest, fh)
        rows = self.do("baselines")["baselines"]
        self.assertEqual(len(rows), 1)
        self.assertIn("does not hash", rows[0]["broken"])

    def test_an_unreadable_manifest_is_reported_broken_too(self):
        digest, path = self.promote()
        with open(os.path.join(path, "MANIFEST.json"), "w") as fh:
            fh.write("{nope")
        rows = self.do("baselines")["baselines"]
        self.assertIn("unreadable", rows[0]["broken"])

    def test_a_directory_with_no_manifest_at_all_is_reported_broken(self):
        os.makedirs(os.path.join(self.store, "b" * 64))
        rows = self.do("baselines")["baselines"]
        self.assertEqual(len(rows), 1)
        self.assertIn("unreadable", rows[0]["broken"])

    def test_the_stores_own_scratch_is_not_a_row(self):
        # promote-baseline.sh stages under `.staging.<hash>.<pid>` and renames.
        # A staging directory caught mid-promotion is not a baseline and is not
        # an error; reporting it broken would cry wolf on a healthy promotion.
        os.makedirs(os.path.join(self.store, ".staging." + "a" * 64 + ".77"))
        os.makedirs(os.path.join(self.store, "README"))
        self.assertEqual(self.do("baselines")["baselines"], [])

    def test_exclude_dates_are_reported_because_they_were_declared(self):
        # Not derivable from the files, so a listing that hid them would present
        # a choice as though it had none.
        self.promote(exclude_dates=["2026-07-04"])
        rows = self.do("baselines")["baselines"]
        self.assertEqual(rows[0]["exclude_dates"], ["2026-07-04"])

    def test_the_listing_is_ordered_by_promotion_time(self):
        first, _ = self.promote(promoted_at="2026-08-01T00:00:00Z",
                                ndjson_rows=1)
        second, _ = self.promote(promoted_at="2026-08-20T00:00:00Z",
                                 ndjson_rows=2)
        order = [r["baseline_hash"] for r in self.do("baselines")["baselines"]]
        self.assertEqual(order, [first, second])

    def test_a_truncated_listing_says_so(self):
        # A listing that quietly stops is how a prefix resolves to "no match"
        # for a baseline that is right there.
        for n in range(3):
            self.promote(ndjson_rows=n)
        with mock.patch.object(qfd.Dispatcher, "BASELINES_LIMIT", 2):
            resp = self.do("baselines")
        self.assertEqual(len(resp["baselines"]), 2)
        self.assertTrue(resp["truncated"])
        self.assertEqual(resp["published"], 3)

    def test_a_full_listing_says_it_is_not_truncated(self):
        self.promote()
        resp = self.do("baselines")
        self.assertFalse(resp["truncated"])

    def test_promoted_at_is_a_sidecar_never_the_directory_mtime(self):
        # An mtime survives a filesystem copy as a confident wrong answer, and
        # the promotion time cannot live in the manifest: the manifest IS the
        # content key, so a timestamp inside it would make every promotion of
        # the same bytes a different artifact.
        digest, path = self.promote(promoted_at="")
        rows = self.do("baselines")["baselines"]
        self.assertIsNone(rows[0]["promoted_at"])
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "qfd.py")) as fh:
            body = fh.read()
        resolve = body[body.index("def _promoted_at("):]
        resolve = resolve[:resolve.index("\n    def ")]
        self.assertNotIn("getmtime", code_only(resolve))
        self.assertNotIn("st_mtime", code_only(resolve))

    def test_the_promoter_writes_the_sidecar_the_listing_reads(self):
        # Pinned together: a sidecar nothing writes reports "unknown" for every
        # baseline, which reads like a store that has never been promoted to.
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(os.path.dirname(here),
                               "promote-baseline.sh")) as fh:
            promoter = fh.read()
        self.assertIn('"$STAGING/PROMOTED_AT"', promoter)
        # And it is written INTO THE STAGING directory, so it lands by the same
        # atomic rename as everything else -- not after it, where a reader could
        # see a published baseline without one.
        staging = promoter.index('mkdir -p "$STAGING"')
        self.assertLess(promoter.index('"$STAGING/PROMOTED_AT"'),
                        promoter.index('mv -T "$STAGING"'))
        self.assertGreater(promoter.index('"$STAGING/PROMOTED_AT"'), staging)

    def test_it_is_on_the_client_op_table_not_the_admin_one(self):
        # Research picks a baseline; a listing behind qfadmin would mean asking
        # an operator which hash to type.
        self.assertIn("baselines", qfd.CLIENT_OPS)
        self.assertNotIn("baselines", qfd.ADMIN_OPS)


class TestABaselinePrefixResolvesInTheClient(unittest.TestCase):
    """The same ergonomics as an extract prefix, over the same code: one
    algorithm, two thin wrappers. Two copies would be two places for the
    ambiguity refusal to drift out of, and an ambiguous baseline is worse than an
    ambiguous extract -- a probe whose comparison shifted underneath it still
    produces plausible numbers."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.mod = {}
        with open(os.path.join(here, "qf")) as fh:
            exec(compile(fh.read(), "qf", "exec"), self.mod)  # noqa: S102
        self.calls = []

        def fake_call(op, payload=None, **kw):
            self.calls.append(op)
            return {"ok": True, "baselines": [
                {"baseline_hash": "1a2b3c4d5e6f" + "0" * 52},
                {"baseline_hash": "9f8e7d6c5b4a" + "1" * 52},
                {"baseline_hash": "9f8e7d6c5bff" + "2" * 52},
            ]}

        self.mod["call"] = fake_call

    def test_a_full_hash_needs_no_lookup(self):
        full = "a" * 64
        self.assertEqual(self.mod["resolve_baseline"](full), full)
        self.assertEqual(self.calls, [])

    def test_a_unique_prefix_resolves_against_the_baselines_op(self):
        self.assertEqual(self.mod["resolve_baseline"]("1a2b3c4d"),
                         "1a2b3c4d5e6f" + "0" * 52)
        # The BASELINES op, not `extracts`: a resolver reading the wrong listing
        # would refuse every valid prefix and resolve none.
        self.assertEqual(self.calls, ["baselines"])

    def test_an_ambiguous_prefix_is_refused(self):
        with self.assertRaises(SystemExit):
            self.mod["resolve_baseline"]("9f8e7d6c")

    def test_a_prefix_matching_nothing_names_the_promoter(self):
        with self.assertRaises(SystemExit):
            self.mod["resolve_baseline"]("deadbeef")

    def test_non_hex_is_refused_before_any_call(self):
        for bad in ("1a2b", "", "ZZZZZZZZ", "g" * 12, "1a2b-3c4d"):
            with self.subTest(given=bad), self.assertRaises(SystemExit):
                self.mod["resolve_baseline"](bad)
        self.assertEqual(self.calls, [])

    def test_the_two_resolvers_share_one_implementation(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "qf")) as fh:
            source = code_only(fh.read())
        for name in ("resolve_extract", "resolve_baseline"):
            body = source[source.index(f"def {name}(given):"):]
            body = body[:body.index("\ndef ")]
            self.assertIn("_resolve_prefix(", body)

    def test_the_listing_prints_the_flag_and_the_full_value(self):
        # Same fix as `qf extracts`: printing only the short form made the
        # natural copy-paste the value the validator refuses.
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "qf")) as fh:
            source = fh.read()
        body = source[source.index("def cmd_baselines"):]
        body = body[:body.index("\ndef ")]
        self.assertIn("--baseline {row.get('baseline_hash')}", body)

    def test_probe_carries_the_baseline_only_when_one_was_given(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "qf")) as fh:
            body = code_only(fh.read())
        probe = body[body.index("def cmd_probe(args):"):]
        probe = probe[:probe.index("\ndef ")]
        self.assertIn("if args.baseline:", probe)
        self.assertIn("resolve_baseline(args.baseline)", probe)

    def test_the_flag_is_optional_on_the_parser(self):
        parser = self.mod["build_parser"](False)   # not admin_mode
        ns = parser.parse_args(["probe", "--sha", "b" * 40, "--path",
                                "research/experiments/x.py",
                                "--extract", "c" * 64])
        self.assertIsNone(ns.baseline)


class TestNc19IsWiredAndItsClaimsAreEarned(unittest.TestCase):
    """Task 16. NC19 is the baseline half of NC18's argument, and the same three
    instrument rules apply: a canary gates the group, a negative claim names what
    it observed, and nothing the suite breaks is left broken."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.host = os.path.dirname(here)
        with open(os.path.join(self.host, "nc-suite-phase2.sh")) as fh:
            self.suite = fh.read()
        body = self.suite[self.suite.index("nc19() {"):]
        self.nc19 = code_only(body[:body.index("\n}\n")])

    def test_it_actually_runs(self):
        # A clause nothing calls is a clause that passes for ever.
        assert_clause_runs(self, self.suite, "nc19")

    def test_the_group_is_gated_on_a_baseline_promoting_at_all(self):
        # Every refusal below is measured against a promotion that worked.
        self.assertIn("NC19 canary: a baseline promotes", self.nc19)
        canary = self.nc19.index("NC19 canary: the promoter published nothing")
        first_claim = self.nc19.index("NC19 (a)")
        self.assertLess(canary, first_claim)

    def test_the_fixture_is_canaried_before_any_claim(self):
        # If `baseline_contract.py` is not on the fixture branch, every probe
        # below fails for that reason -- and the clauses would report it as the
        # baseline mount being broken. A control blamed for a fixture nobody
        # pushed costs an investigation into working code.
        canary = self.nc19.index("printed no BASELINE-CONTRACT summary")
        for claim in ("NC19 (a)", "NC19 (b)", "NC19 (c)", "NC19 (d)"):
            self.assertLess(canary, self.nc19.index(claim), claim)
        # And it names the remedy, because "void" without one is a dead end.
        window = self.nc19[canary:canary + 400]
        self.assertIn("nc-fixtures-phase2b.sh", window)

    def test_the_canary_failures_are_void_and_return(self):
        for reason in ("the promoter published nothing",
                       "no extract is published",
                       "printed no BASELINE-CONTRACT summary"):
            i = self.nc19.index(reason)
            # Scoped to the enclosing branch rather than a byte window: the
            # multi-line void messages pushed `return` past a fixed +200, so a
            # window would have to grow every time a message did.
            branch = self.nc19[i:self.nc19.index("\n  fi", i)]
            self.assertIn("void", self.nc19[i - 200:i], reason)
            self.assertIn("return", branch, reason)

    def test_double_promotion_is_asserted_on_bytes_not_only_on_a_message(self):
        # "already published" is what the promoter SAYS. The artifact not having
        # changed is what it DID, and only the second is the property.
        self.assertIn("sha256sum", self.nc19)
        self.assertIn('[ "$before" = "$after" ]', self.nc19)

    def test_the_edited_baseline_clause_leaves_the_manifest_edit_alone(self):
        # An edit that also rewrote `baseline_hash` would be caught by the
        # cheaper directory-name check, so the clause would pass without ever
        # exercising the recomputation it exists to prove.
        self.assertIn("leaves baseline_hash", self.nc19)
        self.assertNotIn('m["baseline_hash"]', self.nc19)

    def test_it_restores_what_it_broke_and_checks_the_restore(self):
        # A suite that leaves a baseline broken makes every later run fail for a
        # reason this clause caused -- and NC15's disk flood taught that the
        # check has to be asserted, not assumed.
        self.assertIn("the store was left intact", self.nc19)
        self.assertLess(self.nc19.index('cp -p "$saved" "$manifest"'),
                        self.nc19.index("the store was left intact"))

    def test_the_present_flag_is_asserted_in_both_directions(self):
        # The fixture reports what it saw; the suite claims it against what it
        # asked for. Asserting only the present=1 run would leave "a baseline
        # must not be ambient" untested, which is the half a leak would show up
        # in.
        self.assertIn("present=1", self.nc19)
        self.assertIn("present=0", self.nc19)

    def test_the_summary_pattern_requires_zero_failures(self):
        # `grep BASELINE-CONTRACT` alone matches a summary reporting failures.
        for pattern in ("present=1 pass=[0-9]* fail=0",
                        "present=0 pass=[0-9]* fail=0"):
            self.assertIn(pattern, self.nc19)

    def test_the_provenance_pins_are_asserted_not_just_the_state(self):
        self.assertIn('pin_of "$rid" baseline_hash', self.nc19)
        self.assertIn('pin_of "$rid" baseline', self.nc19)

    def test_the_refusal_is_timed_against_the_build_it_must_precede(self):
        self.assertIn("BUILD_SETTLE_S", self.nc19)

    def test_the_pin_helper_has_one_implementation(self):
        # NC18 carried two copies of the same six lines, and NC19 would have
        # made four. A pin reader that disagrees with itself across clauses is
        # how a missing pin comes to look like a different pin.
        code = code_only(self.suite)
        self.assertEqual(code.count("json.load(sys.stdin)['job'].get('pins')"), 1)


class TestTheBaselineProbeFixtureObservesRatherThanAssumes(unittest.TestCase):
    """The fixture cannot know whether its run asked for a baseline -- only the
    submitter knows -- so it prints `present=` and the suite makes the claim."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(os.path.dirname(here),
                               "nc-fixtures-phase2b.sh")) as fh:
            gen = fh.read()
        body = gen.split('cat > "$EXP/baseline_contract.py" <<\'EOF\'\n', 1)[1]
        self.fixture = body.split("\nEOF\n", 1)[0]

    def test_it_is_valid_python(self):
        import ast
        ast.parse(self.fixture)

    def test_it_is_stdlib_only(self):
        # It runs inside the sandbox with --network none against a read-only
        # tree; a dependency would fail at import for a reason unrelated to the
        # contract it is asserting.
        imports = re.findall(r"^\s*(?:import|from)\s+([a-zA-Z_][\w.]*)",
                             self.fixture, re.M)
        for name in imports:
            with self.subTest(module=name):
                self.assertIn(name.split(".")[0],
                              {"hashlib", "json", "os", "sys"})

    def test_it_reports_what_it_saw_rather_than_what_it_expected(self):
        self.assertIn("present={int(present)}", self.fixture)

    def test_the_absent_case_is_a_pass_not_a_failure(self):
        # A non-residual cohort reads no baseline; a fixture that failed on
        # absence would make that legitimate probe unrunnable.
        i = self.fixture.index("is absent, as it must be")
        self.assertIn("ok(", self.fixture[i - 120:i])

    def test_the_write_attempt_is_the_clause_that_matters(self):
        self.assertIn(".probe-write", self.fixture)
        i = self.fixture.index("is WRITABLE")
        self.assertIn("bad(", self.fixture[i - 120:i])

    def test_it_verifies_the_digests_not_only_the_sizes(self):
        # The manifest carries a sha256 per file, and this is the only place it
        # is ever checked against the bytes. A size check passes on a file of
        # the right length and the wrong content.
        self.assertIn("hashlib.sha256()", self.fixture)
        self.assertIn('entry.get("sha256")', self.fixture)

    def test_its_canonical_form_matches_the_trusted_module_byte_for_byte(self):
        # A SECOND implementation, deliberately -- agent-authored code inside the
        # sandbox cannot import the trusted module. Pinned here so a change to
        # one is a failure rather than a silent divergence, which would surface
        # as "the mounted baseline does not hash to its identity" on every probe.
        import baseline as baseline_mod
        namespace = {}
        exec(compile(self.fixture.replace('sys.exit(main())', 'pass'),
                     "baseline_contract.py", "exec"), namespace)  # noqa: S102
        sample = {"schema": 1, "days": ["2026-08-01"], "ndjson_rows": 3,
                  "files": {"x": {"sha256": "a" * 64}},
                  "exclude_dates": [], "baseline_hash": "ignored"}
        self.assertEqual(namespace["canonical"](sample),
                         baseline_mod.canonical(
                             {k: v for k, v in sample.items()
                              if k != "baseline_hash"}))

    def test_the_generator_tells_the_operator_to_add_it(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(os.path.dirname(here),
                               "nc-fixtures-phase2b.sh")) as fh:
            gen = fh.read()
        # A fixture the operator never pushes is a clause that voids on a host
        # where everything works.
        self.assertIn("research/experiments/baseline_contract.py",
                      gen[gen.index("git add"):])


class TestQfdCannotWriteTheFrozenStores(unittest.TestCase):
    """The read-only intent is enforced by the unit, not only by the code.

    `_probe_baseline` and `_probe_extract` only read, and `promote-baseline.sh`
    is the only writer -- but "only reads" is a property of today's code, and the
    two stores are the inputs every recorded result cites. `ProtectSystem=strict`
    plus a `ReadWritePaths=` that omits them makes a write impossible rather than
    merely unintended, so a future bug in qfd cannot corrupt an artifact a
    published comparison was measured against.
    """

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "qf-dispatch.service")) as fh:
            self.unit = fh.read()
        self.rw = [line.split("=", 1)[1].split()
                   for line in self.unit.splitlines()
                   if line.startswith("ReadWritePaths=")]

    def test_the_filesystem_is_read_only_by_default(self):
        self.assertIn("ProtectSystem=strict", self.unit)

    def test_neither_frozen_store_is_writable(self):
        writable = {path for group in self.rw for path in group}
        for store in ("/var/lib/qf-baselines", "/var/lib/qf-extracts"):
            with self.subTest(store=store):
                self.assertNotIn(store, writable)
                # Nor via a parent: `/var/lib` in ReadWritePaths would grant both.
                for granted in writable:
                    self.assertFalse(
                        store.startswith(granted.rstrip("/") + "/"),
                        f"{granted} grants write access to {store}")

    def test_the_paths_the_unit_names_are_the_ones_the_config_defaults_to(self):
        # A hardening assertion about a path nothing reads proves nothing.
        cfg = qfd.Config.from_env({"QFD_ADMIN_UID": "1001"})
        self.assertEqual(cfg.baselines_dir, "/var/lib/qf-baselines")
        self.assertEqual(cfg.extracts_dir, "/var/lib/qf-extracts")
        for path in (cfg.baselines_dir, cfg.extracts_dir):
            self.assertIn(f"={path}", self.unit)


class TestFromEnvActuallyReadsTheEnvItWasGiven(unittest.TestCase):
    """`from_env(env)` took an argument that its integer reads and three of its
    string reads ignored -- they went straight to `os.environ`. A parameter that
    looks like an injection point and is not is the same defect as an injected
    clock nothing consults: it makes the object look testable and the test
    meaningless, because the values under test came from the process."""

    BASE = {"QFD_ADMIN_UID": "1001"}

    def test_every_integer_knob_comes_from_the_mapping(self):
        cfg = qfd.Config.from_env({**self.BASE, "QFD_DISK_FLOOR_GB": "77",
                                   "QFD_LOG_CAP_MB": "3"})
        self.assertEqual(cfg.disk_floor_gb, 77)
        self.assertEqual(cfg.log_cap_mb, 3)

    def test_every_path_knob_comes_from_the_mapping(self):
        cfg = qfd.Config.from_env({**self.BASE,
                                   "QFD_BASELINES_DIR": "/tmp/b",
                                   "QFD_EXTRACTS_DIR": "/tmp/e",
                                   "QFD_EXTRACT_SOCKET": "/tmp/s"})
        self.assertEqual(cfg.baselines_dir, "/tmp/b")
        self.assertEqual(cfg.extracts_dir, "/tmp/e")
        self.assertEqual(cfg.extract_socket, "/tmp/s")

    def test_the_process_environment_does_not_leak_in(self):
        # The assertion that would have failed before: with the mapping silent
        # about a knob, the value must be the DEFAULT, not whatever the process
        # happens to carry.
        with mock.patch.dict(os.environ, {"QFD_BASELINES_DIR": "/leaked",
                                          "QFD_DISK_FLOOR_GB": "999"}):
            cfg = qfd.Config.from_env(self.BASE)
        self.assertEqual(cfg.baselines_dir, "/var/lib/qf-baselines")
        self.assertEqual(cfg.disk_floor_gb, 20)

    def test_a_missing_required_knob_still_refuses(self):
        # Threading the mapping must not turn a fatal missing value into a
        # silent default: every one of these is a control whose absence is quiet.
        with self.assertRaises(qfd.ConfigError) as cm:
            qfd.Config.from_env({})
        self.assertIn("QFD_ADMIN_UID", str(cm.exception))

    def test_no_read_inside_from_env_bypasses_the_mapping(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "qfd.py")) as fh:
            source = fh.read()
        body = source[source.index("    def from_env(cls, env=None):"):]
        body = code_only(body[:body.index("        cfg.check_deadline_chain()")])
        # Exactly one: the `env if env is not None else os.environ` default.
        self.assertEqual(body.count("os.environ"), 1, body)


class TestTheSuiteNeverGuardsOnARunDirectoryAtSubmitTime(unittest.TestCase):
    """A submitted job is QUEUED and has NO run directory: it is created by
    `prepare_run_dir` during execute, after the lease.

    Four NC19 clauses guarded on `[ -d "$RUNS_DIR/$rid" ]` immediately after
    submitting. On the host that voided the group's canary on a working probe --
    the message read "produced no run id" and then printed one -- and in the
    unpromoted-baseline clause it printed `ok "never became a run"` for a job the
    dispatcher had accepted and was about to start. A positive claim about
    absence, resting on a directory that does not exist yet.
    """

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.host = os.path.dirname(here)
        with open(os.path.join(self.host, "nc-suite-phase2.sh")) as fh:
            self.suite = fh.read()
        self.code = code_only(self.suite)

    def test_no_clause_tests_for_a_run_directory_right_after_submitting(self):
        self.assertNotIn('[ -d "$RUNS_DIR/$rid" ]', self.code)

    def test_the_shape_check_exists_and_reads_no_filesystem(self):
        body = self.code[self.code.index("is_run_id() {"):]
        body = body[:body.index("\n}")]
        for probe in ("-d ", "-e ", "RUNS_DIR", "ls ", "stat "):
            self.assertNotIn(probe, body, f"is_run_id consults {probe!r}")

    def test_it_matches_what_make_run_id_actually_mints(self):
        # Grounded in the generator, not in a guessed length. The first version
        # used an 8-character floor, which rejected nothing the suite actually
        # has to reject: the client's one-line error messages are all longer.
        run_id = qfd.make_run_id("probe", "9d54e39271d7" + "0" * 28, 4290,
                                 now=1756470000)
        self.assertRegex(run_id, r"^probe-\d{8}T\d{6}Z-[0-9a-f]{12}-4290$")
        script = ("set -u\n"
                  + self.code[self.code.index("is_run_id() {"):
                              self.code.index("\n}", self.code.index(
                                  "is_run_id() {")) + 2]
                  + f'\nis_run_id {run_id!r}\n')
        self.assertEqual(subprocess.run(["bash", "-c", script]).returncode, 0,
                         run_id)

    def test_it_rejects_the_client_error_that_was_being_accepted(self):
        script_head = self.code[self.code.index("is_run_id() {"):]
        script_head = script_head[:script_head.index("\n}") + 2]
        for junk in ("qf: error: unrecognized arguments: --baseline",
                     "no dispatcher socket at /run/qf-dispatch/client/sock",
                     "submit refused"):
            with self.subTest(junk=junk):
                script = f"set -u\n{script_head}\nis_run_id {junk!r}\n"
                self.assertEqual(
                    subprocess.run(["bash", "-c", script]).returncode, 1, junk)


class TestNc18GenerationClauseIsRerunnable(unittest.TestCase):
    """It required the extract directory COUNT to increase, which made it valid
    exactly once per host: extracts are immutable and reused by `request_hash`
    (D20), so on the second run generation 2 is already published, the extraction
    is a reuse hit, and no directory appears. It passed on its first run and then
    reported a failure -- because the reuse it exists to protect was working.

    The count was never the property either: it would rise for any unrelated
    extract published in the same interval, and it says nothing about the two
    artifacts being DIFFERENT.
    """

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(os.path.dirname(here),
                               "nc-suite-phase2.sh")) as fh:
            suite = fh.read()
        body = suite[suite.index("nc18() {"):]
        self.nc18 = code_only(body[:body.index("\nnc19() {")])

    def test_separateness_is_asserted_on_the_recorded_identities(self):
        self.assertIn('pin_of "$rid" request_hash', self.nc18)
        self.assertIn('pin_of "$rid3" request_hash', self.nc18)

    def test_it_no_longer_depends_on_a_directory_count_increasing(self):
        self.assertNotIn("count2", self.nc18)
        self.assertNotIn('-gt "$count"', self.nc18)

    def test_both_generations_are_required_to_survive(self):
        # "different hashes" alone would pass if generation 2 had replaced
        # generation 1, which is the failure immutability exists to prevent.
        self.assertIn('[ ! -d "$d1" ] || [ ! -d "$d2" ]', self.nc18)

    def test_a_missing_pin_is_void_rather_than_a_failed_control(self):
        i = self.nc18.index("recorded no request_hash")
        self.assertIn("void", self.nc18[i - 120:i])

    def test_generation_really_is_part_of_the_request_identity(self):
        # The clause's premise, checked against the validator rather than
        # assumed: if generation were not in the hash, the clause would be
        # asserting something the design does not provide.
        import extract_spec
        base = {"schema": 1,
                "target": "wait_time", "train_start": "2026-07-21T00:00:00Z",
                "as_of_date": "2026-08-26T00:00:00Z", "lookback_days": 30}
        now = datetime.datetime(2026, 8, 29, tzinfo=datetime.timezone.utc)
        one = extract_spec.validate({**base, "generation": 1}, now=now,
                                    settlement_lag_s=48 * 3600)
        two = extract_spec.validate({**base, "generation": 2}, now=now,
                                    settlement_lag_s=48 * 3600)
        self.assertNotEqual(extract_spec.request_hash(one),
                            extract_spec.request_hash(two))


class TestTheContractsListingAndClient(ProtocolCase):
    """Task 20. `qf contracts`, and `--contract` resolving a prefix the same way
    `--extract` and `--baseline` do."""

    def setUp(self):
        super().setUp()
        self.dir = os.path.join(self.tmp.name, "contracts")
        os.makedirs(self.dir)
        self.cfg.contracts_dir = self.dir

    def write(self, name="wait_time.v1.json", **over):
        import contract as contract_mod
        body = {"schema": 1, "name": "wait_time_v1", "target": "wait_time",
                "baseline_hash": "a" * 64,
                "primary_slice": {"reason_resolved": ["completed"]},
                "metrics": {"mae": {"direction": "lower_is_better",
                                    "bar": {"kind": "relative_improvement",
                                            "value": 0.15}}},
                "consistency": {"days_required": 3}, "holdout_days": 5}
        body.update(over)
        body = contract_mod.validate(body)
        digest = contract_mod.contract_hash(body)
        body["contract_hash"] = digest
        with open(os.path.join(self.dir, name), "w") as fh:
            json.dump(body, fh)
        return digest

    def test_it_lists_a_contract_with_its_file(self):
        digest = self.write()
        resp = self.do("contracts")
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["contracts"],
                         [{"contract_hash": digest, "file": "wait_time.v1.json"}])

    def test_an_empty_directory_is_not_an_error(self):
        resp = self.do("contracts")
        self.assertEqual(resp["contracts"], [])

    def test_it_is_a_client_op_not_an_admin_one(self):
        # Research picks a contract to be judged by; behind qfadmin it would
        # mean asking an operator which hash to type.
        self.assertIn("contracts", qfd.CLIENT_OPS)
        self.assertNotIn("contracts", qfd.ADMIN_OPS)

    def test_the_client_prints_the_full_hash_on_its_own_line(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "qf")) as fh:
            body = fh.read()
        cmd = body[body.index("def cmd_contracts"):]
        cmd = cmd[:cmd.index("\ndef ")]
        self.assertIn("--contract {row.get('contract_hash')}", cmd)

    def test_the_empty_listing_explains_that_a_template_is_not_a_contract(self):
        # The likeliest reason the list is empty, and the one an operator cannot
        # guess: a `.json.in` carries no pinned baseline, so it judges nothing.
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "qf")) as fh:
            body = fh.read()
        cmd = body[body.index("def cmd_contracts"):]
        cmd = cmd[:cmd.index("\ndef ")]
        self.assertIn("instantiate-contract.sh", cmd)

    def test_a_contract_prefix_resolves_through_the_shared_resolver(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        mod = {}
        with open(os.path.join(here, "qf")) as fh:
            exec(compile(fh.read(), "qf", "exec"), mod)      # noqa: S102
        calls = []

        def fake_call(op, payload=None, **kw):
            calls.append(op)
            return {"ok": True, "contracts": [
                {"contract_hash": "1234abcd" + "0" * 56},
                {"contract_hash": "9999beef" + "1" * 56},
                {"contract_hash": "9999beef" + "2" * 56}]}
        mod["call"] = fake_call
        self.assertEqual(mod["resolve_contract"]("1234abcd"),
                         "1234abcd" + "0" * 56)
        self.assertEqual(calls, ["contracts"])
        # Ambiguity is a refusal, not a guess: a shorter prefix silently
        # selecting a different RULE is the worst of the three resolvers to get
        # wrong, because the verdict still looks like a verdict.
        with self.assertRaises(SystemExit):
            mod["resolve_contract"]("9999beef")

    def test_evaluate_sends_no_bar_and_no_baseline(self):
        # Everything about HOW a result is judged lives in the contract. A
        # caller that could pass a bar could pass its own bar.
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "qf")) as fh:
            body = code_only(fh.read())
        cmd = body[body.index("def cmd_evaluate"):]
        cmd = cmd[:cmd.index("\ndef ")]
        spec_body = cmd[cmd.index('"args"'):cmd.index("}")]
        for forbidden in ("baseline", "mae", "bar", "threshold", "extract"):
            self.assertNotIn(forbidden, spec_body, forbidden)

    def test_evaluate_prints_the_verdict_with_the_rule_that_produced_it(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "qf")) as fh:
            body = fh.read()
        cmd = body[body.index("def cmd_evaluate"):]
        cmd = cmd[:cmd.index("\ndef cmd_status")]
        # A verdict with no contract hash beside it is a number whose rule
        # nobody can look up.
        for key in ("verdict", "contract_hash", "judged_run"):
            self.assertIn(f'"{key}"', cmd)


class TestTheEvaluateKindTakesNoPolicy(unittest.TestCase):
    """The spec half of NC9: the only things an `evaluate` job can say are WHICH
    run and WHICH contract."""

    def a_spec(self, **args_over):
        args = {"run": "probe-20260829T123756Z-9d54e39271d7-4290",
                "contract": "a" * 64}
        args.update(args_over)
        return {"schema": 1, "kind": "evaluate", "args": args}

    def test_the_accepted_args_are_exactly_run_and_contract(self):
        effective = spec.normalize(self.a_spec())
        self.assertEqual(set(effective["args"]), {"run", "contract"})

    def test_no_policy_field_is_accepted(self):
        for extra in ("baseline", "bar", "mae", "threshold", "metrics",
                      "holdout_days", "primary_slice", "extract"):
            with self.subTest(field=extra):
                with self.assertRaises(spec.SpecError) as cm:
                    spec.normalize(self.a_spec(**{extra: "x"}))
                self.assertIn(extra, str(cm.exception))

    def test_a_contract_name_is_refused_in_favour_of_a_hash(self):
        with self.assertRaises(spec.SpecError) as cm:
            spec.normalize(self.a_spec(contract="wait_time.v1.json"))
        self.assertIn("contract_hash", str(cm.exception))

    def test_it_takes_no_source_sha(self):
        # It runs no candidate code, so a commit would record a dependency it
        # does not have.
        body = self.a_spec()
        body["source_sha"] = "b" * 40
        with self.assertRaises(spec.SpecError):
            spec.normalize(body)

    def test_its_identity_is_the_pair_it_judges(self):
        one = spec.normalize(self.a_spec())
        again = spec.normalize(self.a_spec())
        self.assertEqual(one["source_sha"], again["source_sha"])
        self.assertEqual(one["source_ref"], spec.EVALUATE_SOURCE_REF)
        other_run = spec.normalize(
            self.a_spec(run="probe-20260829T123756Z-9d54e39271d7-1"))
        other_rule = spec.normalize(self.a_spec(contract="b" * 64))
        # Judging two runs by one contract is two pieces of work; an identity
        # that collapsed them would make the second look like a duplicate.
        self.assertNotEqual(one["source_sha"], other_run["source_sha"])
        self.assertNotEqual(one["source_sha"], other_rule["source_sha"])

    def test_the_source_ref_says_it_is_not_a_commit(self):
        # The value looks exactly like a sha, so the record has to say otherwise
        # itself or a reader will join on it.
        self.assertIn("not a commit", spec.EVALUATE_SOURCE_REF)

    def test_it_lands_in_the_light_lane(self):
        # A judge must never take the training mutex: scoring a finished
        # experiment has no business making the nightly wait.
        self.assertEqual(spec.normalize(self.a_spec())["lane"], "light")
        self.assertIn("evaluate", qfd.RELAYED_KINDS)


class TestNc9IsWiredAndGated(unittest.TestCase):
    """Design negative control 9, deferred from 2a to 2c because it needs a
    contract to exist. Same three instrument rules as NC18/NC19: a canary gates
    the group, the positive case is asserted alongside the refusals, and nothing
    the suite claims rests on something it did not observe."""

    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.host = os.path.dirname(here)
        with open(os.path.join(self.host, "nc-suite-phase2.sh")) as fh:
            self.suite = fh.read()
        body = self.suite[self.suite.index("nc9() {"):]
        self.nc9 = code_only(body[:body.index("\n}\n")])

    def test_it_actually_runs(self):
        assert_clause_runs(self, self.suite, "nc9")

    def test_the_group_is_gated_on_a_contract_resolving(self):
        # Without this, a resolver returning nothing satisfies every refusal.
        canary = self.nc9.index("no contract resolves")
        for claim in ("NC9 (a)", "NC9 (b)", "NC9 (c)", "NC9 (d)"):
            self.assertLess(canary, self.nc9.index(claim), claim)
        branch = self.nc9[canary:self.nc9.index("\n  fi", canary)]
        self.assertIn("return", branch)

    def test_the_canary_explains_the_likeliest_reason_it_failed(self):
        # An empty contracts directory is the expected state until a baseline is
        # promoted, and "no contract resolves" alone would send somebody hunting
        # for a bug in the resolver.
        self.assertIn("instantiate-contract.sh", self.nc9)
        self.assertIn("unpinned baseline", self.nc9)

    def test_the_positive_case_is_asserted_next_to_the_refusals(self):
        # A submit path that refused EVERY evaluate job would pass (b) and (c).
        self.assertIn("a trusted contract is accepted at submit", self.nc9)
        self.assertIn("evaluate_input_missing", self.nc9)

    def test_the_untrusted_contract_refusal_checks_the_list_too(self):
        self.assertIn("lists what the checkout carries", self.nc9)

    def test_no_policy_field_is_probed_through_the_client(self):
        # The CLIENT cannot send an unknown args key -- argparse would refuse the
        # flag -- so this has to go over the socket directly, or the clause would
        # be testing argparse.
        # Sliced from the loop header, not from a byte offset before the first
        # claim: the field names live in `for field in ...`, which sits further
        # back than a fixed window reaches.
        policy = self.nc9[self.nc9.index("for field in "):]
        policy = policy[:policy.index("NC9 (d)")]
        self.assertIn("socket.AF_UNIX", policy)
        for field in ("baseline", "bar", "mae", "threshold", "metrics",
                      "holdout_days"):
            self.assertIn(field, policy, field)

    def test_the_independent_resolution_is_asserted_from_outside(self):
        # qfd's check is for legibility; a control enforced only by the process
        # in the `docker` group is not a control.
        self.assertIn("cannot reach the evaluator socket", self.nc9)

    def test_the_evaluator_units_read_write_paths_are_checked(self):
        self.assertIn("ReadWritePaths=", self.nc9)
        for store in ("qf-extracts", "qf-baselines", "contracts"):
            self.assertIn(store, self.nc9, store)

    def test_it_uses_the_shape_check_not_a_run_directory(self):
        # The defect NC19's canary shipped with: a submitted job is QUEUED and
        # has no directory until it is leased.
        self.assertIn("is_run_id", self.nc9)
        self.assertNotIn('[ -d "$RUNS_DIR/$rid" ]', self.nc9)

    def test_the_error_class_it_expects_is_one_the_dispatcher_produces(self):
        # Pinned to the source of the vocabulary, the same way NC16's clause is
        # pinned to EXIT_CLASSES: a clause asserting a class nothing emits fails
        # for a reason unrelated to the control.
        self.assertEqual(qfd.EvaluateInputMissing.error_class,
                         "evaluate_input_missing")
        self.assertIn("evaluate_input_missing", self.nc9)

class TestEveryScriptedQfInvocationMatchesTheClient(unittest.TestCase):
    """A command the client rejects is not a command. Task 24 / NC11.

    THE DEFECT THIS EXISTS FOR, twice in two days. Two NC11 clauses ran
    `qf list --state SUCCEEDED --kind probe`; `list` takes `--state` and
    `--limit` and nothing else, so every invocation exited 2 with "unrecognized
    arguments", read nothing, and the clause voided with a message blaming the
    absence of the probe it never looked for. And the 2c fixture generator told
    the operator to run `qf submit --kind probe ... --extract <hash>`, which is
    three impossibilities in one line: `submit --kind` accepts only test and
    selftest, `submit` has no `--extract`, and probes have their own subcommand.

    Neither could be caught by running the suite on a healthy host: argparse
    exits 2, the helper discards stderr, and the clause reports its subject
    missing. So the flags are checked against the client's own parser instead.
    """

    HOST = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))

    # Every flag argparse defines on the TOP-LEVEL parser, which is legal after
    # any subcommand.
    GLOBAL = {"--json", "--help", "-h"}

    # Scanned scripts: everything that tells somebody, or something, to run the
    # client. Add a script here when it starts issuing `qf` commands.
    SCRIPTS = ("nc-suite-phase2.sh", "nc-fixtures-phase2b.sh",
               "nc-fixtures-phase2c.sh", "phase2c-setup.sh",
               "phase2b-setup.sh", "phase2-setup.sh")
    # NOT README.md, and that is a property of the file rather than laziness: it
    # documents invocations that were WRONG, as lessons -- "`qf status <rid>
    # --json` was never valid" is a sentence this test would have to be taught to
    # disbelieve. Every `qf` in a SCRIPT is meant to run.

    # `qf` in a COMMAND POSITION, not `qf` in a sentence. These anchors are what
    # separate `sudo -H -u research qf probe ...` from "install /usr/local/bin/qf
    # and /usr/local/sbin/qfadmin", which is prose inside a shell string.
    AT_COMMAND = r"""(?:^[ \t]*|["'`]|\$\(|\|[ \t]*|;[ \t]*|&&[ \t]*|sudo |-u \w+ )"""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(cls.HOST, "dispatcher", "qf")) as fh:
            cls.client = fh.read()
        cls.commands = cls.parse_parser(cls.client)

    @staticmethod
    def parse_parser(source):
        """`{subcommand: {flags}}`, read off `add_parser`/`add_argument`.

        A PARSE OF THE REAL FILE rather than a list written here: a hand-kept
        copy of the client's interface is the thing that was wrong in the first
        place.
        """
        named = {}                      # variable -> subcommand
        commands = {}
        for line in source.splitlines():
            line = line.strip()
            m = re.match(r'(?:(\w+)\s*=\s*)?sub\.add_parser\("([a-z-]+)"',
                         line)
            if m:
                variable, name = m.group(1), m.group(2)
                commands.setdefault(name, set())
                if variable:
                    named[variable] = name
                continue
            m = re.match(r'(\w+)\.add_argument\("(-[^"]+)"', line)
            if m and m.group(1) in named:
                commands[named[m.group(1)]].add(m.group(2))
        return commands

    def invocations(self):
        """Every `qf <sub> [--flags]` written in a scanned script."""
        for name in self.SCRIPTS:
            path = os.path.join(self.HOST, name)
            if not os.path.isfile(path):
                continue
            with open(path) as fh:
                text = fh.read()
            for line in text.splitlines():
                m = re.search(self.AT_COMMAND + r"qf ([a-z][a-z-]*)(.*)$", line)
                if not m:
                    continue
                sub, rest = m.group(1), m.group(2)
                # CUT AT THE COMMENT. `qf extracts   # copy the `--extract
                # <hash>` line it prints` is one invocation and one instruction,
                # and reading the second as a flag of the first reported a
                # defect that was not there.
                rest = rest.split("#", 1)[0]
                yield name, sub, re.findall(r"--[a-z][a-z-]*", rest)

    def test_the_parse_found_the_clients_real_interface(self):
        # THE CANARY. A regex that matched nothing would make every assertion
        # below pass, which is the shape of failure this file has already fixed
        # six times.
        self.assertIn("list", self.commands)
        self.assertIn("--state", self.commands["list"])
        self.assertIn("--limit", self.commands["list"])
        self.assertIn("probe", self.commands)
        self.assertIn("--extract", self.commands["probe"])
        self.assertNotIn("--extract", self.commands["submit"])
        self.assertGreaterEqual(len(self.commands), 10)

    def test_every_scripted_subcommand_exists(self):
        seen = 0
        for script, sub, _flags in self.invocations():
            if sub not in self.commands:
                # Only flag it when it LOOKS like a subcommand slot: prose says
                # "qf status shows what exists", and `status` is real, so the
                # unknown ones are what matter.
                self.fail(f"{script} runs `qf {sub}`, which the client has no"
                          f" subcommand for. Known: {sorted(self.commands)}")
            seen += 1
        self.assertGreater(seen, 10, "the scan found almost nothing")

    def test_every_scripted_flag_exists_on_that_subcommand(self):
        for script, sub, flags in self.invocations():
            for flag in flags:
                if flag in self.GLOBAL:
                    continue
                with self.subTest(script=script, sub=sub, flag=flag):
                    self.assertIn(
                        flag, self.commands.get(sub, set()),
                        f"{script} runs `qf {sub} {flag}`; that subcommand"
                        f" accepts {sorted(self.commands.get(sub, set()))}."
                        f" argparse exits 2 on an unknown flag, and a clause"
                        f" that cannot ask reports its subject missing.")

    def test_no_script_asks_submit_for_a_kind_it_refuses(self):
        # `submit --kind` is `test|selftest`: extract, probe and evaluate have
        # their own subcommands, and the generator told an operator otherwise.
        kinds = re.search(r'add_argument\("--kind", required=True,'
                          r' choices=\(([^)]*)\)', self.client)
        self.assertIsNotNone(kinds)
        allowed = set(re.findall(r'"([a-z]+)"', kinds.group(1)))
        self.assertEqual(allowed, {"test", "selftest"})
        for script in self.SCRIPTS:
            path = os.path.join(self.HOST, script)
            if not os.path.isfile(path):
                continue
            with open(path) as fh:
                text = fh.read()
            for m in re.finditer(r"qf submit[^\n]*--kind\s+([a-z]+)", text):
                with self.subTest(script=script, kind=m.group(1)):
                    self.assertIn(m.group(1), allowed)


if __name__ == "__main__":
    # AT THE END. This guard had drifted into the middle of the file as classes
    # were appended below it, so running the file directly executed only the
    # classes above it and reported OK. `discover` imports the whole module, so
    # the suite was green and the gap invisible.
    unittest.main()
