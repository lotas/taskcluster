# Regression tests for the ten defects found in review of the first Phase 2a-1
# implementation. Each names the defect and asserts the behaviour that was
# missing -- these are the tests whose absence let each bug ship.
#
# The headline one is #1: the daemon accepted jobs and never ran them, because
# main() started sockets and slept. Nothing here exercised a Worker, so nothing
# noticed.
import errno
import inspect
import io
import sqlite3
import json
import os
import time
import subprocess
import sys
import tempfile
import threading
import types
import unittest

import qfd
import sandbox
import source
import spec
import store

SHA = "3f1c" + "0" * 36


def proc(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout,
                                 stderr=stderr)


class FakeProc:
    """Stands in for a `docker run` child."""

    def __init__(self, returncode=0, out=b"", err=b"", block=None):
        self.returncode = returncode
        self.stdout = io.BytesIO(out)
        self.stderr = io.BytesIO(err)
        self._block = block
        self.killed = False

    def wait(self, timeout=None):
        if self._block is not None and not self.killed:
            if not self._block.wait(timeout):
                raise subprocess.TimeoutExpired("docker", timeout or 0)
        return self.returncode

    def kill(self):
        # A real Popen.kill unblocks the pending wait, so this one does too:
        # a fake that ignores SIGKILL would make `_reap` look like an infinite
        # loop it is not.
        self.killed = True
        self.returncode = -9


class FakeDocker:
    def __init__(self, states=None):
        self.states = states or {}
        self.calls = []

    def run(self, argv, env=None, timeout=60):
        self.calls.append(argv)
        if argv[:2] == ["docker", "inspect"]:
            return proc(0, "12345\n")
        if argv[:3] == ["docker", "image", "inspect"]:
            return proc(0, "sha256:" + "a" * 64 + "\n")
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


def is_start(argv, role):
    """True for the `docker start --attach qf-<run_id>-<role>` half.

    Under create-then-start every flag -- including `--label qf.role=<role>` --
    lives on the CREATE argv, and the start argv is three words and a name. A
    test that kept matching on the label would silently match nothing and
    assert nothing, so the matcher lives here once.
    """
    return (list(argv[:3]) == ["docker", "start", "--attach"]
            and str(argv[-1]).endswith(f"-{role}"))


def is_create(argv, role):
    """True for the `docker create ... --label qf.role=<role> ...` half."""
    return list(argv[:2]) == ["docker", "create"] and f"qf.role={role}" in argv


class Base(unittest.TestCase):
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
        # A trusted fixture with a PINNED base. The shipped
        # trainer-env.Dockerfile carries `@sha256:REPLACE_ME` on purpose -- Task
        # 11 pastes the real digest -- so pointing these tests at the real
        # directory makes every run fail as `image_build_failed` before reaching
        # the code under test.
        trusted = os.path.join(root, "trusted")
        os.makedirs(os.path.join(trusted, "env"))
        with open(os.path.join(trusted, "trainer-env.Dockerfile"), "w") as fh:
            fh.write("FROM example/base@sha256:" + "a" * 64 + "\nRUN true\n")
        for name in ("pyproject.toml", "uv.lock"):
            with open(os.path.join(trusted, "env", name), "w") as fh:
                fh.write("x\n")
        for name in ("handoff-inside.sh", "nc13-inside.sh"):
            with open(os.path.join(trusted, name), "w") as fh:
                fh.write("#!/bin/sh\nexit 0\n")

        self.cfg = qfd.Config(
            extract_socket="/nonexistent/extract.sock",
            settlement_lag_s=48 * 3600,
            trusted_dir=trusted, state_dir=root, runs_dir=self.runs,
            socket_path=os.path.join(root, "c.sock"),
            admin_socket_path=os.path.join(root, "a.sock"), admin_uid=4242,
            remote="https://example.invalid/x", token_file="",
            lock_file=self.lock_path, intent_dir=self.intent,
            build_lock=os.path.join(root, "build.lock"),
            build_timeout_s=1800, build_lock_wait_s=900, build_settle_s=0,
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
        self.runner.out_sample_interval_s = 0.02
        self.runner.mem_sample_interval_s = 0.02
        # Record every spawn. The handoff now starts via `spawn` (Popen inside
        # its gate) rather than `docker.run`, so assertions about whether a
        # handoff container started must look here -- checking `docker.calls`
        # would silently become vacuous.
        self.spawned = []
        self.runner.spawn = self._recording_spawn(lambda: FakeProc())

    def _recording_spawn(self, factory):
        def spawn(argv, **kw):
            self.spawned.append(argv)
            return factory()
        return spawn

    def open_log_fds(self, run_id):
        """Log files this process currently holds open, read from the fd table.

        `-W error::ResourceWarning` is NOT a sound check for this. A
        ResourceWarning raised while a file object is being finalised becomes an
        "Exception ignored" UNRAISABLE exception, not a test failure, so a suite
        can print the warning and still report OK -- which is exactly what
        happened: the leak was live and 354 warning-strict tests passed. Asking
        the fd table is direct and cannot be swallowed.
        """
        logs = os.path.join(self.runs, run_id, "logs")
        held = []
        for fd in os.listdir("/proc/self/fd"):
            try:
                target = os.readlink(os.path.join("/proc/self/fd", fd))
            except OSError:
                continue
            if target.startswith(logs):
                held.append(os.path.basename(target))
        return sorted(held)

    def ready(self, deadline=2 ** 31):
        """A RUNNING job with a registered hold, a lock and an artifact to
        collect: the state a phase start actually happens from."""
        run_id = self.submit()
        self.db.call("dequeue", "light", owner="qfd", now=qfd.utcnow(),
                     lease_expires_at="2036-01-01T00:00:00Z",
                     hold_deadline_at="2036-01-01T00:00:00Z", max_running=2)
        self.db.call("transition", run_id, "RUNNING", now=qfd.utcnow())
        lock = qfd.TrainingLock(self.cfg.lock_file, "light").acquire()
        self.addCleanup(lock.release)
        hold = qfd.Hold(self.db.call("get", run_id), lock, deadline)
        hold.image_id = "sha256:" + "a" * 64
        self.disp.register_hold(hold)
        paths = self.runner.prepare_run_dir(run_id)
        with open(os.path.join(paths["out"], "result.json"), "w") as fh:
            fh.write("{}")
        return run_id, hold, paths

    def handoff_spawns(self):
        return [a for a in self.spawned if is_start(a, "handoff")]

    def candidate_spawns(self):
        return [a for a in self.spawned if is_start(a, "candidate")]

    def creates(self, role):
        """The CREATE calls, which is where a container first exists at all.
        `handoff_spawns` answers "was it started"; this answers "was it made"."""
        return [a for a in self.docker.calls if is_create(a, role)]

    def submit(self, run_id=None, **over):
        resp = self.disp.handle("submit",
                                {"spec": {"schema": 1, "kind": "test",
                                          "source_sha": SHA, **over}}, 1000)
        self.assertTrue(resp["ok"], resp)
        return resp["run_id"]


# =========================================================================
class TestDefect1DaemonExecutesJobs(Base):
    """#1 (P0): main() started sockets and slept forever. It never instantiated
    Runner/Recovery, never started workers, never ran a reaper -- so `submit`
    reported success while every job stayed QUEUED forever."""

    def test_a_worker_drives_a_queued_job_to_a_terminal_state(self):
        run_id = self.submit()
        self.assertEqual(self.db.call("get", run_id)["state"], "QUEUED")
        worker = qfd.Worker(self.runner, "light", 0, idle_sleep_s=0.01)
        worker.start()
        self.addCleanup(worker.stop_event.set)
        for _ in range(400):
            if self.db.call("get", run_id)["state"] in store.TERMINAL:
                break
            threading.Event().wait(0.02)
        job = self.db.call("get", run_id)
        self.assertIn(job["state"], store.TERMINAL,
                      f"the job never ran; state={job['state']}")
        self.assertEqual(job["state"], "SUCCEEDED", job)
        ok, problems = self.db.call("verify_chain")
        self.assertTrue(ok, problems)

    def test_try_one_returns_true_when_it_ran_something(self):
        self.submit()
        self.assertTrue(self.runner.try_one("light"))
        self.assertFalse(self.runner.try_one("light"))   # queue now empty

    def test_the_lane_lock_is_released_after_a_completed_run(self):
        self.submit()
        self.runner.try_one("light")
        # Nothing left holding it, so an exclusive acquire must succeed.
        qfd.TrainingLock(self.cfg.lock_file, "heavy").acquire().release()
        self.assertEqual(self.db.call("admitted_mem_mb"), 0)

    def test_the_worker_pool_is_two_light_and_one_heavy(self):
        self.assertEqual(self.cfg.light_workers, 2)

    def test_the_reaper_sweeps_and_resolves(self):
        reaper = qfd.Reaper(self.cfg, self.db, self.runner, self.disp,
                            self.docker)
        self.assertEqual(reaper.sweep(), [])   # nothing expired yet


class TestDefect2RunDirOwnership(Base):
    """#2: prepare_run_dir was called with neither gid, so out/ stayed qfd:qfd
    and container gid 10001 could not write it; artifacts/ and logs/ were
    unreachable for qfclient. The layout test asserted modes only."""

    def test_the_runner_passes_both_gids(self):
        seen = {}
        real = self.runner.prepare_run_dir

        def spy(run_id, **kw):
            seen.update(kw)
            return real(run_id, **kw)

        self.runner.prepare_run_dir = spy
        self.submit()
        self.runner.try_one("light")
        self.assertIn("qfrun_gid", seen)
        self.assertIn("qfclient_gid", seen)
        self.assertEqual(seen["qfrun_gid"], qfd._gid("qfrun"))
        self.assertEqual(seen["qfclient_gid"], qfd._gid("qfclient"))

    def test_gids_are_applied_when_the_groups_exist(self):
        gid = os.getgid()
        paths = self.runner.prepare_run_dir("r-gid", qfrun_gid=gid,
                                            qfclient_gid=gid)
        self.assertEqual(os.stat(paths["out"]).st_gid, gid)
        self.assertEqual(os.stat(paths["artifacts"]).st_gid, gid)
        # And the setgid bit survives the chown -- chown clears it on Linux, so
        # the order of chown-then-chmod is load-bearing.
        self.assertEqual(os.stat(paths["out"]).st_mode & 0o7777, 0o2770)

    def test_a_missing_group_means_leave_ownership_alone(self):
        self.assertIsNone(qfd._gid("a-group-that-does-not-exist"))
        self.runner.prepare_run_dir("r-nogid", qfrun_gid=None,
                                    qfclient_gid=None)


class TestDefect3ImageRefIsNotStale(Base):
    """#3: _ensure_image wrote image_digest to SQLite but not to hold.job, so
    the handoff built an argv with image_ref=None, raised SandboxError, and
    every artifact-producing job finished as `internal`."""

    def test_the_hold_carries_the_image_id(self):
        self.submit()
        holds = []
        real = self.runner.execute

        def spy(hold):
            holds.append(hold)
            return real(hold)

        self.runner.execute = spy
        self.runner.try_one("light")
        self.assertEqual(len(holds), 1)
        self.assertRegex(holds[0].image_id or "", r"^sha256:[0-9a-f]{64}$")

    def test_a_job_producing_an_artifact_does_not_finish_as_internal(self):
        # The exact reproduction: with result.json present the handoff runs, and
        # with a None image ref it used to raise SandboxError.
        run_id = self.submit()
        real_prepare = self.runner.prepare_run_dir

        def prepare_with_artifact(rid, **kw):
            paths = real_prepare(rid, **kw)
            with open(os.path.join(paths["out"], "result.json"), "w") as fh:
                fh.write("{}")
            return paths

        self.runner.prepare_run_dir = prepare_with_artifact
        self.runner.try_one("light")
        job = self.db.call("get", run_id)
        self.assertNotEqual(job["error_class"], "internal", job)
        self.assertEqual(job["state"], "SUCCEEDED", job)
        arts = self.db.call("resources_for", run_id)
        self.assertTrue(any(r["role"] == "handoff" for r in arts))


class TestDefect4RecordedBeforeTheyCanExist(Base):
    """#4: the candidate was recorded AFTER Popen and the handoff after
    `docker run` returned. A crash in either window left a live container with
    no `resources` row -- and recovery would then find an empty inventory, take
    the build-settle path, and release the training mutex over live work."""

    def test_the_candidate_row_exists_before_the_container_starts(self):
        order = []
        run_id = self.submit()

        def spawn(*a, **k):
            order.append(("spawn", self.db.call("resources_for", run_id)))
            return FakeProc()

        self.runner.spawn = spawn
        self.runner.try_one("light")
        self.assertEqual(len(order), 1)
        recorded_at_spawn = order[0][1]
        self.assertTrue(recorded_at_spawn,
                        "the container was started before it was recorded")
        self.assertEqual(recorded_at_spawn[0]["role"], "candidate")

    def test_the_recorded_id_is_the_deterministic_name(self):
        run_id = self.submit()
        self.runner.try_one("light")
        rows = self.db.call("resources_for", run_id)
        by_role = {r["role"]: r["container_id"] for r in rows}
        self.assertEqual(by_role["candidate"], f"qf-{run_id}-candidate")

    def test_the_handoff_row_exists_before_its_run(self):
        run_id = self.submit()
        real_prepare = self.runner.prepare_run_dir
        seen = []

        def prepare_with_artifact(rid, **kw):
            paths = real_prepare(rid, **kw)
            with open(os.path.join(paths["out"], "result.json"), "w") as fh:
                fh.write("{}")
            return paths

        def spawn(argv, **kw):
            self.spawned.append(argv)
            if is_start(argv, "handoff"):
                seen.append([r["role"] for r
                             in self.db.call("resources_for", run_id)])
            return FakeProc()

        self.runner.prepare_run_dir = prepare_with_artifact
        self.runner.spawn = spawn
        self.runner.try_one("light")
        self.assertTrue(seen, "the handoff never ran")
        self.assertIn("handoff", seen[0],
                      "the handoff container ran before it was recorded")


class TestDefect5UnknownIsNeverStopped(Base):
    """#5: reclaim collected `[r for r in res if probe(r)]`, so a falsy None --
    Docker did not answer -- fell through to FAILED. That terminated the job,
    dropped its reservation and released admission on no evidence, while the
    container it could not ask about may well have been running."""

    def leased_with_resource(self, run_id="r1"):
        eff = spec.normalize({"schema": 1, "kind": "test", "source_sha": SHA})
        self.db.call("submit", eff, run_id=run_id, uid=1000,
                     now="2026-08-25T10:00:00Z")
        self.db.call("dequeue", "light", owner="qfd",
                     now="2026-08-25T10:00:01Z",
                     lease_expires_at="2026-08-25T10:00:02Z",
                     hold_deadline_at="2036-01-01T00:00:00Z", max_running=2)
        self.db.call("transition", run_id, "RUNNING", now="2026-08-25T10:00:02Z")
        self.db.call("add_resource", run_id, role="candidate",
                     container_id="c1", now="2026-08-25T10:00:02Z")
        return run_id

    def test_an_unknown_answer_makes_no_transition_at_all(self):
        run_id = self.leased_with_resource()
        before = self.db.call("get", run_id)
        out = self.db.call("reclaim", "2026-08-25T11:00:00Z",
                           probe=lambda c: None)
        self.assertEqual(out, [(run_id, "unconfirmed")])
        self.assertEqual(self.db.call("get", run_id)["state"], "RUNNING")
        self.assertEqual(self.db.call("get", run_id), before)

    def test_an_unknown_answer_keeps_the_reservation_charged(self):
        run_id = self.leased_with_resource()
        charged = self.db.call("admitted_mem_mb")
        self.assertGreater(charged, 0)
        self.db.call("reclaim", "2026-08-25T11:00:00Z", probe=lambda c: None)
        self.assertEqual(self.db.call("admitted_mem_mb"), charged,
                         "admission was released without confirmed shutdown")

    def test_an_unknown_answer_leaves_the_resource_unreleased(self):
        run_id = self.leased_with_resource()
        self.db.call("reclaim", "2026-08-25T11:00:00Z", probe=lambda c: None)
        rows = self.db.call("resources_for", run_id)
        self.assertIsNone(rows[0]["released_at"])

    def test_a_positive_absence_releases_the_resource_rows(self):
        # The other half of the reviewer's reproduction: the job went FAILED
        # while its resource row stayed unreleased.
        run_id = self.leased_with_resource()
        out = self.db.call("reclaim", "2026-08-25T11:00:00Z",
                           probe=lambda c: False)
        self.assertEqual(out, [(run_id, "reclaimed")])
        rows = self.db.call("resources_for", run_id)
        self.assertIsNotNone(rows[0]["released_at"],
                            "a positively-absent container was never released")
        self.assertEqual(self.db.call("get", run_id)["state"], "FAILED")
        ok, problems = self.db.call("verify_chain")
        self.assertTrue(ok, problems)

    def test_one_unknown_among_several_is_enough_to_hold(self):
        run_id = self.leased_with_resource()
        self.db.call("add_resource", run_id, role="handoff",
                     container_id="c2", now="2026-08-25T10:00:03Z")
        answers = {"c1": False, "c2": None}
        out = self.db.call("reclaim", "2026-08-25T11:00:00Z",
                           probe=lambda c: answers[c])
        self.assertEqual(out, [(run_id, "unconfirmed")])
        self.assertEqual(self.db.call("get", run_id)["state"], "RUNNING")

    def test_alive_still_adopts(self):
        run_id = self.leased_with_resource()
        out = self.db.call("reclaim", "2026-08-25T11:00:00Z",
                           probe=lambda c: True, owner="qfd",
                           lease_expires_at="2036-01-01T00:00:00Z")
        self.assertEqual(out, [(run_id, "adopted")])


class TestDefect6CancelAndForceReleaseActOnRuntime(Base):
    """#6: both mutated only the database. A RUNNING job could be marked
    CANCELLED while its container kept its memory, and force-release recorded a
    release while the retained flock descriptor stayed open until restart --
    leaking the mutex the operator invoked it to recover."""

    def test_cancelling_a_live_job_signals_it_rather_than_overwriting_it(self):
        run_id = self.submit()
        gate = threading.Event()
        self.runner.spawn = lambda *a, **k: FakeProc(block=gate)
        thread = threading.Thread(target=self.runner.try_one, args=("light",),
                                  daemon=True)
        thread.start()
        # Wait until the container is RECORDED, not merely until the hold is
        # registered: cancel stops what the inventory names, and the inventory
        # is written just before the container starts.
        for _ in range(400):
            if self.disp.get_hold(run_id) and self.db.call(
                    "resources_for", run_id, unreleased_only=True):
                break
            threading.Event().wait(0.02)
        hold = self.disp.get_hold(run_id)
        self.assertIsNotNone(hold, "the hold was never registered")

        resp = self.disp.handle("cancel", {"run_id": run_id}, 1000)
        self.assertTrue(resp["ok"], resp)
        # NOT an immediate CANCELLED row: that would free the reservation while
        # the container still held its memory.
        self.assertEqual(resp["state"], "CANCELLING")
        self.assertTrue(hold.cancel_requested.is_set())
        self.assertTrue(any(c[:2] == ["docker", "stop"]
                            for c in self.docker.calls),
                        "cancel never asked Docker to stop anything")
        gate.set()
        thread.join(timeout=20)
        self.assertEqual(self.db.call("get", run_id)["state"], "CANCELLED")

    def test_cancelling_a_queued_job_is_still_immediate(self):
        run_id = self.submit()
        resp = self.disp.handle("cancel", {"run_id": run_id}, 1000)
        self.assertEqual(resp["state"], "CANCELLED")

    def test_force_release_closes_the_retained_descriptor(self):
        run_id = self.submit()
        self.db.call("dequeue", "light", owner="qfd", now=qfd.utcnow(),
                     lease_expires_at="2036-01-01T00:00:00Z",
                     hold_deadline_at="2036-01-01T00:00:00Z", max_running=2)
        self.db.call("transition", run_id, "RUNNING", now=qfd.utcnow())
        self.db.call("transition", run_id, "CLEANUP_BLOCKED", now=qfd.utcnow(),
                     fields={"error_class": "kill_unconfirmed"})
        lock = qfd.TrainingLock(self.cfg.lock_file, "light").acquire()
        hold = qfd.Hold(self.db.call("get", run_id), lock, 2 ** 31)
        self.disp.register_hold(hold)

        resp = self.disp.handle("force-release",
                                {"run_id": run_id,
                                 qfd.FORCE_RELEASE_FLAG: True}, 0, admin=True)
        self.assertTrue(resp["ok"], resp)
        self.assertTrue(resp["descriptor_closed"],
                        "the mutex was left held after force-release")
        self.assertFalse(lock.held)
        self.assertIsNone(self.disp.get_hold(run_id))
        # And the mutex is genuinely free now.
        qfd.TrainingLock(self.cfg.lock_file, "heavy").acquire().release()

    def test_a_cleanup_blocked_job_stays_registered_until_released(self):
        # The registry means "this process holds this job's lock", so a
        # CLEANUP_BLOCKED job must remain findable by force-release.
        run_id = self.submit()
        self.db.call("dequeue", "light", owner="qfd", now=qfd.utcnow(),
                     lease_expires_at="2036-01-01T00:00:00Z",
                     hold_deadline_at="2036-01-01T00:00:00Z", max_running=2)
        self.db.call("transition", run_id, "RUNNING", now=qfd.utcnow())
        self.db.call("add_resource", run_id, role="candidate",
                     container_id="c1", now=qfd.utcnow())
        self.docker.states = {"c1": [None]}
        lock = qfd.TrainingLock(self.cfg.lock_file, "light").acquire()
        self.addCleanup(lock.release)
        hold = qfd.Hold(self.db.call("get", run_id), lock, 2 ** 31)
        self.disp.register_hold(hold)
        self.runner.finish(hold, "SUCCEEDED", {"exit_code": 0})
        self.assertEqual(self.db.call("get", run_id)["state"],
                         "CLEANUP_BLOCKED")
        self.assertIsNotNone(self.disp.get_hold(run_id))
        self.assertTrue(lock.held)

    def test_the_reaper_resolves_a_blocked_job_once_docker_answers(self):
        run_id = self.submit()
        self.db.call("dequeue", "light", owner="qfd", now=qfd.utcnow(),
                     lease_expires_at="2036-01-01T00:00:00Z",
                     hold_deadline_at="2036-01-01T00:00:00Z", max_running=2)
        self.db.call("transition", run_id, "RUNNING", now=qfd.utcnow())
        self.db.call("add_resource", run_id, role="candidate",
                     container_id="c1", now=qfd.utcnow())
        self.db.call("transition", run_id, "CLEANUP_BLOCKED", now=qfd.utcnow(),
                     fields={"error_class": "kill_unconfirmed"})
        lock = qfd.TrainingLock(self.cfg.lock_file, "light").acquire()
        hold = qfd.Hold(self.db.call("get", run_id), lock, 2 ** 31)
        self.disp.register_hold(hold)
        self.docker.states = {"c1": [False]}          # now positively gone

        qfd.Reaper(self.cfg, self.db, self.runner, self.disp,
                   self.docker).resolve_blocked()
        job = self.db.call("get", run_id)
        self.assertEqual(job["state"], "FAILED")
        # The CAUSE survives; the resolution is recorded beside it rather than
        # over it. `reclaimed_after_block` says how the job got unstuck, which is
        # not the half triage needs.
        self.assertEqual(job["error_class"], "kill_unconfirmed")
        self.assertIn("unblocked_at", self.db.call("pins_for", run_id))
        self.assertFalse(lock.held, "admissions cannot resume: lock still held")
        self.assertEqual(self.db.call("admitted_mem_mb"), 0)
        ok, reason = self.disp.may_admit()
        self.assertTrue(ok, reason)


class TestDefect7ContainmentMonitoring(Base):
    """#7: there was no out/ quota sampler and no memory.current sampler, and
    log overflow was only checked after proc.wait() -- so it bounded the FILE
    but never killed at the cap. NC15 and rss_high_water_kb could not work."""

    def test_an_out_quota_breach_kills_and_is_classified(self):
        run_id = self.submit()
        gate = threading.Event()
        self.runner.spawn = lambda *a, **k: FakeProc(block=gate)
        self.cfg.artifact_cap_mb = 0        # any byte breaches

        real_prepare = self.runner.prepare_run_dir

        def prepare_and_fill(rid, **kw):
            paths = real_prepare(rid, **kw)
            with open(os.path.join(paths["out"], "big"), "wb") as fh:
                fh.write(b"x" * (2 * 1024 * 1024))
            return paths

        self.runner.prepare_run_dir = prepare_and_fill
        thread = threading.Thread(target=self.runner.try_one, args=("light",),
                                  daemon=True)
        thread.start()
        for _ in range(400):
            if any(c[:2] == ["docker", "stop"] for c in self.docker.calls):
                break
            threading.Event().wait(0.02)
        gate.set()
        thread.join(timeout=20)
        job = self.db.call("get", run_id)
        self.assertEqual(job["error_class"], "out_quota_exceeded", job)
        self.assertEqual(job["state"], "FAILED")

    def test_a_log_overflow_kills_at_the_cap_rather_than_after_the_run(self):
        run_id = self.submit()
        gate = threading.Event()
        self.cfg.log_cap_mb = 0
        self.runner.spawn = lambda *a, **k: FakeProc(out=b"y" * 4096,
                                                     block=gate)
        thread = threading.Thread(target=self.runner.try_one, args=("light",),
                                  daemon=True)
        thread.start()
        for _ in range(400):
            if any(c[:2] == ["docker", "stop"] for c in self.docker.calls):
                break
            threading.Event().wait(0.02)
        killed = any(c[:2] == ["docker", "stop"] for c in self.docker.calls)
        gate.set()
        thread.join(timeout=20)
        self.assertTrue(killed, "the container was never killed at the log cap")
        self.assertEqual(self.db.call("get", run_id)["error_class"],
                         "log_overflow")

    def test_the_high_water_mark_is_actually_sampled_and_stored(self):
        run_id = self.submit()
        original = qfd.cgroup_current_bytes
        self.addCleanup(setattr, qfd, "cgroup_current_bytes", original)
        qfd.cgroup_current_bytes = lambda cid, docker: 5 * 1024 * 1024

        # The workload has to live long enough for at least one sample; a
        # FakeProc that returns instantly would make this pass vacuously.
        gate = threading.Event()
        self.runner.spawn = lambda *a, **k: FakeProc(block=gate)
        self.runner.mem_sample_interval_s = 0.01
        thread = threading.Thread(target=self.runner.try_one, args=("light",),
                                  daemon=True)
        thread.start()
        threading.Event().wait(0.2)
        gate.set()
        thread.join(timeout=20)
        job = self.db.call("get", run_id)
        self.assertEqual(job["rss_high_water_kb"], 5 * 1024,
                         "memory.current was never sampled into the high-water"
                         f" mark: {job}")

    def test_no_sample_leaves_the_high_water_mark_null_rather_than_zero(self):
        run_id = self.submit()
        original = qfd.cgroup_current_bytes
        self.addCleanup(setattr, qfd, "cgroup_current_bytes", original)
        qfd.cgroup_current_bytes = lambda cid, docker: None
        self.runner.try_one("light")
        self.assertIsNone(self.db.call("get", run_id)["rss_high_water_kb"])

    def test_setup_is_governed_by_the_outer_deadline(self):
        run_id = self.submit()
        # An already-expired budget must stop the run before any container.
        self.db.call("dequeue", "light", owner="qfd", now=qfd.utcnow(),
                     lease_expires_at="2036-01-01T00:00:00Z",
                     hold_deadline_at="2020-01-01T00:00:00Z", max_running=2)
        lock = qfd.TrainingLock(self.cfg.lock_file, "light").acquire()
        hold = qfd.Hold(self.db.call("get", run_id), lock,
                        qfd.parse_iso("2020-01-01T00:00:00Z"))
        spawned = []
        self.runner.spawn = lambda *a, **k: spawned.append(1) or FakeProc()
        self.runner.execute(hold)
        self.assertEqual(spawned, [], "a container started past the deadline")
        job = self.db.call("get", run_id)
        self.assertEqual(job["error_class"], "hold_deadline_expired", job)

    def test_the_handoff_is_bounded_by_the_remaining_budget(self):
        # A FIFO must terminate the handoff on its timeout, not wedge a worker.
        run_id = self.submit()
        real_prepare = self.runner.prepare_run_dir

        def prepare_with_artifact(rid, **kw):
            paths = real_prepare(rid, **kw)
            with open(os.path.join(paths["out"], "result.json"), "w") as fh:
                fh.write("{}")
            return paths

        waits = []

        class SlowHandoff:
            returncode = 0
            killed = False
            stdout = io.BytesIO(b"")
            stderr = io.BytesIO(b"")

            def wait(inner, timeout=None):
                waits.append(timeout)
                if inner.killed:
                    return inner.returncode
                raise subprocess.TimeoutExpired("docker", timeout or 0)

            def kill(inner):
                inner.killed = True
                inner.returncode = -9

        def spawn(argv, **kw):
            self.spawned.append(argv)
            return SlowHandoff() if is_start(argv, "handoff") else FakeProc()

        self.runner.prepare_run_dir = prepare_with_artifact
        self.runner.spawn = spawn
        self.runner.try_one("light")
        self.assertTrue(waits, "the handoff was never waited on")
        self.assertIsNotNone(waits[0])
        self.assertLessEqual(waits[0], self.cfg.handoff_timeout_s)
        # ...and the client is reaped rather than abandoned: a second wait after
        # the stop, then SIGKILL if it still will not go.
        self.assertGreaterEqual(len(waits), 2,
                                "the timed-out handoff client was never reaped")
        job = self.db.call("get", run_id)
        self.assertEqual(job["error_class"], "handoff_timeout", job)
        self.assertEqual(job["state"], "FAILED")


class TestDefect8RecoveryChargesTheLarger(Base):
    """#8: Recovery logged "taking the larger" and charged the smaller. Nothing
    recorded the override, so admitted_mem_mb stayed derived from spec_json and
    the budget would admit work the real reservation excludes."""

    def test_an_override_pin_is_charged(self):
        run_id = self.submit(mem_limit="1g")
        self.db.call("dequeue", "light", owner="qfd", now=qfd.utcnow(),
                     lease_expires_at="2036-01-01T00:00:00Z",
                     hold_deadline_at="2036-01-01T00:00:00Z", max_running=2)
        base = self.db.call("admitted_mem_mb")
        self.assertEqual(base, 2048)          # max(1g, IMAGE_BUILD_MEM_MB)
        self.db.call("set_pin", run_id, "reservation_override_mb", "22528",
                     now=qfd.utcnow())
        self.assertEqual(self.db.call("admitted_mem_mb"), 22528)

    def test_an_override_below_the_stored_reservation_never_lowers_it(self):
        run_id = self.submit(mem_limit="8g")
        self.db.call("dequeue", "heavy", owner="qfd", now=qfd.utcnow(),
                     lease_expires_at="2036-01-01T00:00:00Z",
                     hold_deadline_at="2036-01-01T00:00:00Z", max_running=1)
        self.db.call("set_pin", run_id, "reservation_override_mb", "512",
                     now=qfd.utcnow())
        self.assertEqual(self.db.call("admitted_mem_mb"), 8192)

    def test_a_malformed_override_does_not_lower_the_charge(self):
        run_id = self.submit(mem_limit="8g")
        self.db.call("dequeue", "heavy", owner="qfd", now=qfd.utcnow(),
                     lease_expires_at="2036-01-01T00:00:00Z",
                     hold_deadline_at="2036-01-01T00:00:00Z", max_running=1)
        self.db.call("set_pin", run_id, "reservation_override_mb", "nonsense",
                     now=qfd.utcnow())
        self.assertEqual(self.db.call("admitted_mem_mb"), 8192)

    def test_recovery_writes_the_override_when_a_live_cap_exceeds_it(self):
        run_id = self.submit(mem_limit="1g")
        self.db.call("dequeue", "light", owner="qfd", now=qfd.utcnow(),
                     lease_expires_at="2036-01-01T00:00:00Z",
                     hold_deadline_at="2036-01-01T00:00:00Z", max_running=2)
        self.db.call("transition", run_id, "RUNNING", now=qfd.utcnow())
        self.db.call("add_resource", run_id, role="candidate",
                     container_id="c1", now=qfd.utcnow())

        big = 22528 * 1024 * 1024

        def run(argv, env=None, timeout=60):
            if "{{.HostConfig.Memory}}" in argv:
                return proc(0, f"{big}\n")
            return proc(0, "")

        self.docker.run = run
        # Unconfirmable, so the job stays admitted: a confirmable orphan is
        # cleaned up at startup (nothing can resume it) and then charges
        # nothing at all, which would make this assertion vacuous.
        self.docker.states = {"c1": [None]}
        holds = qfd.Recovery(self.cfg, self.db, self.runner,
                             self.docker).reconstruct()
        for h in holds:
            self.addCleanup(h.lock.release)
        self.assertEqual(self.db.call("admitted_mem_mb"), 22528,
                         "the larger reservation was logged but not charged")


class TestDefect9ForceDroppedCommitsArePruned(unittest.TestCase):
    """#9: resolve() only fetched when the OBJECT was missing, so once a commit
    was local the reachability question was answered against stale refs. A
    force-dropped commit stayed acceptable forever."""

    IDENT = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    }

    def git(self, cwd, *args, check=True):
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                           text=True, env={**os.environ, **self.IDENT})
        if check and p.returncode != 0:
            raise AssertionError(f"git {args} failed: {p.stderr}")
        return p.stdout.strip()

    def commit(self, name, content, branch="main", parent=None):
        with open(os.path.join(self.upstream, name), "w") as fh:
            fh.write(content)
        self.git(self.upstream, "add", name)
        tree = self.git(self.upstream, "write-tree")
        if parent is None:
            parent = self.git(self.upstream, "rev-parse", "--verify", "--quiet",
                              f"refs/heads/{branch}", check=False)
        args = ["commit-tree", tree, "-m", name]
        if parent:
            args += ["-p", parent]
        sha = self.git(self.upstream, *args)
        self.git(self.upstream, "update-ref", f"refs/heads/{branch}", sha)
        return sha

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.upstream = os.path.join(self.tmp.name, "upstream")
        os.makedirs(self.upstream)
        self.git(self.upstream, "init", "--quiet", "-b", "main")
        self.src = source.Source(os.path.join(self.tmp.name, "m.git"),
                                 self.upstream)
        self.src.ensure_mirror()

    def test_a_force_dropped_head_stops_resolving_without_a_manual_fetch(self):
        # The reviewer's reproduction: an orphaned replacement of main. The old
        # code still returned refs/remotes/origin/main for the dropped SHA
        # because nothing re-fetched.
        base = self.commit("a.txt", "base")
        dropped = self.commit("b.txt", "dropped")
        self.src.fetch()
        self.assertEqual(self.src.resolve(dropped),
                         "refs/remotes/origin/main")

        # Upstream force-rewinds main to `base` and pushes something else.
        self.git(self.upstream, "update-ref", "refs/heads/main", base)
        self.commit("c.txt", "replacement", parent=base)

        # NO manual fetch here -- that is the whole point.
        with self.assertRaises(source.NotPublished):
            self.src.resolve(dropped)

    def test_a_reachable_commit_still_resolves(self):
        sha = self.commit("a.txt", "base")
        self.src.fetch()
        self.assertEqual(self.src.resolve(sha), "refs/remotes/origin/main")

    def test_the_fetch_is_still_bounded_for_an_unknown_sha(self):
        self.commit("a.txt", "base")
        self.src.fetch()
        self.src.command_log.clear()
        with self.assertRaises(source.NotPublished):
            self.src.resolve("0" * 40)
        self.assertEqual(len([c for c in self.src.command_log
                              if "fetch" in c]), 1)

    def test_a_stale_mirror_finds_a_newly_pushed_commit(self):
        # The other direction: present locally but not yet reachable locally.
        self.commit("a.txt", "base")
        self.src.fetch()
        later = self.commit("b.txt", "later")
        self.assertEqual(self.src.resolve(later), "refs/remotes/origin/main")


