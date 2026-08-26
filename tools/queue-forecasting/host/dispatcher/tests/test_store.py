# Tests for store.py. One case per row of Task 2's table; each names the
# failure it prevents. Real SQLite files throughout -- an in-memory fake would
# hide exactly the transaction and threading behaviour under test.
import os
import sqlite3
import tempfile
import threading
import unittest

import spec
import store

SHA = "3f1c" + "0" * 36


def eff(**over):
    d = {"schema": 1, "kind": "test", "source_sha": SHA}
    d.update(over)
    return spec.normalize(d)


def ts(n):
    """Ordered ISO instants. submitted_at orders the queue, so the fixture has
    to be able to say 'later' unambiguously."""
    return f"2026-08-25T10:{n // 60:02d}:{n % 60:02d}Z"


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "state.db")
        self.s = store.Store(self.path)
        self.addCleanup(self.dir.cleanup)
        self.addCleanup(self.s.close)

    def raw(self, sql, params=()):
        """A direct write that bypasses the chain -- i.e. the tamper every
        projection test is about."""
        db = sqlite3.connect(self.path, isolation_level=None)
        db.execute(sql, params)
        db.close()

    def submit(self, run_id="r1", *, at=None, uid=1000, **over):
        return self.s.submit(eff(**over), run_id=run_id, uid=uid,
                             now=at or ts(0))

    def lease(self, run_id="r1", *, lane="light", owner="qfd-1", at=None,
              max_running=1):
        return self.s.dequeue(lane, owner=owner, now=at or ts(1),
                              lease_expires_at=ts(30),
                              hold_deadline_at=ts(50), max_running=max_running)

    def assertVerifies(self):
        ok, problems = self.s.verify_chain()
        self.assertTrue(ok, f"chain should verify, got: {problems}")

    def assertBroken(self, needle):
        ok, problems = self.s.verify_chain()
        self.assertFalse(ok, "tamper went undetected")
        joined = " | ".join(problems)
        self.assertIn(needle, joined, f"problem list did not name it: {joined}")


class TestGenesisAndSubmit(StoreCase):
    def test_fresh_store_head_is_seq_zero_and_genesis(self):
        # An empty chain must be distinguishable from a verified one, or
        # "verify_chain passed" is vacuous on a brand new store.
        self.assertEqual(self.s.head(), (0, store.GENESIS))
        self.assertVerifies()

    def test_submit_writes_one_job_and_one_event_in_one_transaction(self):
        self.submit()
        self.assertEqual(len(self.s.list()), 1)
        rows = list(self.s.db.execute("SELECT kind FROM events"))
        self.assertEqual([r["kind"] for r in rows], ["SUBMITTED"])
        self.assertEqual(self.s.get("r1")["state"], "QUEUED")
        self.assertVerifies()

    def test_submit_failure_leaves_neither_row_nor_event(self):
        self.submit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.submit()            # duplicate run_id
        self.assertEqual(len(self.s.list()), 1)
        self.assertEqual(self.s.head()[0], 1)
        self.assertVerifies()


class TestChainMaterial(StoreCase):
    def test_hash_covers_seq_at_run_id_kind_and_payload(self):
        # A digest that omits any of these lets a reordering or a retargeting
        # survive verification.
        base = dict(prev_hash=store.GENESIS, seq=1, at=ts(0), run_id="r1",
                    kind="STATE", payload_json='{"a":1}')
        h = store.event_hash(**base)
        for field, other in [("seq", 2), ("at", ts(9)), ("run_id", "r2"),
                             ("kind", "PIN"), ("payload_json", '{"a":2}'),
                             ("prev_hash", "1" * 64)]:
            with self.subTest(field=field):
                self.assertNotEqual(h, store.event_hash(**{**base,
                                                           field: other}))

    def test_payload_edit_breaks_verification(self):
        self.submit()
        self.raw("UPDATE events SET payload_json='{\"fields\":{}}' WHERE seq=1")
        self.assertBroken("hash mismatch at seq 1")

    def test_reordering_breaks_verification(self):
        self.submit("r1", at=ts(0))
        self.submit("r2", at=ts(1))
        self.raw("UPDATE events SET seq=99 WHERE seq=2")
        self.assertBroken("seq gap")

    def test_jobs_row_edit_breaks_verification(self):
        # The projection drifting from the authority (design D7). A chain that
        # only checks itself would pass this.
        self.submit()
        self.raw("UPDATE jobs SET source_sha='dead' WHERE run_id='r1'")
        self.assertBroken("r1.source_sha")

    def test_jobs_row_with_no_event_chain_is_reported(self):
        self.submit()
        self.raw("INSERT INTO jobs(run_id, kind, lane, state, spec_json,"
                 " spec_hash, source_sha, submitted_by_uid, submitted_at)"
                 " VALUES('ghost','test','light','QUEUED','{}','h',?,1,?)",
                 (SHA, ts(0)))
        self.assertBroken("ghost: in jobs with no event chain")


