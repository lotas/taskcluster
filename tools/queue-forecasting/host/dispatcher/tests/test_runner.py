# Tests for the runner's safety rules. Docker and git are injected, so these
# assert the decisions rather than the daemon: what gets released, when, and on
# what evidence.
#
# The rule under test throughout is that a RELEASE IS A CLAIM ABOUT REALITY. A
# subprocess timeout on `docker kill` proves the CLI stopped waiting, not that
# the workload died, and closing the training descriptor on that basis hands the
# mutex to the nightly run while live work continues.
import datetime
import json
import os
import shutil
import subprocess
import tempfile
import threading
import types
import unittest

import os
import sys

# `host/shared` on the path: `spec.normalize` delegates the `extract` kind to
# `shared/extract_spec.py`, the one closed-world definition both privilege
# domains use (D16). Inline rather than in a shared helper module, because
# `tests/` is not a package and a helper only resolves under `unittest discover`
# -- a bootstrap that works under one invocation is worse than two copies.
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "shared"))

import qfd                                                     # noqa: E402
import source
import spec
import store

SHA = "3f1c" + "0" * 36


def proc(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout,
                                 stderr=stderr)


class FakeDocker:
    """Scripted answers, and a record of what was asked."""

    def __init__(self, states=None, inspect_id="c-real"):
        # container_id -> list of answers for is_running, consumed in order
        self.states = states or {}
        self.inspect_id = inspect_id
        self.calls = []

    def run(self, argv, env=None, timeout=60):
        self.calls.append(argv)
        if argv[:2] == ["docker", "inspect"]:
            return proc(0, self.inspect_id + "\n")
        return proc(0, "")

    def is_running(self, cid, timeout=15):
        answers = self.states.get(cid, [False])
        return answers.pop(0) if len(answers) > 1 else answers[0]


class FakeSource:
    def __init__(self):
        self.removed = []

    def resolve(self, sha, deadline=None):
        self.resolve_deadline = deadline
        return "refs/remotes/origin/main"

    def add_worktree(self, sha, dest, deadline=None):
        self.worktree_deadline = deadline
        os.makedirs(dest, exist_ok=True)
        return dest

    def remove_worktree(self, dest):
        self.removed.append(dest)


class RunnerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.root = root
        self.runs = os.path.join(root, "runs")
        self.intent = os.path.join(root, "intent.d")
        os.makedirs(self.runs)
        os.makedirs(self.intent)
        self.lock_path = os.path.join(root, "lock")
        open(self.lock_path, "w").close()
        trusted = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        self.cfg = qfd.Config(
            trusted_dir=trusted, state_dir=root, runs_dir=self.runs,
            socket_path=os.path.join(root, "c.sock"),
            admin_socket_path=os.path.join(root, "a.sock"), admin_uid=4242,
            # Never connected to in these tests -- `extract_client` is injected
            # -- but present because `Config` sets only the keys it is given, so
            # a fixture missing one fails at use rather than at construction.
            extract_socket=os.path.join(root, "extract.sock"),
            settlement_lag_s=48 * 3600,
            remote="https://example.invalid/x", token_file="",
            lock_file=self.lock_path, intent_dir=self.intent,
            build_lock=os.path.join(root, "build.lock"),
            build_timeout_s=1800, build_lock_wait_s=900,
            build_settle_s=0,                    # no real sleeping in tests
            job_hold_deadline_s=7800, kill_confirm_s=1, stop_timeout_s=0,
            reap_interval_s=60, setup_teardown_allowance_s=600,
            marker_stale_margin_s=900, lock_migrated_marker="",
            mem_budget_mb=22528, timeout_max_s=3600, lock_wait_s=9000,
            image_build_mem_mb=2048, light_workers=2, log_cap_mb=16,
            artifact_cap_mb=2048, handoff_timeout_s=120, disk_floor_gb=0,
            queued_cap_per_uid=20, lease_s=300)

        self.db = qfd.DbOwner(os.path.join(root, "state.db"),
                              mem_budget_mb=self.cfg.mem_budget_mb,
                              disk_floor_mb=0, out_quota_mb=0,
                              artifact_cap_mb=0).start()
        self.addCleanup(self.db.stop)
        self.docker = FakeDocker()
        self.src = FakeSource()
        self.disp = qfd.Dispatcher(self.cfg, self.db, docker=self.docker,
                                   src=self.src)
        self.runner = qfd.Runner(self.cfg, self.db, self.disp,
                                 docker=self.docker, src=self.src)
        self.runner.poll_interval_s = 0.02

    def a_job(self, run_id="r1", state="RUNNING", **over):
        eff = spec.normalize({"schema": 1, "kind": "test", "source_sha": SHA,
                              **over})
        self.db.call("submit", eff, run_id=run_id, uid=1000,
                     now="2026-08-25T10:00:00Z")
        if state == "QUEUED":
            return self.db.call("get", run_id)
        self.db.call("dequeue", eff["lane"], owner="qfd",
                     now="2026-08-25T10:00:01Z",
                     lease_expires_at="2026-08-25T10:05:00Z",
                     hold_deadline_at="2036-08-25T12:00:00Z", max_running=2)
        for step in ("BUILDING", "RUNNING"):
            if state == "LEASED":
                break
            self.db.call("transition", run_id, step, now="2026-08-25T10:00:02Z")
            if step == state:
                break
        return self.db.call("get", run_id)

    def hold_for(self, job, lane="light"):
        lock = qfd.TrainingLock(self.cfg.lock_file, lane).acquire()
        self.addCleanup(lock.release)
        return qfd.Hold(job, lock, qfd.parse_iso(job["hold_deadline_at"]))