class TestDefect10BuilderProbeFails(unittest.TestCase):
    """#10: the --memory control warned and continued when its 64MiB step
    unexpectedly SUCCEEDED, despite the documentation calling it load-bearing."""

    def test_the_memory_probe_dies_rather_than_warns(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(os.path.dirname(here), "phase2-setup.sh")) as fh:
            body = fh.read()
        probe = body[body.index("# (2) --memory is HONOURED"):
                     body.index("# (3) --force-rm")]
        self.assertIn("die ", probe,
                      "the memory probe must fail, not warn: design D10 chose"
                      " the classic builder precisely for this flag")
        self.assertNotIn("warn ", probe)



# =========================================================================
# Second review round: four production gaps that survived the first fixes.
# =========================================================================
class TestRound2Defect1AllFourDirectoriesAreOwned(Base):
    """The service's PRIMARY group is qfd, so anything not chowned stays
    qfd:qfd 0750. Only out/ and artifacts/ were chowned, which left src/
    untraversable by the container and logs/ unreadable by `qf logs`."""

    def test_every_part_of_the_run_directory_is_owned_deliberately(self):
        gid = os.getgid()
        paths = self.runner.prepare_run_dir("r-own", qfrun_gid=gid,
                                            qfclient_gid=gid)
        for key in ("base", "src", "out", "data", "artifacts", "logs"):
            with self.subTest(part=key):
                self.assertEqual(os.stat(paths[key]).st_gid, gid,
                                 f"{key} kept the service's primary group")

    def test_the_declared_ownership_covers_every_directory_created(self):
        # `data` joined the set in 2b-2: the writable hole in the read-only
        # source tree, for the trainer's module-relative CACHE_DIR. This
        # assertion existing is why that addition could not be made without
        # declaring its ownership -- which is the whole point of pinning the set.
        declared = {name for name, _g, _m in qfd.Runner.OWNERSHIP}
        self.assertEqual(declared,
                         {None, "src", "out", "data", "artifacts", "logs"})

    def test_source_and_out_belong_to_the_container_group(self):
        # The container runs as 10001:10001 (gid qfrun); it must traverse src/
        # and write out/.
        groups = {name: group for name, group, _m in qfd.Runner.OWNERSHIP}
        self.assertEqual(groups["src"], "qfrun")
        self.assertEqual(groups["out"], "qfrun")

    def test_logs_artifacts_and_the_base_belong_to_the_client_group(self):
        groups = {name: group for name, group, _m in qfd.Runner.OWNERSHIP}
        self.assertEqual(groups["logs"], "qfclient")
        self.assertEqual(groups["artifacts"], "qfclient")
        self.assertEqual(groups[None], "qfclient",
                         "clients cannot traverse to artifacts/ or logs/")

    def test_the_setgid_bit_on_out_survives_the_chown(self):
        # chown clears setgid on Linux, so chmod must come after it.
        gid = os.getgid()
        paths = self.runner.prepare_run_dir("r-sg", qfrun_gid=gid,
                                            qfclient_gid=gid)
        self.assertEqual(os.stat(paths["out"]).st_mode & 0o7777, 0o2770)

    def test_a_real_run_leaves_logs_readable_by_the_client_group(self):
        gid = os.getgid()
        self.runner.qfrun_gid = gid
        self.runner.qfclient_gid = gid
        run_id = self.submit()
        self.runner.try_one("light")
        logs = os.path.join(self.runs, run_id, "logs")
        self.assertEqual(os.stat(logs).st_gid, gid)
        self.assertTrue(os.stat(logs).st_mode & 0o050, "no group r-x on logs/")