class TestProjectionCoversEveryColumn(StoreCase):
    # Driven as a loop over PROJECTED so a column cannot be added to the
    # projection without a test for it. A hand-written list would rot.
    INT_COLS = {"submitted_by_uid", "attempts", "exit_code", "rss_high_water_kb"}
    REAL_COLS = {"wall_s"}

    def test_every_projected_column_is_checked(self):
        for col in store.Store.PROJECTED:
            with self.subTest(column=col):
                d = tempfile.TemporaryDirectory()
                path = os.path.join(d.name, "s.db")
                s = store.Store(path)
                try:
                    s.submit(eff(), run_id="r1", uid=1000, now=ts(0))
                    # Lease it so the lease/hold columns hold a non-NULL value
                    # to change; otherwise those cases test NULL -> 'x' only.
                    s.dequeue("light", owner="qfd-1", now=ts(1),
                              lease_expires_at=ts(30), hold_deadline_at=ts(50),
                              max_running=1)
                    ok, _ = s.verify_chain()
                    self.assertTrue(ok)
                    if col in self.INT_COLS:
                        new = 424242
                    elif col in self.REAL_COLS:
                        new = 42.5
                    else:
                        new = "tampered"
                    db = sqlite3.connect(path, isolation_level=None)
                    db.execute(f"UPDATE jobs SET {col}=? WHERE run_id='r1'",
                               (new,))
                    db.close()
                    ok, problems = s.verify_chain()
                    self.assertFalse(ok, f"{col}: tamper undetected")
                    self.assertIn(f"r1.{col}", " | ".join(problems))
                finally:
                    s.close()
                    d.cleanup()

    def test_non_authoritative_set_is_empty(self):
        # If a column is ever excluded, it must be named and justified; an
        # unexplained omission reads as coverage.
        self.assertEqual(store.Store.NON_AUTHORITATIVE, ())

    def test_projected_matches_the_jobs_table(self):
        cols = {r[1] for r in self.s.db.execute("PRAGMA table_info(jobs)")}
        self.assertEqual(cols - {"run_id"}, set(store.Store.PROJECTED))


class TestLeaseFieldsAreChained(StoreCase):
    def test_first_dequeue_still_verifies(self):
        # Revision 3: the dequeue event omitted the lease fields its UPDATE set,
        # so the very first dequeue broke verification.
        self.submit()
        self.lease()
        self.assertVerifies()

    def test_hold_columns_are_persisted_not_merely_announced(self):
        # Revision 7: payload-only columns leave NULLs, so verification fails on
        # the first dequeue and restart has no deadline to restore.
        self.submit()
        row = self.lease()
        self.assertEqual(row["hold_started_at"], ts(1))
        self.assertEqual(row["hold_deadline_at"], ts(50))
        fresh = self.s.get("r1")
        self.assertEqual(fresh["hold_deadline_at"], ts(50))
        self.assertVerifies()

    def test_lease_owner_and_expiry_edits_are_detected(self):
        self.submit()
        self.lease()
        self.raw("UPDATE jobs SET lease_owner='mallory' WHERE run_id='r1'")
        self.assertBroken("r1.lease_owner")

    def test_renew_is_chained(self):
        self.submit()
        self.lease()
        self.assertTrue(self.s.renew("r1", owner="qfd-1",
                                     lease_expires_at=ts(40), now=ts(5)))
        self.assertEqual(self.s.get("r1")["lease_expires_at"], ts(40))
        self.assertVerifies()

    def test_renew_by_a_foreign_owner_returns_false_and_changes_nothing(self):
        # A reclaimed job must not be resurrected by its old runner.
        self.submit()
        self.lease()
        before = self.s.get("r1")
        self.assertFalse(self.s.renew("r1", owner="qfd-2",
                                      lease_expires_at=ts(40), now=ts(5)))
        self.assertEqual(self.s.get("r1"), before)
        self.assertVerifies()

    def test_lease_expiry_is_an_absolute_instant(self):
        self.submit()
        row = self.lease()
        self.assertEqual(row["lease_expires_at"], ts(30))
        self.assertNotEqual(row["lease_expires_at"], "1800")


class TestStateSets(StoreCase):
    def test_state_sets_are_derived_from_allowed(self):
        # Revision 9 added BUILDING to ALLOWED and to nothing else. Asserting
        # the sets against ALLOWED's keys means a new state cannot be added
        # without appearing in them.
        self.assertEqual(set(store.ALLOWED) - {None},
                         {"QUEUED"} | set(store.ADMITTED_STATES))
        self.assertTrue(set(store.LEASE_ACTIVE_STATES)
                        <= set(store.ADMITTED_STATES))
        self.assertFalse(set(store.ADMITTED_STATES) & store.TERMINAL)
        self.assertTrue(set(store.STATES) >= set(store.ALLOWED) - {None})

    def test_cleanup_blocked_is_non_terminal_and_keeps_its_resources(self):
        # A terminal state that keeps resources has no path back; that was
        # revision 7's bug.
        self.assertNotIn("CLEANUP_BLOCKED", store.TERMINAL)
        self.assertEqual(store.ALLOWED["CLEANUP_BLOCKED"], {"FAILED"})
        self.assertIn("CLEANUP_BLOCKED", store.ADMITTED_STATES)

    def test_a_cleanup_blocked_job_still_shows_its_reservation(self):
        self.submit(mem_limit="8g")
        self.lease(lane="heavy")
        self.s.transition("r1", "RUNNING", now=ts(2))
        self.s.transition("r1", "CLEANUP_BLOCKED", now=ts(3))
        self.assertEqual(self.s.lane_busy("heavy"), 1)
        self.assertGreater(self.s.admitted_mem_mb(), 0)
        self.assertVerifies()