class TestConfirmBeforeRelease(RunnerCase):
    def test_a_positively_stopped_container_is_confirmed_and_released(self):
        job = self.a_job()
        self.db.call("add_resource", "r1", role="candidate", container_id="c1",
                     now="2026-08-25T10:00:03Z")
        self.docker.states = {"c1": [False]}
        hold = self.hold_for(job)
        self.assertTrue(self.runner.confirm_all_stopped(hold))
        rows = self.db.call("resources_for", "r1")
        self.assertIsNotNone(rows[0]["released_at"])
        ok, problems = self.db.call("verify_chain")
        self.assertTrue(ok, problems)

    def test_an_unknown_state_is_never_treated_as_stopped(self):
        # This is the whole rule. `None` means Docker did not answer.
        job = self.a_job()
        self.db.call("add_resource", "r1", role="candidate", container_id="c1",
                     now="2026-08-25T10:00:03Z")
        self.docker.states = {"c1": [None]}
        hold = self.hold_for(job)
        self.assertFalse(self.runner.confirm_all_stopped(hold))
        rows = self.db.call("resources_for", "r1")
        self.assertIsNone(rows[0]["released_at"],
                          "an unconfirmed container must not be released")

    def test_a_still_running_container_is_not_confirmed(self):
        job = self.a_job()
        self.db.call("add_resource", "r1", role="candidate", container_id="c1",
                     now="2026-08-25T10:00:03Z")
        self.docker.states = {"c1": [True]}
        hold = self.hold_for(job)
        self.assertFalse(self.runner.confirm_all_stopped(hold))

    def test_every_recorded_container_must_confirm_not_just_one(self):
        # Revision 7 labelled only the candidate; the handoff could still be up.
        job = self.a_job()
        for role, cid in (("candidate", "c1"), ("handoff", "c2")):
            self.db.call("add_resource", "r1", role=role, container_id=cid,
                         now="2026-08-25T10:00:03Z")
        self.docker.states = {"c1": [False], "c2": [True]}
        hold = self.hold_for(job)
        self.assertFalse(self.runner.confirm_all_stopped(hold))
        by_role = {r["role"]: r for r in self.db.call("resources_for", "r1")}
        self.assertIsNotNone(by_role["candidate"]["released_at"])
        self.assertIsNone(by_role["handoff"]["released_at"])

    def test_a_container_that_stops_during_the_window_is_confirmed(self):
        job = self.a_job()
        self.db.call("add_resource", "r1", role="candidate", container_id="c1",
                     now="2026-08-25T10:00:03Z")
        self.docker.states = {"c1": [True, False]}
        hold = self.hold_for(job)
        self.assertTrue(self.runner.confirm_all_stopped(hold))

    def test_an_empty_inventory_takes_the_build_settle_path(self):
        # Confirmation over an empty set is not confirmation, so this must NOT
        # be reached by inspecting nothing and believing the answer -- it is a
        # separate, documented path (design D10).
        job = self.a_job(state="BUILDING")
        self.assertEqual(self.db.call("resources_for", "r1"), [])
        hold = self.hold_for(job)
        self.assertTrue(self.runner.confirm_all_stopped(hold))
        self.assertFalse([c for c in self.docker.calls
                          if c[:2] == ["docker", "inspect"]],
                         "nothing should have been inspected")

    def test_already_released_resources_are_not_re_inspected(self):
        job = self.a_job()
        self.db.call("add_resource", "r1", role="candidate", container_id="c1",
                     now="2026-08-25T10:00:03Z")
        self.db.call("release_resource", "r1", role="candidate",
                     container_id="c1", now="2026-08-25T10:00:04Z")
        hold = self.hold_for(job)
        self.runner.confirm_all_stopped(hold)
        self.assertFalse([c for c in self.docker.calls
                          if c[:2] == ["docker", "inspect"]])


class TestFinishRetainsTheDescriptor(RunnerCase):
    def test_an_unconfirmed_shutdown_blocks_and_keeps_the_lock(self):
        # The failure the mutex exists to prevent, reached through the mechanism
        # meant to bound it.
        job = self.a_job()
        self.db.call("add_resource", "r1", role="candidate", container_id="c1",
                     now="2026-08-25T10:00:03Z")
        self.docker.states = {"c1": [None]}
        hold = self.hold_for(job)
        self.runner.finish(hold, "SUCCEEDED", {"exit_code": 0})
        self.assertEqual(self.db.call("get", "r1")["state"], "CLEANUP_BLOCKED")
        self.assertTrue(hold.lock.held,
                        "the training descriptor must NOT be released")
        self.assertEqual(self.db.call("get", "r1")["error_class"],
                         "kill_unconfirmed")

    def test_a_blocked_job_keeps_its_reservation_and_freezes_admission(self):
        job = self.a_job(mem_limit="8g")
        self.db.call("add_resource", "r1", role="candidate", container_id="c1",
                     now="2026-08-25T10:00:03Z")
        self.docker.states = {"c1": [None]}
        self.runner.finish(self.hold_for(job, "heavy"), "SUCCEEDED",
                           {"exit_code": 0})
        self.assertGreater(self.db.call("admitted_mem_mb"), 0)
        ok, reason = self.disp.may_admit()
        self.assertFalse(ok)
        self.assertEqual(reason, "cleanup_blocked")

    def test_a_confirmed_shutdown_reaches_the_terminal_state_and_releases(self):
        job = self.a_job()
        self.db.call("add_resource", "r1", role="candidate", container_id="c1",
                     now="2026-08-25T10:00:03Z")
        self.docker.states = {"c1": [False]}
        hold = self.hold_for(job)
        self.runner.finish(hold, "SUCCEEDED", {"exit_code": 0,
                                               "finished_at": qfd.utcnow()})
        self.assertEqual(self.db.call("get", "r1")["state"], "SUCCEEDED")
        self.assertFalse(hold.lock.held)
        self.assertEqual(self.db.call("admitted_mem_mb"), 0)
        self.assertIn(os.path.join(self.runs, "r1", "src"), self.src.removed)

    def test_a_terminal_run_says_so_in_the_journal(self):
        """A healthy run used to log NOTHING at all.

        The event store is the audit trail, but an operator reads `journalctl`,
        and there silence meant either "it worked" or "nothing was ever picked
        up" -- indistinguishable. A subsystem that only speaks when it is unhappy
        cannot be watched, and this was noticed on the host by someone asking why
        a run that plainly executed left no trace.
        """
        job = self.a_job()
        self.db.call("add_resource", "r1", role="candidate", container_id="c1",
                     now="2026-08-25T10:00:03Z")
        self.docker.states = {"c1": [False]}
        with self.assertLogs(qfd.log, level="INFO") as caught:
            self.runner.finish(self.hold_for(job), "FAILED",
                               {"exit_code": 1, "error_class": "nonzero_exit",
                                "finished_at": qfd.utcnow()})
        line = "\n".join(caught.output)
        for expected in ("r1", "FAILED", "exit_code=1", "nonzero_exit"):
            self.assertIn(expected, line)

    def test_the_chain_still_verifies_after_either_path(self):
        for cid, answer in (("c1", [False]), ("c2", [None])):
            with self.subTest(answer=answer):
                job = self.a_job(run_id=cid)
                self.db.call("add_resource", cid, role="candidate",
                             container_id=cid, now="2026-08-25T10:00:03Z")
                self.docker.states = {cid: answer}
                self.runner.finish(self.hold_for(job), "SUCCEEDED",
                                   {"exit_code": 0})
        ok, problems = self.db.call("verify_chain")
        self.assertTrue(ok, problems)


