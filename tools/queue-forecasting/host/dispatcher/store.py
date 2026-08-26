"""Live state for the dispatcher: SQLite, WAL, one append-only hash chain.

`events` is the authority; `jobs` is a materialised projection maintained in
the same transaction (design D7). `verify_chain` therefore does two things:
recompute the chain, and replay it into a projection and compare. Checking only
the first would let a direct UPDATE on `jobs` pass.

Single writer by construction, and literally so: one **DB-owner thread** inside
the one dispatcher process serves every call over a queue. `sqlite3.connect()`
binds a connection to its creating thread, so sharing this object between a
scheduler thread and the socket handler would raise `ProgrammingError` -- and
serialising through one thread also serialises the hash chain for free.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

import spec as spec_mod

GENESIS = "0" * 64
SCHEMA = 1

STATES = ("QUEUED", "LEASED", "BUILDING", "RUNNING", "CLEANUP_BLOCKED",
          "SUCCEEDED", "FAILED", "TIMEOUT", "CANCELLED", "REFUSED")
TERMINAL = frozenset({"SUCCEEDED", "FAILED", "TIMEOUT", "CANCELLED", "REFUSED"})
# The state sets every state-dependent query must use. Revision 9 added BUILDING
# to ALLOWED and to nothing else, so a BUILDING job vacated its lane, could not
# renew its lease, and was invisible to reclaim -- three silent bugs from one
# omission. Naming the sets once is the fix; open-coding a state list in SQL is
# the bug.
ADMITTED_STATES = ("LEASED", "BUILDING", "RUNNING", "CLEANUP_BLOCKED")
LEASE_ACTIVE_STATES = ("LEASED", "BUILDING", "RUNNING")
# The states in which NEW WORK MAY APPEAR -- i.e. in which a phase is entitled to
# record a container. RUNNING only, and each exclusion is a rule rather than an
# oversight: LEASED and BUILDING own no container of ours (the classic builder is
# not recorded), and by CLEANUP_BLOCKED cleanup has already BEGUN, so a container
# recorded then is a workload appearing behind the teardown that is trying to
# confirm its absence. Terminal states are excluded for the obvious reason.
PHASE_ACTIVE_STATES = ("RUNNING",)

ALLOWED = {
    None:              {"QUEUED", "REFUSED"},
    "QUEUED":          {"LEASED", "CANCELLED"},
    "LEASED":          {"BUILDING", "RUNNING", "FAILED", "CANCELLED"},
    # BUILDING exists because an unconfirmed BUILDER shutdown must reach
    # CLEANUP_BLOCKED, and LEASED had no such edge -- revision 8 specified a
    # transition this table forbade.
    "BUILDING":        {"RUNNING", "FAILED", "CANCELLED", "CLEANUP_BLOCKED"},
    "RUNNING":         {"SUCCEEDED", "FAILED", "TIMEOUT", "CANCELLED",
                        "CLEANUP_BLOCKED"},
    # Non-terminal on purpose. A job whose workload could not be CONFIRMED dead
    # still holds its training-lock descriptor and its memory reservation, so it
    # must not be terminal: the general rule is that admission lasts until a
    # terminal state, and revision 7 marked these FAILED while they kept both,
    # with nothing saying how admission ever resumed. The reaper keeps polling;
    # confirmation moves it to FAILED and releases everything; `qf force-release`
    # is the operator escape.
    "CLEANUP_BLOCKED": {"FAILED"},
}

# Admission figures (design D10 / §4.5). Defaults mirror the unit file's
# environment so a Store built without overrides behaves like production.
ADMITTED_MEM_BUDGET_MB = 22528
IMAGE_BUILD_MEM_MB = 2048
OUT_QUOTA_MB = 2048
ARTIFACT_CAP_MB = 2048
DISK_FLOOR_MB = 20 * 1024

DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
  run_id            TEXT PRIMARY KEY,
  kind              TEXT NOT NULL,
  lane              TEXT NOT NULL,
  state             TEXT NOT NULL,
  spec_json         TEXT NOT NULL,
  spec_hash         TEXT NOT NULL,
  source_sha        TEXT NOT NULL,
  source_ref        TEXT,
  image_digest      TEXT,
  submitted_by_uid  INTEGER NOT NULL,
  submitted_at      TEXT NOT NULL,
  started_at        TEXT,
  finished_at       TEXT,
  -- The outer hold deadline is PERSISTED, not kept in the runner's memory:
  -- otherwise an adopted job gets a fresh budget on every restart and repeated
  -- restarts extend one lock hold without limit (design 4.2 step 1a).
  hold_started_at   TEXT,
  hold_deadline_at  TEXT,
  attempts          INTEGER NOT NULL DEFAULT 0,
  lease_owner       TEXT,
  lease_expires_at  TEXT,
  container_id      TEXT,
  exit_code         INTEGER,
  error_class       TEXT,
  wall_s            REAL,
  rss_high_water_kb INTEGER
);
CREATE INDEX IF NOT EXISTS jobs_lane_state ON jobs(lane, state);
CREATE INDEX IF NOT EXISTS jobs_state_submitted ON jobs(state, submitted_at);
CREATE TABLE IF NOT EXISTS pins(
  run_id TEXT NOT NULL REFERENCES jobs(run_id),
  key    TEXT NOT NULL,
  value  TEXT NOT NULL,
  PRIMARY KEY(run_id, key)
);
CREATE TABLE IF NOT EXISTS resources(
  -- Every container this run created, by role. `jobs.container_id` is
  -- candidate-only, so forced cleanup and restart recovery inventory THIS
  -- rather than a label query: an ephemeral builder cannot carry a per-run
  -- label, and a
  -- container that has already stopped is invisible to `docker ps`.
  run_id       TEXT NOT NULL REFERENCES jobs(run_id),
  role         TEXT NOT NULL,          -- candidate | handoff
  container_id TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  released_at  TEXT,
  PRIMARY KEY(run_id, role, container_id)
);
CREATE TABLE IF NOT EXISTS artifacts(
  run_id TEXT NOT NULL REFERENCES jobs(run_id),
  name   TEXT NOT NULL,
  path   TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  bytes  INTEGER NOT NULL,
  PRIMARY KEY(run_id, name)
);
CREATE TABLE IF NOT EXISTS events(
  seq          INTEGER PRIMARY KEY,
  at           TEXT NOT NULL,
  run_id       TEXT,
  kind         TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  prev_hash    TEXT NOT NULL,
  hash         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id);
"""


