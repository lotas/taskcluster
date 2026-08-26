# Tests for the runner's safety rules. Docker and git are injected, so these
# assert the decisions rather than the daemon: what gets released, when, and on
# what evidence.
#
# The rule under test throughout is that a RELEASE IS A CLAIM ABOUT REALITY. A
# subprocess timeout on `docker kill` proves the CLI stopped waiting, not that
# the workload died, and closing the training descriptor on that basis hands the
# mutex to the nightly run while live work continues.
import json
import os
import subprocess
import tempfile
import types
import unittest

import qfd
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