class TestBuildingState(StoreCase):
    def setUp(self):
        super().setUp()
        self.submit(mem_limit="8g")
        self.lease(lane="heavy")
        self.s.transition("r1", "BUILDING", now=ts(2))

    def test_building_occupies_its_lane(self):
        self.assertEqual(self.s.lane_busy("heavy"), 1)
        self.assertIsNone(self.lease(lane="heavy", at=ts(3)))

    def test_building_renews_its_lease(self):
        self.assertTrue(self.s.renew("r1", owner="qfd-1",
                                     lease_expires_at=ts(45), now=ts(4)))

    def test_building_cannot_record_a_container_at_all(self):
        # This fixture used to record one and then exercise reclaim over it,
        # which asserted the daemon's behaviour in a state it cannot reach: the
        # classic builder owns no container of ours, so nothing is recorded until
        # RUNNING. The rule is the assertion now; reclaim's adopt-then-reclaim
        # sequence is covered by TestAdoptAndReclaim, where the state is real.
        with self.assertRaises(store.WorkNotPermitted):
            self.s.add_resource("r1", role="candidate", container_id="c1",
                                now=ts(3))
        self.assertEqual(self.s.resources_for("r1"), [])
        self.assertVerifies()

    def test_building_with_no_resources_at_all_is_not_a_free_pass(self):
        # Revision 12: confirmation over an empty inventory is not confirmation.
        out = self.s.reclaim(ts(31), probe=lambda c: False)
        self.assertEqual(out, [("r1", "cleanup_blocked")])
        self.assertEqual(self.s.get("r1")["state"], "CLEANUP_BLOCKED")
        self.assertIn("CLEANUP_BLOCKED", store.ADMITTED_STATES)
        self.assertVerifies()


class TestAbsenceSettling(unittest.TestCase):
    """The pure decision, tested as one. Both the runner's confirmation path and
    `reclaim` consult it, and the whole point of it being a function is that they
    cannot disagree."""

    def pins(self, value):
        return {store.absence_settles_pin("candidate"): value}

    def test_no_outstanding_create_means_absence_is_proof(self):
        for pins in ({}, self.pins("")):
            with self.subTest(pins=pins):
                self.assertFalse(store.create_unacked(pins, "candidate"))
                self.assertTrue(
                    store.absence_believable(pins, "candidate", ts(1)))

    def test_a_create_that_was_never_issued_never_settles(self):
        # An instant is a bet that a request in flight completes within a window.
        # Before the request is issued there is nothing to bet on, so elapsed
        # time is not evidence -- a stalled phase is still going to ask.
        pins = self.pins(store.ABSENCE_NOT_YET_ISSUED)
        self.assertTrue(store.create_unacked(pins, "candidate"))
        # The sentinel must not look like an instant, or the two meanings become
        # one comparison and the explicit refusal below becomes accidental.
        self.assertFalse(store.ABSENCE_NOT_YET_ISSUED[:1].isdigit(),
                         "a sentinel shaped like an instant is a trap")
        for at in (ts(1), ts(30), "2999-01-01T00:00:00Z"):
            with self.subTest(now=at):
                self.assertFalse(
                    store.absence_believable(pins, "candidate", at))

    def test_an_outstanding_create_holds_absence_until_its_instant(self):
        pins = self.pins(ts(30))
        self.assertTrue(store.create_unacked(pins, "candidate"))
        self.assertFalse(store.absence_believable(pins, "candidate", ts(29)))
        self.assertTrue(store.absence_believable(pins, "candidate", ts(30)))
        self.assertTrue(store.absence_believable(pins, "candidate", ts(31)))

    def test_the_roles_do_not_share_a_verdict(self):
        pins = self.pins(ts(90))
        self.assertTrue(store.absence_believable(pins, "handoff", ts(1)))
        self.assertFalse(store.absence_believable(pins, "candidate", ts(1)))