class IllegalTransition(Exception):
    """A state change the transition table forbids. Raised, never logged and
    swallowed: a silent state machine is the thing design §4.2 rules out."""


class WorkNotPermitted(Exception):
    """New work was recorded for a run that may not have any: it is terminal, or
    its cleanup has already begun."""


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def event_hash(prev_hash, seq, at, run_id, kind, payload_json):
    """Chain link. `seq` is inside the digest, so a reordering is detectable.

    seq is assigned explicitly (not AUTOINCREMENT) inside the same IMMEDIATE
    transaction, precisely so it can be hashed.
    """
    material = "\n".join([prev_hash, str(seq), at, run_id or "", kind, payload_json])
    return hashlib.sha256(material.encode()).hexdigest()


def absence_settles_pin(role):
    """The pin naming the instant from which an ABSENCE may be believed for one
    container name.

    Written for as long as a `docker create` is UNACKNOWLEDGED -- from the moment
    the row exists until an answer arrives -- because a non-zero exit or a
    timeout leaves it unknown whether the daemon bound the name, and a daemon can
    finish a submitted request after the client that submitted it has died. So
    `docker inspect` answering "No such object" a moment later is a reading, not
    a proof -- stop, kill and remove can all run before a delayed create binds
    the name.

    Two values, because "unacknowledged" covers two situations that time treats
    differently: `ABSENCE_NOT_YET_ISSUED` before the request goes out, and an
    instant once an answer has come back ambiguous. See below.

    An INSTANT rather than a duration, for the same reason the rest of the store
    keeps instants: every reader then compares two fixed-width ISO strings and
    nobody has to agree on where the clock is read. Pushing it forward is how
    "stable absence" is expressed -- see `absence_believable`.
    """
    return f"absence_settles_at_{role}"


# The pin value that means "nothing has been ASKED of Docker yet". Distinct from
# an instant on purpose: an instant is a bet that a request already in flight
# completes within the window, which is the delayed-daemon residual design D10
# accepts. Before the request is issued there is no such bet to make -- the phase
# holding the gate is still going to ask -- so no amount of elapsed time makes the
# absence mean anything. A stalled phase must not become a settled absence.
ABSENCE_NOT_YET_ISSUED = "not-yet-issued"


def create_unacked(pins, role):
    """True while a create for this name is recorded and not acknowledged --
    whether or not the request has been issued yet. Both cases need the same
    treatment from a confirmation pass: keep removing, and never release."""
    return bool(pins.get(absence_settles_pin(role)))


def absence_believable(pins, role, now):
    """May an absence be treated as proof for this name, at `now`?

    Yes when no create is outstanding for it -- the ordinary case, where absence
    means the container was created, ran and was removed.

    NEVER while the pin says `ABSENCE_NOT_YET_ISSUED`: the row exists but nothing
    has been asked of Docker, so the name is absent because the container has not
    been made yet, and the phase that will make it is still holding its gate.
    Time cannot resolve that -- a phase stalled past any window is still a phase
    that is going to issue its create.

    Otherwise once the settle instant has passed. Anything that SEES the
    container pushes that instant forward, so passing it means the name has been
    continuously absent for the whole settle window rather than absent at one
    convenient moment.

    `now` and the pin are both `%Y-%m-%dT%H:%M:%SZ`, which is fixed-width, so
    the lexicographic comparison is chronological.
    """
    settles_at = pins.get(absence_settles_pin(role)) or ""
    if settles_at == ABSENCE_NOT_YET_ISSUED:
        # Explicit, not left to the comparison below. The sentinel happens to
        # sort after every ISO instant today, which would make this branch
        # redundant -- and would make renaming it to anything digit-leading a
        # silent hole. A test pins the shape; this pins the meaning.
        return False
    return not settles_at or now >= settles_at


def reservation_mb(mem_limit, image_build_mem_mb=IMAGE_BUILD_MEM_MB):
    """One reservation per job, sized max(mem_limit, IMAGE_BUILD_MEM_MB), held
    from admission to terminal state (design D10). Charging the build on top of
    the job made a 22 GB job with a cold cache permanently unadmittable."""
    return max(spec_mod.mem_mb(mem_limit), image_build_mem_mb)