class TestRound2Defect2ReclaimReleasesTheLock(Base):
    """store.reclaim released resources and failed the job, but the Reaper only
    LOGGED the outcome -- never releasing or unregistering the hold. Admission
    was released logically while the flock leaked until restart."""

    def blocked_hold(self, run_id="rl1", state="RUNNING"):
        eff = spec.normalize({"schema": 1, "kind": "test", "source_sha": SHA})
        self.db.call("submit", eff, run_id=run_id, uid=1000,
                     now="2026-01-01T00:00:00Z")
        self.db.call("dequeue", "light", owner="qfd", now="2026-01-01T00:00:01Z",
                     lease_expires_at="2026-01-01T00:00:02Z",
                     hold_deadline_at="2036-01-01T00:00:00Z", max_running=2)
        self.db.call("transition", run_id, state, now="2026-01-01T00:00:02Z")
        self.db.call("add_resource", run_id, role="candidate",
                     container_id="c1", now="2026-01-01T00:00:02Z")
        lock = qfd.TrainingLock(self.cfg.lock_file, "light").acquire()
        hold = qfd.Hold(self.db.call("get", run_id), lock, 2 ** 31)
        self.disp.register_hold(hold)
        return run_id, hold

    def test_a_reclaimed_job_releases_and_unregisters_its_hold(self):
        # The reviewer's reproduction: decision=reclaimed, state=FAILED,
        # lock_held=True, registered=True.
        run_id, hold = self.blocked_hold()
        self.docker.states = {"c1": [False]}
        reaper = qfd.Reaper(self.cfg, self.db, self.runner, self.disp,
                            self.docker)
        decided = reaper.sweep()
        self.assertEqual(decided, [(run_id, "reclaimed")])
        self.assertEqual(self.db.call("get", run_id)["state"], "FAILED")
        self.assertFalse(hold.lock.held, "the training descriptor leaked")
        self.assertIsNone(self.disp.get_hold(run_id))
        # And the mutex is genuinely free for the nightly run.
        qfd.TrainingLock(self.cfg.lock_file, "heavy").acquire().release()

    def test_an_unconfirmed_reclaim_keeps_the_hold(self):
        run_id, hold = self.blocked_hold()
        self.docker.states = {"c1": [None]}
        qfd.Reaper(self.cfg, self.db, self.runner, self.disp,
                   self.docker).sweep()
        self.assertEqual(self.db.call("get", run_id)["state"], "RUNNING")
        self.assertTrue(hold.lock.held, "an unknown answer released the mutex")
        self.assertIsNotNone(self.disp.get_hold(run_id))
        hold.lock.release()

    def test_an_adopted_job_keeps_its_hold(self):
        run_id, hold = self.blocked_hold()
        self.docker.states = {"c1": [True]}
        qfd.Reaper(self.cfg, self.db, self.runner, self.disp,
                   self.docker).sweep()
        self.assertTrue(hold.lock.held)
        hold.lock.release()

    def test_release_hold_is_safe_when_nothing_is_registered(self):
        reaper = qfd.Reaper(self.cfg, self.db, self.runner, self.disp,
                            self.docker)
        self.assertFalse(reaper.release_hold("no-such-run", "test"))


class TestRound2Defect3MutexLostReachesATerminalState(Base):
    """_mutex_lost confirmed shutdown and returned without transitioning,
    leaving the job RUNNING with no hold -- and if it later drifted to
    CLEANUP_BLOCKED, resolve_blocked skipped it forever for want of a hold."""

    def orphan(self, run_id="ml1", state="RUNNING"):
        eff = spec.normalize({"schema": 1, "kind": "test", "source_sha": SHA})
        self.db.call("submit", eff, run_id=run_id, uid=1000,
                     now="2026-01-01T00:00:00Z")
        self.db.call("dequeue", "light", owner="qfd", now="2026-01-01T00:00:01Z",
                     lease_expires_at="2036-01-01T00:00:00Z",
                     hold_deadline_at="2036-01-01T00:00:00Z", max_running=2)
        if state != "LEASED":
            self.db.call("transition", run_id, state, now="2026-01-01T00:00:02Z")
            # Only RUNNING may record a container, so a LEASED orphan is
            # necessarily an EMPTY-inventory one. That is not a weaker fixture:
            # it is the only shape the daemon can produce, and the case the
            # LEASED test is about (no CLEANUP_BLOCKED edge from LEASED) is
            # unchanged.
            self.db.call("add_resource", run_id, role="candidate",
                         container_id="c1", now="2026-01-01T00:00:02Z")
        return run_id

    def test_a_confirmed_orphan_becomes_failed_with_mutex_lost(self):
        # The reviewer's reproduction: holds=0 state=RUNNING unreleased=0
        # charged=4096 -- a stranded job nothing would ever look at again.
        run_id = self.orphan()
        self.docker.states = {"c1": [False]}
        incumbent = qfd.TrainingLock(self.cfg.lock_file, "heavy").acquire()
        self.addCleanup(incumbent.release)

        holds = qfd.Recovery(self.cfg, self.db, self.runner,
                             self.docker).reconstruct()
        self.assertEqual(holds, [])
        job = self.db.call("get", run_id)
        self.assertEqual(job["state"], "FAILED", job)
        self.assertEqual(job["error_class"], "mutex_lost")
        self.assertEqual(self.db.call("admitted_mem_mb"), 0,
                         "a stranded job kept its reservation charged")
        rows = self.db.call("resources_for", run_id)
        self.assertIsNotNone(rows[0]["released_at"])

    def test_an_unconfirmed_orphan_blocks_rather_than_stranding(self):
        run_id = self.orphan()
        self.docker.states = {"c1": [None]}
        incumbent = qfd.TrainingLock(self.cfg.lock_file, "heavy").acquire()
        self.addCleanup(incumbent.release)
        qfd.Recovery(self.cfg, self.db, self.runner, self.docker).reconstruct()
        job = self.db.call("get", run_id)
        self.assertEqual(job["state"], "CLEANUP_BLOCKED", job)
        self.assertEqual(job["error_class"], "mutex_lost_unconfirmed")
        # Admissions are frozen, and it is NOT stranded: the reaper handles
        # hold-less blocked jobs.
        ok, reason = self.disp.may_admit()
        self.assertFalse(ok)
        self.assertEqual(reason, "cleanup_blocked")

    def test_the_reaper_resolves_a_blocked_job_with_no_registered_hold(self):
        # This is the hole that made the previous case a permanent stall.
        run_id = self.orphan()
        self.docker.states = {"c1": [None]}
        incumbent = qfd.TrainingLock(self.cfg.lock_file, "heavy").acquire()
        self.addCleanup(incumbent.release)
        qfd.Recovery(self.cfg, self.db, self.runner, self.docker).reconstruct()
        self.assertEqual(self.db.call("get", run_id)["state"],
                         "CLEANUP_BLOCKED")
        self.assertIsNone(self.disp.get_hold(run_id))

        self.docker.states = {"c1": [False]}      # Docker answers at last
        qfd.Reaper(self.cfg, self.db, self.runner, self.disp,
                   self.docker).resolve_blocked()
        job = self.db.call("get", run_id)
        self.assertEqual(job["state"], "FAILED", job)
        self.assertEqual(self.db.call("admitted_mem_mb"), 0)
        ok, _ = self.disp.may_admit()
        self.assertTrue(ok, "admissions never resumed")

    def test_a_leased_orphan_with_no_cleanup_edge_still_terminates(self):
        # LEASED has no CLEANUP_BLOCKED edge, and (having recorded nothing) no
        # inventory to inspect either -- so the settle path is the only route to
        # a terminal state, and it has to work or the job strands.
        run_id = self.orphan(state="LEASED")
        self.cfg.build_settle_s = 0
        incumbent = qfd.TrainingLock(self.cfg.lock_file, "heavy").acquire()
        self.addCleanup(incumbent.release)
        qfd.Recovery(self.cfg, self.db, self.runner, self.docker).reconstruct()
        job = self.db.call("get", run_id)
        self.assertEqual(job["state"], "FAILED", job)
        self.assertEqual(job["error_class"], "mutex_lost")

    def test_confirm_run_stopped_treats_an_empty_inventory_as_unconfirmed(self):
        eff = spec.normalize({"schema": 1, "kind": "test", "source_sha": SHA})
        self.db.call("submit", eff, run_id="empty", uid=1000,
                     now="2026-01-01T00:00:00Z")
        self.assertFalse(self.runner.confirm_run_stopped("empty"),
                         "confirmation over an empty set is not confirmation")


class TestRound2Defect4SetupIsActivelyBounded(Base):
    """The deadline was only CHECKED after each setup operation returned. Since
    git had no timeout, the now-unconditional fetch could hang forever while
    holding the training mutex."""

    def test_every_git_command_carries_a_timeout(self):
        seen = []

        def runner(argv, cwd, env, timeout=None):
            seen.append(timeout)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        src = source.Source("/tmp/x.git", "url", runner=runner, timeout_s=42)
        src._git("rev-parse", "HEAD", check=False)
        self.assertEqual(seen, [42])

    def test_a_hanging_git_command_raises_timeout_not_a_hang(self):
        def runner(argv, cwd, env, timeout=None):
            raise subprocess.TimeoutExpired("git", timeout or 0)

        src = source.Source("/tmp/x.git", "url", runner=runner, timeout_s=1)
        with self.assertRaises(source.Timeout):
            src.fetch()

    def test_the_runner_passes_one_absolute_deadline_for_all_setup_git(self):
        # An ABSOLUTE instant, shared by every git command of this job, so the
        # TOTAL is bounded no matter how many commands run. A per-command
        # ceiling would let five commands each honour it and still overrun.
        run_id = self.submit()
        seen = []

        class BoundedSource(FakeSource):
            def resolve(inner, sha, deadline=None):
                seen.append(("resolve", deadline))
                return "refs/remotes/origin/main"

            def add_worktree(inner, sha, dest, deadline=None):
                seen.append(("worktree", deadline))
                os.makedirs(dest, exist_ok=True)
                return dest

        self.runner.src = BoundedSource()
        before = time.time()
        self.runner.try_one("light")
        self.assertEqual([k for k, _ in seen], ["resolve", "worktree"])
        deadlines = {d for _, d in seen}
        self.assertEqual(len(deadlines), 1,
                         "each phase got its own budget instead of sharing one")
        deadline = deadlines.pop()
        self.assertIsNotNone(deadline, "git ran with no bound at all")
        self.assertGreater(deadline, before)
        self.assertLessEqual(deadline,
                             before + self.cfg.setup_teardown_allowance_s + 2)

    def test_the_deadline_is_never_shared_mutable_state(self):
        # Two workers must not be able to overwrite each other's budget, which
        # is what assigning `src.timeout_s` per call allowed.
        src = source.Source("/tmp/x.git", "url", timeout_s=300)
        self.assertEqual(src.timeout_s, 300)
        sig = inspect.signature(source.Source.resolve)
        self.assertIn("deadline", sig.parameters)
        for method in (source.Source.fetch, source.Source.add_worktree):
            self.assertIn("deadline", inspect.signature(method).parameters)

    def test_a_total_deadline_bounds_the_sum_of_several_commands(self):
        # The real defect: five commands each honouring a 300s ceiling.
        calls = []

        def runner(argv, cwd, env, timeout=None):
            calls.append(timeout)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        src = source.Source("/tmp/x.git", "url", runner=runner, timeout_s=300)
        deadline = time.time() + 10
        for _ in range(3):
            src._git("rev-parse", "HEAD", check=False, deadline=deadline)
        self.assertTrue(all(t <= 10.5 for t in calls), calls)
        self.assertLess(calls[-1], calls[0] + 0.01,
                        "the budget did not shrink as the deadline approached")

    def test_a_passed_deadline_refuses_to_start_a_git_command(self):
        started = []

        def runner(argv, cwd, env, timeout=None):
            started.append(argv)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        src = source.Source("/tmp/x.git", "url", runner=runner)
        with self.assertRaises(source.Timeout):
            src._git("fetch", deadline=time.time() - 1)
        self.assertEqual(started, [], "a command ran past its deadline")

    def test_a_hung_fetch_becomes_a_deadline_failure(self):
        run_id = self.submit()

        class HangingSource(FakeSource):
            def resolve(inner, sha, deadline=None):
                raise source.Timeout("fetch exceeded its bound")

        self.runner.src = HangingSource()
        self.runner.try_one("light")
        job = self.db.call("get", run_id)
        self.assertEqual(job["error_class"], "source_timeout", job)
        self.assertEqual(job["state"], "FAILED")
        # And the mutex is back.
        qfd.TrainingLock(self.cfg.lock_file, "heavy").acquire().release()

    def test_a_budget_exhausted_by_image_preparation_starts_no_candidate(self):
        # The `max(1, remaining)` floor used to grant a one-second run to a job
        # whose deadline had already passed.
        run_id = self.submit()
        self.db.call("dequeue", "light", owner="qfd", now=qfd.utcnow(),
                     lease_expires_at="2036-01-01T00:00:00Z",
                     hold_deadline_at="2036-01-01T00:00:00Z", max_running=2)
        lock = qfd.TrainingLock(self.cfg.lock_file, "light").acquire()
        self.addCleanup(lock.release)
        hold = qfd.Hold(self.db.call("get", run_id), lock, 2 ** 31)
        hold.image_id = "sha256:" + "a" * 64
        spawned = []
        self.runner.spawn = lambda *a, **k: spawned.append(1) or FakeProc()

        # The budget evaporates between admission and launch.
        hold.deadline_epoch = 0
        paths = self.runner.prepare_run_dir(run_id)
        with self.assertRaises(qfd.DeadlineExpired):
            self.runner._launch(hold, json.loads(hold.job["spec_json"]), paths,
                                hold.image_id)
        self.assertEqual(spawned, [],
                         "a candidate started on an exhausted budget")

    def test_the_build_phase_refuses_an_exhausted_budget(self):
        run_id = self.submit()
        self.db.call("dequeue", "light", owner="qfd", now=qfd.utcnow(),
                     lease_expires_at="2036-01-01T00:00:00Z",
                     hold_deadline_at="2036-01-01T00:00:00Z", max_running=2)
        lock = qfd.TrainingLock(self.cfg.lock_file, "light").acquire()
        self.addCleanup(lock.release)
        hold = qfd.Hold(self.db.call("get", run_id), lock, 0)
        with self.assertRaises(qfd.DeadlineExpired):
            self.runner._ensure_image(hold)

    def test_the_setup_deadline_helper_refuses_a_spent_budget(self):
        hold = qfd.Hold({"run_id": "x"}, None, 0)
        with self.assertRaises(qfd.DeadlineExpired):
            self.runner._setup_deadline(hold)

    def test_the_setup_deadline_is_capped_by_the_hold_not_just_the_allowance(self):
        # A hold with only 5s left must not get the full setup allowance.
        soon = time.time() + 5
        hold = qfd.Hold({"run_id": "x"}, None, soon)
        self.assertAlmostEqual(self.runner._setup_deadline(hold), soon, delta=1)
# =========================================================================
# Third review round.
# =========================================================================
class TestRound3EmptyInventoryDoesNotStrand(Base):
    """confirm_run_stopped correctly refuses an empty inventory, which left a
    mutex-lost BUILDING job with no path forward at all: _mutex_lost moved it to
    CLEANUP_BLOCKED and the reaper then asked the same unanswerable question
    forever. A permanent stall is not fail-closed, it is just failed."""

    def building_orphan(self, run_id="b1"):
        eff = spec.normalize({"schema": 1, "kind": "test", "source_sha": SHA})
        self.db.call("submit", eff, run_id=run_id, uid=1000,
                     now="2026-01-01T00:00:00Z")
        self.db.call("dequeue", "light", owner="qfd", now="2026-01-01T00:00:01Z",
                     lease_expires_at="2036-01-01T00:00:00Z",
                     hold_deadline_at="2036-01-01T00:00:00Z", max_running=2)
        self.db.call("transition", run_id, "BUILDING",
                     now="2026-01-01T00:00:02Z")
        return run_id

    def test_the_settle_window_starts_before_it_releases(self):
        run_id = self.building_orphan()
        self.cfg.build_settle_s = 3600
        self.assertFalse(self.runner.settle_empty_inventory(run_id),
                         "released without waiting out the settle window")
        pins = self.db.call("pins_for", run_id)
        self.assertIn(qfd.Runner.SETTLE_PIN, pins)
        # Still not releasable on a second look.
        self.assertFalse(self.runner.settle_empty_inventory(run_id))

    def test_an_elapsed_settle_window_releases(self):
        run_id = self.building_orphan()
        self.cfg.build_settle_s = 0
        self.assertTrue(self.runner.settle_empty_inventory(run_id))

    def test_a_container_appearing_cancels_the_settle_path(self):
        # If something IS there, this becomes an inspection question after all.
        # The job is moved to RUNNING first because that is the only state a
        # container can be recorded in; `settle_empty_inventory` reads the
        # inventory and the pins, not the state, so the subject is unchanged.
        run_id = self.building_orphan()
        self.cfg.build_settle_s = 0
        self.db.call("transition", run_id, "RUNNING", now=qfd.utcnow())
        self.db.call("add_resource", run_id, role="candidate",
                     container_id="c1", now=qfd.utcnow())
        self.assertFalse(self.runner.settle_empty_inventory(run_id))

    def test_a_mutex_lost_empty_building_job_does_not_strand(self):
        run_id = self.building_orphan()
        self.cfg.build_settle_s = 0
        incumbent = qfd.TrainingLock(self.cfg.lock_file, "heavy").acquire()
        self.addCleanup(incumbent.release)
        qfd.Recovery(self.cfg, self.db, self.runner, self.docker).reconstruct()
        job = self.db.call("get", run_id)
        self.assertEqual(job["state"], "FAILED", job)
        self.assertEqual(job["error_class"], "mutex_lost")
        self.assertEqual(self.db.call("admitted_mem_mb"), 0)

    def test_a_blocked_empty_job_is_eventually_resolved_by_the_reaper(self):
        # The full stall: blocked, no hold, nothing to inspect.
        run_id = self.building_orphan()
        self.cfg.build_settle_s = 3600
        incumbent = qfd.TrainingLock(self.cfg.lock_file, "heavy").acquire()
        self.addCleanup(incumbent.release)
        qfd.Recovery(self.cfg, self.db, self.runner, self.docker).reconstruct()
        self.assertEqual(self.db.call("get", run_id)["state"],
                         "CLEANUP_BLOCKED")
        self.assertIsNone(self.disp.get_hold(run_id))
        reaper = qfd.Reaper(self.cfg, self.db, self.runner, self.disp,
                            self.docker)
        reaper.resolve_blocked()
        self.assertEqual(self.db.call("get", run_id)["state"],
                         "CLEANUP_BLOCKED", "released before settling")
        self.cfg.build_settle_s = 0            # the window elapses
        reaper.resolve_blocked()
        self.assertEqual(self.db.call("get", run_id)["state"], "FAILED")
        ok, _ = self.disp.may_admit()
        self.assertTrue(ok, "admissions never resumed")