class TestSettlingUnissuedCreatesAtStartup(StoreCase):
    """The one moment "no phase can issue this create" is true.

    The sentinel is immune to time on purpose, so something has to end it when
    the phase that would have ended it no longer exists. That something is a
    restart, and only a restart.
    """

    def setUp(self):
        super().setUp()
        self.submit()

    def convert(self, at=None):
        return self.s.settle_unissued_creates("r1", settles_at=ts(30),
                                              now=at or ts(1))

    def test_a_sentinel_becomes_the_given_instant(self):
        self.s.set_pin("r1", store.absence_settles_pin("candidate"),
                       store.ABSENCE_NOT_YET_ISSUED, now=ts(0))
        self.assertEqual(self.convert(), ["candidate"])
        pins = self.s.pins_for("r1")
        self.assertEqual(pins[store.absence_settles_pin("candidate")], ts(30))
        # Still unacked -- the ambiguity is KEPT, only made bounded. A crash
        # cannot tell "never asked" from "asked, and the answer died with the
        # client", and the second case can still bind the name.
        self.assertTrue(store.create_unacked(pins, "candidate"))
        self.assertFalse(store.absence_believable(pins, "candidate", ts(29)))
        self.assertTrue(store.absence_believable(pins, "candidate", ts(30)))

    def test_every_role_that_stalled_is_converted(self):
        for role in ("candidate", "handoff"):
            self.s.set_pin("r1", store.absence_settles_pin(role),
                           store.ABSENCE_NOT_YET_ISSUED, now=ts(0))
        self.assertEqual(self.convert(), ["candidate", "handoff"])

    def test_an_instant_already_running_is_left_alone(self):
        # It was written by an ANSWER, so it already means something and its
        # window is already elapsing. Rewriting it would extend the window on
        # every restart -- a crash loop would then never settle anything.
        self.s.set_pin("r1", store.absence_settles_pin("candidate"), ts(5),
                       now=ts(0))
        self.assertEqual(self.convert(), [])
        self.assertEqual(self.s.pins_for("r1")[
            store.absence_settles_pin("candidate")], ts(5))

    def test_unrelated_pins_are_untouched(self):
        self.s.set_pin("r1", "image_tag", store.ABSENCE_NOT_YET_ISSUED,
                       now=ts(0))
        self.assertEqual(self.convert(), [])
        self.assertEqual(self.s.pins_for("r1")["image_tag"],
                         store.ABSENCE_NOT_YET_ISSUED)

    def test_one_role_can_be_converted_alone(self):
        # The phase-level finalizer owns ITS create and no other: the candidate's
        # pin belongs to the candidate phase, which may still be holding it.
        for role in ("candidate", "handoff"):
            self.s.set_pin("r1", store.absence_settles_pin(role),
                           store.ABSENCE_NOT_YET_ISSUED, now=ts(0))
        self.assertEqual(
            self.s.settle_unissued_creates("r1", settles_at=ts(30), now=ts(1),
                                           role="handoff"), ["handoff"])
        pins = self.s.pins_for("r1")
        self.assertEqual(pins[store.absence_settles_pin("handoff")], ts(30))
        self.assertEqual(pins[store.absence_settles_pin("candidate")],
                         store.ABSENCE_NOT_YET_ISSUED)

    def test_a_role_with_nothing_outstanding_is_a_no_op(self):
        self.assertEqual(
            self.s.settle_unissued_creates("r1", settles_at=ts(30), now=ts(1),
                                           role="handoff"), [])

    def test_a_run_with_nothing_outstanding_is_a_no_op(self):
        self.assertEqual(self.convert(), [])
        self.assertEqual(self.s.pins_for("r1"), {})


class TestDequeue(StoreCase):
    def test_peek_reports_the_head_without_changing_state(self):
        # Admission is taken between peek and dequeue, so peek must not create
        # a state transition there is no legal way to undo.
        self.submit()
        head = self.s.peek("light")
        self.assertEqual(head["run_id"], "r1")
        self.assertEqual(self.s.get("r1")["state"], "QUEUED")
        self.assertEqual(self.s.head()[0], 1)

    def test_peek_on_an_empty_lane_is_none(self):
        self.assertIsNone(self.s.peek("light"))

    def test_dequeue_returns_the_post_update_row(self):
        # A runner acting on stale lease fields would renew a lease it does not
        # hold and adopt a container id it never set.
        self.submit()
        row = self.lease()
        self.assertEqual(row["state"], "LEASED")
        self.assertEqual(row["lease_owner"], "qfd-1")
        self.assertEqual(row["attempts"], 1)

    def test_second_dequeue_is_none_while_one_is_leased_or_running(self):
        self.submit("r1", at=ts(0), mem_limit="8g")
        self.submit("r2", at=ts(1), mem_limit="8g")
        self.assertIsNotNone(self.lease("r1", lane="heavy"))
        self.assertIsNone(self.lease(lane="heavy", at=ts(2)))
        self.s.transition("r1", "RUNNING", now=ts(3))
        self.assertIsNone(self.lease(lane="heavy", at=ts(4)))

    def test_dequeue_respects_submission_order(self):
        for i, rid in enumerate(["r3", "r1", "r2"]):
            self.submit(rid, at=ts(i))
        got = []
        for i in range(3):
            row = self.s.dequeue("light", owner="qfd-1", now=ts(10 + i),
                                 lease_expires_at=ts(30),
                                 hold_deadline_at=ts(50), max_running=99)
            got.append(row["run_id"])
        self.assertEqual(got, ["r3", "r1", "r2"])

    def test_dequeue_on_an_empty_queue_is_none(self):
        self.assertIsNone(self.lease())


class TestTransitions(StoreCase):
    def test_no_leased_to_queued_transition_exists(self):
        # The first revision's contention path, which the state table forbade.
        self.assertNotIn("QUEUED", store.ALLOWED["LEASED"])
        self.submit()
        self.lease()
        with self.assertRaises(store.IllegalTransition):
            self.s.transition("r1", "QUEUED", now=ts(5))
        self.assertEqual(self.s.get("r1")["state"], "LEASED")
        self.assertVerifies()

    def test_every_illegal_transition_raises(self):
        for frm, allowed in store.ALLOWED.items():
            if frm is None:
                continue
            for to in store.STATES:
                if to in allowed:
                    continue
                with self.subTest(frm=frm, to=to):
                    d = tempfile.TemporaryDirectory()
                    s = store.Store(os.path.join(d.name, "s.db"))
                    try:
                        s.submit(eff(), run_id="r1", uid=1000, now=ts(0))
                        s.db.execute("UPDATE jobs SET state=? WHERE run_id='r1'",
                                     (frm,))
                        with self.assertRaises(store.IllegalTransition):
                            s.transition("r1", to, now=ts(5))
                    finally:
                        s.close()
                        d.cleanup()

    def test_transition_on_a_missing_job_raises(self):
        with self.assertRaises(store.IllegalTransition):
            self.s.transition("nope", "CANCELLED", now=ts(5))

    def test_transition_refuses_a_field_that_is_not_a_projected_column(self):
        # The field names become SQL. Closed world here too.
        self.submit()
        with self.assertRaises(store.IllegalTransition):
            self.s.transition("r1", "CANCELLED", now=ts(5),
                              fields={"state=1; DROP TABLE jobs": "x"})

    def test_a_terminal_state_has_no_outgoing_edges(self):
        for t in store.TERMINAL:
            self.assertNotIn(t, store.ALLOWED)