class Store:
    def __init__(self, path, *, mem_budget_mb=ADMITTED_MEM_BUDGET_MB,
                 image_build_mem_mb=IMAGE_BUILD_MEM_MB,
                 out_quota_mb=OUT_QUOTA_MB, artifact_cap_mb=ARTIFACT_CAP_MB,
                 disk_floor_mb=DISK_FLOOR_MB):
        self.db = sqlite3.connect(path, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(DDL)
        self.db.execute("INSERT OR IGNORE INTO schema_meta VALUES('schema', ?)",
                        (str(SCHEMA),))
        self.mem_budget_mb = mem_budget_mb
        self.image_build_mem_mb = image_build_mem_mb
        self.out_quota_mb = out_quota_mb
        self.artifact_cap_mb = artifact_cap_mb
        self.disk_floor_mb = disk_floor_mb

    def close(self):
        self.db.close()

    # --- chain -----------------------------------------------------------
    def _head(self):
        row = self.db.execute(
            "SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        return (0, GENESIS) if row is None else (row["seq"], row["hash"])

    def head(self):
        """Public: (seq, hash) of the chain head. A fresh store is (0, GENESIS),
        which is what makes an empty chain distinguishable from a verified one."""
        return self._head()

    def _append(self, at, run_id, kind, payload):
        seq_prev, prev_hash = self._head()
        seq = seq_prev + 1
        pj = _canon(payload)
        h = event_hash(prev_hash, seq, at, run_id, kind, pj)
        self.db.execute(
            "INSERT INTO events(seq, at, run_id, kind, payload_json, prev_hash, hash)"
            " VALUES(?,?,?,?,?,?,?)", (seq, at, run_id, kind, pj, prev_hash, h))
        return seq, h

    def verify_chain(self):
        """Return (ok, problems). Recompute the chain, then replay it into a
        projection and compare with `jobs`."""
        problems, prev, expect = [], GENESIS, 1
        projection = {}
        for row in self.db.execute("SELECT * FROM events ORDER BY seq"):
            if row["seq"] != expect:
                problems.append(f"seq gap at {row['seq']} (expected {expect})")
            if row["prev_hash"] != prev:
                problems.append(f"prev_hash mismatch at seq {row['seq']}")
            h = event_hash(prev, row["seq"], row["at"], row["run_id"],
                           row["kind"], row["payload_json"])
            if h != row["hash"]:
                problems.append(f"hash mismatch at seq {row['seq']}")
            prev, expect = row["hash"], row["seq"] + 1
            self._replay(projection, row)
        for run_id, expected in projection.items():
            # Problems the replay itself found: an event whose `from` disagrees
            # with the replayed state, or a release for a resource that was never
            # created. The plan's `_replay` recorded the first of these into the
            # projection and nothing ever read it, so two of this task's own test
            # cases could not pass; they are surfaced here.
            for note in expected.get("_problems", ()):
                problems.append(f"{run_id}: {note}")
            got = self.db.execute("SELECT * FROM jobs WHERE run_id=?",
                                  (run_id,)).fetchone()
            if got is None:
                problems.append(f"{run_id}: event chain has it, jobs does not")
                continue
            # Every projected column, not just state. Naming the field matters:
            # "the chain disagrees" is not an actionable report.
            for col in self.PROJECTED:
                if got[col] != expected.get(col):
                    problems.append(
                        f"{run_id}.{col}: jobs has {got[col]!r},"
                        f" chain has {expected.get(col)!r}")
            for key, value in sorted(expected["pins"].items()):
                row = self.db.execute(
                    "SELECT value FROM pins WHERE run_id=? AND key=?",
                    (run_id, key)).fetchone()
                if row is None or row["value"] != value:
                    problems.append(f"{run_id}: pin {key} disagrees")
            for (role, cid), meta in sorted(expected["resources"].items()):
                row2 = self.db.execute(
                    "SELECT created_at, released_at FROM resources"
                    " WHERE run_id=? AND role=? AND container_id=?",
                    (run_id, role, cid)).fetchone()
                if row2 is None or dict(row2) != meta:
                    problems.append(f"{run_id}: resource {role}/{cid} disagrees")
            for name, meta in sorted(expected["artifacts"].items()):
                row = self.db.execute(
                    "SELECT path, sha256, bytes FROM artifacts"
                    " WHERE run_id=? AND name=?", (run_id, name)).fetchone()
                if row is None or dict(row) != meta:
                    problems.append(f"{run_id}: artifact {name} disagrees")
            seen_res = {(r["role"], r["container_id"]) for r in self.db.execute(
                "SELECT role, container_id FROM resources WHERE run_id=?",
                (run_id,))}
            for orphan in sorted(seen_res - set(expected["resources"])):
                problems.append(f"{run_id}: resource row {orphan} has no event")
            for tbl, key in (("pins", "key"), ("artifacts", "name")):
                seen = {r[key] for r in self.db.execute(
                    f"SELECT {key} FROM {tbl} WHERE run_id=?", (run_id,))}
                for orphan in sorted(seen - set(expected[tbl])):
                    problems.append(f"{run_id}: {tbl} row {orphan} has no event")
        extra = {r["run_id"] for r in self.db.execute("SELECT run_id FROM jobs")}
        for run_id in sorted(extra - set(projection)):
            problems.append(f"{run_id}: in jobs with no event chain")
        return (not problems), problems

    # Every projected field is replayed, not just `state`. The design's claim is
    # that an edit to a projected row is detectable; a state-only comparison
    # would let an edit to spec_json, source_sha, image_digest, exit_code, a
    # timestamp, the resource high-water mark, a pin or an artifact digest pass
    # verification -- which are exactly the fields a verdict is argued from.
    # EVERY column of `jobs`, lease fields included. Revision 2 omitted
    # lease_owner/lease_expires_at and left renew() unchained, so an edit to
    # either was undetectable. The non-authoritative set is empty; if a column
    # is ever removed from here, name it and say why, because an unexplained
    # omission reads as coverage.
    PROJECTED = ("kind", "lane", "state", "spec_json", "spec_hash", "source_sha",
                 "source_ref", "image_digest", "submitted_by_uid", "submitted_at",
                 "started_at", "finished_at", "hold_started_at",
                 "hold_deadline_at", "attempts", "lease_owner",
                 "lease_expires_at", "container_id", "exit_code", "error_class",
                 "wall_s", "rss_high_water_kb")
    NON_AUTHORITATIVE = ()   # intentionally empty

    @staticmethod
    def _replay(projection, row):
        """Apply one event. Event payloads carry the values they set, so the
        projection is reconstructible without reading `jobs` at all."""
        p = json.loads(row["payload_json"])
        rid = row["run_id"]
        if row["kind"] in ("SUBMITTED", "REFUSED"):
            projection[rid] = {"pins": {}, "artifacts": {}, "resources": {},
                               "_problems": [], **p["fields"]}
        elif rid in projection:
            job = projection[rid]
            if row["kind"] == "STATE":
                if job["state"] != p["from"]:
                    job["_problems"].append(
                        f"event seq {row['seq']} says from={p['from']},"
                        f" replay has {job['state']}")
                job.update(p["fields"])
            elif row["kind"] == "PIN":
                job["pins"][p["key"]] = p["value"]
            elif row["kind"] == "LEASE":
                # Assignment AND renewal both land here, so a lease edit is
                # detectable. An hour-long job renewing every ~5 min adds a few
                # dozen text rows; the alternative is a blind spot.
                job.update(p["fields"])
            elif row["kind"] == "ARTIFACT":
                job["artifacts"][p["name"]] = {"path": p["path"],
                                               "sha256": p["sha256"],
                                               "bytes": p["bytes"]}
            elif row["kind"] == "RESOURCE":
                # Keyed by (role, container_id): a run legitimately has several
                # containers per role over its life, and a release must match the
                # exact one it claims to release.
                key = (p["role"], p["container_id"])
                if p["op"] == "create":
                    job["resources"][key] = {"created_at": p["created_at"],
                                             "released_at": None}
                elif key in job["resources"]:
                    job["resources"][key]["released_at"] = p["released_at"]
                else:
                    # A release for a container that was never created. The row
                    # it claims to update does not exist, so no orphan check can
                    # see it -- only the replay can.
                    job["_problems"].append(
                        f"release of {key} that was never created"
                        f" (event seq {row['seq']})")

    # --- submission ------------------------------------------------------
    def _initial_fields(self, effective, *, run_id, uid, now, state,
                        source_ref=None, error_class=None):
        """Every PROJECTED column with its value at birth. The SUBMITTED event
        must carry the whole row: `_replay` seeds the projection from this
        payload alone, so an omitted column is a column verification cannot
        check."""
        return {
            "kind": effective["kind"],
            "lane": effective["lane"],
            "state": state,
            "spec_json": _canon(effective),
            "spec_hash": spec_mod.spec_hash(effective),
            "source_sha": effective["source_sha"],
            "source_ref": source_ref,
            "image_digest": None,
            "submitted_by_uid": uid,
            "submitted_at": now,
            "started_at": None,
            "finished_at": now if state == "REFUSED" else None,
            "hold_started_at": None,
            "hold_deadline_at": None,
            "attempts": 0,
            "lease_owner": None,
            "lease_expires_at": None,
            "container_id": None,
            "exit_code": None,
            "error_class": error_class,
            "wall_s": None,
            "rss_high_water_kb": None,
        }

    def _insert_job(self, run_id, fields, now, kind):
        cols = ", ".join(("run_id", *self.PROJECTED))
        marks = ", ".join("?" * (1 + len(self.PROJECTED)))
        self.db.execute(f"INSERT INTO jobs({cols}) VALUES({marks})",
                        (run_id, *(fields[c] for c in self.PROJECTED)))
        self._append(now, run_id, kind, {"fields": fields})

    def submit(self, effective, *, run_id, uid, now, source_ref=None):
        """One `jobs` row and one SUBMITTED event, in one transaction. A job
        that exists without an event is unauditable; an event without a job is
        a projection that never happened."""
        fields = self._initial_fields(effective, run_id=run_id, uid=uid, now=now,
                                      state="QUEUED", source_ref=source_ref)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._insert_job(run_id, fields, now, "SUBMITTED")
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return self.get(run_id)

    def refuse(self, effective, *, run_id, uid, now, error_class,
               source_ref=None):
        """A refusal is recorded, not discarded: REFUSED is a terminal state
        reachable from None, so the audit trail shows what was turned away."""
        fields = self._initial_fields(effective, run_id=run_id, uid=uid, now=now,
                                      state="REFUSED", source_ref=source_ref,
                                      error_class=error_class)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._insert_job(run_id, fields, now, "REFUSED")
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return self.get(run_id)

    # --- transitions -----------------------------------------------------
    def transition(self, run_id, to, *, now, fields=None, expect_from=None):
        """Move a job, refusing anything outside ALLOWED by raising.

        `fields` names extra columns this transition sets; each must be a
        PROJECTED column, because the column list becomes SQL and the payload
        becomes the projection. Closed world here too.
        """
        extra = dict(fields or {})
        bad = set(extra) - set(self.PROJECTED)
        if bad:
            raise IllegalTransition(f"not projected column(s): {sorted(bad)}")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT state FROM jobs WHERE run_id=?",
                                  (run_id,)).fetchone()
            if row is None:
                raise IllegalTransition(f"{run_id}: no such job")
            cur = row["state"]
            if expect_from is not None and cur != expect_from:
                raise IllegalTransition(
                    f"{run_id}: expected state {expect_from}, found {cur}")
            if to not in ALLOWED.get(cur, set()):
                raise IllegalTransition(
                    f"{run_id}: {cur} -> {to} is not an allowed transition")
            payload_fields = {"state": to, **extra}
            assigns = ", ".join(f"{c}=?" for c in payload_fields)
            self.db.execute(
                f"UPDATE jobs SET {assigns} WHERE run_id=? AND state=?",
                (*payload_fields.values(), run_id, cur))
            self._append(now, run_id, "STATE",
                         {"from": cur, "to": to, "fields": payload_fields})
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return self.get(run_id)

    def add_resource(self, run_id, *, role, container_id, now):
        """Record a container this run created. Transactional and chained, like
        every other mutation: revision 9 promised RESOURCE replay in the test
        list and shipped neither the method nor the event, so the table forced
        cleanup depends on was outside verification entirely.

        REFUSED unless the run is in `PHASE_ACTIVE_STATES`, and that refusal is
        load-bearing. `reclaim` can move a run out from under a phase that
        already holds its gate -- to FAILED when the recorded inventory was
        momentarily and legitimately empty (the candidate exited and `--rm` took
        its container), or to CLEANUP_BLOCKED when it never had one. Every
        mutation is serialised through the DB-owner thread, so the two cannot
        interleave inside a statement; but they can ARRIVE in that order, and a
        row inserted afterwards is the worst available shape:

          * FAILED -- the reservation and the lane are already freed, the phase
            then starts a real container, and a terminal job is invisible to
            `expired` (lease-active states only) and to `resolve_blocked`
            (CLEANUP_BLOCKED only), so nothing looks at it again;
          * CLEANUP_BLOCKED -- `resolve_blocked` is already confirming this run's
            absence, so it sees the new row, finds the deterministic name not yet
            bound, releases it as gone and finishes the job. The phase then
            creates and starts the container, and `release_hold` finds nothing
            to veto on.

        Both end the same way: the mutex closed over live work. `not terminal`
        is therefore the wrong test -- CLEANUP_BLOCKED is not terminal, and it is
        precisely a state where no new workload may appear.

        Refusing HERE is what makes that unreachable rather than merely
        unlikely: containers are recorded before they can exist, so a refused
        record means no container is ever created. The check and the insert are
        in one transaction in the single writer thread, which is the only place
        this ordering can be decided.
        """
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT state FROM jobs WHERE run_id=?",
                                  (run_id,)).fetchone()
            state = None if row is None else row["state"]
            if state not in PHASE_ACTIVE_STATES:
                raise WorkNotPermitted(
                    f"{run_id} is {state or 'unknown'}, not one of"
                    f" {PHASE_ACTIVE_STATES}; refusing to record a {role}"
                    " container for a run that may not start new work")
            self.db.execute(
                "INSERT INTO resources(run_id, role, container_id, created_at)"
                " VALUES(?,?,?,?)", (run_id, role, container_id, now))
            self._append(now, run_id, "RESOURCE",
                         {"op": "create", "role": role,
                          "container_id": container_id, "created_at": now})
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def release_resource(self, run_id, *, role, container_id, now):
        """Mark a resource released. Called only after Docker has POSITIVELY
        confirmed the container is stopped or absent -- the release record is a
        claim about reality, and an unconfirmed one is how the mutex leaks."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                "UPDATE resources SET released_at=? WHERE run_id=? AND role=?"
                " AND container_id=?", (now, run_id, role, container_id))
            self._append(now, run_id, "RESOURCE",
                         {"op": "release", "role": role,
                          "container_id": container_id, "released_at": now})
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def resources_for(self, run_id, *, unreleased_only=False):
        """The recorded container inventory. Forced cleanup and restart recovery
        read THIS, not `docker ps`: a stopped container is invisible to a label
        query and an ephemeral builder never carried a label."""
        sql = ("SELECT role, container_id, created_at, released_at FROM resources"
               " WHERE run_id=?")
        if unreleased_only:
            sql += " AND released_at IS NULL"
        return [dict(r) for r in self.db.execute(sql + " ORDER BY created_at,"
                                                 " role, container_id", (run_id,))]

    def peek(self, lane):
        """The head of a lane's queue, without changing anything.

        Admission (memory budget, and the training lock for heavy) is taken
        BETWEEN peek and dequeue. The first revision dequeued first and pushed
        the job back to QUEUED when the flock failed, which the state table
        forbids -- an ordinary contention would have raised.
        """
        return self.db.execute(
            "SELECT * FROM jobs WHERE lane=? AND state='QUEUED'"
            " ORDER BY submitted_at, run_id LIMIT 1", (lane,)).fetchone()

    def dequeue(self, lane, *, owner, now, lease_expires_at, hold_deadline_at,
                max_running):
        """Atomically lease the head of `lane`. Returns the POST-update row, or
        None if the lane is full or the queue emptied under us -- in which case
        the caller releases the admission it took and re-peeks.

        `lease_expires_at` is an absolute UTC instant supplied by the caller,
        not a duration: storing the duration (as the first revision did) makes
        every lease malformed and every reclaim decision arbitrary.
        """
        self.db.execute("BEGIN IMMEDIATE")
        try:
            busy = self.db.execute(
                "SELECT COUNT(*) c FROM jobs WHERE lane=? AND state IN"
                f" ({','.join('?' * len(ADMITTED_STATES))})",
                (lane, *ADMITTED_STATES)).fetchone()["c"]
            if busy >= max_running:
                self.db.execute("COMMIT")
                return None
            row = self.db.execute(
                "SELECT * FROM jobs WHERE lane=? AND state='QUEUED'"
                " ORDER BY submitted_at, run_id LIMIT 1", (lane,)).fetchone()
            if row is None:
                self.db.execute("COMMIT")
                return None
            # The event must carry EVERY column this transaction changes.
            # Revision 3 listed only state and attempts while the UPDATE also
            # set lease_owner and lease_expires_at -- and verify_chain now
            # compares those, so the FIRST dequeue would have reported a
            # disagreement. A projection is only as good as its payloads.
            fields = {"state": "LEASED", "attempts": row["attempts"] + 1,
                      "lease_owner": owner,
                      "lease_expires_at": lease_expires_at,
                      "hold_started_at": now,
                      "hold_deadline_at": hold_deadline_at}
            self.db.execute(
                # hold_started_at/hold_deadline_at are written HERE, not merely
                # announced in the event payload. Revision 7 put them in the
                # payload only, so the columns stayed NULL: verify_chain would
                # have disagreed on the very first dequeue, and restart recovery
                # would have had no deadline to restore.
                "UPDATE jobs SET state='LEASED', lease_owner=?, lease_expires_at=?,"
                " hold_started_at=?, hold_deadline_at=?,"
                " attempts=attempts+1 WHERE run_id=? AND state='QUEUED'",
                (owner, lease_expires_at, now, hold_deadline_at, row["run_id"]))
            self._append(now, row["run_id"], "STATE",
                         {"from": "QUEUED", "to": "LEASED", "owner": owner,
                          "fields": fields})
            fresh = self.db.execute("SELECT * FROM jobs WHERE run_id=?",
                                    (row["run_id"],)).fetchone()
            self.db.execute("COMMIT")
            return dict(fresh)
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def renew(self, run_id, *, owner, lease_expires_at, now):
        """Ownership-checked lease renewal. A job may run up to TIMEOUT_MAX
        (3600 s); any sane lease is shorter, so without renewal the reclaimer
        eats live work.
        Returns False if the caller no longer owns the lease.

        Chained, like every other mutation: revision 2 updated `jobs` here with
        no event, which put lease_expires_at outside verification entirely.
        """
        self.db.execute("BEGIN IMMEDIATE")
        try:
            cur = self.db.execute(
                "UPDATE jobs SET lease_expires_at=? WHERE run_id=? AND lease_owner=?"
                f" AND state IN ({','.join('?' * len(LEASE_ACTIVE_STATES))})",
                (lease_expires_at, run_id, owner, *LEASE_ACTIVE_STATES))
            if cur.rowcount == 1:
                self._append(now, run_id, "LEASE",
                             {"fields": {"lease_owner": owner,
                                         "lease_expires_at": lease_expires_at}})
            self.db.execute("COMMIT")
            return cur.rowcount == 1
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def adopt(self, run_id, container_id, *, owner, lease_expires_at, now):
        """Reconcile a live run after a dispatcher restart: keep it where it is,
        take ownership of the lease and extend it. NOT a transition -- the run
        never stopped, and inventing a state change would put a fiction in the
        chain."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT state FROM jobs WHERE run_id=?",
                                  (run_id,)).fetchone()
            if row is None or row["state"] not in LEASE_ACTIVE_STATES:
                self.db.execute("COMMIT")
                return None
            fields = {"lease_owner": owner, "lease_expires_at": lease_expires_at,
                      "container_id": container_id}
            self.db.execute(
                "UPDATE jobs SET lease_owner=?, lease_expires_at=?, container_id=?"
                " WHERE run_id=?", (owner, lease_expires_at, container_id, run_id))
            self._append(now, run_id, "LEASE", {"fields": fields})
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return self.get(run_id)

    def expired(self, now):
        """Lease-active jobs whose lease has lapsed. Expiry is a REASON TO ASK
        Docker, not a verdict: on an hour-long job, reaping on expiry alone is a
        guaranteed false positive."""
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM jobs WHERE lease_expires_at IS NOT NULL"
            " AND lease_expires_at <= ? AND state IN"
            f" ({','.join('?' * len(LEASE_ACTIVE_STATES))})"
            " ORDER BY submitted_at, run_id", (now, *LEASE_ACTIVE_STATES))]

    def reclaim(self, now, *, probe, owner=None, lease_expires_at=None):
        """Reap crashed runs, and only crashed runs.

        For each expired job, ask Docker about EVERY container in its `resources`
        row -- candidate and handoff alike -- rather than about one labelled
        container, since a BUILDING job may have no container at all and a
        stopped one is invisible to `docker ps`.

        `probe(container_id)` is **tri-state** and that is the whole point:
        `True` alive, `False` POSITIVELY absent-or-stopped, `None` Docker did not
        answer. `None` is never "stopped". An earlier version of this method
        collected `[r for r in res if probe(r)]`, where a falsy `None` fell
        through to `FAILED` -- terminating the job, dropping its reservation out
        of ADMITTED_STATES and releasing admission on no evidence at all, while
        the container it could not ask about was possibly still running. That is
        the exact failure the confirm-before-release rule exists to prevent, and
        the design states the invariant applies to `reclaim` as much as to
        forced cleanup.

        Any resource alive     -> adopt and renew.
        Past the HOLD DEADLINE with anything recorded that is not confirmed gone
          -> CLEANUP_BLOCKED, whatever the probe said. This outranks both of the
          branches below it: renewing a run nobody drives, and re-asking about
          one for ever, are the same stall. A settled absence is exempt because
          it has a better answer available -- release and FAIL, below.
        Any resource UNKNOWN   -> no transition at all; the reaper asks again.
        All positively absent   -> release the rows, then FAILED
                                   (`error_class='reclaimed'`), logs preserved
          -- UNLESS a row's create was never acknowledged and its settle instant
          has not passed, which counts as unknown here. This method has a probe,
          not a Docker client, so it cannot remove anything; it can only wait,
          and it is re-run every reap interval, so the instant does arrive. The
          runner's confirmation path is what actively removes.
        Resource set EMPTY      -> nothing was ever confirmed, so this is not a
          free pass (revision 12). A LEASED job had not started anything yet and
          the state itself is the confirmation, so it fails; a BUILDING or
          RUNNING job goes to CLEANUP_BLOCKED and keeps its reservation until
          something positive is known.

        Returns the list of (run_id, outcome) it decided.
        """
        decided = []
        for job in self.expired(now):
            rid = job["run_id"]
            res = self.resources_for(rid, unreleased_only=True)
            pins = self.pins_for(rid)
            states = [(r, probe(r["container_id"])) for r in res]
            alive = [r for r, v in states if v is True]
            unknown = [r for r, v in states if v is None]
            # An absence that is not yet believable is not evidence, so it joins
            # the unknowns rather than counting towards a release.
            unknown += [r for r, v in states if v is False
                        and not absence_believable(pins, r["role"], now)]

            deadline = job["hold_deadline_at"]
            past_deadline = bool(deadline) and now >= deadline
            if past_deadline and (alive or unknown):
                # THE LEASE MAY BE EXTENDED; THE HOLD MAY NOT -- and that is
                # true of the ASKING as much as of the renewing, which is why
                # this sits above every probe outcome instead of inside one.
                #
                # An expired lease means whatever was renewing it stopped, so
                # nothing in this process is driving the run. Two ways that used
                # to become permanent: adopting a live container renewed it, and
                # every later sweep found the same container and renewed it
                # again; and an UNANSWERED probe left the job RUNNING, where
                # `resolve_blocked` (which only looks at CLEANUP_BLOCKED) never
                # saw it and `force-release` refused it for not being
                # CLEANUP_BLOCKED -- a stall with no operator escape at all,
                # which is worse than the one it replaced.
                #
                # So past the deadline, a job with a recorded inventory goes to
                # CLEANUP_BLOCKED whatever Docker said: alive, unknown, or
                # absent-but-not-yet-settled. That is still fail-closed -- the
                # state keeps the lock, the lane and the reservation -- and it is
                # what puts the job in front of both the automatic cleanup path
                # and the manual one.
                if "CLEANUP_BLOCKED" in ALLOWED.get(job["state"], set()):
                    # Where a container that must DIE goes: the reaper's
                    # resolve_blocked path has a Docker client, unlike this
                    # method, so it kills and confirms.
                    self.transition(
                        rid, "CLEANUP_BLOCKED", now=now,
                        fields={"error_class": "hold_deadline_expired"})
                    decided.append((rid, "deadline_expired"))
                else:
                    # No edge from here (a container is only recorded while
                    # RUNNING, so this is unreachable today). Hold rather than
                    # invent a transition, and say so every sweep.
                    decided.append((rid, "unconfirmed"))
                continue
            if alive:
                self.adopt(rid, alive[0]["container_id"], owner=owner,
                           lease_expires_at=lease_expires_at or
                           job["lease_expires_at"], now=now)
                decided.append((rid, "adopted"))
                continue
            if unknown:
                # Docker did not answer. Leave the job exactly where it is,
                # holding everything, and let the next sweep ask again. Doing
                # anything else here would be acting on an absence of evidence.
                decided.append((rid, "unconfirmed"))
                continue
            if res:
                # Every recorded container is POSITIVELY gone, so the release
                # records below are claims backed by evidence.
                for r, _ in states:
                    self.release_resource(rid, role=r["role"],
                                          container_id=r["container_id"],
                                          now=now)
                self.transition(rid, "FAILED", now=now,
                                fields={"error_class": "reclaimed",
                                        "finished_at": now})
                decided.append((rid, "reclaimed"))
                continue
            if job["state"] == "LEASED":
                self.transition(rid, "FAILED", now=now,
                                fields={"error_class": "reclaimed",
                                        "finished_at": now})
                decided.append((rid, "reclaimed"))
            else:
                self.transition(rid, "CLEANUP_BLOCKED", now=now,
                                fields={"error_class": "unconfirmed"})
                decided.append((rid, "cleanup_blocked"))
        return decided

    # --- side tables -----------------------------------------------------
    def settle_unissued_creates(self, run_id, *, settles_at, now, role=None):
        """Convert `ABSENCE_NOT_YET_ISSUED` pins on this run into a settle
        instant -- one role, or all of them when `role` is None. Returns the
        roles converted.

        FOR THE TWO PLACES A SENTINEL LOSES ITS OWNER, and the reason is exactly
        what the sentinel means. `ABSENCE_NOT_YET_ISSUED` says "a phase holds
        this run's gate and has not asked Docker yet", so elapsed time proves
        nothing and `absence_believable` refuses it no matter how long it has
        been there. That is correct while such a phase exists -- and a lie the
        moment there is no longer a phase that can ask. Then a sentinel left
        behind would refuse EVERY absence for ever: cleanup could never confirm,
        the job would sit in CLEANUP_BLOCKED, and the lock, the lane and the
        reservation would stay held until an operator ran `force-release`.

        The owner ends either when the phase abandons the create
        (`Runner._unacked_create`, by role) or when the process dies with the pin
        still up (`Recovery._settle_unissued`, every role of every re-adopted
        run). Only the sentinel is touched: an instant was written by an ANSWER,
        so its window is already elapsing, and rewriting it on each pass would
        mean a retry or a crash loop never settles anything.

        An instant, not a clear: a crash cannot distinguish "the create was never
        issued" from "it was issued and the answer died with the client", and in
        the second case the daemon may still bind the name. So the ambiguity is
        kept -- as the bounded kind, which repeated removal terminates -- rather
        than resolved by assumption in the direction that releases a mutex.

        The window starts NOW rather than from whenever the pin was written: the
        instant is a bet about a request that may be in flight at this moment,
        and the time qfd spent down is not time anything was watching.
        """
        pins = self.pins_for(run_id)
        prefix = absence_settles_pin("")
        roles = sorted(k[len(prefix):] for k, v in pins.items()
                       if k.startswith(prefix) and v == ABSENCE_NOT_YET_ISSUED
                       and role in (None, k[len(prefix):]))
        for r in roles:
            self.set_pin(run_id, absence_settles_pin(r), settles_at, now=now)
        return roles

    def set_pin(self, run_id, key, value, *, now):
        """`pins` takes new keys without a schema change, so 2b and 2c add
        provenance without a migration."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                "INSERT INTO pins(run_id, key, value) VALUES(?,?,?)"
                " ON CONFLICT(run_id, key) DO UPDATE SET value=excluded.value",
                (run_id, key, value))
            self._append(now, run_id, "PIN", {"key": key, "value": value})
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def add_artifact(self, run_id, *, name, path, sha256, bytes_, now):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                "INSERT INTO artifacts(run_id, name, path, sha256, bytes)"
                " VALUES(?,?,?,?,?)"
                " ON CONFLICT(run_id, name) DO UPDATE SET path=excluded.path,"
                " sha256=excluded.sha256, bytes=excluded.bytes",
                (run_id, name, path, sha256, bytes_))
            self._append(now, run_id, "ARTIFACT",
                         {"name": name, "path": path, "sha256": sha256,
                          "bytes": bytes_})
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    # --- admission -------------------------------------------------------
    def admitted_mem_mb(self):
        """Sum of reservations over everything admitted. Derived from `jobs`
        rather than tracked separately, so it cannot drift from the states that
        actually hold the memory.

        A run may carry a `reservation_override_mb` pin, and if so the charge is
        `max(stored, override)`. Recovery needs that: where a live container's
        own cap EXCEEDS the reservation derived from the stored spec, the design
        says take the larger -- and "take" has to mean charged, not logged.
        Logging it and charging the smaller figure would admit work the real
        reservation excludes, which is the same arithmetic hole as trusting the
        container's cap in the first place.
        """
        total = 0
        for row in self.db.execute(
                "SELECT j.spec_json AS spec_json, p.value AS override"
                " FROM jobs j LEFT JOIN pins p"
                "   ON p.run_id = j.run_id AND p.key = 'reservation_override_mb'"
                " WHERE j.state IN"
                f" ({','.join('?' * len(ADMITTED_STATES))})", ADMITTED_STATES):
            eff = json.loads(row["spec_json"])
            charge = reservation_mb(eff["mem_limit"], self.image_build_mem_mb)
            if row["override"] is not None:
                try:
                    charge = max(charge, int(row["override"]))
                except (TypeError, ValueError):
                    pass          # a malformed pin must not lower the charge
            total += charge
        return total

    def admit(self, mem_limit, *, free_disk_mb=None):
        """Can a job of this size be admitted right now? Returns (ok, reason).

        Two independent boundaries (design D10, §4.5): the aggregate memory
        budget, and free space on the runs filesystem. Free space is passed in
        rather than measured here so this module stays free of I/O -- the caller
        owns the statvfs.
        """
        want = reservation_mb(mem_limit, self.image_build_mem_mb)
        have = self.admitted_mem_mb()
        if have + want > self.mem_budget_mb:
            return False, (f"memory budget: {have}m admitted + {want}m requested"
                           f" exceeds {self.mem_budget_mb}m")
        if free_disk_mb is not None:
            allowance = self.out_quota_mb + self.artifact_cap_mb
            if free_disk_mb < self.disk_floor_mb + allowance:
                return False, (f"disk floor: {free_disk_mb}m free is below"
                               f" {self.disk_floor_mb}m + {allowance}m allowance")
        return True, ""

    # --- reads -----------------------------------------------------------
    def queued_count_for_uid(self, uid):
        return self.db.execute(
            "SELECT COUNT(*) c FROM jobs WHERE submitted_by_uid=?"
            " AND state='QUEUED'", (uid,)).fetchone()["c"]

    def lane_busy(self, lane):
        return self.db.execute(
            "SELECT COUNT(*) c FROM jobs WHERE lane=? AND state IN"
            f" ({','.join('?' * len(ADMITTED_STATES))})",
            (lane, *ADMITTED_STATES)).fetchone()["c"]

    def pins_for(self, run_id):
        """Every pin for a run, as a plain dict. `pins` is key/value precisely so
        that later phases add provenance without a migration."""
        return {r["key"]: r["value"] for r in self.db.execute(
            "SELECT key, value FROM pins WHERE run_id=?", (run_id,))}

    def get(self, run_id):
        row = self.db.execute("SELECT * FROM jobs WHERE run_id=?",
                              (run_id,)).fetchone()
        return None if row is None else dict(row)

    def list(self, *, state=None, lane=None, limit=100):
        sql = "SELECT * FROM jobs"
        where, params = [], []
        if state is not None:
            where.append("state=?")
            params.append(state)
        if lane is not None:
            where.append("lane=?")
            params.append(lane)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY submitted_at DESC, run_id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.db.execute(sql, params)]