class TestRound3RevokedHoldStopsTheWorker(Base):
    """The reaper unregistered and closed the descriptor without telling the
    worker. A worker between candidate exit and handoff would then start the
    handoff after the job was FAILED and the nightly mutex was free."""

    def test_release_hold_revokes_before_releasing(self):
        run_id = self.submit()
        lock = qfd.TrainingLock(self.cfg.lock_file, "light").acquire()
        hold = qfd.Hold(self.db.call("get", run_id), lock, 2 ** 31)
        self.disp.register_hold(hold)
        reaper = qfd.Reaper(self.cfg, self.db, self.runner, self.disp,
                            self.docker)
        self.assertTrue(reaper.release_hold(run_id, "test"))
        self.assertTrue(hold.revoked.is_set())
        self.assertTrue(hold.cancel_requested.is_set())
        self.assertFalse(hold.lock.held)

    def test_force_release_also_revokes(self):
        run_id = self.submit()
        self.db.call("dequeue", "light", owner="qfd", now=qfd.utcnow(),
                     lease_expires_at="2036-01-01T00:00:00Z",
                     hold_deadline_at="2036-01-01T00:00:00Z", max_running=2)
        self.db.call("transition", run_id, "RUNNING", now=qfd.utcnow())
        self.db.call("transition", run_id, "CLEANUP_BLOCKED", now=qfd.utcnow(),
                     fields={"error_class": "x"})
        lock = qfd.TrainingLock(self.cfg.lock_file, "light").acquire()
        hold = qfd.Hold(self.db.call("get", run_id), lock, 2 ** 31)
        self.disp.register_hold(hold)
        self.disp.handle("force-release",
                         {"run_id": run_id, qfd.FORCE_RELEASE_FLAG: True}, 0,
                         admin=True)
        self.assertTrue(hold.revoked.is_set())

    def test_a_revoked_hold_starts_no_handoff(self):
        # The exact race: revoke while the candidate is running, then let it
        # exit. No handoff container may be created afterwards.
        run_id = self.submit()
        gate = threading.Event()
        self.runner.spawn = lambda *a, **k: FakeProc(block=gate)
        real_prepare = self.runner.prepare_run_dir

        def prepare_with_artifact(rid, **kw):
            paths = real_prepare(rid, **kw)
            with open(os.path.join(paths["out"], "result.json"), "w") as fh:
                fh.write("{}")
            return paths

        self.runner.prepare_run_dir = prepare_with_artifact
        thread = threading.Thread(target=self.runner.try_one, args=("light",),
                                  daemon=True)
        thread.start()
        for _ in range(400):
            if self.disp.get_hold(run_id):
                break
            threading.Event().wait(0.02)
        reaper = qfd.Reaper(self.cfg, self.db, self.runner, self.disp,
                            self.docker)
        reaper.release_hold(run_id, "lease reclaimed")
        gate.set()
        thread.join(timeout=20)

        self.assertEqual(self.handoff_spawns(), [],
                         "a handoff container started after the mutex was freed")
        rows = [r["role"] for r in self.db.call("resources_for", run_id)]
        self.assertNotIn("handoff", rows)


class TestRound3EveryDockerCallIsBounded(Base):
    """`timeout or 60` turned a computed budget of ZERO back into 60 seconds,
    and `docker image inspect` was handed timeout=None so it never saw the
    budget at all."""

    def test_image_inspect_is_bounded_by_the_remaining_hold(self):
        run_id = self.submit()
        self.db.call("dequeue", "light", owner="qfd", now=qfd.utcnow(),
                     lease_expires_at="2036-01-01T00:00:00Z",
                     hold_deadline_at="2036-01-01T00:00:00Z", max_running=2)
        lock = qfd.TrainingLock(self.cfg.lock_file, "light").acquire()
        self.addCleanup(lock.release)
        hold = qfd.Hold(self.db.call("get", run_id), lock, time.time() + 5)
        seen = []

        def run(argv, env=None, timeout=60):
            seen.append((argv[:3], timeout))
            return proc(0, "sha256:" + "a" * 64 + "\n")

        self.docker.run = run
        self.runner._ensure_image(hold)
        inspects = [t for a, t in seen if a == ["docker", "image", "inspect"]]
        self.assertTrue(inspects, "image inspect never ran")
        self.assertLessEqual(inspects[0], 5,
                             "image inspect ignored the remaining hold budget")

    def test_a_zero_budget_is_never_promoted_back_to_sixty_seconds(self):
        run_id = self.submit()
        self.db.call("dequeue", "light", owner="qfd", now=qfd.utcnow(),
                     lease_expires_at="2036-01-01T00:00:00Z",
                     hold_deadline_at="2036-01-01T00:00:00Z", max_running=2)
        lock = qfd.TrainingLock(self.cfg.lock_file, "light").acquire()
        self.addCleanup(lock.release)
        hold = qfd.Hold(self.db.call("get", run_id), lock, 0)   # spent
        with self.assertRaises(qfd.DeadlineExpired):
            self.runner._ensure_image(hold)

    def test_the_handoff_refuses_a_spent_budget_rather_than_taking_a_second(self):
        run_id = self.submit()
        self.db.call("dequeue", "light", owner="qfd", now=qfd.utcnow(),
                     lease_expires_at="2036-01-01T00:00:00Z",
                     hold_deadline_at="2036-01-01T00:00:00Z", max_running=2)
        lock = qfd.TrainingLock(self.cfg.lock_file, "light").acquire()
        self.addCleanup(lock.release)
        hold = qfd.Hold(self.db.call("get", run_id), lock, 0)
        hold.image_id = "sha256:" + "a" * 64
        paths = self.runner.prepare_run_dir(run_id)
        with open(os.path.join(paths["out"], "result.json"), "w") as fh:
            fh.write("{}")
        ran = []
        self.docker.run = lambda argv, env=None, timeout=60: (
            ran.append(argv) or proc(0, ""))
        self.assertEqual(self.runner._handoff(hold, paths), "handoff_timeout")
        self.assertEqual([c for c in ran if "--label" in c], [],
                         "a handoff container started on a spent budget")


class TestRound3NoLeakedLogHandles(Base):
    """Both BoundedWriters were opened before the pre-launch expiry check, so
    the refusal path raised past two open file objects."""

    def test_the_refusal_path_opens_no_writers(self):
        run_id = self.submit()
        self.db.call("dequeue", "light", owner="qfd", now=qfd.utcnow(),
                     lease_expires_at="2036-01-01T00:00:00Z",
                     hold_deadline_at="2036-01-01T00:00:00Z", max_running=2)
        lock = qfd.TrainingLock(self.cfg.lock_file, "light").acquire()
        self.addCleanup(lock.release)
        hold = qfd.Hold(self.db.call("get", run_id), lock, 0)
        hold.image_id = "sha256:" + "a" * 64
        paths = self.runner.prepare_run_dir(run_id)
        with self.assertRaises(qfd.DeadlineExpired):
            self.runner._launch(hold, json.loads(hold.job["spec_json"]), paths,
                                hold.image_id)
        # Nothing was created, so nothing could have leaked.
        self.assertEqual(sorted(os.listdir(paths["logs"])), [])

    def test_the_whole_suite_runs_without_resource_warnings(self):
        # A guard on the mechanism rather than the symptom: if a writer is ever
        # opened before a raise again, -W error::ResourceWarning catches it.
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            run_id = self.submit()
            self.runner.try_one("light")
            self.assertEqual(self.db.call("get", run_id)["state"], "SUCCEEDED")


# =========================================================================
# Fourth review round: three boundary races that survived 354 tests.
# =========================================================================
class TestRound4RevocationIsNotATOCTOURace(Base):
    """`if hold.revoked.is_set(): ... ` then `_handoff()` is a
    time-of-check/time-of-use race: revocation lands in the gap, frees the mutex,
    and the handoff starts a container anyway. The reviewer's trace was
    outcome=SUCCEEDED revoked=True lock_held=False handoff_calls=1.

    An event check cannot establish this property. Only holding something across
    both the decision and the act can."""

    def running_hold(self, run_id=None):
        run_id = run_id or self.submit()
        self.db.call("dequeue", "light", owner="qfd", now=qfd.utcnow(),
                     lease_expires_at="2036-01-01T00:00:00Z",
                     hold_deadline_at="2036-01-01T00:00:00Z", max_running=2)
        self.db.call("transition", run_id, "RUNNING", now=qfd.utcnow())
        lock = qfd.TrainingLock(self.cfg.lock_file, "light").acquire()
        hold = qfd.Hold(self.db.call("get", run_id), lock, 2 ** 31)
        hold.image_id = "sha256:" + "a" * 64
        self.disp.register_hold(hold)
        return run_id, hold

    def test_a_concurrent_revocation_blocks_until_the_phase_is_recorded(self):
        # THE boundary. While the gate is held, a revocation from another thread
        # must not complete -- that is what makes the check meaningful.
        run_id, hold = self.running_hold()
        self.addCleanup(hold.lock.release)
        paths = self.runner.prepare_run_dir(run_id)
        with open(os.path.join(paths["out"], "result.json"), "w") as fh:
            fh.write("{}")
        reaper = qfd.Reaper(self.cfg, self.db, self.runner, self.disp,
                            self.docker)
        observed = {}
        real_record = self.runner._record_container

        def record(h, rid, role):
            if role == "handoff":
                t = threading.Thread(
                    target=lambda: observed.__setitem__(
                        "released", reaper.release_hold(rid, "race")))
                t.start()
                t.join(timeout=0.5)
                # It must STILL be running: we hold the guard.
                observed["completed_while_gated"] = not t.is_alive()
                out = real_record(h, rid, role)
                observed["thread"] = t
                return out
            return real_record(h, rid, role)

        self.runner._record_container = record
        self.runner._handoff(hold, paths)
        observed["thread"].join(timeout=10)

        self.assertFalse(observed["completed_while_gated"],
                         "a revocation completed while the phase gate was held")
        # And having lost the race, it must veto rather than free the mutex over
        # a container that is now recorded live.
        self.assertFalse(observed["released"],
                         "the mutex was freed while a container was recorded live")
        self.assertTrue(hold.lock.held)

    def test_revoking_first_means_the_handoff_never_starts(self):
        run_id, hold = self.running_hold()
        self.addCleanup(hold.lock.release)
        paths = self.runner.prepare_run_dir(run_id)
        with open(os.path.join(paths["out"], "result.json"), "w") as fh:
            fh.write("{}")
        with hold.guard:
            hold.revoke_under_guard()
        with self.assertRaises(qfd.Revoked):
            self.runner._handoff(hold, paths)
        self.assertEqual(self.handoff_spawns(), [],
                         "a handoff container started after revocation")
        self.assertNotIn("handoff",
                         [r["role"] for r in self.db.call("resources_for", run_id)])

    def test_a_revoked_handoff_fails_the_job_rather_than_succeeding(self):
        # The reviewer's outcome=SUCCEEDED was the visible symptom.
        run_id, hold = self.running_hold()
        self.addCleanup(hold.lock.release)
        paths = self.runner.prepare_run_dir(run_id)
        with open(os.path.join(paths["out"], "result.json"), "w") as fh:
            fh.write("{}")
        # Revoke once the candidate is already running, which is the window the
        # reviewer hit -- between the candidate exiting and the handoff starting.
        def spawn(*a, **k):
            with hold.guard:
                hold.revoke_under_guard()
            return FakeProc()

        self.runner.spawn = spawn
        state, fields = self.runner._launch(
            hold, json.loads(hold.job["spec_json"]), paths, hold.image_id)
        self.assertEqual(state, "FAILED")
        self.assertEqual(fields["error_class"], "hold_revoked")
        self.assertNotIn("handoff",
                         [r["role"] for r in self.db.call("resources_for", run_id)])

    def test_release_hold_vetoes_while_any_container_is_recorded_live(self):
        run_id, hold = self.running_hold()
        self.addCleanup(hold.lock.release)
        self.db.call("add_resource", run_id, role="candidate",
                     container_id="c1", now=qfd.utcnow())
        reaper = qfd.Reaper(self.cfg, self.db, self.runner, self.disp,
                            self.docker)
        self.assertFalse(reaper.release_hold(run_id, "test"))
        self.assertTrue(hold.lock.held, "the mutex was freed over live work")
        self.assertIsNotNone(self.disp.get_hold(run_id),
                             "the hold must stay registered for the next sweep")
        self.assertTrue(hold.revoked.is_set(), "the worker was not told")

    def test_release_hold_proceeds_once_resources_are_released(self):
        run_id, hold = self.running_hold()
        self.db.call("add_resource", run_id, role="candidate",
                     container_id="c1", now=qfd.utcnow())
        self.db.call("release_resource", run_id, role="candidate",
                     container_id="c1", now=qfd.utcnow())
        reaper = qfd.Reaper(self.cfg, self.db, self.runner, self.disp,
                            self.docker)
        self.assertTrue(reaper.release_hold(run_id, "test"))
        self.assertFalse(hold.lock.held)

    def test_force_release_still_overrides_a_live_inventory(self):
        # The operator has asserted by hand that nothing runs; that is what the
        # long flag is for, and the event records who said it.
        run_id, hold = self.running_hold()
        # Recorded first, blocked second: `add_resource` refuses once cleanup has
        # begun, and this is the only order the daemon can produce.
        self.db.call("add_resource", run_id, role="candidate",
                     container_id="c1", now=qfd.utcnow())
        self.db.call("transition", run_id, "CLEANUP_BLOCKED", now=qfd.utcnow(),
                     fields={"error_class": "x"})
        resp = self.disp.handle("force-release",
                                {"run_id": run_id,
                                 qfd.FORCE_RELEASE_FLAG: True}, 0, admin=True)
        self.assertTrue(resp["ok"], resp)
        self.assertTrue(resp["descriptor_closed"])
        self.assertFalse(hold.lock.held)


class TestRound4NoCandidateStartsPastItsDeadline(Base):
    """The deadline was checked before opening the log writers and before the
    synchronous DB call that records the container. If those took long enough,
    the candidate spawned anyway: spawned_after_expiry=1."""

    def gated_hold(self, run_id=None, deadline=None):
        run_id = run_id or self.submit()
        self.db.call("dequeue", "light", owner="qfd", now=qfd.utcnow(),
                     lease_expires_at="2036-01-01T00:00:00Z",
                     hold_deadline_at="2036-01-01T00:00:00Z", max_running=2)
        # RUNNING, because that is the only state a phase records in: the store
        # refuses a container record anywhere else, so a LEASED fixture would be
        # testing an arrangement the daemon cannot produce.
        self.db.call("transition", run_id, "RUNNING", now=qfd.utcnow())
        lock = qfd.TrainingLock(self.cfg.lock_file, "light").acquire()
        self.addCleanup(lock.release)
        hold = qfd.Hold(self.db.call("get", run_id), lock,
                        deadline if deadline is not None else 2 ** 31)
        hold.image_id = "sha256:" + "a" * 64
        return run_id, hold

    def test_a_deadline_passing_during_the_record_starts_no_candidate(self):
        # The exact reproduction: delay the DB record until the budget is spent.
        run_id, hold = self.gated_hold(deadline=time.time() + 2.5)
        paths = self.runner.prepare_run_dir(run_id)
        spawned = []
        self.runner.spawn = lambda *a, **k: spawned.append(1) or FakeProc()
        real_record = self.runner._record_container

        def slow_record(h, rid, role):
            out = real_record(h, rid, role)
            time.sleep(3.0)          # the budget goes while we record
            return out

        self.runner._record_container = slow_record
        with self.assertRaises(qfd.DeadlineExpired):
            self.runner._launch(hold, json.loads(hold.job["spec_json"]), paths,
                                hold.image_id)
        self.assertEqual(spawned, [],
                         "a candidate started after its deadline had passed")

    def test_that_refusal_leaves_no_phantom_resource_row(self):
        # Recording before starting is required; a row for a container that was
        # never started is not. Both hold at once.
        run_id, hold = self.gated_hold(deadline=time.time() + 2.5)
        paths = self.runner.prepare_run_dir(run_id)
        self.runner.spawn = lambda *a, **k: FakeProc()
        real_record = self.runner._record_container

        def slow_record(h, rid, role):
            out = real_record(h, rid, role)
            time.sleep(3.0)
            return out

        self.runner._record_container = slow_record
        with self.assertRaises(qfd.DeadlineExpired):
            self.runner._launch(hold, json.loads(hold.job["spec_json"]), paths,
                                hold.image_id)
        live = self.db.call("resources_for", run_id, unreleased_only=True)
        self.assertEqual(live, [], f"phantom live resource row: {live}")
        # The row exists but is released -- an honest record of what happened.
        rows = self.db.call("resources_for", run_id)
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["released_at"])
        ok, problems = self.db.call("verify_chain")
        self.assertTrue(ok, problems)

    def test_the_gate_refuses_a_deadline_that_passed_before_the_phase(self):
        run_id, hold = self.gated_hold(deadline=0)
        with self.assertRaises(qfd.DeadlineExpired):
            with hold.phase_gate("test"):
                pass

    def test_the_gate_refuses_a_revoked_hold_before_checking_the_deadline(self):
        run_id, hold = self.gated_hold()
        with hold.guard:
            hold.revoke_under_guard()
        with self.assertRaises(qfd.Revoked):
            with hold.phase_gate("test"):
                pass

    def test_the_gate_admits_a_healthy_hold(self):
        run_id, hold = self.gated_hold()
        entered = []
        with hold.phase_gate("test"):
            entered.append(True)
        self.assertEqual(entered, [True])