class TestAdmissionSequence(RunnerCase):
    def test_an_empty_queue_does_nothing(self):
        self.assertFalse(self.runner.try_one("light"))

    def test_a_live_intent_marker_stops_admission_and_takes_no_lock(self):
        self.a_job(state="QUEUED")
        with open(os.path.join(self.intent, "nightly.1.100.intent"), "w") as fh:
            fh.write(f"pid={os.getpid()}\ndeadline={10 ** 10}\n")
        self.assertFalse(self.runner.try_one("light"))
        self.assertEqual(self.db.call("get", "r1")["state"], "QUEUED")
        # Nothing was acquired, so an exclusive lock is still available.
        qfd.TrainingLock(self.cfg.lock_file, "heavy").acquire().release()

    def test_a_cleanup_blocked_job_stops_admission(self):
        job = self.a_job(run_id="stuck")
        self.db.call("transition", "stuck", "CLEANUP_BLOCKED",
                     now=qfd.utcnow(), fields={"error_class": "x"})
        self.a_job(run_id="fresh", state="QUEUED")
        self.assertFalse(self.runner.try_one("light"))
        self.assertEqual(self.db.call("get", "fresh")["state"], "QUEUED")

    def test_a_held_exclusive_lock_stops_a_light_admission(self):
        self.a_job(state="QUEUED")
        incumbent = qfd.TrainingLock(self.cfg.lock_file, "heavy").acquire()
        self.addCleanup(incumbent.release)
        self.assertFalse(self.runner.try_one("light"))
        self.assertEqual(self.db.call("get", "r1")["state"], "QUEUED")

    def test_a_refused_memory_budget_releases_the_lock_and_leaves_it_queued(self):
        # Contention never produces a state transition.
        self.db.stop()
        self.db = qfd.DbOwner(os.path.join(self.root, "tight.db"),
                              mem_budget_mb=1, disk_floor_mb=0,
                              out_quota_mb=0, artifact_cap_mb=0).start()
        self.addCleanup(self.db.stop)
        self.disp = qfd.Dispatcher(self.cfg, self.db, docker=self.docker,
                                   src=self.src)
        self.runner = qfd.Runner(self.cfg, self.db, self.disp,
                                 docker=self.docker, src=self.src)
        self.runner.poll_interval_s = 0.02
        self.a_job(state="QUEUED")
        self.assertFalse(self.runner.try_one("light"))
        self.assertEqual(self.db.call("get", "r1")["state"], "QUEUED")
        # The lock must have been given back.
        qfd.TrainingLock(self.cfg.lock_file, "heavy").acquire().release()

    def test_a_full_heavy_lane_leaves_the_next_job_queued(self):
        self.a_job(run_id="running", mem_limit="8g")
        self.a_job(run_id="waiting", state="QUEUED", mem_limit="8g")
        # An exclusive holder already exists for the running job, so heavy
        # cannot admit again anyway; assert the state does not move.
        self.runner.try_one("heavy")
        self.assertEqual(self.db.call("get", "waiting")["state"], "QUEUED")

    def test_no_leased_to_queued_edge_is_ever_needed(self):
        self.assertNotIn("QUEUED", store.ALLOWED["LEASED"])


class TestRecovery(RunnerCase):
    def test_it_is_driven_from_sqlite_not_docker_ps(self):
        # Revision 8 started from live containers, so a CLEANUP_BLOCKED job
        # whose workload died during the outage was never discovered and the
        # no-admissions rule stopped the loop permanently.
        job = self.a_job(run_id="blocked")
        # Recorded while RUNNING and blocked afterwards, which is the only order
        # the daemon can produce: `add_resource` refuses once cleanup has begun.
        self.db.call("add_resource", "blocked", role="candidate",
                     container_id="c1", now=qfd.utcnow())
        self.db.call("transition", "blocked", "CLEANUP_BLOCKED",
                     now=qfd.utcnow(), fields={"error_class": "x"})
        self.docker.states = {"c1": [False]}
        rec = qfd.Recovery(self.cfg, self.db, self.runner, self.docker)
        holds = rec.reconstruct()
        self.assertEqual(holds, [])
        self.assertEqual(self.db.call("get", "blocked")["state"], "FAILED")
        self.assertEqual(self.db.call("get", "blocked")["error_class"],
                         "reclaimed_at_startup")
        self.assertFalse([c for c in self.docker.calls if "ps" in c])

    def test_a_building_job_with_no_containers_retains_its_hold(self):
        # Acting on a vacuously-true check would release a BUILDING job -- which
        # under the classic builder owns no container of ours -- the instant a
        # restart found it (revision 12).
        self.a_job(run_id="building", state="BUILDING")
        rec = qfd.Recovery(self.cfg, self.db, self.runner, self.docker)
        holds = rec.reconstruct()
        self.assertEqual(len(holds), 1)
        self.assertTrue(holds[0].lock.held)
        self.addCleanup(holds[0].lock.release)
        self.assertEqual(self.db.call("get", "building")["state"], "BUILDING")

    def test_it_recharges_the_stored_reservation_not_the_live_container_cap(self):
        # A 22 GB job whose only live container is its 2 GB builder must not come
        # back charged 2 GB.
        self.a_job(run_id="big", state="BUILDING", mem_limit="22g")
        self.assertEqual(self.db.call("admitted_mem_mb"), 22528)
        rec = qfd.Recovery(self.cfg, self.db, self.runner, self.docker)
        holds = rec.reconstruct()
        for h in holds:
            self.addCleanup(h.lock.release)
        self.assertEqual(self.db.call("admitted_mem_mb"), 22528)
        self.assertEqual(
            store.reservation_mb("22g", self.cfg.image_build_mem_mb), 22528)

    def test_a_light_orphan_gets_its_shared_lock_back(self):
        # Revision 4 re-acquired locks for heavy orphans only, leaving an
        # orphaned light container running with no LOCK_SH while nightly could
        # take LOCK_EX.
        #
        # The orphan carries an UNCONFIRMABLE container, because that is the case
        # in which a hold comes back at all: recovery hands one over only when
        # something must keep asking. A confirmable orphan is cleaned up and its
        # lock is released, which the next test covers.
        self.a_job(run_id="light1", state="RUNNING", mem_limit="1g")
        self.db.call("add_resource", "light1", role="candidate",
                     container_id="c1", now="2026-08-25T10:00:03Z")
        self.docker.states = {"c1": [None]}          # Docker will not answer
        rec = qfd.Recovery(self.cfg, self.db, self.runner, self.docker)
        holds = rec.reconstruct()
        self.assertEqual(len(holds), 1)
        self.assertEqual(holds[0].lock.lane, "light")
        self.assertTrue(holds[0].lock.held)
        self.addCleanup(holds[0].lock.release)
        with self.assertRaises(qfd.LockHeld):
            qfd.TrainingLock(self.cfg.lock_file, "heavy").acquire()

    def test_the_lane_lock_is_held_before_any_cleanup_runs(self):
        # The other half of the same rule: the lock comes back FIRST, so the
        # cleanup that follows cannot race a nightly LOCK_EX.
        self.a_job(run_id="light2", state="RUNNING", mem_limit="1g")
        self.db.call("add_resource", "light2", role="candidate",
                     container_id="c2", now="2026-08-25T10:00:03Z")
        observed = {}
        real = self.runner.finish

        def finish(hold, state, fields):
            try:
                qfd.TrainingLock(self.cfg.lock_file, "heavy").acquire()
                observed["heavy_free_during_cleanup"] = True
            except qfd.LockHeld:
                observed["heavy_free_during_cleanup"] = False
            observed["lane"] = hold.lock.lane
            return real(hold, state, fields)

        self.runner.finish = finish
        qfd.Recovery(self.cfg, self.db, self.runner, self.docker).reconstruct()
        self.assertEqual(observed.get("lane"), "light")
        self.assertFalse(observed.get("heavy_free_during_cleanup", True),
                         "cleanup ran without the lane lock")

    def test_a_confirmable_orphan_is_cleaned_up_rather_than_re_adopted(self):
        # Nothing in a restarted process can resume one of these: the
        # `docker start --attach` client died with the old process, so the exit
        # status is gone, the logs are not being pumped and the handoff will
        # never run. A hold handed back here would be driven by NOBODY, and an
        # undriven hold is a permanent stall -- the lease lapses, reclaim finds a
        # live container and renews it, for ever.
        self.a_job(run_id="orphan", state="RUNNING", mem_limit="1g")
        self.db.call("add_resource", "orphan", role="candidate",
                     container_id="c3", now="2026-08-25T10:00:03Z")
        holds = qfd.Recovery(self.cfg, self.db, self.runner,
                             self.docker).reconstruct()
        self.assertEqual(holds, [], "an undriven hold was handed back")
        job = self.db.call("get", "orphan")
        self.assertEqual((job["state"], job["error_class"]),
                         ("FAILED", "reclaimed_at_startup"))
        self.assertEqual(self.db.call("resources_for", "orphan",
                                      unreleased_only=True), [])
        # ...and the lock really is free, not merely reported so.
        qfd.TrainingLock(self.cfg.lock_file, "heavy").acquire().release()

    def test_an_expired_hold_deadline_triggers_forced_cleanup(self):
        # Repeated restarts must not hand the job a fresh budget.
        eff = spec.normalize({"schema": 1, "kind": "test", "source_sha": SHA})
        self.db.call("submit", eff, run_id="stale", uid=1000,
                     now="2026-08-25T10:00:00Z")
        self.db.call("dequeue", "light", owner="qfd",
                     now="2026-08-25T10:00:01Z",
                     lease_expires_at="2026-08-25T10:05:00Z",
                     hold_deadline_at="2020-01-01T00:00:00Z", max_running=2)
        self.db.call("transition", "stale", "RUNNING", now=qfd.utcnow())
        self.db.call("add_resource", "stale", role="candidate",
                     container_id="c1", now=qfd.utcnow())
        self.docker.states = {"c1": [False]}
        rec = qfd.Recovery(self.cfg, self.db, self.runner, self.docker)
        self.assertEqual(rec.reconstruct(), [])
        job = self.db.call("get", "stale")
        self.assertEqual(job["state"], "FAILED")
        self.assertEqual(job["error_class"], "deadline_expired")

    def test_terminal_jobs_are_not_reconstructed(self):
        job = self.a_job(run_id="done")
        self.db.call("transition", "done", "SUCCEEDED", now=qfd.utcnow(),
                     fields={"exit_code": 0})
        rec = qfd.Recovery(self.cfg, self.db, self.runner, self.docker)
        self.assertEqual(rec.reconstruct(), [])