class TestAdoptAndReclaim(StoreCase):
    def test_adopt_keeps_a_reconciled_run_running_and_extends_the_lease(self):
        # Reaping a live container after a dispatcher restart is the failure.
        self.submit()
        self.lease()
        self.s.transition("r1", "RUNNING", now=ts(2))
        row = self.s.adopt("r1", "c-live", owner="qfd-2",
                           lease_expires_at=ts(59), now=ts(3))
        self.assertEqual(row["state"], "RUNNING")
        self.assertEqual(row["container_id"], "c-live")
        self.assertEqual(row["lease_owner"], "qfd-2")
        self.assertEqual(row["lease_expires_at"], ts(59))
        self.assertVerifies()

    def test_adoption_restores_the_remaining_budget_not_a_fresh_one(self):
        # Repeated restarts must not extend one lock hold past LOCK_WAIT_S, so
        # the deadline is persisted and adoption does not reset it.
        self.submit()
        self.lease()
        deadline = self.s.get("r1")["hold_deadline_at"]
        self.s.transition("r1", "RUNNING", now=ts(2))
        self.s.adopt("r1", "c-live", owner="qfd-2", lease_expires_at=ts(59),
                     now=ts(3))
        self.assertEqual(self.s.get("r1")["hold_deadline_at"], deadline)
        self.assertEqual(self.s.get("r1")["hold_started_at"], ts(1))

    def test_reclaim_fails_an_expired_run_whose_containers_are_gone(self):
        self.submit()
        self.lease()
        self.s.transition("r1", "RUNNING", now=ts(2))
        self.s.add_resource("r1", role="candidate", container_id="c1", now=ts(2))
        out = self.s.reclaim(ts(31), probe=lambda c: False)
        self.assertEqual(out, [("r1", "reclaimed")])
        job = self.s.get("r1")
        self.assertEqual((job["state"], job["error_class"]),
                         ("FAILED", "reclaimed"))
        self.assertVerifies()

    def test_a_live_container_is_adopted_only_while_the_hold_deadline_holds(self):
        # The lease may be extended; the HOLD may not. An expired lease means
        # whatever was renewing it stopped, so nothing is driving this run --
        # and adopting a live container renews it anyway, so every later sweep
        # would find the same container and grant another lease, for ever.
        self.submit()
        self.lease()                                    # hold deadline ts(50)
        self.s.transition("r1", "RUNNING", now=ts(2))
        self.s.add_resource("r1", role="candidate", container_id="c1", now=ts(2))

        # Inside the deadline: adoption is right, the run is still within budget.
        out = self.s.reclaim(ts(31), probe=lambda c: True, owner="qfd-2",
                             lease_expires_at=ts(60))
        self.assertEqual(out, [("r1", "adopted")])
        self.assertEqual(self.s.get("r1")["lease_expires_at"], ts(60))

        # Past it: no renewal, and it goes where a live container that must die
        # goes. `reclaim` holds a probe, not a Docker client, so the killing is
        # the reaper's job -- but the RENEWING stops here.
        out = self.s.reclaim(ts(61), probe=lambda c: True, owner="qfd-2",
                             lease_expires_at=ts(120))
        self.assertEqual(out, [("r1", "deadline_expired")])
        job = self.s.get("r1")
        self.assertEqual(job["state"], "CLEANUP_BLOCKED")
        self.assertEqual(job["error_class"], "hold_deadline_expired")
        self.assertEqual(job["lease_expires_at"], ts(60),
                         "the lease was renewed past the hold deadline")
        self.assertIn("CLEANUP_BLOCKED", store.ADMITTED_STATES)
        self.assertVerifies()

    def test_past_the_deadline_an_unanswered_probe_still_reaches_cleanup(self):
        # The stall with no escape: an unanswered probe left the job RUNNING, so
        # `resolve_blocked` (CLEANUP_BLOCKED only) never saw it and
        # `force-release` refused it for not being CLEANUP_BLOCKED. Past the
        # deadline the state has to move even though nothing is known, because
        # CLEANUP_BLOCKED is what holds everything AND is visible to both the
        # automatic and the manual cleanup path.
        self.submit()
        self.lease()                                    # hold deadline ts(50)
        self.s.transition("r1", "RUNNING", now=ts(2))
        self.s.add_resource("r1", role="candidate", container_id="c1", now=ts(2))

        # Before the deadline an unknown is simply re-asked: nothing is known,
        # and the run is still within its budget.
        self.assertEqual(self.s.reclaim(ts(31), probe=lambda c: None),
                         [("r1", "unconfirmed")])
        self.assertEqual(self.s.get("r1")["state"], "RUNNING")

        out = self.s.reclaim(ts(61), probe=lambda c: None)
        self.assertEqual(out, [("r1", "deadline_expired")])
        job = self.s.get("r1")
        self.assertEqual(job["state"], "CLEANUP_BLOCKED")
        self.assertEqual(job["error_class"], "hold_deadline_expired")
        # Fail-closed: still admitted, nothing released.
        self.assertIn("CLEANUP_BLOCKED", store.ADMITTED_STATES)
        self.assertEqual(len(self.s.resources_for("r1", unreleased_only=True)), 1)
        self.assertVerifies()

    def test_past_the_deadline_an_unsettled_absence_also_reaches_cleanup(self):
        self.submit()
        self.lease()
        self.s.transition("r1", "RUNNING", now=ts(2))
        self.s.add_resource("r1", role="candidate", container_id="c1", now=ts(2))
        self.s.set_pin("r1", store.absence_settles_pin("candidate"), ts(999),
                       now=ts(2))
        out = self.s.reclaim(ts(61), probe=lambda c: False)
        self.assertEqual(out, [("r1", "deadline_expired")])
        self.assertEqual(self.s.get("r1")["state"], "CLEANUP_BLOCKED")

    def test_past_the_deadline_a_settled_absence_still_just_fails(self):
        # The exemption: a settled absence has a better answer available than
        # CLEANUP_BLOCKED, so the deadline does not downgrade it.
        self.submit()
        self.lease()
        self.s.transition("r1", "RUNNING", now=ts(2))
        self.s.add_resource("r1", role="candidate", container_id="c1", now=ts(2))
        out = self.s.reclaim(ts(61), probe=lambda c: False)
        self.assertEqual(out, [("r1", "reclaimed")])
        job = self.s.get("r1")
        self.assertEqual((job["state"], job["error_class"]),
                         ("FAILED", "reclaimed"))
        self.assertEqual(self.s.resources_for("r1", unreleased_only=True), [])

    def test_reclaim_will_not_release_an_absence_that_has_not_settled(self):
        # An absence is only proof when the create that would have produced the
        # container was acknowledged. Until the settle instant passes, "No such
        # object" is a reading -- the daemon can finish a submitted request after
        # the client that submitted it died -- so it counts as unknown and the
        # job keeps everything.
        self.submit()
        self.lease()
        self.s.transition("r1", "RUNNING", now=ts(2))
        self.s.add_resource("r1", role="candidate", container_id="c1", now=ts(2))
        self.s.set_pin("r1", store.absence_settles_pin("candidate"), ts(90),
                       now=ts(2))

        out = self.s.reclaim(ts(31), probe=lambda c: False)
        self.assertEqual(out, [("r1", "unconfirmed")])
        self.assertEqual(self.s.get("r1")["state"], "RUNNING")
        self.assertEqual(len(self.s.resources_for("r1", unreleased_only=True)), 1)

        # ...and it does resolve, rather than stalling for good: reclaim runs
        # again every reap interval and the instant is fixed.
        out = self.s.reclaim(ts(91), probe=lambda c: False)
        self.assertEqual(out, [("r1", "reclaimed")])
        self.assertEqual(self.s.resources_for("r1", unreleased_only=True), [])
        self.assertVerifies()

    def test_reclaim_still_adopts_a_container_that_turns_up(self):
        # The delayed create landing is the case the settle window exists for,
        # and a live container is evidence, not a reading.
        self.submit()
        self.lease()
        self.s.transition("r1", "RUNNING", now=ts(2))
        self.s.add_resource("r1", role="candidate", container_id="c1", now=ts(2))
        self.s.set_pin("r1", store.absence_settles_pin("candidate"), ts(90),
                       now=ts(2))
        out = self.s.reclaim(ts(31), probe=lambda c: True, owner="qfd-1",
                             lease_expires_at=ts(120))
        self.assertEqual(out, [("r1", "adopted")])

    def test_reclaim_leaves_an_unexpired_lease_alone(self):
        self.submit()
        self.lease()
        self.assertEqual(self.s.reclaim(ts(10), probe=lambda c: False), [])
        self.assertEqual(self.s.get("r1")["state"], "LEASED")

    def test_reclaim_asks_about_every_recorded_container_not_just_one(self):
        self.submit()
        self.lease()
        self.s.transition("r1", "RUNNING", now=ts(2))
        self.s.add_resource("r1", role="candidate", container_id="c1", now=ts(2))
        self.s.add_resource("r1", role="handoff", container_id="c2", now=ts(3))
        asked = []

        def probe(cid):
            asked.append(cid)
            return cid == "c2"           # the handoff is still up

        out = self.s.reclaim(ts(31), probe=probe, owner="qfd-1",
                             lease_expires_at=ts(60))
        self.assertEqual(sorted(asked), ["c1", "c2"])
        self.assertEqual(out, [("r1", "adopted")])
        self.assertEqual(self.s.get("r1")["state"], "RUNNING")

    def test_reclaim_ignores_already_released_resources(self):
        self.submit()
        self.lease()
        self.s.transition("r1", "RUNNING", now=ts(2))
        self.s.add_resource("r1", role="candidate", container_id="c1", now=ts(2))
        self.s.release_resource("r1", role="candidate", container_id="c1",
                                now=ts(3))
        # Released and nothing else recorded: an empty live inventory, which is
        # not a confirmation, so RUNNING settles rather than passing.
        out = self.s.reclaim(ts(31), probe=lambda c: False)
        self.assertEqual(out, [("r1", "cleanup_blocked")])