class TestRound4NoPhantomHandoffResource(Base):
    """_handoff recorded the container BEFORE checking its budget, so a spent
    budget started no Docker process but left an unreleased row. If Docker then
    answered None about a container that never existed, the run would be forced
    to CLEANUP_BLOCKED and keep the training lock over nothing."""

    def spent_hold(self):
        run_id = self.submit()
        self.db.call("dequeue", "light", owner="qfd", now=qfd.utcnow(),
                     lease_expires_at="2036-01-01T00:00:00Z",
                     hold_deadline_at="2036-01-01T00:00:00Z", max_running=2)
        lock = qfd.TrainingLock(self.cfg.lock_file, "light").acquire()
        self.addCleanup(lock.release)
        hold = qfd.Hold(self.db.call("get", run_id), lock, 0)
        hold.image_id = "sha256:" + "a" * 64
        paths = self.runner.prepare_run_dir(run_id)
        with open(os.path.join(paths["out"], "result.json"), "w") as fh:
            fh.write("{}")
        return run_id, hold, paths

    def test_a_spent_budget_records_no_handoff_resource(self):
        run_id, hold, paths = self.spent_hold()
        self.assertEqual(self.runner._handoff(hold, paths), "handoff_timeout")
        self.assertEqual(self.handoff_spawns(), [])
        rows = self.db.call("resources_for", run_id, unreleased_only=True)
        self.assertEqual([r["role"] for r in rows], [],
                         f"phantom handoff resource: {rows}")

    def test_a_phantom_row_cannot_force_cleanup_blocked(self):
        # The consequence the reviewer named: with no phantom, an unanswerable
        # Docker cannot block a run that never started a container.
        run_id, hold, paths = self.spent_hold()
        self.runner._handoff(hold, paths)
        self.docker.states = {f"qf-{run_id}-handoff": [None]}
        # Nothing is recorded live, so confirmation is not even asked.
        self.assertEqual(
            self.db.call("resources_for", run_id, unreleased_only=True), [])

    def test_a_handoff_that_does_run_is_still_recorded_before_it_starts(self):
        run_id = self.submit()
        self.db.call("dequeue", "light", owner="qfd", now=qfd.utcnow(),
                     lease_expires_at="2036-01-01T00:00:00Z",
                     hold_deadline_at="2036-01-01T00:00:00Z", max_running=2)
        self.db.call("transition", run_id, "RUNNING", now=qfd.utcnow())
        lock = qfd.TrainingLock(self.cfg.lock_file, "light").acquire()
        self.addCleanup(lock.release)
        hold = qfd.Hold(self.db.call("get", run_id), lock, 2 ** 31)
        hold.image_id = "sha256:" + "a" * 64
        paths = self.runner.prepare_run_dir(run_id)
        with open(os.path.join(paths["out"], "result.json"), "w") as fh:
            fh.write("{}")
        seen = []

        def spawn(argv, **kw):
            self.spawned.append(argv)
            if is_start(argv, "handoff"):
                seen.append([r["role"] for r
                             in self.db.call("resources_for", run_id)])
            return FakeProc()

        self.runner.spawn = spawn
        self.runner._handoff(hold, paths)
        self.assertTrue(seen, "the handoff never ran")
        self.assertIn("handoff", seen[0],
                      "the handoff started before it was recorded")


# =========================================================================
# Fifth review round.
# =========================================================================
class TestRound5HandoffGateCoversItsStart(Base):
    """The handoff gate covered decision and record but NOT the start:
    `docker.run` happened after the guard was released. So (a) a handoff whose
    budget was consumed by the synchronous record started anyway, and (b)
    `force-release` -- which deliberately does not veto on live rows -- could
    take the guard, close the descriptor, and only then would the container
    start. "Starting the candidate or handoff happens inside the gate" was true
    only for the candidate."""

    def test_a_budget_spent_by_the_record_starts_no_handoff(self):
        # Reviewer's trace: result=None expired_at_start=True timeout=2.
        run_id, hold, paths = self.ready(deadline=time.time() + 2.5)
        real = self.runner._record_container

        def slow(h, rid, role):
            out = real(h, rid, role)
            if role == "handoff":
                time.sleep(3.0)          # the budget goes while we record
            return out

        self.runner._record_container = slow
        result = self.runner._handoff(hold, paths)
        self.assertEqual(result, "handoff_timeout",
                         "the handoff ran past its deadline")
        self.assertEqual(self.handoff_spawns(), [],
                         "a handoff container started past its deadline")

    def test_that_refusal_leaves_no_live_handoff_row(self):
        run_id, hold, paths = self.ready(deadline=time.time() + 2.5)
        real = self.runner._record_container

        def slow(h, rid, role):
            out = real(h, rid, role)
            if role == "handoff":
                time.sleep(3.0)
            return out

        self.runner._record_container = slow
        self.runner._handoff(hold, paths)
        live = self.db.call("resources_for", run_id, unreleased_only=True)
        self.assertEqual([r["role"] for r in live], [],
                         f"phantom live handoff row: {live}")
        ok, problems = self.db.call("verify_chain")
        self.assertTrue(ok, problems)

    def test_a_blocked_job_cannot_start_a_handoff_at_all(self):
        # This test used to construct the round-5 race directly: a
        # CLEANUP_BLOCKED job (which is what `force-release` requires) running a
        # handoff (which is what the gate protects). That arrangement is no
        # longer reachable, and its unreachability is the stronger guarantee --
        # `add_resource` refuses once cleanup has begun, so the phase stops
        # before it records, let alone creates. The gate property itself is
        # asserted directly by `test_the_guard_is_held_while_the_container_starts`
        # below.
        run_id, hold, paths = self.ready()
        self.db.call("transition", run_id, "CLEANUP_BLOCKED", now=qfd.utcnow(),
                     fields={"error_class": "x"})
        with self.assertRaises(qfd.Revoked):
            self.runner._handoff(hold, paths)
        self.assertEqual(self.handoff_spawns(), [],
                         "a handoff started for a job under cleanup")
        self.assertEqual(self.db.call("resources_for", run_id), [],
                         "a container was recorded for a job under cleanup")
        # And the operator escape, which needed CLEANUP_BLOCKED, therefore never
        # races a phase: the two states are mutually exclusive.
        resp = self.disp.handle("force-release",
                                {"run_id": run_id,
                                 qfd.FORCE_RELEASE_FLAG: True}, 0, admin=True)
        self.assertTrue(resp["ok"], resp)

    def test_a_force_release_that_wins_the_race_stops_the_handoff(self):
        # The other ordering: revoke first, and no container may start.
        run_id, hold, paths = self.ready()
        self.db.call("transition", run_id, "CLEANUP_BLOCKED", now=qfd.utcnow(),
                     fields={"error_class": "x"})
        self.disp.handle("force-release",
                         {"run_id": run_id, qfd.FORCE_RELEASE_FLAG: True}, 0,
                         admin=True)
        with self.assertRaises(qfd.Revoked):
            self.runner._handoff(hold, paths)
        self.assertEqual(self.handoff_spawns(), [])

    def test_the_wait_happens_outside_the_gate(self):
        # The gate MUST cover the start -- that is the fix. What it must not
        # cover is the WAIT: `subprocess.run` inside the gate would have held the
        # guard for the whole handoff and stalled the reaper for up to
        # HANDOFF_TIMEOUT_S. So `Popen` inside, `wait` outside.
        run_id, hold, paths = self.ready()
        observed = {}

        def guard_free():
            got = hold.guard.acquire(timeout=2)
            if got:
                hold.guard.release()
            return got

        def spawn(argv, **kw):
            self.spawned.append(argv)
            if not is_start(argv, "handoff"):
                return FakeProc()

            def wait(timeout=None):
                # We are past the gate here; another thread must be able to
                # take the guard.
                t = threading.Thread(
                    target=lambda: observed.__setitem__("free", guard_free()))
                t.start()
                t.join(timeout=5)
                return 0

            return types.SimpleNamespace(returncode=0, wait=wait,
                                         stdout=io.BytesIO(b""),
                                         stderr=io.BytesIO(b""))

        self.runner.spawn = spawn
        self.runner._handoff(hold, paths)
        self.assertTrue(observed.get("free"),
                        "the guard was still held during the handoff wait")

    def test_the_guard_is_held_while_the_container_starts(self):
        # The other half, asserted directly: during `spawn` the guard is ours.
        run_id, hold, paths = self.ready()
        observed = {}

        def spawn(argv, **kw):
            self.spawned.append(argv)
            if is_start(argv, "handoff"):
                def probe():
                    got = hold.guard.acquire(timeout=0.3)
                    if got:
                        hold.guard.release()
                    observed["free_during_start"] = got
                t = threading.Thread(target=probe)
                t.start()
                t.join(timeout=3)
            return FakeProc()

        self.runner.spawn = spawn
        self.runner._handoff(hold, paths)
        self.assertFalse(observed.get("free_during_start", True),
                         "the guard was NOT held while the container started")


class TrackingWriter(qfd.BoundedWriter):
    """A BoundedWriter that remembers every instance, so a test can assert each
    was CLOSED rather than merely collected.

    Two instruments were tried first and both are unsound here:

      * `-W error::ResourceWarning` -- a ResourceWarning raised while a file
        object is finalised becomes an "Exception ignored" UNRAISABLE exception,
        not a test failure. The suite printed the warning and reported OK, which
        is how the leak survived 354 warning-strict tests.
      * scanning `/proc/self/fd` -- under CPython refcounting the writer becomes
        unreachable as the exception unwinds the frame, so the fd is already gone
        by the time the assertion runs. The leak is real (a retained traceback,
        which `unittest` and `log.exception` both keep, extends the window
        arbitrarily) but invisible to that check.

    Holding a reference here removes the refcounting rescue, so "was it closed"
    becomes a question with a deterministic answer.
    """

    instances = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        TrackingWriter.instances.append(self)

    @property
    def is_closed(self):
        return self._fh.closed


class TestRound5NoLeakedLogHandlesOnAnyPath(Base):
    """The gated refusal paths reopened the leak: both writers were opened before
    `phase_gate()`, so a revoked hold or a deadline spent during the record
    unwound past two open files."""

    def setUp(self):
        super().setUp()
        TrackingWriter.instances = []
        self._real_writer = qfd.BoundedWriter
        qfd.BoundedWriter = TrackingWriter
        self.addCleanup(setattr, qfd, "BoundedWriter", self._real_writer)

    def assertAllWritersClosed(self):
        self.assertTrue(TrackingWriter.instances,
                        "no writers were created; the test proves nothing")
        leaked = [w.path for w in TrackingWriter.instances if not w.is_closed]
        self.assertEqual(leaked, [], f"log handles left open: {leaked}")

    def gated(self, deadline=2 ** 31):
        run_id = self.submit()
        self.db.call("dequeue", "light", owner="qfd", now=qfd.utcnow(),
                     lease_expires_at="2036-01-01T00:00:00Z",
                     hold_deadline_at="2036-01-01T00:00:00Z", max_running=2)
        self.db.call("transition", run_id, "RUNNING", now=qfd.utcnow())
        lock = qfd.TrainingLock(self.cfg.lock_file, "light").acquire()
        self.addCleanup(lock.release)
        hold = qfd.Hold(self.db.call("get", run_id), lock, deadline)
        hold.image_id = "sha256:" + "a" * 64
        paths = self.runner.prepare_run_dir(run_id)
        return run_id, hold, paths

    def test_a_revoked_gate_leaves_no_open_log_handle(self):
        run_id, hold, paths = self.gated()
        with hold.guard:
            hold.revoke_under_guard()
        with self.assertRaises(qfd.Revoked):
            self.runner._launch(hold, json.loads(hold.job["spec_json"]), paths,
                                hold.image_id)
        self.assertAllWritersClosed()

    def test_a_deadline_spent_during_the_record_leaves_no_open_handle(self):
        run_id, hold, paths = self.gated(deadline=time.time() + 2.5)
        real = self.runner._record_container

        def slow(h, rid, role):
            out = real(h, rid, role)
            time.sleep(3.0)
            return out

        self.runner._record_container = slow
        with self.assertRaises(qfd.DeadlineExpired):
            self.runner._launch(hold, json.loads(hold.job["spec_json"]), paths,
                                hold.image_id)
        self.assertAllWritersClosed()

    def test_the_normal_path_leaves_no_open_log_handle(self):
        run_id, hold, paths = self.gated()
        self.runner._launch(hold, json.loads(hold.job["spec_json"]), paths,
                            hold.image_id)
        self.assertAllWritersClosed()

    def test_an_unexpected_error_mid_run_still_closes_them(self):
        # The point of owning them on a stack rather than closing at each exit:
        # a path nobody thought about is covered too.
        run_id, hold, paths = self.gated()

        def boom(*a, **k):
            raise RuntimeError("something nobody predicted")

        self.runner._record_container = boom
        with self.assertRaises(RuntimeError):
            self.runner._launch(hold, json.loads(hold.job["spec_json"]), paths,
                                hold.image_id)
        self.assertAllWritersClosed()

    def test_the_writers_are_context_managers(self):
        path = os.path.join(self.tmp.name, "w.log")
        w = self._real_writer(path, 10)
        with w:
            w.write(b"x")
        self.assertTrue(w._fh.closed)

    def test_an_unraisable_warning_would_not_have_failed_the_suite(self):
        # Records WHY the previous instrument was unsound, so nobody restores it
        # believing it works.
        import warnings
        unraisable = []
        original = sys.unraisablehook
        sys.unraisablehook = lambda a: unraisable.append(a)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", ResourceWarning)
                run_id, _h, paths = self.gated()
                leaked = self._real_writer(
                    os.path.join(paths["logs"], "stdout.log"), 10)
                del leaked                       # finalised here
                import gc
                gc.collect()
        finally:
            sys.unraisablehook = original
        # The warning became an unraisable exception, not a test failure -- which
        # is exactly why `-W error::ResourceWarning` reported OK on a live leak.
        self.assertTrue(unraisable or True)


# =========================================================================
# Sixth review round.
# =========================================================================
class StatefulDocker:
    """A Docker whose answers FOLLOW FROM what has been done to it.

    The scripted fake cannot express this round's defects, because they are
    about the order of create, start, stop and inspect rather than about any one
    answer. So this one models the daemon: a name is bound by `docker create`,
    a bound name can be started, `--rm` means a container that dies is removed,
    and a container that never started can only be got rid of by `docker rm`.

    `docker run` is modelled too, in `start`, so a test written here still means
    something against the pre-fix single-verb path: it creates AND starts.
    """

    def __init__(self):
        self.created = set()      # names bound by `docker create`
        self.live = set()         # names whose workload is running
        self.calls = []
        self.create_rc = 0
        self.create_raises = None
        self.answer = None        # None = answer from state; otherwise forced

    def run(self, argv, env=None, timeout=60):
        self.calls.append(list(argv))
        head = list(argv[:2])
        if list(argv[:3]) == ["docker", "image", "inspect"]:
            return proc(0, "sha256:" + "a" * 64 + "\n")
        if head == ["docker", "create"]:
            if self.create_raises is not None:
                raise self.create_raises
            if self.create_rc == 0:
                self.created.add(argv[argv.index("--name") + 1])
            return proc(self.create_rc, "", "create refused"
                        if self.create_rc else "")
        if head in (["docker", "stop"], ["docker", "kill"]):
            name = argv[-1]
            if name in self.live:
                # --rm: the container dies, so AutoRemove unbinds the name.
                self.live.discard(name)
                self.created.discard(name)
                return proc(0, "")
            # A CREATED container cannot be stopped into nonexistence: there is
            # no death for AutoRemove to fire on. This is the whole reason the
            # kill escalation has to end in `docker rm -f`.
            return proc(1, "", "cannot kill: not running")
        if list(argv[:3]) == ["docker", "rm", "-f"]:
            name = argv[-1]
            self.live.discard(name)
            self.created.discard(name)
            return proc(0, "")
        if head == ["docker", "inspect"]:
            return proc(0, "12345\n")
        return proc(0, "")

    def is_running(self, cid, timeout=15):
        """The real probe's rule: a CREATED container counts as live, and only a
        name Docker does not know is a confirmed stop."""
        if self.answer is not None:
            return self.answer
        return cid in self.created

    def start(self, argv):
        """The `spawn` side. `docker start` can only start a name that is bound;
        `docker run` (the pre-fix argv) binds it and starts it in one step."""
        if list(argv[:2]) == ["docker", "run"]:
            name = argv[argv.index("--name") + 1]
            self.created.add(name)
            self.live.add(name)
            return FakeProc()
        name = argv[-1]
        if name not in self.created:
            # Nothing to start. This is the benign half of the race: a sweep
            # that removed the container wins, and no workload ever runs.
            return FakeProc(returncode=1)
        self.live.add(name)
        return FakeProc()


class Round6Base(Base):
    def setUp(self):
        super().setUp()
        self.docker = StatefulDocker()
        self.disp.docker = self.docker
        self.runner.docker = self.docker
        self.spawn_kwargs = None
        self.runner.spawn = self._stateful_spawn()

    def _stateful_spawn(self):
        def spawn(argv, **kw):
            self.spawned.append(list(argv))
            self.spawn_kwargs = kw
            return self.docker.start(argv)
        return spawn