class TestHoldArithmetic(RunnerCase):
    def test_the_deadline_comes_from_the_database_not_the_clock(self):
        job = self.a_job()
        hold = self.hold_for(job)
        self.assertEqual(hold.deadline_epoch,
                         qfd.parse_iso(job["hold_deadline_at"]))

    def test_an_expired_hold_reports_expired(self):
        job = self.a_job()
        hold = qfd.Hold(job, None, qfd.parse_iso("2020-01-01T00:00:00Z"))
        self.assertTrue(hold.expired())
        self.assertLess(hold.remaining(), 0)

    def test_iso_and_parse_round_trip(self):
        self.assertEqual(qfd.parse_iso(qfd.iso_at(1750000000)), 1750000000)


class TestBuildLock(RunnerCase):
    def test_it_is_exclusive_and_bounded(self):
        path = os.path.join(self.root, "b.lock")
        with qfd.BuildLock(path, 1):
            with self.assertRaises(qfd.LockHeld):
                with qfd.BuildLock(path, 1):
                    pass
        # Released on exit, so the next attempt succeeds.
        with qfd.BuildLock(path, 1):
            pass

    def test_each_attempt_closes_its_own_descriptor(self):
        # flock ownership is per open file description, so a leaked descriptor
        # would keep the build mutex held after the block exited. (fd NUMBERS
        # are reused after close, so identity is not the thing to assert.)
        path = os.path.join(self.root, "b2.lock")
        a = qfd.BuildLock(path, 1)
        with a:
            self.assertIsNotNone(a.fd)
        self.assertIsNone(a.fd)
        b = qfd.BuildLock(path, 1)
        with b:
            self.assertIsNotNone(b.fd)
        self.assertIsNone(b.fd)


class TestRunDirLayout(RunnerCase):
    def test_out_is_setgid_and_artifacts_is_not_group_writable(self):
        # out/ is 2770 qfd:qfrun so uid 10001 can write and the group sticks;
        # artifacts/ is 0750 qfd:qfclient and is the only thing a client reads.
        paths = self.runner.prepare_run_dir("r-layout")
        out_mode = os.stat(paths["out"]).st_mode & 0o7777
        art_mode = os.stat(paths["artifacts"]).st_mode & 0o7777
        self.assertEqual(out_mode, 0o2770)
        self.assertEqual(art_mode, 0o750)

    def test_it_is_idempotent(self):
        self.runner.prepare_run_dir("r-twice")
        paths = self.runner.prepare_run_dir("r-twice")
        self.assertTrue(os.path.isdir(paths["logs"]))


if __name__ == "__main__":
    unittest.main()