class TestResources(StoreCase):
    def setUp(self):
        super().setUp()
        self.submit()
        self.lease()
        # RUNNING first: `add_resource` refuses any other state, because that is
        # the only one in which new work may appear.
        self.s.transition("r1", "RUNNING", now=ts(2))
        self.s.add_resource("r1", role="candidate", container_id="c1", now=ts(2))

    def test_resource_create_and_release_verify(self):
        self.s.release_resource("r1", role="candidate", container_id="c1",
                                now=ts(4))
        self.assertVerifies()
        self.assertEqual(self.s.resources_for("r1")[0]["released_at"], ts(4))

    def test_resource_is_keyed_by_role_and_container_id(self):
        self.s.add_resource("r1", role="handoff", container_id="c1", now=ts(3))
        self.s.release_resource("r1", role="handoff", container_id="c1",
                                now=ts(4))
        rows = {(r["role"], r["released_at"]) for r in self.s.resources_for("r1")}
        self.assertEqual(rows, {("candidate", None), ("handoff", ts(4))})
        self.assertVerifies()

    def test_a_deleted_resource_row_is_reported(self):
        self.raw("DELETE FROM resources WHERE run_id='r1'")
        self.assertBroken("resource candidate/c1 disagrees")

    def test_an_extra_resource_row_is_reported(self):
        self.raw("INSERT INTO resources(run_id, role, container_id, created_at)"
                 " VALUES('r1','candidate','ghost',?)", (ts(2),))
        self.assertBroken("resource row ('candidate', 'ghost') has no event")

    def test_a_changed_resource_timestamp_is_reported(self):
        self.raw("UPDATE resources SET created_at='1999' WHERE run_id='r1'")
        self.assertBroken("resource candidate/c1 disagrees")

    def test_a_forged_release_timestamp_is_reported(self):
        self.raw("UPDATE resources SET released_at='1999' WHERE run_id='r1'")
        self.assertBroken("resource candidate/c1 disagrees")

    def test_a_release_for_a_container_never_created_is_reported(self):
        # A release record fabricated for a container that never existed. The
        # UPDATE touches no row, so no orphan check can see it -- only the
        # replay can.
        self.s.release_resource("r1", role="candidate", container_id="never",
                                now=ts(5))
        self.assertBroken("release of ('candidate', 'never') that was never created")