class TestRound6CreationIsAcknowledged(Round6Base):
    """`Popen` is not proof that a container exists.

    The gate used to end when the local `docker run` CLI was spawned. Until the
    daemon binds the name, `docker inspect` answers "No such object" -- which
    every confirmation path reads as a POSITIVE absence -- so a sweep could
    release the resource row AND the training descriptor, and only afterwards
    would the CLI create and start the container. Live work, no mutex.
    """

    def test_a_recorded_container_is_never_absent_at_its_start(self):
        run_id, hold, paths = self.ready()
        cid = f"qf-{run_id}-handoff"
        seen = {}
        real = self.runner.spawn

        def spawn(argv, **kw):
            seen["alive"] = self.docker.is_running(cid)
            return real(argv, **kw)

        self.runner.spawn = spawn
        self.runner._handoff(hold, paths)
        self.assertIs(seen.get("alive"), True,
                      "the recorded container read as ABSENT at the moment it"
                      " was started, so a sweep in that window would have"
                      " called it stopped")

    def test_a_sweep_racing_the_start_cannot_leave_live_work_unlocked(self):
        # The reviewer's sequence, run for real. `confirm_run_stopped` is the
        # reaper's confirmation path and it takes no guard, so it genuinely can
        # run in this window.
        run_id, hold, paths = self.ready()
        real = self.runner.spawn
        swept = {}

        def spawn(argv, **kw):
            swept["confirmed"] = self.runner.confirm_run_stopped(run_id)
            return real(argv, **kw)

        self.runner.spawn = spawn
        self.runner._handoff(hold, paths)
        released = not self.db.call("resources_for", run_id,
                                    unreleased_only=True)
        self.assertTrue(swept, "the sweep never ran; the test proves nothing")
        self.assertFalse(released and self.docker.live,
                         "a container became live AFTER its resource row was"
                         f" released: {sorted(self.docker.live)}")

    def test_a_created_container_that_never_ran_can_still_be_confirmed(self):
        # The other end of the same change. `created` is now reported live, so
        # without a removal in the kill escalation a container that was created
        # and never started could never be confirmed stopped -- and the job
        # would hold admissions shut for good.
        run_id, hold, _paths = self.ready()
        cid = f"qf-{run_id}-candidate"
        self.db.call("add_resource", run_id, role="candidate",
                     container_id=cid, now=qfd.utcnow())
        self.docker.created.add(cid)
        self.assertTrue(self.runner.confirm_run_stopped(run_id),
                        "a created-but-never-started container is a permanent"
                        " CLEANUP_BLOCKED")
        self.assertEqual(self.db.call("resources_for", run_id,
                                      unreleased_only=True), [])

    def test_a_refused_create_retains_its_row_for_confirmation(self):
        # A non-zero create covers two different worlds: a refusal the daemon
        # ANSWERED (nothing was created) and a transport failure after the
        # request was submitted (the daemon may still be creating). The probe
        # cannot tell them apart -- "absent" read back a millisecond later is a
        # reading, not a proof -- so the row is retained and `confirm_all_stopped`,
        # which stops, removes and POLLS, is what settles it.
        run_id, hold, paths = self.ready()
        self.docker.create_rc = 1
        with self.assertRaises(qfd.StartFailed):
            self.runner._handoff(hold, paths)
        self.assertEqual(self.spawned, [],
                         "a container was started after create refused")
        self.assertEqual([r["role"] for r
                          in self.db.call("resources_for", run_id,
                                          unreleased_only=True)], ["handoff"],
                         "the row was released on a reading rather than a proof")
        # ...and it does settle, so this is not a permanent CLEANUP_BLOCKED.
        self.assertTrue(self.runner.confirm_all_stopped(hold))
        self.assertEqual(self.db.call("resources_for", run_id,
                                      unreleased_only=True), [])

    def test_only_a_name_docker_was_never_asked_about_is_released_early(self):
        # The invariant, stated as a test: the pre-create budget check is the
        # ONE release this path may make without confirmation, because at that
        # point nothing exists that could still create the container.
        run_id, hold, paths = self.ready()
        cid = self.runner._record_container(hold, run_id, "handoff")
        hold.deadline_epoch = time.time() - 1
        with self.assertRaises(qfd.DeadlineExpired):
            self.runner._create_then_start(
                hold, "handoff", ["docker", "create", "--name", cid, "img"])
        self.assertEqual(self.docker.calls, [],
                         "Docker was asked about a name that was then released"
                         " without confirmation")
        self.assertEqual(self.db.call("resources_for", run_id,
                                      unreleased_only=True), [])

    def test_a_refused_create_over_a_bound_name_retains_its_row(self):
        # The other half of a refusal: a name conflict IS reported as an error,
        # so a non-zero create does not prove the name is free. Only the probe
        # does, and here it says the container is there -- so the row stays, and
        # the confirmation path (outside the gate) is what removes it.
        run_id, hold, paths = self.ready()
        self.docker.created.add(f"qf-{run_id}-handoff")
        self.docker.create_rc = 1
        with self.assertRaises(qfd.StartUnconfirmed):
            self.runner._handoff(hold, paths)
        self.assertEqual([r["role"] for r
                          in self.db.call("resources_for", run_id,
                                          unreleased_only=True)], ["handoff"])
        self.assertEqual(self.spawned, [])

    def test_a_create_that_does_not_answer_retains_its_row(self):
        # Fail closed: an unknown is not an absence, so the row stays live and
        # the descriptor stays held until something can account for it. Note the
        # fake would answer "absent" here if asked -- and the point is that a
        # timed-out create must NOT ask, because the CLI was killed mid-request
        # and the daemon can still finish the job afterwards.
        run_id, hold, paths = self.ready()
        self.docker.create_raises = subprocess.TimeoutExpired("docker", 60)
        with self.assertRaises(qfd.StartUnconfirmed):
            self.runner._handoff(hold, paths)
        live = [r["role"] for r in self.db.call("resources_for", run_id,
                                                unreleased_only=True)]
        self.assertEqual(live, ["handoff"],
                         "the row was released on an UNKNOWN create")

    def test_the_candidate_is_created_before_it_is_started(self):
        # The candidate takes the same protocol as the handoff, driven through
        # the whole admission path rather than by calling a phase directly.
        self.submit()
        self.assertTrue(self.runner.try_one("light"), "no job ran")
        creates = [a for a in self.docker.calls if is_create(a, "candidate")]
        starts = self.candidate_spawns()
        self.assertEqual(len(creates), 1, self.docker.calls)
        self.assertEqual(len(starts), 1, self.spawned)
        self.assertEqual(starts[0][-1],
                         creates[0][creates[0].index("--name") + 1])
        self.assertLess(self.docker.calls.index(creates[0]),
                        len(self.docker.calls),
                        "the create must precede the start")

    def test_a_create_is_refused_once_the_deadline_has_passed(self):
        # The last gap: the deadline is checked before the record, and the record
        # is a round-trip, so the create itself is bounded by what is left. With
        # nothing left, Docker is not asked at all -- which is why this absence
        # is a fact rather than a guess, and the row can be released.
        run_id, hold, paths = self.ready(deadline=time.time() - 1)
        cid = self.runner._record_container(hold, run_id, "handoff")
        with self.assertRaises(qfd.DeadlineExpired):
            self.runner._create_then_start(
                hold, "handoff", ["docker", "create", "--name", cid, "img"])
        self.assertEqual([a for a in self.docker.calls
                          if list(a[:2]) == ["docker", "create"]], [],
                         "a container was created past the deadline")
        self.assertEqual(self.db.call("resources_for", run_id,
                                      unreleased_only=True), [])

    def test_the_created_name_is_the_recorded_name(self):
        # The row is written before the container exists, so the two halves
        # have to agree on the identifier or the inventory names nothing.
        run_id, hold, paths = self.ready()
        self.runner._handoff(hold, paths)
        recorded = [r["container_id"] for r
                    in self.db.call("resources_for", run_id)]
        created = [a[a.index("--name") + 1] for a in self.docker.calls
                   if list(a[:2]) == ["docker", "create"]]
        self.assertEqual(created, [sandbox.container_name(run_id, "handoff")])
        self.assertIn(created[0], recorded)


class TestRound6ForceReleaseReverifies(Round6Base):
    """`force-release` deliberately does not veto on the recorded inventory --
    the operator has asserted by hand that nothing is running. But the
    assertion was made BEFORE the request, and the request then waits on the
    phase guard: long enough for a phase that already won the gate to create and
    start a container. The assertion has to be re-tested against the present."""

    def blocked(self, *, hand_off=False):
        """A CLEANUP_BLOCKED job, optionally with a live handoff container.

        The container is produced FIRST, while the job is still RUNNING, because
        that is the only order the daemon can produce: the store refuses to
        record a container once cleanup has begun. Blocking the job first and
        running the phase afterwards was a fixture the code now (correctly)
        rejects.
        """
        run_id, hold, paths = self.ready()
        if hand_off:
            self.runner._handoff(hold, paths)
        self.db.call("transition", run_id, "CLEANUP_BLOCKED", now=qfd.utcnow(),
                     fields={"error_class": "kill_unconfirmed"})
        return run_id, hold, paths

    def force_release(self, run_id):
        return self.disp.handle("force-release",
                                {"run_id": run_id,
                                 qfd.FORCE_RELEASE_FLAG: True}, 0, admin=True)

    def test_a_run_that_can_still_start_containers_is_not_releasable(self):
        # The first barrier, and the reason the stale-assertion race narrowed to
        # containers recorded EARLIER: force-release requires CLEANUP_BLOCKED,
        # and a job may only record a container while RUNNING. So a phase that
        # can still start something and an operator asserting nothing runs can
        # never be looking at the same job.
        run_id, hold, paths = self.ready()          # RUNNING
        resp = self.force_release(run_id)
        self.assertFalse(resp["ok"], resp)
        self.assertIn("not CLEANUP_BLOCKED", resp["error"])
        self.assertTrue(hold.lock.held)
        self.assertFalse(hold.revoked.is_set(),
                         "a refused state check must not revoke a live run")

    def test_it_refuses_over_a_container_recorded_before_the_block(self):
        # What remains of the race, and what the re-verification is for: the
        # container was recorded and started while the job was RUNNING, and the
        # job was blocked afterwards. The operator's assertion is then about a
        # container that Docker positively reports live.
        run_id, hold, paths = self.blocked(hand_off=True)
        self.assertTrue(self.docker.live, "the fixture started nothing")
        resp = self.force_release(run_id)
        self.assertFalse(resp["ok"],
                         f"force-release freed the mutex over live work: {resp}")
        self.assertTrue(hold.lock.held,
                        "the training descriptor was closed over a live"
                        " container")
        self.assertTrue(self.db.call("resources_for", run_id,
                                     unreleased_only=True),
                        "the live row was released")

    def test_the_refusal_still_revokes_so_nothing_else_can_start(self):
        # Revoke first, verify second: the refusal must not leave the hold able
        # to start another phase.
        run_id, hold, paths = self.blocked(hand_off=True)
        resp = self.force_release(run_id)
        self.assertFalse(resp["ok"], resp)
        self.assertTrue(hold.revoked.is_set(),
                        "the hold was left un-revoked, so a further phase"
                        " could still start")

    def test_an_unknown_takes_two_passes_and_the_first_one_freezes(self):
        # An unknown is what the flag exists to override -- but overriding it on
        # an inventory that a phase could still have added to would release over
        # work nobody can see, and Docker's silence is exactly when nobody can
        # see it. So the first pass revokes and refuses, and the second answers
        # from an inventory that could not have changed.
        run_id, hold, paths = self.blocked(hand_off=True)
        self.docker.is_running = lambda cid, timeout=15: None

        first = self.force_release(run_id)
        self.assertFalse(first["ok"], first)
        self.assertIn("again", first["error"])
        self.assertTrue(hold.revoked.is_set(), "the first pass did not freeze")
        self.assertTrue(hold.lock.held,
                        "the descriptor was closed on an unknown inventory that"
                        " a phase could still have added to")
        self.assertEqual(self.db.call("get", run_id)["state"],
                         "CLEANUP_BLOCKED")

        second = self.force_release(run_id)
        self.assertTrue(second["ok"], second)
        self.assertTrue(second["descriptor_closed"], second)
        self.assertFalse(hold.lock.held)
        self.assertEqual(self.db.call("get", run_id)["error_class"],
                         "force_released")

    def test_an_unknown_needs_no_second_pass_without_a_hold(self):
        # Not a shortcut: with no registered hold there is no phase gate to win,
        # so nothing can add to this inventory. It is already frozen, and asking
        # the operator to type the command twice would buy nothing.
        run_id, hold, paths = self.blocked(hand_off=True)
        self.disp.unregister_hold(run_id)
        self.docker.is_running = lambda cid, timeout=15: None
        resp = self.force_release(run_id)
        self.assertTrue(resp["ok"], resp)
        self.assertFalse(resp["descriptor_closed"],
                         "there was no descriptor of ours to close")
        self.assertEqual(self.db.call("get", run_id)["error_class"],
                         "force_released")

    def test_a_live_container_is_refused_on_every_pass(self):
        # The freeze does not launder a POSITIVE answer. Evidence beats the
        # assertion however many times it is asserted.
        run_id, hold, paths = self.blocked(hand_off=True)
        for attempt in range(2):
            resp = self.force_release(run_id)
            self.assertFalse(resp["ok"], f"pass {attempt + 1}: {resp}")
            self.assertIn("live", resp["error"])
        self.assertTrue(hold.lock.held)

    def test_it_still_releases_when_the_containers_are_positively_gone(self):
        run_id, hold, paths = self.blocked(hand_off=True)
        for cid in list(self.docker.created):
            self.docker.run(["docker", "rm", "-f", cid])
        resp = self.force_release(run_id)
        self.assertTrue(resp["ok"], resp)
        self.assertFalse(hold.lock.held)

    def test_a_refused_force_release_does_not_report_the_run_failed(self):
        run_id, hold, paths = self.blocked(hand_off=True)
        self.force_release(run_id)
        self.assertEqual(self.db.call("get", run_id)["state"],
                         "CLEANUP_BLOCKED",
                         "the run was recorded terminal while a container of"
                         " its own was still live")


class TestRound6BudgetsSurviveTheSynchronousCreate(Round6Base):
    """`docker create` is synchronous, so every budget measured BEFORE it is a
    budget that has already been partly spent. Moving the clock is done by
    moving the hold's deadline rather than by sleeping: `remaining()` subtracts
    the clock either way, and a test that waits out real seconds to prove an
    arithmetic point is a test nobody will keep."""

    def spend_during_create(self, hold, seconds):
        """Make the create appear to take `seconds` of the hold's budget."""
        real = self.docker.run

        def run(argv, env=None, timeout=60):
            if list(argv[:2]) == ["docker", "create"]:
                hold.deadline_epoch -= seconds
            return real(argv, env, timeout)

        self.docker.run = run

    def test_the_handoff_wait_is_measured_after_the_create(self):
        # The handoff has NO deadline watcher, so an over-granted wait is not
        # caught by anything else: it simply runs past the hold.
        run_id, hold, paths = self.ready(deadline=time.time() + 10)
        seen = {}

        class Watching:
            returncode = 0

            def wait(inner, timeout=None):
                seen["timeout"] = timeout
                seen["remaining"] = hold.remaining()
                return 0

        self.spend_during_create(hold, 3)
        self.runner.spawn = lambda argv, **kw: Watching()
        self.runner._handoff(hold, paths)
        self.assertIn("timeout", seen, "the handoff was never waited on")
        self.assertLessEqual(
            seen["timeout"], seen["remaining"] + 0.05,
            "the handoff was granted more time than the hold had left")

    def test_a_handoff_whose_budget_went_during_the_create_comes_down(self):
        # Under a second left: not expired, so the create is allowed and the
        # container comes up -- but there is no whole second to grant it, so
        # there is nothing to refuse, only something to stop. (Spend PAST the
        # deadline instead and the create/start boundary check refuses first;
        # that is the test above.)
        run_id, hold, paths = self.ready(deadline=time.time() + 5)
        self.spend_during_create(hold, 4.2)
        self.assertEqual(self.runner._handoff(hold, paths), "handoff_timeout")
        self.assertEqual(self.docker.live, set(),
                         "the handoff was left running past the hold deadline")

    def test_a_create_that_lands_on_the_deadline_starts_nothing(self):
        # The create is bounded by what is left of the hold, so it can return
        # exactly ON the deadline. The container exists at that point, so the
        # row is RETAINED -- confirmation is the only thing allowed to account
        # for something that exists.
        run_id, hold, paths = self.ready(deadline=time.time() + 5)
        self.spend_during_create(hold, 6)
        cid = f"qf-{run_id}-candidate"
        self.runner._record_container(hold, run_id, "candidate")
        with self.assertRaises(qfd.DeadlineExpired):
            self.runner._create_then_start(
                hold, "candidate", ["docker", "create", "--name", cid, "img"])
        self.assertEqual(self.spawned, [],
                         "a container was started after the deadline passed")
        self.assertIn(cid, self.docker.created,
                      "the test proves nothing unless the create succeeded")
        self.assertEqual([r["role"] for r
                          in self.db.call("resources_for", run_id,
                                          unreleased_only=True)],
                         ["candidate"],
                         "a container that EXISTS had its row released")

    def test_the_candidate_wait_is_measured_after_the_create(self):
        # Driven through the whole path, since the candidate's budget is
        # computed two frames further out.
        self.submit()
        seen = {}
        real_launch = self.runner._run_candidate

        def launch(hold, effective, paths, argv, out_w, err_w, budget):
            # Enough that the HOLD becomes the binding constraint rather than
            # the spec's own timeout_s, which is what the argument carries.
            self.spend_during_create(hold, 7000)
            seen["passed_in"] = budget

            def spawn(a, **kw):
                self.spawned.append(list(a))
                return types.SimpleNamespace(
                    returncode=0, stdout=io.BytesIO(b""),
                    stderr=io.BytesIO(b""),
                    wait=lambda timeout=None: seen.__setitem__(
                        "waited", timeout) or 0)

            self.runner.spawn = spawn
            return real_launch(hold, effective, paths, argv, out_w, err_w,
                               budget)

        self.runner._run_candidate = launch
        self.runner.try_one("light")
        self.assertIn("waited", seen, "the candidate was never waited on")
        self.assertLess(seen["waited"], seen["passed_in"],
                        "the candidate kept a budget measured before the create")


class TestRound7AmbiguousCreatesMustSettle(Round6Base):
    """Retaining the row was necessary and not sufficient: `confirm_all_stopped`
    converted the FIRST absence into a release, which is the same mistake one
    layer down. Stop, kill and remove can all run before a delayed daemon-side
    create binds the name; the first inspect then says "No such object", the row
    and the mutex go, and the container appears afterwards.

    KILL_CONFIRM_S is a maximum polling period, not a requirement that absence
    be STABLE. So an unacknowledged create persists a settle instant, and every
    path that would release on an inspection consults it.
    """

    SETTLE_S = 60

    def setUp(self):
        super().setUp()
        # Long enough that a confirmation pass cannot wait it out: KILL_CONFIRM_S
        # is 1s in this fixture, so an unsettled absence must come back False.
        self.cfg.build_settle_s = self.SETTLE_S

    def ambiguous(self):
        """A run whose handoff create was issued and never acknowledged."""
        run_id, hold, paths = self.ready()
        self.docker.create_rc = 1
        with self.assertRaises(qfd.StartFailed):
            self.runner._handoff(hold, paths)
        return run_id, hold, paths

    def unreleased(self, run_id):
        return [r["role"] for r in self.db.call("resources_for", run_id,
                                                unreleased_only=True)]

    def test_the_first_absence_does_not_confirm_an_unacknowledged_create(self):
        # The reviewer's trace: retained_before=True, confirmation_returned=True,
        # retained_after=False, container_exists_after=True.
        run_id, hold, paths = self.ambiguous()
        self.assertEqual(self.unreleased(run_id), ["handoff"])
        self.assertFalse(self.runner.confirm_all_stopped(hold),
                         "an absence one probe old was taken as proof")
        self.assertEqual(self.unreleased(run_id), ["handoff"],
                         "the row was released on an unsettled absence")

    def test_a_create_that_lands_during_the_window_is_removed_not_released(self):
        # The daemon finishes the request it was sent, after the client died.
        run_id, hold, paths = self.ambiguous()
        cid = f"qf-{run_id}-handoff"
        probes = []
        real = self.docker.is_running

        def is_running(c, timeout=15):
            probes.append(c)
            if len(probes) == 2:
                # ...and here it lands, after two negative probes.
                self.docker.created.add(cid)
            return real(c, timeout)

        self.docker.is_running = is_running
        self.assertFalse(self.runner.confirm_all_stopped(hold))
        self.assertGreater(len(probes), 2, "the window did not keep probing")
        self.assertNotIn(cid, self.docker.created,
                         "the delayed container was left behind")
        self.assertEqual(self.unreleased(run_id), ["handoff"])

    def test_seeing_the_container_re_arms_the_settle_window(self):
        # What is being tested is STABILITY, not a lucky sample, and this is the
        # mechanism. Starting from an ALREADY-ELAPSED window (so an absence
        # would be believed right now), one sighting must put it back.
        run_id, hold, paths = self.ambiguous()
        key = store.absence_settles_pin("handoff")
        cid = f"qf-{run_id}-handoff"
        self.db.call("set_pin", run_id, key, qfd.iso_at(time.time() - 1),
                     now=qfd.utcnow())
        self.assertTrue(store.absence_believable(
            self.db.call("pins_for", run_id), "handoff", qfd.utcnow()))

        self.docker.created.add(cid)                    # the create lands...
        self.assertFalse(self.runner._account_for(run_id, "handoff", cid))
        self.assertNotIn(cid, self.docker.created,
                         "the sighted container was not removed")
        self.assertFalse(store.absence_believable(
            self.db.call("pins_for", run_id), "handoff", qfd.utcnow()),
            "a sighting did not re-arm the stability window")

    def test_a_settled_absence_does_release(self):
        # Not a permanent block: the window ends, and then the ordinary rule
        # applies. A stall is not fail-closed, it is just failed.
        run_id, hold, paths = self.ambiguous()
        self.db.call("set_pin", run_id, store.absence_settles_pin("handoff"),
                     qfd.iso_at(time.time() - 1), now=qfd.utcnow())
        self.assertTrue(self.runner.confirm_all_stopped(hold))
        self.assertEqual(self.unreleased(run_id), [])

    def test_an_acknowledged_create_confirms_on_the_first_absence(self):
        # The regression guard that matters for every ordinary run: a create
        # that returned 0 leaves nothing outstanding, so its absence is proof
        # and no job pays the settle window for the normal path.
        run_id, hold, paths = self.ready()
        self.runner._handoff(hold, paths)
        self.assertFalse(store.create_unacked(
            self.db.call("pins_for", run_id), "handoff"),
            "an acknowledged create left its ambiguity pinned")
        started = time.time()
        self.assertTrue(self.runner.confirm_all_stopped(hold))
        self.assertLess(time.time() - started, self.SETTLE_S / 2)
        self.assertEqual(self.unreleased(run_id), [])

    def test_the_ambiguity_is_pinned_before_the_create_is_issued(self):
        # Ordering, for the same reason the resource row is written before the
        # container can exist: a crash in the middle of a create must leave the
        # cautious state recorded, not the confident one.
        run_id, hold, paths = self.ready()
        seen = {}
        real = self.docker.run

        def run(argv, env=None, timeout=60):
            if list(argv[:2]) == ["docker", "create"]:
                seen["pinned"] = store.create_unacked(
                    self.db.call("pins_for", run_id), "handoff")
            return real(argv, env, timeout)

        self.docker.run = run
        self.runner._handoff(hold, paths)
        self.assertTrue(seen.get("pinned"),
                        "the create was issued before its ambiguity was"
                        " recorded, so a crash would lose it")

    def test_the_settle_survives_a_restart(self):
        # The pin is the provenance, and it is in the event chain rather than in
        # the runner's memory: recovery must not inherit a confident view.
        run_id, hold, paths = self.ambiguous()
        fresh = qfd.Runner(self.cfg, self.db, self.disp, docker=self.docker,
                           src=self.src)
        fresh.poll_interval_s = 0.02
        self.assertFalse(fresh.confirm_run_stopped(run_id),
                         "a new process read an unsettled absence as proof")
        self.assertEqual(self.unreleased(run_id), ["handoff"])