class TestWorktreesAreReclaimedHoweverAJobEnds(RunnerCase):
    """`finish` was the ONLY place a worktree was removed.

    So it was cleaned on the happy path and leaked on every other one -- a lease
    reclaimed by the reaper, a CLEANUP_BLOCKED job resolved by `resolve_blocked`,
    an operator force-release, a startup recovery that goes straight to FAILED.
    Those are exactly the paths the fault gates exercise, so a gate run left one
    full checkout of qf-research per hard kill, and `qf-runs-prune` ignores them
    for ninety days -- on a host whose admission floor is 20 GiB of the same
    filesystem.

    A sweep rather than a call bolted onto each terminal transition: enumerating
    the ways a job can end is a list that goes stale, while "terminal and still
    has a worktree" is a condition.
    """

    def reaper(self):
        return qfd.Reaper(self.cfg, self.db, self.runner, self.disp,
                          self.docker)

    def plant(self, run_id, state):
        self.a_job(run_id=run_id, state="QUEUED")
        if state != "QUEUED":
            self.db.call("dequeue", "light", owner="qfd",
                         now="2026-08-25T10:00:01Z",
                         lease_expires_at="2026-08-25T10:05:00Z",
                         hold_deadline_at="2036-08-25T12:00:00Z", max_running=2)
            for step in ("BUILDING", "RUNNING", state):
                if step == "RUNNING" and state == "BUILDING":
                    break
                self.db.call("transition", run_id, step,
                             now="2026-08-25T10:00:02Z")
                if step == state:
                    break
        src = os.path.join(self.runs, run_id, "src")
        os.makedirs(src)
        return src

    def test_a_worktree_left_by_a_reclaimed_job_is_reclaimed(self):
        src = self.plant("gone", "FAILED")
        self.assertEqual(self.reaper().sweep_worktrees(), 1)
        self.assertIn(src, self.src.removed)

    def test_a_live_jobs_worktree_is_left_alone(self):
        # The dangerous direction: removing the source tree out from under a
        # running container.
        src = self.plant("live", "RUNNING")
        self.assertEqual(self.reaper().sweep_worktrees(), 0)
        self.assertNotIn(src, self.src.removed)

    def test_a_run_directory_with_no_job_row_is_still_swept(self):
        # Driven from the filesystem on purpose, so a directory whose row is gone
        # entirely has an owner too.
        src = os.path.join(self.runs, "orphan-dir", "src")
        os.makedirs(src)
        self.assertEqual(self.reaper().sweep_worktrees(), 1)
        self.assertIn(src, self.src.removed)

    def test_the_sweep_is_bounded(self):
        for i in range(qfd.Reaper.WORKTREE_SWEEP_LIMIT + 5):
            os.makedirs(os.path.join(self.runs, f"r{i:03d}", "src"))
        self.assertEqual(self.reaper().sweep_worktrees(),
                         qfd.Reaper.WORKTREE_SWEEP_LIMIT)


class TestAdmissionRefusalsAreLoggedAtTransitions(RunnerCase):
    """One line when it starts, one when it clears -- not one per poll.

    The 2026-08-27 journal carried two identical `disk floor: 12043m free is
    below 20480m` lines every two seconds. The number moves, so keying the
    de-duplication on the whole message would log every poll anyway; the KIND is
    what changes rarely, and it is what an operator is reading for.
    """

    def setUp(self):
        super().setUp()
        # A floor nothing can satisfy, so step 4 refuses deterministically.
        self.db.stop()
        self.db = qfd.DbOwner(os.path.join(self.root, "state2.db"),
                              mem_budget_mb=self.cfg.mem_budget_mb,
                              disk_floor_mb=1 << 30, out_quota_mb=0,
                              artifact_cap_mb=0).start()
        self.addCleanup(self.db.stop)
        self.disp = qfd.Dispatcher(self.cfg, self.db, docker=self.docker,
                                   src=self.src)
        self.runner = qfd.Runner(self.cfg, self.db, self.disp,
                                 docker=self.docker, src=self.src)

    def test_a_persistent_refusal_is_logged_once(self):
        self.a_job(state="QUEUED")
        with self.assertLogs(qfd.log, level="INFO") as caught:
            for _ in range(6):
                self.assertFalse(self.runner.try_one("light"))
        lines = [line for line in caught.output if "not admitting" in line]
        self.assertEqual(len(lines), 1, caught.output)
        self.assertIn("disk floor", lines[0])

    def test_the_detail_still_carries_the_numbers(self):
        self.a_job(state="QUEUED")
        with self.assertLogs(qfd.log, level="INFO") as caught:
            self.runner.try_one("light")
        self.assertRegex("\n".join(caught.output), r"\d+m")


class TestAnUnusableMutexIsNotContention(RunnerCase):
    """The live failure of 2026-08-26 22:05.

    NC8 deleted the training lock inode; the heavy worker then raised a bare
    `PermissionError` out of `os.open` every two seconds. The worker loop caught
    it and carried on, so the lane was never lost -- but the journal filled with
    tracebacks and the one actionable fact (the mutex inode is unusable, and one
    command restores it) appeared nowhere as a sentence.

    Contention and an unusable inode need different words: the first clears by
    itself, the second clears only when a human acts.
    """

    def test_an_unopenable_lock_raises_mutex_unusable_not_lock_held(self):
        lock = qfd.TrainingLock(os.path.join(self.runs, "no-such-lock"), "light")
        with self.assertRaises(qfd.MutexUnusable):
            lock.acquire()

    def test_mutex_unusable_is_not_a_lock_held_subclass(self):
        # If it were, `try_one` would report a broken mutex as ordinary
        # contention and wait for it for ever, and Recovery would take the
        # mutex_lost path over a configuration fault.
        self.assertFalse(issubclass(qfd.MutexUnusable, qfd.LockHeld))

    def test_try_one_refuses_rather_than_raising(self):
        self.a_job(state="QUEUED")
        self.runner.cfg.lock_file = os.path.join(self.runs, "no-such-lock")
        with self.assertLogs(qfd.log, level="ERROR") as caught:
            self.assertFalse(self.runner.try_one("light"))
        self.assertIn("systemd-tmpfiles", "\n".join(caught.output),
                      "the refusal must carry the remedy; an operator cannot act"
                      " on 'Permission denied'")

    def test_the_fault_is_logged_once_not_once_per_poll(self):
        self.a_job(state="QUEUED")
        self.runner.cfg.lock_file = os.path.join(self.runs, "no-such-lock")
        with self.assertLogs(qfd.log, level="ERROR") as caught:
            for _ in range(5):
                self.runner.try_one("light")
        self.assertEqual(len(caught.output), 1, caught.output)

    def test_contention_is_logged_at_the_transition_with_a_duration(self):
        # 900 identical lines across one nightly run is unreadable, and hides the
        # only interesting number: how long the wait lasted.
        self.a_job(state="QUEUED")
        held = qfd.TrainingLock(self.runner.cfg.lock_file, "heavy").acquire()
        self.addCleanup(held.release)
        with self.assertLogs(qfd.log, level="INFO") as caught:
            for _ in range(5):
                self.runner.try_one("light")
        waits = [line for line in caught.output
                 if "waiting for the training mutex" in line]
        self.assertEqual(len(waits), 1, caught.output)
        held.release()
        with self.assertLogs(qfd.log, level="INFO") as caught:
            self.runner.try_one("light")
        self.assertTrue(any("acquired after" in line for line in caught.output),
                        caught.output)