class TestPinsAndArtifacts(StoreCase):
    def setUp(self):
        super().setUp()
        self.submit()

    def test_pins_accept_new_keys_without_a_schema_change(self):
        # 2b/2c must not need a migration to record provenance.
        self.s.set_pin("r1", "extract_parquet_sha256", "abc", now=ts(2))
        self.s.set_pin("r1", "a_key_invented_in_2c", "xyz", now=ts(3))
        self.assertVerifies()

    def test_a_pin_edited_directly_is_reported(self):
        self.s.set_pin("r1", "k", "v", now=ts(2))
        self.raw("UPDATE pins SET value='tampered' WHERE run_id='r1'")
        self.assertBroken("pin k disagrees")

    def test_a_pin_row_with_no_event_is_reported(self):
        self.raw("INSERT INTO pins(run_id, key, value) VALUES('r1','ghost','v')")
        self.assertBroken("pins row ghost has no event")

    def test_artifact_path_bytes_and_digest_are_all_checked(self):
        # Revision 2 widened the projection but left path and bytes uncovered.
        for col, value in [("path", "/elsewhere"), ("bytes", 999),
                           ("sha256", "0" * 64)]:
            with self.subTest(column=col):
                d = tempfile.TemporaryDirectory()
                s = store.Store(os.path.join(d.name, "s.db"))
                try:
                    s.submit(eff(), run_id="r1", uid=1000, now=ts(0))
                    s.add_artifact("r1", name="result.json", path="/out/r.json",
                                   sha256="a" * 64, bytes_=12, now=ts(2))
                    ok, _ = s.verify_chain()
                    self.assertTrue(ok)
                    db = sqlite3.connect(os.path.join(d.name, "s.db"),
                                         isolation_level=None)
                    db.execute(f"UPDATE artifacts SET {col}=? WHERE run_id='r1'",
                               (value,))
                    db.close()
                    ok, problems = s.verify_chain()
                    self.assertFalse(ok, f"{col}: tamper undetected")
                    self.assertIn("artifact result.json disagrees",
                                  " | ".join(problems))
                finally:
                    s.close()
                    d.cleanup()

    def test_an_artifact_row_with_no_event_is_reported(self):
        self.raw("INSERT INTO artifacts(run_id, name, path, sha256, bytes)"
                 " VALUES('r1','ghost','/p','h',1)")
        self.assertBroken("artifacts row ghost has no event")