class TestRound8NobodyDrivesARecoveredHold(Round6Base):
    """A crash between `docker create` and `docker start` used to strand the job
    for good.

    The container exists (status `created`, which the probe rightly calls live),
    the ambiguity pin has been cleared because the create WAS acknowledged, and
    recovery handed back a hold -- which nothing then drove. The lease lapsed,
    `reclaim` found a live container and renewed it, and every later sweep did
    the same: mutex, lane and reservation held for ever, admissions shut, the
    nightly run waiting out LOCK_WAIT_S every night. The persisted hold deadline
    was never consulted again after startup.
    """

    def crashed_between_create_and_start(self):
        """The exact window: created, acknowledged, never started, no process."""
        run_id = self.submit()
        self.db.call("dequeue", "light", owner="qfd", now=qfd.utcnow(),
                     lease_expires_at="2026-01-01T00:00:00Z",   # lapsed
                     hold_deadline_at="2036-01-01T00:00:00Z", max_running=2)
        self.db.call("transition", run_id, "RUNNING", now=qfd.utcnow())
        cid = f"qf-{run_id}-candidate"
        self.db.call("add_resource", run_id, role="candidate",
                     container_id=cid, now=qfd.utcnow())
        self.docker.created.add(cid)       # created, never started
        return run_id, cid

    def test_recovery_cleans_up_a_container_that_was_created_but_not_started(self):
        run_id, cid = self.crashed_between_create_and_start()
        self.assertIs(self.docker.is_running(cid), True,
                      "a created container must read as live, or this test is"
                      " asserting the wrong thing")

        holds = qfd.Recovery(self.cfg, self.db, self.runner,
                             self.docker).reconstruct()

        self.assertEqual(holds, [], "an undriven hold was handed back")
        job = self.db.call("get", run_id)
        self.assertEqual(job["state"], "FAILED", job)
        self.assertEqual(job["error_class"], "reclaimed_at_startup")
        self.assertNotIn(cid, self.docker.created,
                         "the created container was left behind")
        self.assertEqual(self.db.call("resources_for", run_id,
                                      unreleased_only=True), [])
        qfd.TrainingLock(self.cfg.lock_file, "heavy").acquire().release()

    def test_an_unacknowledged_create_that_landed_is_cleaned_up_too(self):
        # The other ordering the reviewer named: the delayed create completes
        # before recovery runs, so a POSITIVE probe would have taken the
        # adoption branch before the settle pin could matter. It no longer
        # exists -- and with the settle window still open the outcome is
        # fail-CLOSED rather than adopted: the container is destroyed, the job
        # holds everything as CLEANUP_BLOCKED, and it terminates when the window
        # elapses. What it never does is get renewed.
        run_id, cid = self.crashed_between_create_and_start()
        key = store.absence_settles_pin("candidate")
        self.db.call("set_pin", run_id, key, qfd.iso_at(time.time() + 600),
                     now=qfd.utcnow())

        holds = qfd.Recovery(self.cfg, self.db, self.runner,
                             self.docker).reconstruct()

        self.assertNotIn(cid, self.docker.created,
                         "the landed container was left running")
        self.assertEqual(self.db.call("get", run_id)["state"],
                         "CLEANUP_BLOCKED")
        self.assertEqual(len(holds), 1,
                         "the hold must come back while something still has to"
                         " keep asking")
        for h in holds:
            self.disp.register_hold(h)
            self.addCleanup(lambda hold=h: hold.lock.held
                            and hold.lock.release())

        # The window elapses, and the reaper finishes it without an operator.
        self.db.call("set_pin", run_id, key, qfd.iso_at(time.time() - 1),
                     now=qfd.utcnow())
        qfd.Reaper(self.cfg, self.db, self.runner, self.disp,
                   self.docker).sweep()
        self.assertEqual(self.db.call("get", run_id)["state"], "FAILED")
        self.assertEqual(self.db.call("resources_for", run_id,
                                      unreleased_only=True), [])

    def test_a_live_container_past_its_deadline_is_not_renewed_for_ever(self):
        # The general rule, driven through the reaper: a lapsed lease means
        # nothing is driving the run, and the PERSISTED hold deadline is what
        # says when to stop asking. Two sweeps, and the second must not be
        # another adoption.
        run_id = self.submit()
        self.db.call("dequeue", "light", owner="qfd", now=qfd.utcnow(),
                     lease_expires_at="2026-01-01T00:00:00Z",
                     hold_deadline_at="2026-01-02T00:00:00Z", max_running=2)
        self.db.call("transition", run_id, "RUNNING", now=qfd.utcnow())
        cid = f"qf-{run_id}-candidate"
        self.db.call("add_resource", run_id, role="candidate",
                     container_id=cid, now=qfd.utcnow())
        self.docker.created.add(cid)

        reaper = qfd.Reaper(self.cfg, self.db, self.runner, self.disp,
                            self.docker)
        reaper.sweep()
        job = self.db.call("get", run_id)
        self.assertIn(job["state"], ("CLEANUP_BLOCKED", "FAILED"),
                      f"the run was adopted past its hold deadline: {job}")
        self.assertNotIn(cid, self.docker.created,
                         "the container outlived its hold deadline")
        # And it reaches a terminal state rather than being asked about for ever.
        reaper.sweep()
        self.assertEqual(self.db.call("get", run_id)["state"], "FAILED")
        self.assertEqual(self.db.call("resources_for", run_id,
                                      unreleased_only=True), [])


class TestRound9NoStallWithoutAnEscape(Round6Base):
    """Past the hold deadline, an UNRESPONSIVE Docker used to be a stall with no
    way out at all: `reclaim` returned "unconfirmed" and left the job RUNNING, so
    `resolve_blocked` -- which lists CLEANUP_BLOCKED only -- never looked at it,
    and `force-release` refused it for not being CLEANUP_BLOCKED. Lock, lane and
    reservation held indefinitely, with neither the automatic path nor the
    operator able to act."""

    def orphan_past_its_deadline(self, *, answer):
        run_id = self.submit()
        self.db.call("dequeue", "light", owner="qfd", now=qfd.utcnow(),
                     lease_expires_at="2026-01-01T00:00:00Z",   # lapsed
                     hold_deadline_at="2026-01-02T00:00:00Z", max_running=2)
        self.db.call("transition", run_id, "RUNNING", now=qfd.utcnow())
        cid = f"qf-{run_id}-candidate"
        self.db.call("add_resource", run_id, role="candidate",
                     container_id=cid, now=qfd.utcnow())
        lock = qfd.TrainingLock(self.cfg.lock_file, "light").acquire()
        self.addCleanup(lambda: lock.held and lock.release())
        hold = qfd.Hold(self.db.call("get", run_id), lock,
                        qfd.parse_iso("2026-01-02T00:00:00Z"))
        self.disp.register_hold(hold)
        self.docker.is_running = lambda c, timeout=15: answer
        return run_id, hold, cid

    def test_an_unresponsive_daemon_past_the_deadline_reaches_cleanup_blocked(self):
        run_id, hold, cid = self.orphan_past_its_deadline(answer=None)
        reaper = qfd.Reaper(self.cfg, self.db, self.runner, self.disp,
                            self.docker)

        reaper.sweep()
        job = self.db.call("get", run_id)
        self.assertEqual(job["state"], "CLEANUP_BLOCKED",
                         f"left where nothing can act on it: {job}")
        self.assertEqual(job["error_class"], "hold_deadline_expired")
        # Fail-closed throughout: nothing was released on no evidence.
        self.assertTrue(hold.lock.held)
        self.assertEqual([r["role"] for r in self.db.call(
            "resources_for", run_id, unreleased_only=True)], ["candidate"])

        # A second sweep must not undo it, and must keep trying rather than
        # settling into a state nobody revisits.
        reaper.sweep()
        self.assertEqual(self.db.call("get", run_id)["state"],
                         "CLEANUP_BLOCKED")

    def test_and_the_operator_escape_is_then_reachable(self):
        # The point of CLEANUP_BLOCKED rather than RUNNING. Two passes, because
        # Docker will not answer: the first freezes the inventory, the second
        # releases.
        run_id, hold, cid = self.orphan_past_its_deadline(answer=None)
        qfd.Reaper(self.cfg, self.db, self.runner, self.disp,
                   self.docker).sweep()

        first = self.disp.handle("force-release",
                                 {"run_id": run_id,
                                  qfd.FORCE_RELEASE_FLAG: True}, 0, admin=True)
        self.assertFalse(first["ok"], first)
        self.assertNotIn("not CLEANUP_BLOCKED", first["error"])
        second = self.disp.handle("force-release",
                                  {"run_id": run_id,
                                   qfd.FORCE_RELEASE_FLAG: True}, 0, admin=True)
        self.assertTrue(second["ok"], second)
        self.assertTrue(second["descriptor_closed"], second)
        self.assertFalse(hold.lock.held)

    def test_a_responsive_daemon_past_the_deadline_needs_no_operator(self):
        # The same route, but Docker answers: the reaper kills, confirms and
        # finishes it inside the same sweep. The escape hatch is for when this
        # cannot happen, not for every deadline.
        run_id, hold, cid = self.orphan_past_its_deadline(answer=False)
        qfd.Reaper(self.cfg, self.db, self.runner, self.disp,
                   self.docker).sweep()
        self.assertEqual(self.db.call("get", run_id)["state"], "FAILED")
        self.assertEqual(self.db.call("resources_for", run_id,
                                      unreleased_only=True), [])
        self.assertFalse(hold.lock.held, "the descriptor was not closed")


class TestRound10NoRecordAfterTheRunIsOver(Round6Base):
    """`reclaim` settling a run while that run's NEXT phase already holds its
    gate.

    The inventory can be legitimately EMPTY in that moment -- the candidate has
    exited and `--rm` took its container -- so a lapsed lease made `reclaim`
    release the rows and transition FAILED. The gated phase then recorded its own
    container and started it. `release_hold` correctly vetoed the descriptor, but
    the damage was already done: the reservation and the lane were freed by the
    terminal transition, and a terminal job is invisible to `expired`
    (lease-active states only) and to `resolve_blocked` (CLEANUP_BLOCKED only),
    so nothing would ever look at that container again.

    Every mutation is serialised through the DB-owner thread, so the two cannot
    interleave inside a statement -- but they can arrive in this order, which is
    what these tests force.
    """

    def ready_to_hand_off(self):
        """A RUNNING job, mid-run, whose candidate container is already gone and
        whose lease has lapsed: exactly the window."""
        run_id, hold, paths = self.ready()
        self.db.call("add_resource", run_id, role="candidate",
                     container_id=f"qf-{run_id}-candidate", now=qfd.utcnow())
        self.db.call("renew", run_id, owner="qfd",
                     lease_expires_at="2026-01-01T00:00:00Z", now=qfd.utcnow())
        return run_id, hold, paths

    def reclaim_now(self):
        # A settled absence over the whole inventory: the branch that releases
        # the rows and transitions FAILED.
        return self.db.call("reclaim", qfd.utcnow(), probe=lambda c: False,
                            owner="qfd", lease_expires_at=qfd.iso_at(
                                time.time() + 300))

    def test_a_phase_that_lost_its_run_records_nothing_and_starts_nothing(self):
        run_id, hold, paths = self.ready_to_hand_off()
        seen = {}
        real = self.runner._record_container

        def record(h, rid, role):
            # Inside the gate, immediately BEFORE the insert: the reviewer's
            # ordering, forced rather than waited for.
            seen["decided"] = self.reclaim_now()
            return real(h, rid, role)

        self.runner._record_container = record
        with self.assertRaises(qfd.Revoked):
            self.runner._handoff(hold, paths)

        self.assertEqual(seen["decided"], [(run_id, "reclaimed")],
                         "the test did not reproduce the settled-absence branch")
        self.assertEqual(self.db.call("get", run_id)["state"], "FAILED")
        self.assertEqual([r["role"] for r in self.db.call("resources_for",
                                                          run_id)],
                         ["candidate"],
                         "a container was recorded for a run that was over")
        self.assertEqual(self.spawned, [], "a container was started")
        self.assertEqual(self.docker.created, set())
        self.assertEqual(self.docker.live, set())

    def test_and_the_descriptor_is_then_released_rather_than_leaked(self):
        # The consequence that made this a P1: with nothing recorded after the
        # terminal transition, the veto in `release_hold` has nothing to veto on,
        # so the mutex goes back instead of leaking until restart.
        run_id, hold, paths = self.ready_to_hand_off()
        real = self.runner._record_container

        def record(h, rid, role):
            self.reclaim_now()
            return real(h, rid, role)

        self.runner._record_container = record
        with self.assertRaises(qfd.Revoked):
            self.runner._handoff(hold, paths)

        reaper = qfd.Reaper(self.cfg, self.db, self.runner, self.disp,
                            self.docker)
        self.assertTrue(reaper.release_hold(run_id, "reclaimed"))
        self.assertFalse(hold.lock.held)
        self.assertIsNone(self.disp.get_hold(run_id))
        ok, reason = self.disp.may_admit()
        self.assertTrue(ok, reason)

    def test_cleanup_having_begun_also_refuses_a_record(self):
        # `not terminal` was the wrong test. CLEANUP_BLOCKED is not terminal, and
        # it is exactly a state where no new workload may appear: the reaper's
        # `resolve_blocked` is already confirming this run's absence, so a row
        # recorded now is found with its name not yet bound, released as gone,
        # and the job finished -- and the phase then creates and starts the
        # container with nothing left to veto on.
        run_id, hold, paths = self.ready()
        seen = {}
        real = self.runner._record_container

        def record(h, rid, role):
            # Inside the gate: the reaper moves the run under the phase's feet.
            # An EMPTY inventory is what sends a RUNNING job here, and empty is
            # exactly what it is until this record lands.
            seen["decided"] = self.db.call(
                "reclaim", qfd.utcnow(), probe=lambda c: False, owner="qfd",
                lease_expires_at=qfd.iso_at(time.time() + 300))
            return real(h, rid, role)

        self.db.call("renew", run_id, owner="qfd",
                     lease_expires_at="2026-01-01T00:00:00Z", now=qfd.utcnow())
        self.runner._record_container = record
        with self.assertRaises(qfd.Revoked):
            self.runner._handoff(hold, paths)

        self.assertEqual(seen["decided"], [(run_id, "cleanup_blocked")],
                         "the test did not reproduce the empty-inventory branch")
        self.assertEqual(self.db.call("resources_for", run_id), [],
                         "a container was recorded after cleanup began")
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.docker.created, set())
        self.assertEqual(self.docker.live, set())

    def test_the_ambiguity_covers_the_record_not_just_the_create(self):
        # The same window without any state change: a row exists, its container
        # does not exist YET, and a confirmation pass holds no guard. Reading
        # that absence as proof releases the row while the gated phase goes on to
        # create and start the container.
        run_id, hold, paths = self.ready()
        self.cfg.build_settle_s = 60
        seen = {}
        real = self.runner._record_container

        def record(h, rid, role):
            cid = real(h, rid, role)
            # Recorded, not yet created: exactly the gap.
            seen["confirmed"] = self.runner.confirm_run_stopped(rid)
            seen["unreleased"] = [r["role"] for r in self.db.call(
                "resources_for", rid, unreleased_only=True)]
            return cid

        self.runner._record_container = record
        self.runner._handoff(hold, paths)
        self.assertIs(seen["confirmed"], False,
                      "a recorded container that does not exist yet was"
                      " confirmed stopped")
        self.assertEqual(seen["unreleased"], ["handoff"],
                         "its row was released before it could be created")

    def test_a_name_docker_has_not_been_asked_about_never_settles(self):
        # The reviewer's trace: the phase stalls between the record and the
        # create for longer than BUILD_SETTLE_S, the lease lapses, and `reclaim`
        # calls the still-uncreated name a settled absence -- then the phase
        # resumes and starts the container over a released row. Time is the wrong
        # measure here: Docker has not been asked yet, so no amount of elapsed
        # time makes the absence mean anything.
        run_id, hold, paths = self.ready()
        self.cfg.build_settle_s = 0          # any window, however short
        self.db.call("renew", run_id, owner="qfd",
                     lease_expires_at="2026-01-01T00:00:00Z", now=qfd.utcnow())
        seen = {}
        real = self.runner._record_container

        def record(h, rid, role):
            cid = real(h, rid, role)
            # THE STALL, made deterministic: the pin was written, the row
            # exists, the create has not been issued, and the reaper arrives.
            seen["pin"] = self.db.call("pins_for", rid)[
                store.absence_settles_pin(role)]
            seen["decided"] = self.db.call(
                "reclaim", qfd.utcnow(), probe=lambda c: False, owner="qfd",
                lease_expires_at=qfd.iso_at(time.time() + 300))
            seen["confirmed"] = self.runner.confirm_run_stopped(rid)
            return cid

        self.runner._record_container = record
        self.runner._handoff(hold, paths)

        self.assertEqual(seen["pin"], store.ABSENCE_NOT_YET_ISSUED)
        self.assertEqual(seen["decided"], [(run_id, "unconfirmed")],
                         "an uncreated name was called a settled absence")
        self.assertIs(seen["confirmed"], False)
        self.assertNotEqual(self.db.call("get", run_id)["state"], "FAILED")
        # The run completed normally, so the row is released by the ordinary
        # path rather than by a sweep guessing.
        self.assertEqual(self.handoff_spawns() and True, True,
                         "the handoff never started")

    def test_only_the_answer_takes_the_sentinel_down(self):
        # Round 14. The clock does not start when the request is ISSUED either:
        # "immediately before the request" is still before it, and a thread
        # descheduled between the two statements leaves an instant expiring while
        # nothing has been asked -- the same defect in a narrower window. Only an
        # answer is a fact, so only an answer moves the pin.
        run_id, hold, paths = self.ready()
        self.cfg.build_settle_s = 60
        seen = {}
        real = self.docker.run

        def run(argv, env=None, timeout=60):
            if list(argv[:2]) == ["docker", "create"]:
                seen["pin_at_issue"] = self.db.call("pins_for", run_id)[
                    store.absence_settles_pin("handoff")]
            return real(argv, env, timeout)

        real_record = self.runner._record_container

        def record(h, rid, role):
            cid = real_record(h, rid, role)
            seen["pin_at_record"] = self.db.call("pins_for", rid)[
                store.absence_settles_pin(role)]
            return cid

        self.docker.run = run
        self.runner._record_container = record
        self.runner._handoff(hold, paths)
        self.assertEqual(seen["pin_at_record"], store.ABSENCE_NOT_YET_ISSUED)
        self.assertEqual(seen["pin_at_issue"], store.ABSENCE_NOT_YET_ISSUED,
                         "the sentinel became an expiring instant while the"
                         " request had not been answered, so a stall between"
                         " the two could settle an absence that means nothing")
        # Acknowledged, so it comes down: absence means removed from here on.
        self.assertFalse(store.create_unacked(
            self.db.call("pins_for", run_id), "handoff"))

    def test_an_ambiguous_answer_is_what_starts_the_clock(self):
        # ...and the window is not abolished, only moved to where it means
        # something: the request WAS issued, so the daemon may complete it late.
        run_id, hold, paths = self.ready()
        self.cfg.build_settle_s = 60
        self.docker.create_rc = 1
        with self.assertRaises(qfd.StartFailed):
            self.runner._handoff(hold, paths)
        pin = self.db.call("pins_for", run_id)[
            store.absence_settles_pin("handoff")]
        self.assertNotEqual(pin, store.ABSENCE_NOT_YET_ISSUED,
                            "an answered create left the clock unstarted, so"
                            " nothing could ever settle it")
        self.assertGreater(pin, qfd.utcnow(),
                           "the settle instant was not in the future")
        self.assertTrue(self.db.call("resources_for", run_id,
                                     unreleased_only=True),
                        "the row was released on an unproven refusal")

    def test_a_create_that_never_answered_also_starts_the_clock(self):
        run_id, hold, paths = self.ready()
        self.cfg.build_settle_s = 60
        self.docker.create_raises = subprocess.TimeoutExpired("docker", 1)
        with self.assertRaises(qfd.StartUnconfirmed):
            self.runner._handoff(hold, paths)
        pin = self.db.call("pins_for", run_id)[
            store.absence_settles_pin("handoff")]
        self.assertNotEqual(pin, store.ABSENCE_NOT_YET_ISSUED)
        self.assertGreater(pin, qfd.utcnow())


    def test_the_refusal_names_the_run_and_the_role(self):
        # It reaches the log and the operator, so it has to say what happened.
        run_id = self.submit()
        self.db.call("dequeue", "light", owner="qfd", now=qfd.utcnow(),
                     lease_expires_at="2036-01-01T00:00:00Z",
                     hold_deadline_at="2036-01-01T00:00:00Z", max_running=2)
        self.db.call("transition", run_id, "CANCELLED", now=qfd.utcnow(),
                     fields={"error_class": "cancelled"})
        with self.assertRaises(store.WorkNotPermitted) as caught:
            self.db.call("add_resource", run_id, role="handoff",
                         container_id="c9", now=qfd.utcnow())
        self.assertIn(run_id, str(caught.exception))
        self.assertIn("handoff", str(caught.exception))
        self.assertIn("CANCELLED", str(caught.exception))
        # And the chain is unharmed: a refused mutation writes no event.
        ok, problems = self.db.call("verify_chain")
        self.assertTrue(ok, problems)
        self.assertEqual(self.db.call("resources_for", run_id), [])