class TestExitCodeClassification(RunnerCase):
    """An error_class is a ROUTING decision (same rule as the source classes).

    The live case: `--kind test` against a repository whose tests are not at the
    default path exited 4 -- pytest's "usage error" -- and was reported as
    `nonzero_exit`, i.e. "the experiment failed". Nothing in the record said the
    experiment never ran, so the next step is to go and debug code that was never
    executed. On a loop that will make this mistake repeatedly, that is the
    difference between a one-line fix to the submission and a wild goose chase.
    """

    def test_a_pytest_usage_error_is_not_a_failed_experiment(self):
        self.assertEqual(self.runner._exit_class(4), "bad_invocation")

    def test_collecting_nothing_is_its_own_class(self):
        self.assertEqual(self.runner._exit_class(5), "no_tests_collected")

    def test_a_real_test_failure_is_still_nonzero_exit(self):
        # 1 is pytest's "tests ran and failed", which IS a failed experiment.
        for code in (1, 2, 3, 127):
            with self.subTest(code=code):
                self.assertEqual(self.runner._exit_class(code), "nonzero_exit")


class TestSourceFailuresAreRouted(RunnerCase):
    """An error_class is a ROUTING decision, so one that names the wrong
    subsystem costs an investigation.

    Only `NotPublished` and `Timeout` were handled; every other git failure --
    a token that cannot read the remote, DNS, a remote that refuses, a corrupt
    mirror -- fell through to the generic handler and was reported as
    `internal`, which sends the operator to read dispatcher tracebacks about a
    fault in the source or the credential. Seen for real on the first live
    submit.
    """

    def run_with_source_error(self, exc):
        job = self.a_job(state="LEASED")
        hold = self.hold_for(job)
        self.addCleanup(lambda: hold.lock.held and hold.lock.release())

        def resolve(sha, deadline=None):
            raise exc

        self.src.resolve = resolve
        self.runner.execute(hold)
        return self.db.call("get", "r1")

    def test_an_unreachable_remote_is_not_called_internal(self):
        job = self.run_with_source_error(
            source.SourceError("fatal: could not read Username for"
                               " 'https://github.com': No such device"))
        self.assertEqual(job["state"], "FAILED")
        self.assertEqual(job["error_class"], "source_unavailable")

    def test_an_unpublished_sha_keeps_its_own_class(self):
        job = self.run_with_source_error(source.NotPublished("nope"))
        self.assertEqual(job["error_class"], "source_not_published")

    def test_a_hung_fetch_keeps_its_own_class(self):
        # Subclass ordering: Timeout and NotPublished are both SourceError, so a
        # base-class handler placed above them would swallow both.
        job = self.run_with_source_error(source.Timeout("hung"))
        self.assertEqual(job["error_class"], "source_timeout")

    def test_a_genuine_bug_is_still_internal(self):
        # The new clause must not widen into "everything is the source's fault".
        job = self.run_with_source_error(ZeroDivisionError("real bug"))
        self.assertEqual(job["error_class"], "internal")


class TestTheLogPumpKeepsDrainingAfterTheCap(unittest.TestCase):
    """A log-flooding job was never killed for `log_overflow`. It sat in
    `proc.wait()` for its full 1800s timeout and NC15 read it as
    TIMEOUT_WAITING with error_class NULL.

    The watcher was fine and the cap was fine -- the file stopped at exactly
    cap+len(MARKER). The pump was the problem: it BROKE out of its read loop on
    overflow, and `docker start --attach` streams the container's output into
    that pipe. A full pipe with no reader blocks the CLI in write(), so the
    process could not exit however hard the watcher killed the container. The
    disk-flood twin passed because it writes to /out and leaves its pipe drained.

    Killing a process does not help if what it is blocked on is a pipe nobody is
    reading."""

    def _run_pump(self, cap, payload_bytes, join_timeout=20):
        """Drive Runner._pump against real pipes. Returns (writer, produced_ok).

        `produced_ok` is False if a producer thread was still blocked writing --
        which is the deadlock, reproduced.
        """
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        producers, files, writers = [], [], []
        streams = {}
        for name in ("stdout", "stderr"):
            r, w = os.pipe()
            streams[name] = os.fdopen(r, "rb")
            writer = qfd.BoundedWriter(os.path.join(tmp, name + ".log"), cap)
            writers.append(writer)
            files.append(os.path.join(tmp, name + ".log"))

            def produce(fd=w):
                try:
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(b"x" * payload_bytes)
                except BrokenPipeError:
                    pass

            t = threading.Thread(target=produce, daemon=True)
            producers.append(t)

        proc = types.SimpleNamespace(stdout=streams["stdout"],
                                     stderr=streams["stderr"])
        for t in producers:
            t.start()
        # self is unused by _pump.
        pump_threads = qfd.Runner._pump(None, proc, writers[0], writers[1])
        produced_ok = True
        for t in producers:
            t.join(timeout=join_timeout)
            if t.is_alive():
                produced_ok = False
        for t in pump_threads:
            t.join(timeout=5)
        for w_ in writers:
            w_.close()
        for s in streams.values():
            s.close()
        return writers, files, produced_ok

    def test_a_producer_far_past_the_cap_is_not_blocked(self):
        # 4 MiB through a 4 KiB cap: ~64 pipe-buffers' worth. If the pump stops
        # reading at the cap, the producer blocks for ever and this fails.
        cap = 4096
        writers, files, produced_ok = self._run_pump(cap, 4 << 20)
        self.assertTrue(produced_ok,
                        "a producer was still blocked writing: the pump stopped"
                        " draining, which is what wedged proc.wait() for 1800s")
        self.assertTrue(writers[0].overflowed)

    def test_the_file_is_still_bounded_at_the_cap(self):
        # Draining must not mean writing: the bound is the whole point of the
        # BoundedWriter, and NC15 asserts it independently on the host.
        cap = 4096
        writers, files, _ = self._run_pump(cap, 4 << 20)
        for path in files:
            size = os.path.getsize(path)
            self.assertEqual(size, cap + len(qfd.BoundedWriter.MARKER),
                             f"{path} is {size}B, cap is {cap}B")

    def test_a_producer_under_the_cap_still_reaches_eof_normally(self):
        writers, files, produced_ok = self._run_pump(1 << 20, 1024)
        self.assertTrue(produced_ok)
        self.assertFalse(writers[0].overflowed)
        self.assertEqual(os.path.getsize(files[0]), 1024)