class TestAdmission(StoreCase):
    def test_admit_refuses_when_the_sum_would_exceed_the_budget(self):
        # Two jobs summing past the host's RAM (design D10) -- the exact failure
        # a caller-chosen lane allowed.
        s = store.Store(os.path.join(self.dir.name, "b.db"), mem_budget_mb=10240)
        self.addCleanup(s.close)
        s.submit(eff(mem_limit="8g"), run_id="r1", uid=1000, now=ts(0))
        ok, _ = s.admit("8g")
        self.assertTrue(ok)
        s.dequeue("heavy", owner="qfd-1", now=ts(1), lease_expires_at=ts(30),
                  hold_deadline_at=ts(50), max_running=1)
        self.assertEqual(s.admitted_mem_mb(), 8192)
        ok, why = s.admit("8g")
        self.assertFalse(ok)
        self.assertIn("memory budget", why)

    def test_admission_is_released_on_completion(self):
        s = store.Store(os.path.join(self.dir.name, "c.db"), mem_budget_mb=10240)
        self.addCleanup(s.close)
        s.submit(eff(mem_limit="8g"), run_id="r1", uid=1000, now=ts(0))
        s.dequeue("heavy", owner="qfd-1", now=ts(1), lease_expires_at=ts(30),
                  hold_deadline_at=ts(50), max_running=1)
        s.transition("r1", "RUNNING", now=ts(2))
        self.assertEqual(s.admitted_mem_mb(), 8192)
        s.transition("r1", "SUCCEEDED", now=ts(3), fields={"exit_code": 0})
        self.assertEqual(s.admitted_mem_mb(), 0)
        ok, _ = s.admit("8g")
        self.assertTrue(ok)

    def test_a_reservation_is_never_smaller_than_an_image_build(self):
        # One reservation per job, sized max(mem_limit, IMAGE_BUILD_MEM_MB):
        # charging the build on top made a big job with a cold cache
        # permanently unadmittable.
        self.assertEqual(store.reservation_mb("1g"), store.IMAGE_BUILD_MEM_MB)
        self.assertEqual(store.reservation_mb("8g"), 8192)

    def test_admit_refuses_when_free_disk_is_below_the_floor(self):
        # Scheduling into a full filesystem (design §4.5).
        ok, why = self.s.admit("4g", free_disk_mb=1024)
        self.assertFalse(ok)
        self.assertIn("disk floor", why)
        ok, _ = self.s.admit("4g", free_disk_mb=10 ** 7)
        self.assertTrue(ok)

    def test_the_disk_allowance_charges_out_plus_artifacts(self):
        # A run's allowance is OUT_QUOTA + ARTIFACT_CAP, since the handoff
        # duplicates output into artifacts/ before out/ is pruned (D9).
        s = store.Store(os.path.join(self.dir.name, "d.db"), disk_floor_mb=100,
                        out_quota_mb=50, artifact_cap_mb=25)
        self.addCleanup(s.close)
        self.assertFalse(s.admit("4g", free_disk_mb=174)[0])
        self.assertTrue(s.admit("4g", free_disk_mb=175)[0])


class TestFlooding(StoreCase):
    def test_queued_count_for_uid_caps_a_flooder(self):
        for i in range(5):
            self.submit(f"r{i}", at=ts(i), uid=2000)
        self.submit("other", at=ts(9), uid=3000)
        self.assertEqual(self.s.queued_count_for_uid(2000), 5)
        self.assertEqual(self.s.queued_count_for_uid(3000), 1)
        # Leaving QUEUED frees the caller's allowance.
        self.lease("r0", at=ts(10))
        self.assertEqual(self.s.queued_count_for_uid(2000), 4)


class TestConcurrency(StoreCase):
    def test_concurrent_submitters_and_workers_keep_the_chain_intact(self):
        # Against a real SQLite file, with one connection per thread: a fake
        # runner sharing one connection would either raise ProgrammingError or
        # hide the interleaving this is about.
        n_sub, n_work = 8, 4
        errors = []

        def submitter(i):
            try:
                s = store.Store(self.path)
                try:
                    for j in range(5):
                        s.submit(eff(), run_id=f"s{i}-{j}", uid=1000 + i,
                                 now=ts(i * 5 + j))
                finally:
                    s.close()
            except Exception as e:                # pragma: no cover - surfaced
                errors.append(repr(e))

        def worker(i):
            try:
                s = store.Store(self.path)
                try:
                    for _ in range(10):
                        s.dequeue("light", owner=f"w{i}", now=ts(59),
                                  lease_expires_at=ts(59),
                                  hold_deadline_at=ts(59), max_running=999)
                finally:
                    s.close()
            except Exception as e:                # pragma: no cover - surfaced
                errors.append(repr(e))

        threads = ([threading.Thread(target=submitter, args=(i,))
                    for i in range(n_sub)]
                   + [threading.Thread(target=worker, args=(i,))
                      for i in range(n_work)])
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(self.s.list(limit=1000)), n_sub * 5)
        self.assertVerifies()

    def test_no_run_id_is_leased_twice(self):
        for i in range(6):
            self.submit(f"r{i}", at=ts(i))
        leased, lock = [], threading.Lock()

        def worker(i):
            s = store.Store(self.path)
            try:
                for _ in range(6):
                    row = s.dequeue("light", owner=f"w{i}", now=ts(20),
                                    lease_expires_at=ts(59),
                                    hold_deadline_at=ts(59), max_running=999)
                    if row:
                        with lock:
                            leased.append(row["run_id"])
            finally:
                s.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(leased), [f"r{i}" for i in range(6)])
        self.assertVerifies()


class TestRefusal(StoreCase):
    def test_a_refusal_is_recorded_and_terminal(self):
        self.s.refuse(eff(), run_id="bad", uid=1000, now=ts(0),
                      error_class="spec")
        job = self.s.get("bad")
        self.assertEqual(job["state"], "REFUSED")
        self.assertEqual(job["error_class"], "spec")
        self.assertIn("REFUSED", store.TERMINAL)
        self.assertVerifies()


if __name__ == "__main__":
    unittest.main()