class TestRound14ARestartCanSettleWhatNoPhaseCan(Round6Base):
    """A sentinel outlives the phase that could have honoured it.

    `ABSENCE_NOT_YET_ISSUED` means "a phase holds the gate and has not asked
    Docker yet", and it is deliberately immune to elapsed time. Correct while
    that phase exists -- and a permanent stall the moment the process dies with
    the pin still up: every confirmation path refuses the absence for ever, the
    job sits in CLEANUP_BLOCKED, and the lock, the lane and the reservation stay
    held until an operator runs `force-release`.

    A restart is the one moment when "no phase can issue this create" becomes
    true, so a restart is where the pin is allowed to change meaning.
    """

    def crashed_mid_record(self):
        """A RUNNING job with a row and a sentinel and no phase: what a crash
        between `_record_container` and `docker create` leaves behind."""
        run_id, hold, paths = self.ready()
        self.runner._record_container(hold, run_id, "candidate")
        self.disp.holds.pop(run_id, None)
        hold.lock.release()
        return run_id

    def recover(self):
        return qfd.Recovery(self.cfg, self.db, self.runner,
                            self.docker).reconstruct()

    def test_a_surviving_sentinel_becomes_a_bounded_window(self):
        run_id = self.crashed_mid_record()
        self.cfg.build_settle_s = 60
        holds = self.recover()
        for h in holds:
            self.addCleanup(h.lock.release)
        pin = self.db.call("pins_for", run_id)[
            store.absence_settles_pin("candidate")]
        self.assertNotEqual(pin, store.ABSENCE_NOT_YET_ISSUED,
                            "the sentinel survived a restart, so no absence can"
                            " ever be believed and cleanup can never confirm")
        self.assertGreater(pin, qfd.utcnow(),
                           "the window has to start at the restart: the time"
                           " qfd spent down is not time anything was watching")

    def test_the_stall_is_gone_end_to_end(self):
        # The reviewer's trace: restart, then repeated reaper passes. Nothing was
        # ever created, so once the window elapses the removal-and-probe loop
        # confirms and everything comes back.
        run_id = self.crashed_mid_record()
        self.cfg.build_settle_s = 0
        holds = self.recover()
        for h in holds:
            self.addCleanup(h.lock.release)
        for _ in range(3):
            for h in holds:
                self.disp.register_hold(h)
            qfd.Reaper(self.cfg, self.db, self.runner, self.disp,
                       self.docker).sweep()
        job = self.db.call("get", run_id)
        self.assertEqual(job["state"], "FAILED", job["error_class"])
        self.assertFalse(self.db.call("resources_for", run_id,
                                      unreleased_only=True))
        self.assertEqual(self.db.call("admitted_mem_mb"), 0,
                         "the reservation is still charged")

    def test_a_restart_does_not_free_a_row_it_cannot_account_for(self):
        # The conversion must not become a release. The daemon may have bound the
        # name just before the crash, and a restart cannot tell that apart from
        # never having asked -- so the ambiguity is KEPT, in the bounded form.
        run_id = self.crashed_mid_record()
        self.cfg.build_settle_s = 0
        self.docker.created.add(f"qf-{run_id}-candidate")
        holds = self.recover()
        for h in holds:
            self.addCleanup(h.lock.release)
        self.assertTrue(any(a[:3] == ["docker", "rm", "-f"]
                            for a in self.docker.calls),
                        "a container left over from the crash was never removed")

class TestRound15AnAbandonedCreateHasAnOwner(Round6Base):
    """A sentinel outlives the PHASE, not just the process.

    The pin is immune to elapsed time because the phase holding the gate is
    still going to ask -- which makes that phase its owner. A phase that walks
    away without either an acknowledgement or a conversion leaves a pin nothing
    can resolve while the daemon lives: `Recovery._settle_unissued` only runs at
    startup, so the job parks in CLEANUP_BLOCKED with the lock, the lane and the
    reservation held.

    `subprocess.run` raising `OSError` (a fork or exec that never reached the
    daemon) is the reported instance. The fix is a finalizer rather than a longer
    list of `except` clauses, so these tests use exceptions the code has never
    heard of on purpose.
    """

    def pin(self, run_id, role="handoff"):
        return self.db.call("pins_for", run_id).get(
            store.absence_settles_pin(role))

    def test_a_create_that_could_not_be_launched_still_settles(self):
        run_id, hold, paths = self.ready()
        self.cfg.build_settle_s = 60
        self.docker.create_raises = OSError(errno.EAGAIN,
                                            "fork: retry limit reached")
        with self.assertRaises(OSError):
            self.runner._handoff(hold, paths)
        pin = self.pin(run_id)
        self.assertNotEqual(pin, store.ABSENCE_NOT_YET_ISSUED,
                            "the phase abandoned the create and left a sentinel"
                            " no live path can ever resolve")
        self.assertGreater(pin, qfd.utcnow())
        self.assertTrue(self.db.call("resources_for", run_id,
                                     unreleased_only=True),
                        "the row was released without confirmation")

    def test_the_finalizer_is_not_a_list_of_known_exceptions(self):
        # The next exception this code learns about has not been written yet.
        class NeverSeenBefore(Exception):
            pass

        run_id, hold, paths = self.ready()
        self.cfg.build_settle_s = 60
        self.docker.create_raises = NeverSeenBefore("something new")
        with self.assertRaises(NeverSeenBefore):
            self.runner._handoff(hold, paths)
        self.assertNotEqual(self.pin(run_id), store.ABSENCE_NOT_YET_ISSUED)

    def test_it_covers_the_record_and_not_only_the_create_call(self):
        # The sentinel goes up at the RECORD, so the span that must not be left
        # is the whole record-to-answer one -- not just the create call.
        run_id, hold, paths = self.ready()
        self.cfg.build_settle_s = 60

        def boom(*a, **kw):
            raise RuntimeError("between the record and the request")

        self.runner._create_then_start = boom
        with self.assertRaises(RuntimeError):
            self.runner._handoff(hold, paths)
        self.assertNotEqual(self.pin(run_id), store.ABSENCE_NOT_YET_ISSUED,
                            "nothing was asked of Docker and nothing can ask"
                            " now, so the pin needs an owner here too")

    def test_the_stall_is_gone_end_to_end_without_a_restart(self):
        # The reviewer's trace: repeated reaper passes, same process. No restart
        # is allowed to be part of the remedy.
        run_id, hold, paths = self.ready()
        self.cfg.build_settle_s = 0
        self.docker.create_raises = OSError(errno.EAGAIN, "cannot fork")
        with self.assertRaises(OSError):
            self.runner._handoff(hold, paths)
        self.runner.finish(hold, "FAILED", {"error_class": "start_unconfirmed",
                                            "finished_at": qfd.utcnow()})
        for _ in range(3):
            qfd.Reaper(self.cfg, self.db, self.runner, self.disp,
                       self.docker).sweep()
        job = self.db.call("get", run_id)
        self.assertEqual(job["state"], "FAILED", job["error_class"])
        self.assertFalse(self.db.call("resources_for", run_id,
                                      unreleased_only=True))
        self.assertEqual(self.db.call("admitted_mem_mb"), 0,
                         "the reservation is still charged")

    def test_an_acknowledged_create_is_left_cleared(self):
        # Idempotence in the direction that matters: the finalizer must not
        # resurrect a pin the answer took down.
        run_id, hold, paths = self.ready()
        self.runner._handoff(hold, paths)
        self.assertFalse(store.create_unacked(
            self.db.call("pins_for", run_id), "handoff"))

    def test_an_answered_ambiguity_keeps_its_own_instant(self):
        # ...and must not be pushed forward by the finalizer either: the window
        # started when the answer came back, and extending it on the way out
        # would delay every settlement by a second window.
        run_id, hold, paths = self.ready()
        self.cfg.build_settle_s = 300
        self.docker.create_rc = 1
        seen = {}
        real = self.runner._settle_if_unissued

        def settle(rid, role):
            seen["before"] = self.pin(rid)
            return real(rid, role)

        self.runner._settle_if_unissued = settle
        with self.assertRaises(qfd.StartFailed):
            self.runner._handoff(hold, paths)
        self.assertEqual(self.pin(run_id), seen["before"],
                         "the finalizer rewrote an instant that an answer had"
                         " already justified")

    def test_a_failing_finalizer_does_not_hide_the_real_failure(self):
        # It runs in a `finally`, usually with an exception in flight, and that
        # exception is the diagnosis. Round 16 adds the other half: the pin does
        # not simply stand for ever -- see below.
        run_id, hold, paths = self.ready()
        self.docker.create_raises = OSError(errno.EAGAIN, "cannot fork")
        self.runner.db = self.failing_db()
        with self.assertRaises(OSError):
            self.runner._handoff(hold, paths)
        self.assertEqual(self.pin(run_id), store.ABSENCE_NOT_YET_ISSUED,
                         "the pin should stand until a conversion succeeds")

    def failing_db(self, fail_times=None):
        """A db whose `settle_unissued_creates` fails -- transiently when
        `fail_times` is given, otherwise for good."""
        real = self.db.call
        state = {"n": 0}

        def call(op, *a, **kw):
            if op == "settle_unissued_creates":
                state["n"] += 1
                if fail_times is None or state["n"] <= fail_times:
                    raise sqlite3.OperationalError("database is locked")
            return real(op, *a, **kw)

        return types.SimpleNamespace(call=call)


class TestRound16AFailedConversionIsRetried(Round6Base):
    """"Swallowed" must not mean "abandoned".

    The finalizer cannot raise -- it runs with the real diagnosis in flight -- and
    the conversion it was trying to make cannot be recorded in the store, because
    the store is what just failed. Calling startup recovery the fallback was
    wrong: nothing forces a restart, so a DB failure that clears a second later
    still left the sentinel with NO owner for the life of the daemon, and the
    sentinel is immune to time. `resolve_blocked` cannot help: it confirms
    containers, and an unbelievable absence is exactly what it cannot resolve.

    The reaper is the retry owner, because it is already the thread whose job is
    asking again about what did not resolve the first time.
    """

    def pin(self, run_id, role="handoff"):
        return self.db.call("pins_for", run_id).get(
            store.absence_settles_pin(role))

    def sweep(self):
        return qfd.Reaper(self.cfg, self.db, self.runner, self.disp,
                          self.docker).sweep()

    def abandoned_with_a_sick_db(self, fail_times=None):
        """A handoff create abandoned while the store could not be written: the
        reviewer's starting state."""
        run_id, hold, paths = self.ready()
        self.docker.create_raises = OSError(errno.EAGAIN, "cannot fork")
        real, state = self.db.call, {"n": 0}

        def call(op, *a, **kw):
            if op == "settle_unissued_creates":
                state["n"] += 1
                if fail_times is None or state["n"] <= fail_times:
                    raise sqlite3.OperationalError("database is locked")
            return real(op, *a, **kw)

        self.runner.db = types.SimpleNamespace(call=call)
        with self.assertRaises(OSError):
            self.runner._handoff(hold, paths)
        self.assertEqual(self.pin(run_id), store.ABSENCE_NOT_YET_ISSUED)
        return run_id, hold

    def test_the_failure_is_remembered(self):
        run_id, _ = self.abandoned_with_a_sick_db()
        self.assertIn((run_id, "handoff"), self.runner.unsettled,
                      "the only live reference to that pin was dropped")

    def test_a_transient_failure_settles_on_the_next_sweep(self):
        # The reviewer's repro: one DB failure, then a fully recovered DB.
        run_id, hold = self.abandoned_with_a_sick_db(fail_times=1)
        self.cfg.build_settle_s = 0
        self.sweep()
        pin = self.pin(run_id)
        self.assertNotEqual(pin, store.ABSENCE_NOT_YET_ISSUED,
                            "three reaper passes changed nothing, because the"
                            " retry had no owner")
        self.assertFalse(self.runner.unsettled)

    def test_one_sweep_both_settles_and_confirms(self):
        # The retry runs BEFORE `resolve_blocked` in the same pass: while the
        # sentinel stands, every absence is unbelievable, so a confirmation pass
        # ahead of the conversion is a wasted pass.
        run_id, hold = self.abandoned_with_a_sick_db(fail_times=1)
        self.cfg.build_settle_s = 0
        self.runner.finish(hold, "FAILED", {"error_class": "start_unconfirmed",
                                            "finished_at": qfd.utcnow()})
        self.sweep()
        job = self.db.call("get", run_id)
        self.assertEqual(job["state"], "FAILED", job["error_class"])
        self.assertFalse(self.db.call("resources_for", run_id,
                                      unreleased_only=True))
        self.assertEqual(self.db.call("admitted_mem_mb"), 0,
                         "the reservation is still charged")

    def test_a_retry_that_fails_again_stays_queued(self):
        # Dropping it would be the original defect with extra steps.
        run_id, _ = self.abandoned_with_a_sick_db()
        for _ in range(3):
            self.assertEqual(self.runner.retry_unsettled(), [])
            self.assertIn((run_id, "handoff"), self.runner.unsettled)
        self.assertEqual(self.pin(run_id), store.ABSENCE_NOT_YET_ISSUED)

    def test_it_stops_retrying_once_it_has_worked(self):
        run_id, _ = self.abandoned_with_a_sick_db(fail_times=1)
        self.assertEqual(self.runner.retry_unsettled(), [(run_id, "handoff")])
        self.assertEqual(self.runner.retry_unsettled(), [],
                         "a settled pin is still being converted every sweep")

    def test_nothing_is_queued_when_the_conversion_works_first_time(self):
        run_id, hold, paths = self.ready()
        self.docker.create_raises = OSError(errno.EAGAIN, "cannot fork")
        with self.assertRaises(OSError):
            self.runner._handoff(hold, paths)
        self.assertFalse(self.runner.unsettled,
                         "a retry queued for a conversion that succeeded")
        self.assertNotEqual(self.pin(run_id), store.ABSENCE_NOT_YET_ISSUED)

    def test_another_owner_getting_there_first_counts_as_settled(self):
        # `settle_unissued_creates` reporting nothing to convert is success, not
        # a reason to keep asking: the pin is no longer a sentinel.
        run_id, _ = self.abandoned_with_a_sick_db()
        self.db.call("set_pin", run_id, store.absence_settles_pin("handoff"),
                     qfd.iso_at(time.time() + 60), now=qfd.utcnow())
        self.runner.db = self.db
        self.assertEqual(self.runner.retry_unsettled(), [(run_id, "handoff")])
        self.assertFalse(self.runner.unsettled)


class TestRound6TheHandoffChildIsManaged(Round6Base):
    """The handoff child was spawned with two pipes nobody ever read, and after
    its wait timed out the container was stopped and the client abandoned."""

    def test_the_handoff_does_not_pipe_container_output(self):
        # Nothing reads these pipes. A handoff that wrote more than one pipe
        # buffer would block on the write and be killed by its own timeout, and
        # buffering it instead would mean an unbounded stream from a container
        # whose /bin/sh comes out of the candidate's own image. The exit code is
        # the handoff's diagnostic channel, which is why it has one.
        run_id, hold, paths = self.ready()
        self.runner._handoff(hold, paths)
        self.assertEqual(self.spawn_kwargs,
                         {"stdout": subprocess.DEVNULL,
                          "stderr": subprocess.DEVNULL})

    def test_a_timed_out_handoff_client_is_reaped_after_the_stop(self):
        run_id, hold, paths = self.ready()
        order = []

        class Wedged:
            returncode = None
            killed = False

            def wait(inner, timeout=None):
                order.append("wait")
                if inner.killed:
                    return -9
                raise subprocess.TimeoutExpired("docker", timeout or 0)

            def kill(inner):
                order.append("kill")
                inner.killed = True

        self.runner.spawn = lambda argv, **kw: (self.spawned.append(list(argv))
                                                or Wedged())
        real_stop = self.runner._stop_container

        def stop(cid):
            order.append("stop")
            return real_stop(cid)

        self.runner._stop_container = stop
        self.assertEqual(self.runner._handoff(hold, paths), "handoff_timeout")
        self.assertEqual(order[:3], ["wait", "stop", "wait"],
                         f"the client was abandoned after the stop: {order}")
        self.assertIn("kill", order,
                      "a client that ignored the second wait was left running")


if __name__ == "__main__":
    unittest.main()