class ExtractRelayCase(RunnerCase):
    """Task 5's relay, driven through `execute()` and `finish()`.

    The first version of these tests called `_relay_extract` and inspected its
    return tuple, and that is why they all passed over a fatal defect: `finish`
    persists the state, and the machine has no LEASED -> SUCCEEDED edge, so every
    successful extraction recorded "cannot move LEASED -> SUCCEEDED" and left the
    row LEASED with no exit code. **A unit test of a function is not a test of
    its caller**, and this file now goes through the caller.
    """

    REQUEST_HASH = None      # computed in setUp, from the real validator

    def setUp(self):
        super().setUp()
        self.sent = []
        self.effective = self.an_extract_job()
        self.REQUEST_HASH = self.effective["source_sha"]
        self.reply = self.a_reply()

        def client(path, payload, timeout):
            self.sent.append((path, payload, timeout))
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply

        self.runner.extract_client = client

    def a_reply(self, **over):
        """A reply that PASSES validation, so each test breaks exactly one rule."""
        manifest = {
            "request_hash": self.REQUEST_HASH,
            "extract_hash": "b" * 64,
            "watermark": {"pending_at": "2026-08-25T23:59:59+00:00"},
            "files": {name: {"sha256": "c" * 64, "rows": 10}
                      for name in ("runs", "worker_counts", "worker_pools",
                                   "throughput_runs", "qctx_runs",
                                   "daily_health")},
        }
        manifest.update(over.pop("manifest", {}))
        # `str()` because some tests deliberately set request_hash to None or an
        # int to exercise the validator; the fixture must build a reply, not
        # crash while building one.
        reply = {"ok": True, "manifest": manifest,
                 "dir": "/var/lib/qf-extracts/"
                        + str(manifest.get("request_hash"))}
        reply.update(over)
        return reply

    def an_extract_job(self, **over):
        args = {"target": "wait_time",
                "train_start": "2026-07-01T00:00:00Z",
                "as_of_date": "2026-08-01T00:00:00Z",
                "lookback_days": 30}
        args.update(over.pop("args", {}))
        effective = dict(spec.normalize(
            {"schema": 1, "kind": "extract", "args": args},
            now=self.runner.clock_dt(),
            settlement_lag_s=self.cfg.settlement_lag_s))
        effective.update(over)
        return effective

    def submit_extract(self, effective=None, state="LEASED"):
        """A real row, dequeued to LEASED, with a hold that is NOT the mutex."""
        effective = self.effective if effective is None else effective
        # UNIQUE per submit. Several tests submit more than once in one method
        # (a `subTest` loop over invalid replies), and a repeated run id is a
        # primary-key collision that reads as a validator failure.
        self._seq = getattr(self, "_seq", 0) + 1
        run_id = (f"extract-20260828T000000Z-{effective['source_sha'][:12]}"
                  f"-{self._seq}")
        self.db.call("submit", effective, run_id=run_id, uid=1001,
                     now="2026-08-28T00:00:00Z")
        self.db.call("dequeue", effective["lane"], owner="qfd",
                     now="2026-08-28T00:00:01Z",
                     lease_expires_at="2036-08-28T00:05:00Z",
                     hold_deadline_at="2036-08-28T02:00:00Z", max_running=2)
        job = self.db.call("get", run_id)
        # An ExtractSlot, not a TrainingLock: that substitution IS the fix for
        # extracts holding the nightly's mutex.
        lock = qfd.ExtractSlot(self.runner._extract_slot,
                               effective["lane"]).acquire()
        self.addCleanup(lock.release)
        return qfd.Hold(job, lock, qfd.parse_iso(job["hold_deadline_at"]))


class TestAnExtractReachesATerminalState(ExtractRelayCase):
    def test_a_successful_extraction_reaches_SUCCEEDED(self):
        # THE REGRESSION. This is what the previous tests could not see.
        hold = self.submit_extract()
        self.runner.execute(hold)
        job = self.db.call("get", hold.run_id)
        self.assertEqual(job["state"], "SUCCEEDED", job)
        self.assertEqual(job["exit_code"], 0)
        self.assertIsNotNone(job["finished_at"])

    def test_it_passes_through_RUNNING_on_the_way(self):
        # LEASED -> SUCCEEDED is not an edge, and RUNNING is also the honest
        # answer while the extractor is working.
        hold = self.submit_extract()
        seen = []
        original = self.db.call

        def spy(method, *a, **kw):
            if method == "transition":
                seen.append(a[1])
            return original(method, *a, **kw)

        self.db.call = spy
        self.runner.execute(hold)
        self.assertIn("RUNNING", seen)

    def test_a_failed_relay_reaches_FAILED_with_its_class(self):
        self.reply = qfd.ExtractRelayError("no socket", "extractor_unreachable")
        hold = self.submit_extract()
        self.runner.execute(hold)
        job = self.db.call("get", hold.run_id)
        self.assertEqual(job["state"], "FAILED")
        self.assertEqual(job["error_class"], "extractor_unreachable")

    def test_the_pins_survive_to_the_finished_row(self):
        hold = self.submit_extract()
        self.runner.execute(hold)
        pins = self.db.call("pins_for", hold.run_id)
        self.assertEqual(pins["extract_hash"], "b" * 64)
        self.assertEqual(pins["request_hash"], self.REQUEST_HASH)
        self.assertIn("qf-extracts", pins["extract_dir"])
        self.assertIn("pending_at", pins["extract_watermark"])

    def test_no_container_no_worktree_no_image(self):
        # The branch short-circuits before all of it. If any of these were
        # reached, an extraction would need a research commit it does not have.
        touched = []
        self.src.resolve = lambda *a, **kw: touched.append("resolve")
        self.src.add_worktree = lambda *a, **kw: touched.append("worktree")
        self.runner._ensure_image = lambda *a, **kw: touched.append("image")
        self.runner.execute(self.submit_extract())
        self.assertEqual(touched, [])


class TestAnExtractDoesNotHoldTheTrainingMutex(ExtractRelayCase):
    """The nightly acquires the training lock EXCLUSIVELY. A shared holder is
    what blocks it, and a minutes-long extraction was taking one."""

    def test_the_lock_is_chosen_before_the_kind_is_known_to_nobody(self):
        # Asserted on the source: `peek` returns the row, so the kind is
        # available at step 1 and the lock decision belongs at step 3.
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "qfd.py")) as fh:
            source = fh.read()
        step3 = source[source.index("try:                                                    # 3"):]
        step3 = step3[:step3.index("except MutexUnusable")]
        self.assertIn("ExtractSlot", step3)
        self.assertIn("is_extract", step3)

    def test_the_training_lock_is_free_while_an_extract_runs(self):
        # The property, measured rather than argued: an exclusive acquisition
        # must succeed while an extract job holds its slot.
        hold = self.submit_extract()
        self.assertEqual(qfd.probe_mutex(self.cfg.lock_file), "free")
        self.runner.execute(hold)
        self.assertEqual(qfd.probe_mutex(self.cfg.lock_file), "free")

    def test_a_second_extraction_waits_rather_than_failing(self):
        # The extractor's own mutex is NON-BLOCKING, so without a slot here the
        # second job would be refused instead of queued.
        first = qfd.ExtractSlot(self.runner._extract_slot, "light").acquire()
        self.addCleanup(first.release)
        with self.assertRaises(qfd.LockHeld):
            qfd.ExtractSlot(self.runner._extract_slot, "light").acquire()

    def test_the_slot_is_released_so_the_next_one_proceeds(self):
        slot = qfd.ExtractSlot(self.runner._extract_slot, "light").acquire()
        slot.release()
        again = qfd.ExtractSlot(self.runner._extract_slot, "light").acquire()
        again.release()

    def test_releasing_twice_is_harmless(self):
        # Every release path in the runner calls `release` on whatever the hold
        # holds, and some call it more than once.
        slot = qfd.ExtractSlot(self.runner._extract_slot, "light").acquire()
        slot.release()
        slot.release()
        qfd.ExtractSlot(self.runner._extract_slot, "light").acquire().release()


class TestAReplySayingOkIsNotAnExtract(ExtractRelayCase):
    """The previous version accepted `{"ok": true, "manifest": {}}`, skipped
    every missing pin and recorded SUCCEEDED -- and a test enshrined that as
    "does not crash". A job row claiming an extract that is not there is worse
    than a failure, because 2b-2 will go looking for it.

    The extractor is trusted, and "trusted" is not "incapable of a bug"."""

    def outcome(self, **over):
        self.reply = self.a_reply(**over)
        hold = self.submit_extract()
        self.runner.execute(hold)
        return self.db.call("get", hold.run_id)

    def test_an_empty_manifest_is_refused(self):
        self.reply = {"ok": True, "manifest": {}}
        hold = self.submit_extract()
        self.runner.execute(hold)
        job = self.db.call("get", hold.run_id)
        self.assertEqual(job["state"], "FAILED")
        self.assertEqual(job["error_class"], "extract_reply_invalid")

    def test_a_missing_or_malformed_hash_is_refused(self):
        for field in ("request_hash", "extract_hash"):
            for value in (None, "", "short", 7):
                with self.subTest(field=field, value=value):
                    job = self.outcome(manifest={field: value})
                    self.assertEqual(job["error_class"],
                                     "extract_reply_invalid")

    def test_a_reply_about_a_different_request_is_refused(self):
        # The field capable of pointing this row at somebody else's extract:
        # reuse is keyed on request_hash (D20).
        job = self.outcome(manifest={"request_hash": "d" * 64})
        self.assertEqual(job["error_class"], "extract_reply_invalid")

    def test_a_directory_outside_the_canonical_location_is_refused(self):
        for directory in ("", None, "/tmp/somewhere",
                          "/var/lib/qf-extracts/" + "e" * 64):
            with self.subTest(directory=directory):
                job = self.outcome(dir=directory)
                self.assertEqual(job["error_class"], "extract_reply_invalid")

    def test_a_dataset_with_no_rows_is_refused(self):
        files = {name: {"sha256": "c" * 64, "rows": 10}
                 for name in ("runs", "worker_counts")}
        files["runs"] = {"sha256": "c" * 64, "rows": 0}
        job = self.outcome(manifest={"files": files})
        self.assertEqual(job["error_class"], "extract_reply_invalid")

    def test_a_file_entry_with_no_digest_is_refused(self):
        job = self.outcome(manifest={"files": {"runs": {"rows": 10}}})
        self.assertEqual(job["error_class"], "extract_reply_invalid")

    def test_a_manifest_with_no_watermark_is_refused(self):
        job = self.outcome(manifest={"watermark": {}})
        self.assertEqual(job["error_class"], "extract_reply_invalid")

    def test_nothing_is_pinned_when_the_reply_is_refused(self):
        # A partial pin set would advertise an extract the row does not have.
        self.reply = {"ok": True, "manifest": {}}
        hold = self.submit_extract()
        self.runner.execute(hold)
        self.assertEqual(self.db.call("pins_for", hold.run_id), {})

    def test_the_valid_reply_still_passes(self):
        # The positive canary for this whole class: if a good reply cannot pass,
        # every refusal above proves nothing.
        job = self.outcome()
        self.assertEqual(job["state"], "SUCCEEDED")


class TestCancellingDuringAnExtraction(ExtractRelayCase):
    """A cancel cannot stop an extraction -- the work is in a domain this
    process has no authority to signal -- so by the time the reply arrives the
    extract is published and immutable. What is still true is that the operator
    asked, and the job's state has to say so."""

    def test_a_cancelled_job_reports_CANCELLED_not_SUCCEEDED(self):
        hold = self.submit_extract()
        hold.cancel_requested.set()
        self.runner.execute(hold)
        job = self.db.call("get", hold.run_id)
        self.assertEqual(job["state"], "CANCELLED")
        self.assertEqual(job["error_class"], "cancelled")

    def test_the_pins_are_still_recorded(self):
        # The extract exists and is immutable. Losing its identity because the
        # job was cancelled would orphan an artifact on disk.
        hold = self.submit_extract()
        hold.cancel_requested.set()
        self.runner.execute(hold)
        pins = self.db.call("pins_for", hold.run_id)
        self.assertEqual(pins["extract_hash"], "b" * 64)

    def test_an_uncancelled_job_still_succeeds(self):
        hold = self.submit_extract()
        self.runner.execute(hold)
        self.assertEqual(self.db.call("get", hold.run_id)["state"],
                         "SUCCEEDED")


class TestTheRelayForwardsTheRightThing(ExtractRelayCase):
    def test_it_forwards_the_input_fields_only(self):
        self.runner.execute(self.submit_extract())
        _path, payload, _timeout = self.sent[0]
        self.assertEqual(payload["op"], "extract")
        for derived in ("target_column", "window_lower", "ref_lower"):
            with self.subTest(field=derived):
                self.assertNotIn(derived, payload["request"])
        self.assertEqual(payload["request"]["target"], "wait_time")

    def test_the_timeout_is_bounded_by_the_hold(self):
        hold = self.submit_extract()
        hold.deadline_epoch = self.runner.disp.clock() + 120
        self.runner.execute(hold)
        self.assertLessEqual(self.sent[0][2], 120)

    def test_the_timeout_is_generous_enough_for_a_measured_extraction(self):
        # 688s measured for 36 days. A short timeout abandons work the extractor
        # completes and publishes, leaving an extract nothing points at.
        self.runner.execute(self.submit_extract())
        self.assertGreaterEqual(self.sent[0][2], 688)


