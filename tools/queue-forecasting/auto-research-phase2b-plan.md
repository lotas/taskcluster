# Auto-Research Loop Phase 2b Implementation Plan — the data plane

Design: `auto-research-phase2-design.md` §2 (the 2b row), D4, D6, D12, D14, §4.6.
Parent: `auto-research-loop-design.md` §3.4, §7, §8.5. Predecessor:
`auto-research-phase2a-plan.md` (the spine this builds on, delivered and
evidenced at fault-gates 32/0 and nc-suite 86/0).

Revision 8, 2026-08-28: the credential check was wrong a THIRD time, and the
third time is the one worth recording, because the first two were fixes to the
wrong thing.

Observed on the host: `0440`, uid 0, **gid 0** — and the gate READ IT
SUCCESSFULLY, since no "cannot read" problem appeared alongside. A uid-997
process reading a `0440 root:root` file is not something the mode and owner can
explain; the likely mechanism is an ACL, systemd granting the service user read
access with `setfacl` while the classic bits stay root-owned.

**So for a systemd-delivered credential the DAC bits are not the access control,
and a DAC-based assertion about it cannot be right in principle — not merely
wrong in its constants.** Revision 5 demanded 0600-owned-by-us; revision 7
allowed root ownership but demanded our own group. Both were adjustments to a
rule whose premise was false, which is why each one refused the same correct host
in a new way.

The rule now depends on **provenance**:

- From `$CREDENTIALS_DIRECTORY`, the confinement is systemd's — a per-service
  ramfs in a private mount namespace, plus whatever ACL it applies. Re-deriving
  that from the mode means encoding a model of systemd's implementation, and this
  check has now been wrong about that model twice. The one assertion kept is the
  one that survives any version: **no `other` bits**. World-readable is
  world-readable whatever the ACL says.
- From `QFX_DSN_FILE` — the development path, no systemd involved — the DAC bits
  ARE the control, so the strict rule stands: no `other` bits, group bits only
  for our own group, owner root or us.

The source file's permissions (`/etc/qf-extract/dsn`, 0600 root:root) are checked
by `phase2b-setup.sh`. That split is deliberate: the script checks the source, the
gate checks what arrived, and neither pretends to see the other's half.

Every round of this bug had the same root cause: **a fixture that built the
credential the convenient way, so the gate's real path never met the real
shape.** There is now an integration test that drives `check_startup` end to end
with the mode, owner and group the host actually reports — not just a unit test
of the helper.

Revision 7, 2026-08-28: the service met a real systemd and the startup gate was
wrong about it. Two defects, both mine, both found by running it:

- **The credential check refused a correctly configured host.** It asserted "mode
  0600 or stricter, owned by us". `LoadCredential=` does not produce that: systemd
  mounts the credential directory as a root-owned ramfs and writes the file
  **`0440 root:<service group>`**, so the service reads it through its GROUP and
  root stays the owner. The gate reported two precondition failures on a host
  configured exactly right.

  **A fail-closed check that fails on the good case is worse than no check**: it
  blocks the working configuration and it teaches whoever is debugging it to
  loosen the gate. The rule is now what it should always have been — *nothing
  outside this service may read it*: no `other` bits at all; if group bits are
  set, the group must be our own; owner must be root or us. That accepts what
  systemd produces and still refuses `0440 root:qfd`, which a mode-only check
  would have passed while handing the DSN to the one process D15 excludes.

  The mistake underneath was in the FIXTURE: it created the credential as the
  test user at 0600 — the development path — so the production arrangement was
  never exercised. **Fourth time this phase that a fake has been shaped more
  conveniently than the real thing** (ISO strings for datetimes, ints for
  doubles, first-batch schema inference, and now this). The positive case is now
  the first test in the class.

- **Exiting non-zero at startup is a HANG, not a refusal.** With socket
  activation systemd accepts the client's connection, starts the service, the
  service exits 2, and the client blocks on `recv` for ever while
  `Restart=on-failure` loops — observed at restart counter 15, with a `ping` that
  had to be interrupted by hand.

  "Refuses to start" and "fail-closed" are not the same thing. Nothing can be
  extracted either way, because every op refuses while `problems` is non-empty;
  the difference is that the caller is now TOLD. Same lesson as 2a's "the
  dispatcher closed the connection without replying". `main()` now serves
  refusals instead of returning 2, and a test asserts `return 2` is gone from it.

  The cost, stated in the code: `systemctl status` reads green on a misconfigured
  host. The journal carries ERROR lines and `ping` reports `ready: false` with the
  reasons, which is where someone debugging this will be looking.

Suites: shared 53, extractor 171, dispatcher 539.

Revision 6, 2026-08-28: `phase2b-setup.sh install` succeeded on the host —
`qfextract` uid 997 in none of the forbidden groups, socket `660 root:qfd`,
credential `600 root:root`, venv importing all three modules. Two follow-ups, one
of them a predicted defect fixed before it could be observed:

- **`env/uv.lock` was generated on the host and is not in the repository.**
  pyarrow 25.0.1, psycopg 3.3.4, CPython 3.13.5. It must be copied back and
  committed; until then one host has a lock and the repo does not, which is the
  situation `env/README.md` warns about.
- **The bound parameters would have failed every windowed query.** Every window
  bound travels as an ISO-8601 *string*, because it must be JSON-serialisable —
  it goes into `request_hash` and into each file's recorded `window`. But psycopg
  **3** sends a Python `str` as PostgreSQL `text`, and `timestamptz >= text` is
  not an operator: `"operator does not exist: timestamp with time zone >= text"`.
  psycopg **2** sent untyped literals and let PostgreSQL coerce from context, so
  the string form worked there — a documented migration gotcha, and precisely the
  class of thing that only surfaces the first time real code meets a real driver.

  Fixed with `inventory.bind_values`, which converts to `datetime` for the driver
  while the record keeps the string. The conversion lives in `inventory.py` rather
  than in `pg.py` because a pure function is one a test can reach — `pg.py` is not
  importable without psycopg, and putting it there would have made the fix as
  unverifiable as the bug. 7 new tests; extractor suite 158 → 165.

  **This was predicted, not observed.** It is recorded that way on purpose: the
  reasoning was psycopg's documented type handling, not a traceback.

Revision 5, 2026-08-28: review round on Task 4. Seven findings, five P1.

- **P1: the installed service could not have started.**
  `ExecStart=/usr/bin/python3 service.py` — and the host python has neither
  `pyarrow` nor `psycopg` (Phase 2a needs neither, since `qfd` is stdlib-only by
  D6), while `extract_spec` lived in `dispatcher/` with nothing putting it on the
  path. **The tests hid both by inserting `sys.path` themselves.** Revision 1's
  claim that the extractor "needs no new dependency closure" was wrong: those
  manifests build the trainer IMAGE, and there is no host environment anywhere in
  2a. Fixed three ways: a new `host/shared/` holding `extract_spec.py` (both units
  put it on `PYTHONPATH`; the alternative — pointing at `dispatcher/` — would make
  `qfd.py` and `store.py` importable by the extractor for the sake of one shared
  module); a new `env/pyproject.toml` with `pyarrow` and `psycopg` only, built by
  the installer with `uv`; and an ExecStart naming the venv's interpreter. The
  installer now *proves* the interpreter can import all three before declaring
  success. **`env/uv.lock` is not in the repository** — `uv` is unavailable here —
  and `env/README.md` says so rather than leaving it to be discovered.
  Consequence: `shared/extract_spec.py` no longer subclasses `spec.SpecError`,
  because that would make `shared` depend on `dispatcher`. A one-way dependency is
  worth more than a tidy exception hierarchy; the cost is that `qfd` catches both
  on its refusal path (Task 5).
- **P1: streaming Parquet corrupted valid data.** The schema was inferred from the
  first batch, which is a schema derived from an arbitrary subset. An all-NULL
  first batch infers `null` and the first real timestamp later raises
  `ArrowNotImplementedError` — and `started_at` is NULL for every still-pending
  run, so that is Tuesday, not a corner case. Worse, `tags` is JSONB: inference
  builds a STRUCT from the first batch's keys and **silently drops** keys that
  first appear later (`{"retries": "2"}` observed becoming `{"kind": null}`). A
  dropped tag is a feature a candidate cannot see and cannot know it cannot see.
  Fixed with declared types in `inventory.py`, read off the live DDL — which also
  caught that `wait_duration_s`/`run_duration_s` are `DOUBLE PRECISION` while my
  fake used ints — and `tags` written as `json_text`, the raw JSON, which
  preserves arbitrary keys by construction and is what the trainer already parses
  `tags.*` out of.
- **P1: the publication fix changed a public contract and left the docs behind.**
  D20 still specified `<extract_hash>/` directories and D21 still mounted
  `/extract/<extract_hash>`. Both amended: the directory is named by
  `request_hash`, `extract_hash` is a manifest field, and resolving one to a
  directory is a read of the manifests rather than a path join. An atomicity fix
  that leaves half a contract behind is a fix that a later session implements
  against the old half.
- **P1: `discover` put the DSN in `psql`'s argv**, where any user on the host can
  read it out of `/proc` — a setup script leaking the credential in order to
  defend the claim that one process holds it. The live-role check moved into
  `service.py`'s `probe_database`, which is the one process legitimately holding
  the DSN, and `ping` now reports `ready: false` with the reason. `discover` also
  accepted a 0600 **qfd:qfd** credential as good: mode alone is not a boundary,
  since "owner-only" is only meaningful once you know the owner. Now requires
  `root:root`.
- **P1: arbitrary dependency exceptions were returned to `qfd`.** `str(e)` for
  everything, on the reasoning that nothing in this codebase puts a DSN in an
  exception — true of this codebase and **unenforceable about psycopg**, whose
  connection errors quote the conninfo, to the one process that must never see
  it. Now: our own refusal types keep their text (they were written to be read);
  everything else returns an opaque message with an 8-hex reference that appears
  in the journal. **An assertion about every dependency's future error text is not
  a control.**
- **P2: the installer was not fail-closed.** `set -Eeuo pipefail` with an ERR trap
  naming the line, and positive confirmation that the socket is active and the
  socket file exists — `enable --now` returning 0 is not the same as listening.
- **P2: the gate did not check the database.** Files and local configuration only,
  so a perfect credential and an unreachable cluster passed and failed at the
  first extraction. `probe_database` runs at startup and its result is part of
  `ping`'s answer, so the NC17 canary cannot pass while nothing can be extracted.

Suites: shared 53, extractor 158, dispatcher 539.

Revision 4, 2026-08-28: review round on Task 3. Six findings, four of them P1,
and every one was a case of the same thing: **the fake was easier to satisfy than
the real system.**

- **P1: real timestamps crashed the watermark.** The merge kept the first value
  native and compared later ones against `str(value)`, so a second batch raised
  `TypeError: '>' not supported between instances of 'str' and
  'datetime.datetime'`. psycopg returns `datetime`/`date`; the fake returned ISO
  strings, so 44 tests passed over a crash. Fixed by comparing natively and
  stringifying once, in `_Watermark`. **The fake now uses production types
  throughout** -- that single divergence was the whole bug, and a fake whose
  types are more convenient than the real thing's is a hole in the coverage, not
  a simplification.
- **P1: the extractor did not enforce its own boundary.** `run` took a
  pre-validated mapping, hashed whatever it was given, and never read its
  injected clock. So D16's independent validation and the extractor-authoritative
  settlement lag did not exist, and a caller could supply forged `ref_lower` /
  `window_lower` and bypass the 120-day scan ceiling. `run` now takes a RAW
  request and calls `extract_spec.validate(..., now=self.clock(),
  settlement_lag_s=self.settlement_lag_s)` itself. **An injected dependency
  nothing consults is a claim nothing backs** -- the unused `clock` parameter was
  the visible symptom and I did not read it as one.
- **P1: publication and discoverability were two acts.** Renaming staging to
  `<extract_hash>/` and then writing a side index left a window in which the
  artifact existed and could not be found -- and the retry took a fresh snapshot,
  got a different `extract_hash`, and published a SECOND artifact for the same
  request. Fault injection reproduced exactly that. Publication is now a single
  atomic rename into `<request_hash>/` and the index is gone: the artifact and
  its discoverability are the same act, so there is nothing to die between.
- **P1: the large reads were materialised several times over.** `fetchall()`,
  then a Python list per column, then a file-sized row group. Against a 4 GiB
  output allowance that is a resource bound nobody declared. The seam is now
  batched end to end -- a server-side cursor with `fetchmany`, a `ParquetWriter`
  fed batch by batch, and row counts and watermarks accumulated incrementally.
  A test asserts the batch size changes nothing about the manifest.
- **P2: the write canary accepted unrelated failures as proof.**
  `except psycopg.Error: return False` treats a connection failure as "the write
  was refused". Worse, the canary itself was wrong: `phase0-setup.sh` revokes ALL
  on the database from PUBLIC and grants back only CONNECT, so `CREATE TEMP
  TABLE` fails on *privileges* and TEMP is a distinct privilege in PostgreSQL's
  GRANT model. The canary is now `UPDATE ... WHERE false`, which is harmless if
  it succeeds and is refused by *either* control; `read_only` (25006) and
  `insufficient_privilege` (42501) both count, and anything else aborts. Note
  what this changed conceptually: with a SELECT-only role, **no write attempt can
  isolate read-onlyness** -- both controls refuse everything -- so what the canary
  actually asserts is "this role cannot write", and the grant is the stronger
  half. When I first fixed this it had no test; reverting the check produced zero
  failures. Now it has four.
- **P2: the claimed serialisation determinism did not exist.** No query has an
  `ORDER BY`, so the same row set can serialise to different bytes. The
  guarantee is **withdrawn** rather than implemented, because nothing depends on
  it: NC18's byte-identical reuse holds because the bytes are not regenerated,
  and adding `ORDER BY` over a months-long window invites the external sort that
  D23 exists to bound. The docstring now says so, so nobody re-adds the sort to
  restore a property nothing needs.

Suites: extractor 91, dispatcher 592.

Revision 3, 2026-08-28: review round on Tasks 1 and 2, four findings, one of
them a correctness bug in delivered code.

- **P1: `qctx_runs` was a SUBSET, not a superset.** `ref_lower` was derived as
  `train_start - lookback_days` and the pending-overlap predicate compared
  against `train_start`. The trainer calls that query as
  `load_task_runs_for_queue_context(c, w.train_start - 90m, ..)` and derives its
  reference floor from the *shifted* start, so both bounds were 90 minutes late:
  the floor excluded older reference runs, and the overlap predicate dropped any
  run that exited between `train_start - 90m` and `train_start`. Nothing would
  have failed -- the extract would simply have carried fewer reference runs and
  the model trained on it would have been slightly different. Fixed by deriving
  a trusted `window_lower` and hanging both bounds off it, with a regression test
  that spells out the trainer's three prefixes (qctx 90m, worker_counts **120m**
  once you follow the call, throughput 90m) and asserts the extract is never
  later than any of them.
- **P2: the window itself was unbounded.** `lookback_days` was bounded and
  `train_start`/`as_of_date` were independent, so `2010..2026` validated -- the
  full-history scan the lookback bound exists to prevent. **Bounding a part is
  not bounding the whole.** `MAX_WINDOW_DAYS = 120` against a largest promoted
  config of 36 days (`run_duration.yaml`: lookback 30 + validation 1 + holdout
  5).
- **P2: valid JSON shapes escaped the typed refusal path.** A list or dict
  `target` raised `TypeError("unhashable type")` from the membership test, and a
  year-0001 `train_start` raised `OverflowError` from the date arithmetic. A
  validator that raises `TypeError` has not refused a request, it has crashed on
  it: the caller gets a traceback instead of a reason and the dispatcher's
  refusal path never runs. Fixed with an `isinstance` before the membership test,
  a `MIN_TRAIN_START` floor, and a wrapped derivation; plus a sweep test that
  pushes 14 shapes through all six fields and fails on any exception outside the
  family.
- **P2: NC18 still stated the superseded watermark rule** in both control tables,
  and D19 still claimed the watermark *closes* the parent §7 hole. Aligned: the
  watermark **records** it, immutability **closes** it. Left uncorrected, a future
  session would have implemented the obsolete control.

One structural change fell out of the P1 fix: **all window derivation moved into
`extract_spec`**, so `inventory.bindings()` does no arithmetic and holds no
constants. Two reasons -- a constant duplicated across the dispatcher and the
extractor is one that gets updated in one of them, and a bound derived privately
inside the extractor would not be in `request_hash`, which would make D20's
immutable reuse quietly wrong. A new cross-domain test asserts every parameter
`inventory` declares is produced by a real validated request; the test reaches
across the two domains, the code does not.

Suites: extractor 36, dispatcher 592.

Revision 2, 2026-08-27: the three open questions answered, two of which changed
the plan. **D20 was rewritten wholesale** — "reuse on an unchanged watermark" does
not work, because a late event can update a row inside an already-extracted window
without moving either watermark maximum, so reproducibility now rests on
immutability and the watermark is provenance only. **D23 is new**:
`temp_file_limit` is already set (revision 1 said otherwise, wrongly) but it is a
*per-process* bound that parallel workers multiply, so
`max_parallel_workers_per_gather = 0` becomes load-bearing. Task 6 stops trying to
create a role Phase 0 already creates, and proves the
`pg_current_snapshot()` privilege instead of granting it. Changes: D17, D20, D23
(new), Task 6, NC17b (new), NC18, verified facts 2 and 11, §9.

Revision 1, 2026-08-27: first draft, written after 2a-1 was evidenced end to end.
Three resolutions were settled before drafting and they change D4's shape rather
than extend it, so they are recorded here as D15–D17 and the amendment is noted
in §9 of the design:

1. The extractor is a **separate trusted profile, outside `qfd` and outside the
   sandbox** — a third privilege domain, not a new function in either.
2. It is triggered through a **narrow host-side service**. `qfd` may *request* an
   extraction and must **never hold the database credential**.
3. Cohort reproduction stays on **`probe`**, restricted to
   `research/experiments/`. A `train` kind is deferred to 2d.

**Deliverable.** A frozen, hash-recorded Parquet extract produced by trusted code
under a read-only role from a single `REPEATABLE READ` snapshot, mounted
read-only into a sandbox that still has no network and no credential, and a
cohort that reproduces from it. The candidate emits predictions and nothing else.

---

## 1. Decisions settled for 2b

### D15 — The extractor is a third privilege domain

Two independent constraints force this, and each alone would be sufficient.

**D6 (`qfd`'s closure is the standard library).** §4.6 freezes Parquet types as
part of the extract contract, and Parquet requires `pyarrow`. So the code that
writes the extract cannot be code that lives inside `qfd`.

**D5 (`qfd` is in the `docker` group).** Anything `qfd` hands to a container,
`qfd` can read back out of it. A containerised extractor *launched by `qfd`*
therefore cannot satisfy "`qfd` never holds the credential" — the DSN would pass
through `qfd` on its way in, by construction. The only shape that satisfies it is
a service `qfd` does not start, does not parameterise beyond a typed request, and
cannot introspect.

So: a dedicated system user `qfextract`, a systemd unit, and the credential
delivered by systemd — never through `qfd`.

**What this boundary is, stated honestly.** Docker-group membership is
root-equivalent (design §7 residual risk 1), so a *compromised* `qfd` can reach
the credential anyway. This is a **least-privilege boundary, not a barrier
against a compromised `qfd`.** What it genuinely buys:

- No `qfd` defect can disclose the DSN. Not a traceback that prints `environ`,
  not a status payload that echoes a spec, not the event chain recording a
  request, not a log line. Those are the failures that actually happen, and every
  one of them is now impossible rather than unlikely.
- The blast radius of the untrusted plane is unchanged and provably so: the
  sandbox never had a credential and still does not.
- It survives Phase 5. When D2 is revisited and the container runtime becomes
  rootless, this boundary becomes a real barrier with no redesign.

`qfextract` is **not** in `docker`, **not** in `qfheavy`, **not** in `qfclient`.
It has no reason to run a container, hold the training mutex, or submit a job.

### D16 — The request channel: a socket-activated unit, validated twice

`qfd` connects to `/run/qf-extract/sock` and sends one typed request. The unit is
**socket-activated**, so the socket exists whether or not the extractor is
running and there is no "is it up yet" question — the same reason 2a's
`wait_ready` exists, removed at the source instead.

A unix socket rather than a spool directory, deliberately. A spool directory
needs its own atomicity story (write-then-rename), its own absence-settling story
(is a missing request file "not yet written" or "already consumed"?), and its own
liveness story. 2a spent sixteen review rounds learning that an absence is only
evidence once something has been *asked* and an *answer* has come back. A socket
gives synchronous admission or refusal, and `SO_PEERCRED` on both ends, reusing
protocol machinery that is already built and tested.

**The request is validated inside the extractor, not only by `qfd`.** `qfd`
validates it so a bad request is refused cheaply and legibly at submit time; the
extractor validates it again because a caller is a caller. `qfd` is trusted code,
and the point of this domain is that its trust is not required.

Only `qfd`'s uid may connect (`SO_PEERCRED`), and the socket's group excludes
`qfclient` — `research` cannot reach it at all, which NC17 asserts positively.

### D17 — The request is closed-world, and `lookback_days` is part of it

| Field | Type | Validation |
|---|---|---|
| `schema` | int | `== 1` |
| `target` | enum | `wait_time` \| `run_duration` — nothing else exists in `configs/*.yaml` |
| `train_start` | timestamp | UTC, `< as_of_date` |
| `as_of_date` | timestamp | UTC, not in the future beyond a small skew |
| `lookback_days` | int | `1 <= n <= 120` |
| `generation` | int | `>= 1`, default `1` |

Nothing else. No filters, no column names, no config file, no flag subset.

**`generation` exists because extracts are immutable (D20).** Incorporating late
data cannot mean rewriting a published extract, so it means asking for a new one:
`generation` is part of `request_hash`, so bumping it yields a distinct extract
that a comparison cannot silently mix with its predecessor. It is the deliberate,
recorded act that re-extraction has to be.

**The settlement lag is trusted configuration, not a request field.** A candidate
that could choose its own lag could choose zero. The extractor holds the
authoritative value; `qfd` keeps its own copy only to refuse early and legibly,
and if the two disagree **the extractor wins** — a fail-closed disagreement, and
a startup gate problem to fix, not a negotiation. The lag in force is recorded in
the manifest, because a reader cannot otherwise tell what the extract assumed.

**`lookback_days` is admitted, and the reason has to be written down or it will
be read as a loophole.** `load_task_runs_for_queue_context` derives
`ref_lower = window_start - timedelta(days=c.lookback_days)` — today from a
research config. It is a *window* parameter, which D4 already admits ("target,
window, watermark"), and it selects no column and contributes no SQL. It becomes
a bounded integer in the request rather than a value read from `qf-research`,
because a trusted query must not consult the research repo for anything, even
something harmless. The bound exists because an unbounded value is a
full-history table scan (the docstring records a confirmed multi-TB read
profile).

**The anomaly flags are *not* in the request.** `load_anomalous_dates` builds its
`WHERE` with an f-string over `c.anomaly_filter["flag_subset"]`. The values are
allowlisted so it is not injectable today, but it is a config-driven SQL
fragment in trusted code and that is the exact shape D4 forbids. The extractor
emits the **whole** `queue_forecast_daily_health` row set — `sample_date`,
`is_anomalous`, and all nine flags — and the candidate subsets it in pandas. The
f-string disappears rather than being made safe.

### D18 — One fixed column inventory, enumerated in trusted code

Six files. Every column is named as a literal; none is computed, and none comes
from outside this table.

| File | Source | Columns |
|---|---|---|
| `runs.parquet` | `queue_forecast_task_runs r` ⋈ `queue_forecast_tasks t` on `task_id`, window on `r.pending_at ∈ [train_start, as_of_date)` | `r.task_id`, `r.run_id`, `r.pending_at`, `r.started_at`, `r.resolved_at`, `r.reason_resolved`, `r.wait_duration_s`, `r.run_duration_s`, `r.priority_at_pending`, `r.queue_pending`, `t.task_queue_id`, `t.scheduler_id`, `t.metadata_name`, `t.normalized_name`, `t.max_run_time_s`, `t.repo_family`, `t.tags` |
| `worker_counts.parquet` | `queue_forecast_worker_counts`, `sampled_at ∈ [train_start - 90m, as_of_date)` | `task_queue_id`, `sampled_at`, `running_workers`, `claimed_tasks`, `existing_capacity` |
| `worker_pools.parquet` | `queue_forecast_worker_pools`, whole table (~650 rows) | `task_queue_id`, `pool_kind`, `provider_type` |
| `throughput_runs.parquet` | `r` ⋈ `t`, `r.resolved_at IS NOT NULL` and in window, `t.task_queue_id IS NOT NULL` | `t.task_queue_id`, `r.started_at`, `r.resolved_at`, `r.wait_duration_s`, `r.run_duration_s` |
| `qctx_runs.parquet` | `r` ⋈ `t`, the pending-overlap predicate, floored at `ref_lower` on both sides | `r.task_id`, `r.run_id`, `r.pending_at`, `r.started_at`, `r.resolved_at`, `r.priority_at_pending`, `t.task_queue_id`, `t.repo_family` |
| `daily_health.parquet` | `queue_forecast_daily_health`, whole table | `sample_date`, `is_anomalous`, and the nine `flag_*` columns |

Two deliberate differences from what the trainer issues today:

- **`runs.parquet` selects both targets and `started_at`.** `_build_query`
  selects `r.{c.target_column} AS y` — one target, chosen by config. The extract
  carries `wait_duration_s` *and* `run_duration_s` under their own names and no
  `y`, so the target is a candidate-side rename. `started_at` is added because
  `_build_query` omits it while bet 2's censoring needs it (`hazard_labels`
  filters on `started_at` *and* `wait_duration_s`), and the union rule says the
  widest superset wins.
- **`daily_health.parquet` replaces `anomalous_dates.json`.** A set of dates is
  the *result* of a config-dependent filter; the rows are the fact. Emitting the
  rows moves the filter to the candidate where it belongs, and the file name
  changes so nothing silently reads a narrowed artifact expecting the old one.

**The rule this draws** (unchanged from D4, restated because it is the whole
point): a new *derivation* over these columns is a change in `qf-research`
alone. A genuinely new table or column is a human promotion into this table.

### D19 — One snapshot, and a watermark that bounds late arrival

All six queries run inside **one** `REPEATABLE READ` read-only transaction, so
they cannot disagree about what existed. `SET TRANSACTION READ ONLY` in addition
to the role's `default_transaction_read_only=on`: the role is the control, the
statement is the assertion, and one of them failing should not be silent.

`extract/MANIFEST.json` records, per file, `sha256`, `rows`, and the window
actually used; and once per extract: `target`, `lookback_days`,
`snapshot_start_ts`, `pg_current_snapshot()`, and the **data watermark** — the
maximum `pending_at` and the maximum resolution timestamp included.

`pg_current_snapshot()`, not `txid_current()`: a transaction id does not encode
what that transaction could *see*. Without the snapshot, two files in one
"frozen" extract could straddle a collector write and nothing in the record
would show it.

**What the watermark does and does not do.** The parent §7 hole is that the
trainer's content-hashed cache key omitted any notion of *when* the data was
read, so the same window re-extracted later silently picked up late-arriving rows
and two runs "on the same window" were not comparable.

The watermark **records** that, and D20 is what **closes** it. An earlier draft
of this section said the watermark closed it, and that was wrong in a way worth
keeping visible: a late event can update a row *inside* an already-extracted
window without moving `max(pending_at)` or `max(resolved_at)`, so the watermark
cannot detect the case it was introduced to catch. It is provenance -- it is how
a later reader learns that two extracts of the same window saw different data --
and immutability is the mechanism that stops the substitution happening.

### D20 — Extract identity, reuse, and retention

Revision 2 replaced this decision wholesale. Revision 1 said "reuse requires
`request_hash` **and** an unchanged watermark", and that rule does not work:

**A late event can update an older run without moving either `max(pending_at)`
or `max(resolved_at)`.** The collector consumes Pulse continuously and runs a
one-minute enrichment backfill (verified fact 10), so a row inside an already
-extracted window can gain a `resolved_at`, a `reason_resolved` or an enrichment
field long after the window's maxima are fixed. An unchanged watermark therefore
does **not** prove byte-identical input, and a rule that treats it as proof would
serve a stale extract while reporting a cache hit — a quieter version of the
parent §7 bug it was written to close.

So reproducibility is a property of **immutability**, not of change detection:

- `request_hash` = digest of the canonicalised request (D17 fields, including
  `generation`). Known *before* extraction, so it names the staging directory.
- `extract_hash` = digest of the canonicalised manifest. Known only *after*, and
  it is what §4.6 requires every member of a comparison to share.
- `as_of_date` **must be a completed UTC day boundary**, and must satisfy
  `as_of_date <= floor_day(now - settlement_lag)`. A window that ends inside the
  live tail is refused rather than extracted, because nothing downstream can make
  it reproducible afterwards.
- **The first published extract for a `request_hash` is immutable, and reuse is
  by `request_hash` alone.** A second extraction naming an existing
  `request_hash` is **refused, not overwritten** — immutability enforced by the
  code that could break it, not asserted in a comment.
- **The watermark is provenance, not a cache-validity oracle.** It records what
  the extract contained; it does not certify that a re-extraction would contain
  the same thing. Recording it is still required: it is how a later reader learns
  that two extracts of "the same window" saw different data.
- **Incorporating late data requires a new `generation` or a new `as_of_date`.**
  Both change `request_hash`, so the new extract is a new artifact with a new
  `extract_hash` and a comparison cannot mix it with its predecessor by accident.
- **The published directory is named by `request_hash`.** Extraction writes to
  `/var/lib/qf-extracts/.staging/<request_hash>.<pid>/` and is published by ONE
  `rename()` to `/var/lib/qf-extracts/<request_hash>/`. `extract_hash` is a field
  of the manifest, not a path.

  Revision 2 said `<extract_hash>/` plus a `request_hash -> extract_hash` index,
  and that is two acts: a crash between them leaves the artifact published and
  undiscoverable, and the retry takes a fresh snapshot, computes a different
  `extract_hash`, and publishes a SECOND artifact for the same request —
  reproduced by fault injection. Naming the directory after the request makes
  publication and discoverability the same act, so there is nothing to die
  between, and `rename()` onto an existing directory is itself the
  "refused, not overwritten" rule.

  So resolving an `extract_hash` to a directory is a read of the manifests, not
  a path join. Nothing in 2b needs that: reuse is by `request_hash`, and §4.6's
  requirement that every member of a comparison share an `extract_hash` is
  satisfied by reading the field. 2c's evaluator gets an index if it wants one —
  built from the manifests, and therefore derivable rather than authoritative.
  A partially written extract is never visible under a real name, the same
  discipline as the nightly's marker publication (D10a).
- Retention extends `qf-runs-prune.sh`'s knobs to `/var/lib/qf-extracts` with its
  own cap. An extract is large and immutable, so the size cap matters more here
  than the age tier; and the "no silent caps" rule applies. **Pruning an extract
  is deleting the input to a recorded result**, so a pruned extract leaves a
  self-describing `PRUNED` marker carrying its manifest digest and watermark —
  the same rule that made partial run-directory pruning legible.

**The settlement lag has no basis in the repository.** There is no lateness SLA
anywhere in the tree, so its value is an operational choice, not a derivation.
It ships as an explicit knob with a stated default and a comment saying exactly
that — a number presented as measured when it was guessed is worse than a guess
labelled as one.

### D23 — Postgres resource discipline: `temp_file_limit` is per *process*

`temp_file_limit` is already set — `20GB` on `forecast_experiment`, from
`phase0-setup.sh:547`, along with `statement_timeout=30min`,
`idle_in_transaction_session_timeout=5min`, `lock_timeout=10s` and
`work_mem=512MB` (verified fact 2). Revision 1 of this plan claimed it was unset.
It was wrong, and it was wrong in the direction that matters: it presented a
configured bound as a missing one.

The real problem is that **the bound does not bound what it appears to.**
PostgreSQL defines `temp_file_limit` per process, and parallel workers are
separate processes. With the server's default four workers per gather, one query
can spill roughly five times the limit collectively, and `work_mem` multiplies
the same way (per process, per node). The documentation warns about exactly this
([runtime-config-resource](https://www.postgresql.org/docs/15/runtime-config-resource.html)).

So 2b-1 makes it a real boundary:

- **`max_parallel_workers_per_gather = 0`** for the extractor's role or session.
  This is the load-bearing setting, and not as an optimisation: it is what
  restores `temp_file_limit` to being a limit, because a single-process query
  cannot multiply a per-process bound.
- **One extraction at a time.** The unit is single-instance and the extractor
  holds its own mutex, so two concurrent extractions cannot each spend the
  allowance. Concurrency here buys nothing — extraction is not on an interactive
  path — and costs the only bound there is.
- **`temp_file_limit` stays explicit and gets sized from measurement**, not
  carried forward at 20GB because that is what it happens to say. The first real
  extraction on a real window is the measurement; until then the number is
  inherited, and the plan says so.
- **`qfextract` refuses admission** unless free space covers `qfd`'s 20 GiB
  admission floor, the PostgreSQL temp allowance, and the expected extract
  output — the same preflight discipline the NC suite and the fault gates now
  share, applied to the one actor that can consume all three at once.
- **NC17/NC18 verify the *live* role settings**, not the migration text. A
  setting that is correct in a file and absent from the cluster is the exact
  shape of failure this project keeps finding.

### D21 — The sandbox read path (2b-2)

`<extracts_dir>/<request_hash>` mounted **read-only** at `/extract` — named by
the request, per D20's single-rename publication, with `extract_hash` read from
`/extract/MANIFEST.json` rather than inferred from the path. The research
worktree stays read-only at `/app/trainer` with a writable run-private directory
over `/app/trainer/data`, because `CACHE_DIR = trainer/data/cache` is computed
relative to the module — which is why 2b needs no path refactor inside
`qf-research` (§4.6).

`--network none` and no credential are unchanged. **NC13 is extended, not
relaxed**: the in-sandbox suite gains clauses asserting `/extract` is present,
readable, and not writable, and that `DATABASE_URL` is still absent — a data
plane that arrived by loosening the sandbox would be the failure.

### D22 — Baseline artifacts (2b-3)

`resolve_baseline_file` returns `c.residual["baseline_file"]` or
`c.baseline_features["baseline_file"]` — a **path chosen by a research config**.
Trusted code must produce the baseline artifact and mount it at a fixed name;
the config selects *whether* to join a baseline, never *which file* is the
baseline. Producer is the existing Node predictor, run as trusted code.

### Amendment to D14 (control numbering)

`nc16()` exists in `nc-suite-phase2.sh` and is absent from D14's table. Recording
it, plus the two 2b controls:

| Control | Assertion | Lands |
|---|---|---|
| NC16 | **Already implemented, undocumented.** `docker create` then `start` relays the container's exit status; no container survives a terminal run; every resource row is released | 2a |
| NC17 | **New.** The database credential is unreachable from both `qfd` and `research`: neither can read the credential file, neither can connect to Postgres, and `research` cannot reach the extractor socket — each asserted against a positive canary proving `qfextract` *can* | 2b-1 |
| NC18 | **New.** A request naming anything outside D17 is refused; a published extract is IMMUTABLE -- re-requesting a `request_hash` is served byte-identically and a second extraction under an existing `request_hash` is refused rather than overwritten; bumping `generation` yields a separate artifact | 2b-1 |

---

## 2. The 2b cut

Same rule as the 2a–2d cut: each group ends where a control can fail closed, and
none needs the next to be useful.

| Group | Delivers | Controls | Standalone value |
|---|---|---|---|
| **2b-1** | `qfextract` domain, the six queries, `MANIFEST.json`, the `extract` kind, the role migration | NC17, NC18 | a frozen hash-recorded extract; the nightly's cache stops being a correctness hazard |
| **2b-2** | `/extract` mount, writable `data/`, `probe` kind, predictions-only contract | NC13 extended | one cohort reproduces with no network and no credential |
| **2b-3** | trusted baseline artifacts, `query` kind | — | residual and hazard configs run through the dispatcher |

2b-1 is detailed below. **2b-2 and 2b-3 are scoped, not specified** — their task
detail lands when 2b-1 is evidenced, for the same reason 2a's plan reached
revision 12: the details worth writing are the ones the previous group taught.
Treating the outlines in §6 as a specification would be reading a sketch as a
spec.

---

## 3. Conventions

Carried from `auto-research-phase2a-plan.md`, unchanged:

- Tasks are ordered. 2b-1's unprivileged tasks come first; the privileged ones
  are run by the human, per the Phase 0 precedent.
- `host/x` → `tools/queue-forecasting/host/x`. On the host
  `$TRUSTED=/srv/queue-forecasting`.
- Test-first where the code is pure. The request type and the query inventory
  have no I/O worth mocking; their tests come first and every case names the
  failure it prevents.
- Stdlib `unittest` for `qfd`-side code. The extractor is not stdlib-only (D15)
  but its tests must still run without a database — the SQL is asserted as
  *text*, and the snapshot behaviour is asserted against a real Postgres only in
  the privileged tasks.
- A negative control that could not be meaningfully attempted is **VOID**, and
  VOID is a failure.
- Every refusal is preceded by a **positive canary** that must succeed, and the
  canary **gates** its group. 2a shipped canaries that reported without gating
  for months, and three clauses passed having observed nothing.

## 4. Verified facts this plan depends on

Read from the tree on 2026-08-27; re-check before implementing.

1. `trainer/src/data_loader.py:199` `_build_query` splices `c.filters` into its
   `WHERE` and selects `f"r.{c.target_column} AS y"`. Both come from a config in
   `qf-research`. This is the code D4 forbids trusted code from reusing.
2. **`forecast_experiment` already exists**, created by
   `host/phase0-setup.sh:509-548` with: `GRANT SELECT` on **every table in
   schema `public`** (derived from the live database, not a named list),
   `default_transaction_read_only = on`, `statement_timeout = 30min`,
   `idle_in_transaction_session_timeout = 5min`, `lock_timeout = 10s`,
   `temp_file_limit = 20GB`, `work_mem = 512MB`. Phase 0 also revokes `CREATE ON
   SCHEMA public` and `ALL ON DATABASE` from `PUBLIC` — neither of which touches
   function `EXECUTE`. **`max_parallel_workers_per_gather` is set nowhere in the
   tree** (`grep` across `host/*.sh`). Revision 1 of this plan asserted
   `temp_file_limit` was unset; that was stale and it presented a configured
   bound as a missing one.
3. `data_loader.py:506` `load_anomalous_dates` builds its `WHERE` with an
   f-string over `c.anomaly_filter["flag_subset"]`, allowlisted against
   `_ALLOWED_FLAGS` (nine flags).
4. `data_loader.py:475` `ref_lower = window_start - timedelta(days=c.lookback_days)`
   — the only config value that reaches a window bound. Hence D17.
5. `data_loader.py:22` `CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"`,
   i.e. `trainer/data/cache` — module-relative, hence D21's nested writable mount.
6. `data_loader.py:56` `_connect` uses `psycopg` with a session `work_mem`.
7. `host/dispatcher/env/pyproject.toml` already pins `pandas`, `pyarrow` and
   `psycopg[binary]`. **The extractor needs no new dependency closure** — it uses
   the same promoted manifest the trainer image is built from, so NC12's
   provenance covers it unchanged and NC10 can name its paths.
8. `configs/*.yaml` contain exactly two `target_column` values:
   `wait_duration_s` and `run_duration_s`. Hence D17's enum.
9. `data_loader.py:66` `resolve_baseline_file` returns a config-chosen path.
   Hence D22.
10. Tables named by the six shapes: `queue_forecast_task_runs`,
   `queue_forecast_tasks`, `queue_forecast_worker_counts`,
   `queue_forecast_worker_pools`, `queue_forecast_daily_health`. These five are
   the entire surface the extractor reads — though not, per fact 2, the entire
   grant surface of the role.
11. **The source tables change continuously.** The collector consumes Pulse
    continuously and runs a one-minute enrichment backfill
    (`src/collector.js`); worker counts are written every five minutes
    (`src/worker-counter.js`, `SAMPLE_INTERVAL_MS`); and
    `trainer/scripts/compute_daily_health_loop.py` re-runs the detector on a
    trailing window every `HEALTH_INTERVAL_SECONDS`, UPSERTing, so
    `queue_forecast_daily_health` is rewritten hourly. This is what forces D20's
    immutability rule: a completed boundary is necessary but **not sufficient**,
    because a late event can update a row inside an already-extracted window
    without moving `max(pending_at)` or `max(resolved_at)`.

---

## 5. Tasks — 2b-1

### Task 1: `extract_spec.py` — the typed request, test-first — **DONE**

Delivered 2026-08-27. `host/dispatcher/extract_spec.py` and
`tests/test_extract_spec.py`, 40 tests; repo suite 539 -> 579. Red-green
verified: reverting the settlement rule and the `isinstance(True, int)` guard
fails five tests.

Three choices made while implementing, each of which is a real constraint rather
than a detail:

1. **`train_start` is held to the same day-boundary rule as `as_of_date`**,
   though D20 only required it of `as_of_date`. The rest of the system speaks in
   days (`daily_health` is keyed by `sample_date`), so a window starting at 06:00
   is one nothing downstream can describe. Loosening this later is easy;
   discovering that half the extracts have unspeakable windows is not.
2. **`DEFAULT_SETTLEMENT_LAG_S = 48h` is a guess, and the code says so.** It is
   the operational choice §9 flagged, and the comment states that it is a choice
   rather than a measurement so nobody later cites it as one.
3. **`GENERATION_MAX = 1000`** exists only because an unbounded integer field in
   a closed-world validator is an inconsistency a later reader takes as
   permission.

Beyond the plan's list, the tests also pin: the effective field set exactly (so a
field added to code without a design change fails, and vice versa); that no field
of a validated request contains a quote, semicolon, comment marker, `%` or
newline (a closed-world request has nothing a query could splice, and if one ever
appears this fails before it reaches `inventory.py`); and that
`request_hash` does **not** cover the settlement lag, asserted by hashing the same
window under two different lags.

Original task description follows.

#### Task 1 (as planned)

Mirrors `spec.py` (D12): closed-world, every field validated, nothing reaching a
shell or a SQL string. Unprivileged, no I/O.

Tests first, each naming its failure:

- an unknown key is refused (not ignored) — an ignored key is how a filter
  arrives later and nobody notices
- `target` outside the enum is refused **by name**, listing what is allowed
- `train_start >= as_of_date` is refused
- `lookback_days` of `0`, `121`, `-1`, `True`, `"30"`, `30.0` are each refused;
  `bool` explicitly, because `isinstance(True, int)`
- an `as_of_date` beyond a bounded future skew is refused
- naive timestamps are refused; the request is UTC or it is nothing
- `request_hash` is stable under key reordering and whitespace, and *changes*
  when any field changes
- a request that validates produces a frozen mapping — no field is mutable after
  validation, so a later stage cannot widen it

### Task 2: `inventory.py` — the six queries as data, test-first — **DONE**

Delivered 2026-08-28. `host/extractor/inventory.py` and
`host/extractor/tests/test_inventory.py`, 32 tests, stdlib-only so the D4
regression tests run on any machine — no database, no `pyarrow`, no extractor
environment. Red-green verified by reintroducing all three hazards that
`data_loader.py` actually contains (`f"r.{c.target_column} AS y"`, `*c.filters`,
and the f-string `WHERE {condition}`): 7 failures across 5 test methods.

**One config dependency removed rather than parameterised.** The throughput
loader's window is `train_start - (max(windows_minutes) + 30)m`, and every config
in the tree sets `windows_minutes: [15, 60]` — so today's bound is 90 minutes,
which is also `load_worker_counts`' fixed `train_start - 90m`. Rather than adding
a request field, one trusted constant `TRAILING_LOOKBACK_MINUTES = 24h`
supersets both. The cost is stated in the code so nobody optimises it back: the
extract window is months long, so an extra day of prefix is a fraction of a
percent of rows, and `worker_counts` samples every five minutes, so 24h is 288
extra rows per queue. A config wanting a longer trailing window needs a human
change here, which is correct — a longer trailing window is a claim about what is
available at prediction time.

**Two gaps the reversion exercise found, which the planned test list missed.**
Both were discovered by reintroducing a hazard and watching every test pass:

1. **A `WHERE is_anomalous = TRUE` on `daily_health` passed all 30 tests.** That
   is precisely the D17 rule ("the whole row set is emitted, the candidate
   subsets it") going unenforced — the narrowing would reduce what a candidate
   can filter on and nothing would say so. Now asserted for both whole-table
   datasets.
2. **Nothing compared the SQL's SELECT list to the *declared* `columns`.** The
   manifest reports `columns`; the file contains the SELECT list. A column
   dropped from the query would still be advertised, and a candidate would read
   a `KeyError` as missing data. Now parsed and compared positionally — which
   also enforces that every selected item is a bare column name, since an
   expression or an alias cannot round-trip the comparison, and `AS y` is exactly
   the shape D4 forbids.

Two defects in my own tests, both worth recording because both are the same
mistake in different clothes: the write-verb scan tokenised on `[a-z]+`, so
`flag_capacity_drop` produced the token `drop` and the test failed against
correct SQL; and the trailing-lookback test hardcoded the trainer's 90-minute
figure, so it failed once the trusted constant was widened. **A scan over source
has to agree with the language about what a word is, and a test that copies out a
constant tests the copy.**

Original task description follows.

#### Task 2 (as planned)

The six SQL texts as module constants, parameterised only by bound placeholders,
plus the column inventory of D18 as literals.

Tests first:

- **no identifier interpolation anywhere.** Assert each constant contains no
  `%` formatting of identifiers and no f-string residue: the only `%(name)s`
  occurrences are in the bound-parameter allowlist for that query. This is the
  regression test for D4, and it is a text assertion because that is the level
  the hazard lives at.
- the union of selected columns equals D18's table exactly — a column added to
  the code and not to the design fails, and vice versa
- `runs.parquet` selects both `wait_duration_s` and `run_duration_s`, and no
  column named `y`
- no query text contains `filters`, `target_column`, or any config key
- every query's bound parameters are a subset of the D17 request fields plus
  `ref_lower` (derived) and the worker-counts lookback (a constant)
- `daily_health` selects all nine flags plus `is_anomalous` — a subset here would
  silently narrow what a candidate can filter on

### Task 3: `extractor.py` — snapshot, files, manifest — **DONE**

Delivered 2026-08-28. `host/extractor/extractor.py` (orchestration, stdlib-only)
plus `pg.py` and `parquet_writer.py` (the two adapters), and 44 new tests —
extractor suite 36 → 80. Red-green verified on all four invariants: one snapshot
per dataset instead of one for six, reuse that re-extracts in place, staging left
behind on failure, and read-onlyness trusted rather than attempted. Each is caught.

**Structure: a seam, because neither `psycopg` nor `pyarrow` is importable in the
development environment.** All ordering rules, refusals and staging guarantees
live in `extractor.py`, which takes a `session_factory` and a `writer` as
arguments and is therefore fully exercised against a fake that can be made to
misbehave on demand — a role whose read-only default was lost, a session still
allowing parallel workers, a query that raises, a dataset that comes back empty.

**`pg.py` and `parquet_writer.py` have never executed and the plan should not
pretend otherwise.** They are flagged as untested in their own docstrings, kept
thin enough that reading them is a reasonable substitute for running them, and
they first run in the privileged tasks. Anything with a decision in it was pushed
back across the seam where a test can reach it.

Four things worth recording:

1. **The read-only canary must run before `BEGIN`.** A failed statement aborts a
   PostgreSQL transaction, so attempting a deliberate write *inside* the
   extraction transaction would poison the very transaction the extract is read
   from. Asserted by ordering, not by comment.
2. **`CREATE TEMP TABLE` is the canary**, because it is the write whose *success*
   is harmless: a read-only transaction refuses `CREATE`, and if the role has
   lost its default all that exists is a temp table that dies with the
   connection. A canary that did damage when it succeeded would be a worse
   bargain than no canary.
3. **The parallel-worker check reads the INHERITED value.** `begin_snapshot` also
   issues `SET LOCAL max_parallel_workers_per_gather = 0` defensively, and if the
   check ran after that it would be reading its own answer — a canary that cannot
   fail. Ordering is what makes the refusal a statement about the *role*, and
   there is now a test on that ordering specifically.
4. **The watermark takes the max of non-null values.** A `max()` that propagated
   a `None` — `started_at` is NULL for a still-pending run — would erase the
   watermark for the whole column, and a missing watermark reads as "nothing was
   extracted" rather than "some rows are still open".

One test of mine had to be rewritten because it asserted nothing: it grepped
`published_for.__doc__` for the word "watermark" to "prove" the reuse path does
not consult one. **A comment is not a control.** Replaced with a behavioural
test: publish, rewrite the stored manifest's watermark to something absurd, and
require that reuse still resolves the same extract without opening a session.

Original task description follows.

#### Task 3 (as planned)

One `REPEATABLE READ` read-only transaction; six files; manifest; publish by
rename (D20).

- `SET TRANSACTION READ ONLY` asserted in addition to the role default, and a
  write attempted deliberately at startup **must fail** — a positive canary for
  read-onlyness, because a role that lost its default would otherwise be
  discovered by an experiment
- `snapshot_start_ts` and `pg_current_snapshot()` captured **inside** the
  transaction, before the first query
- the watermark computed from the rows actually written, not from the request
- per-file `sha256` computed on the written bytes, not on the frame in memory
- refusal if any file is empty *and* its window is non-empty — a zero-row extract
  is a plausible-looking artifact that trains to a meaningless model
- the staging directory is removed on every failure path, and a `TRAP`-equivalent
  covers the ones that raise
- transition logging: one line when the extraction starts naming the request, one
  when it publishes naming the `extract_hash`, and duration. 2a's lesson: a
  subsystem that only speaks when unhappy cannot be watched.

### Task 4: the unit, the user, the credential — **DONE (unprivileged half)**

Delivered 2026-08-28. `host/extractor/service.py`, `qf-extract.socket`,
`qf-extract.service`, `host/phase2b-setup.sh`, plus Task 6's two SQL files
(`migrate-extractor-session.sql`, `verify-role.sql`) since the setup script's
`discover` reads the live setting they concern. 40 new tests; extractor suite
91 → 131. Red-green verified on all four clauses that ARE the D15 boundary:
a group-readable credential, forbidden group membership, the peer-uid check, and
`SupplementaryGroups=docker` appearing in the unit.

**The gate asserts a privilege it must NOT have**, which is the inverse of a
normal permission check and the most important clause in the file: membership of
`docker`, `qfheavy` or `qfclient` is a refusal to start, and the message names
`SupplementaryGroups=`. A future operator adding one for convenience gets a
service that will not start rather than a domain that silently went
root-equivalent. `phase2b-setup.sh install` also *removes* those memberships
rather than only reporting them — taking a group off a service account is safe;
leaving it is not.

**The credential rule is "no group or other bit at all", not "not readable by
qfd".** Owner-only makes the question moot however the groups are arranged later,
and it is checkable in one `stat`. The unit delivers it with `LoadCredential=`,
so systemd reads the source as root and hands this process a 0400 copy — the DSN
never passes through `qfd`, never appears in the unit, and is not in any group.
`phase2b-setup.sh` deliberately **will not generate a credential**: one a setup
script can generate is one in a shell history.

Three places where this unit deliberately differs from `qf-dispatch.service`, all
stated in the file so the divergence reads as a decision:

- `RestrictSUIDSGID=yes` **here**, `no` there. The dispatcher needs the setgid
  bit on each run's `out/` and the seccomp filter fails any such chmod; the
  extractor sets no setgid bit, so the restriction is free.
- `PrivateTmp=yes` **here**, `no` there. PostgreSQL's temp files live on the
  server, so a private `/tmp` cannot affect D23's accounting; the dispatcher
  keeps it off only to stop a future reader moving the training lock back to
  `/tmp` and silently getting two private inodes and no mutex.
- `StateDirectoryMode=0755`, not 0750. The extract is mounted into a sandbox
  running as a different uid, and the data is task metadata rather than a secret
  — the secret is the DSN, 0400 in another directory. 0750 would make every
  extract unreadable by the container that exists to read it.

Socket activation is load-bearing rather than tidy: the socket exists whether or
not the service runs, so `qfd` never asks "is the extractor up yet". 2a answered
that question with a `wait_ready` poll loop; here there is no question.

`%%QFD_UID%%` is substituted at install time, and the installer refuses if any
`%%placeholder%%` survives. A checked-in uid would be wrong on every host but
one, and wrong in the direction of admitting the wrong client.

Two of my own tests were wrong before the code was, and one of them is a
repeat: `test_the_socket_is_not_world_writable` demanded `0600` or `0640`, which a
correct socket cannot be (`qfd` reaches it through its group, so group rw is
required) — the test asserted a mode that would have broken the only client it
has. And a static scan matched the unit's own explanatory comment for the **third
time this phase**; comment-stripping is now a shared `directives()` helper rather
than a thing I remember to do.

**Still outstanding for Task 4:** everything privileged. `phase2b-setup.sh
install` has not run, the units have never been loaded, `pg.py` and
`parquet_writer.py` have still never executed, and the two SQL files have not
touched a database. That is the next thing to do on the host.

Original task description follows.

#### Task 4 (as planned)

`host/extractor/qf-extract.socket`, `qf-extract.service`, and `phase2b-setup.sh`.

- `qfextract` system user; **not** in `docker`, `qfheavy`, or `qfclient`
- credential via systemd `LoadCredential=`, file `0640 root:qfextract`, and
  `qfd`'s user is not in `qfextract`
- socket `/run/qf-extract/sock`, group-restricted so only `qfd`'s uid can
  connect; `SO_PEERCRED` enforced in code as well, because a directory mode is a
  configuration and a peer check is a program
- hardening carried from `qf-dispatch.service`, and the two traps 2a paid for:
  **`PrivateTmp` interacts with the uv environment**, and `StateDirectory=` sets
  `User:Group` in a way that excludes other groups. `RestrictSUIDSGID` is not
  needed here (no setgid directories) but its absence should be *stated*, not
  merely omitted.
- a startup gate in the extractor mirroring `Config.check_startup`: every
  precondition checked at start, fail-closed, each problem naming the setting
  that fixes it. An invariant of the environment belongs in the startup gate, not
  in the first request that trips over it.

### Task 5: the `extract` kind in `qfd`

- `spec.py` gains `extract` with the D17 fields; `qfd` validates, then relays
- the relay is a client of `/run/qf-extract/sock` and **holds no credential** —
  a test asserts `qfd`'s environment and code contain no DSN and no reference to
  one
- `extract_hash`, `request_hash`, and the watermark are recorded as **pins**, not
  columns (§4.6: new pin keys, never new columns)
- a job whose extract is still being produced waits without holding the training
  mutex — extraction is not heavy work and must not serialise against the nightly
- `qf extract --target wait_time --train-start ... --as-of ... --lookback-days 30`
  in the client, plus `qf extracts` to list what exists with hashes and
  watermarks

### Task 6: the role — verify what exists, add the one missing setting

Revision 1 planned a `CREATE ROLE` migration. **That was wrong: Phase 0 already
creates `forecast_experiment` with grants and session limits**
(`phase0-setup.sh:509-548`, verified fact 2). Writing a creation migration would
have produced a file that either did nothing or fought the installer.

So this task is a **verification script plus one addition**, written here and
**not applied by me** — it touches a live database and that is the human's call.

`host/extractor/verify-role.sql`, which *proves* rather than assumes:

```sql
-- 1. The read-only default is in force on the LIVE cluster, not just in a file.
SELECT rolname, rolconfig FROM pg_roles WHERE rolname = 'forecast_experiment';

-- 2. pg_current_snapshot() needs no explicit grant: it is a pg_catalog builtin
--    and PostgreSQL grants EXECUTE on functions to PUBLIC by default
--    (https://www.postgresql.org/docs/15/ddl-priv.html). Phase 0 revokes CREATE
--    on schema public and ALL on the database from PUBLIC, neither of which
--    touches function EXECUTE. Proven, not assumed:
SELECT has_function_privilege(
  'forecast_experiment',
  'pg_catalog.pg_current_snapshot()',
  'EXECUTE'
);

-- 3. And proven live, in the exact transaction shape D19 uses:
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT pg_catalog.pg_current_snapshot()::text;
ROLLBACK;
```

**No `GRANT EXECUTE` belongs in the migration unless check 2 fails on the live
cluster.** A grant added defensively for a privilege already held is a permanent
line of unexplained SQL that a reviewer must later disprove.

`host/extractor/migrate-extractor-session.sql`, the one real change:

```sql
-- The load-bearing setting, per D23: temp_file_limit and work_mem are PER
-- PROCESS, and parallel workers are separate processes. Without this, the 20GB
-- limit bounds roughly 100GB of collective spill.
ALTER ROLE forecast_experiment SET max_parallel_workers_per_gather = 0;
```

Set at the role rather than per session so it cannot be forgotten by a caller;
the extractor **also** sets it per session and asserts it, because a role setting
is configuration and an assertion is a program.

**A finding this task surfaced, and a decision it needs.** Phase 0 grants
`SELECT` on **every table in schema `public`**, derived from the live database
(`phase0-setup.sh:509`), not on a named list. So D4's claim that "a genuinely new
table is a human change, promoted into the trusted extractor" is enforced **only
by the column inventory in trusted code (D18)** — the grant does not enforce it
at all. That is not a defect in D4's logic, but anyone reading "read-only role"
as "can only see the six shapes" would be wrong.

Recommendation, for the human to accept or reject: a **dedicated
`forecast_extract` role** granted `SELECT` on exactly the five tables in verified
fact 9. That makes the grant a second, independent boundary instead of a comment,
and it avoids narrowing `forecast_experiment`, which the nightly trainer also
uses and which would break if its surface shrank. Deliberately *not* done
unilaterally: D4 names `forecast_experiment` as the extractor's role, so changing
it is a design decision, not an implementation detail.

### Task 7: NC17 and NC18

Extends `host/nc-suite-phase2.sh`, using the instrument the 2a runs earned:
`state_of` that can say `UNREADABLE`, canaries that gate, `never_concurrent`-style
positive observation, and reasons printed on every VOID.

NC17 — the credential is unreachable:

- **canary first:** `qfextract` can read the credential file and can connect —
  without it, every refusal below proves nothing
- `qfd`'s user cannot read the credential file (refused)
- `research` cannot read the credential file (refused)
- `research` cannot connect to the extractor socket (refused)
- `research` inside a sandbox still has no `DATABASE_URL` (NC13 clause, re-run
  here because the assertion now has a way to become false)
- the *live* check, not just the file: connecting as `forecast_experiment` and
  attempting a write is refused by the server

NC17b — the resource bound is real (D23), checked against the **cluster**:

- `rolconfig` for the extractor's role actually contains
  `default_transaction_read_only=on`, an explicit `temp_file_limit`, and
  `max_parallel_workers_per_gather=0`. A setting correct in a migration file and
  absent from the cluster is the precise failure shape this project keeps finding.
- `SHOW max_parallel_workers_per_gather` inside the extractor's own session
  returns `0` — the role setting and the session assertion checked separately,
  because they can disagree
- two concurrent extraction requests do not both proceed (D23's one-at-a-time
  rule), measured by **positive observation** of the first actually running
  rather than by neither being seen — the `never_concurrent` lesson from NC8
- an extraction is refused when free space does not cover `qfd`'s floor plus the
  temp allowance plus the expected output, and the refusal **names which of the
  three** was short

NC18 — the request is closed-world and the extract is reproducible:

- **canary first:** a valid request produces an extract and a manifest
- a request with an extra key, a bad target, an out-of-range `lookback_days`, or
  a naive timestamp is refused, each **by name**
- an `as_of_date` that is not a completed UTC day boundary is refused
- an `as_of_date` inside the settlement lag is refused, and the refusal names the
  lag in force
- **re-requesting an existing `request_hash` is served from the published extract,
  byte-identically** — the immutability rule of D20, asserted by digest
- **a second extraction naming an existing `request_hash` is refused, not
  overwritten.** This is the clause that matters: it is the one that fails if
  someone later "fixes" reuse by re-extracting in place, which is how a recorded
  result silently acquires a different input.
- bumping `generation` yields a different `request_hash`, a different
  `extract_hash`, and a *separate* published directory — the predecessor still
  present and unchanged
- the manifest records the settlement lag, the snapshot, and the watermark; and
  the watermark is **not** consulted for reuse. Asserted by construction: reuse
  is a `request_hash` lookup, and a test proves the lookup path never reads a
  watermark, because the tempting shortcut is exactly the bug D20 removed.

---

## 6. Outlines — 2b-2 and 2b-3

Scoped, not specified. Detail lands when 2b-1 is evidenced.

**2b-2.** `sandbox.py` gains the `/extract` read-only mount and the nested
writable `data/`; `probe` kind restricted to `research/experiments/`;
`predictions.parquet` validated against §4.6's frozen types with duplicates and
NaN as refusals; NC13 extended with the three `/extract` clauses. Acceptance: one
cohort reproduces from a frozen extract with `--network none`.

**2b-3.** Trusted baseline production via the Node predictor, mounted at a fixed
name; `query` kind; `resolve_baseline_file` reduced to a boolean choice in
`qf-research`.

---

## 7. Acceptance for 2b-1

1. Every test passes with no database: `python3 -m unittest discover -s
   host/dispatcher/tests` and the extractor's own suite.
2. `phase2b-setup.sh` is idempotent and its startup gate refuses a host missing
   any precondition, naming the setting.
3. NC17 and NC18 pass with zero VOIDs, in `nc-suite-phase2.sh`, alongside the
   existing 86.
4. Fault gates still 32/0 and the rest of the suite still passes — 2b-1 adds a
   domain and must not perturb the spine.
5. A real extract exists for a real window, its manifest carries a watermark and
   a snapshot, and re-requesting it is a cache hit; moving the watermark is not.
6. Evidence appended to `host/nc-evidence-phase2b.txt`, citing a commit on
   `main`.

## 8. Deferred from 2b

Contracts and `contract_hash`; `eval.parquet`, `verdict.py`, the independent
derivation, the evaluator image (all 2c); `screen`, `confirm`, `summarize`,
multi-cohort sweep composition, the `train` kind (all 2d); pre-registration and
the statistical machinery (Phase 3); anything touching
`trainer/data/models/` or the live predictor.

## 9. Resolved questions, and the one operational choice left

Revision 1 listed three open questions. All three are answered; two changed the
plan.

1. **Do the source tables change continuously? Yes** — verified fact 11. This
   invalidated D20's reuse rule rather than refining it: a completed UTC boundary
   is necessary but not sufficient, because a late event can update a row inside
   an already-extracted window without moving either watermark maximum. D20 was
   rewritten so reproducibility rests on **immutability** (first published extract
   for a `request_hash` wins, re-extraction refused, `generation` for deliberate
   re-extraction) and the watermark is demoted to **provenance**.
2. **Is `temp_file_limit` unset? No — and the question was the wrong one.** It is
   `20GB` on `forecast_experiment` from Phase 0, so revision 1's claim was stale.
   The real defect is that the bound is **per process** while parallel workers are
   separate processes, so four workers per gather makes `20GB` bound roughly
   `100GB` of collective spill (`work_mem = 512MB` multiplies identically). Hence
   D23: `max_parallel_workers_per_gather = 0` as the load-bearing setting, one
   extraction at a time, a measured rather than inherited limit, a free-space
   admission check, and **live** verification in NC17b.
3. **Does `pg_current_snapshot()` need a grant? No.** It is a `pg_catalog`
   builtin, PostgreSQL grants function `EXECUTE` to `PUBLIC` by default
   ([ddl-priv](https://www.postgresql.org/docs/15/ddl-priv.html)), and Phase 0's
   revokes (`CREATE ON SCHEMA public`, `ALL ON DATABASE`) do not touch it. No
   `GRANT EXECUTE` goes in the migration; Task 6 **proves** the privilege with
   `has_function_privilege` and a live `REPEATABLE READ READ ONLY` probe instead,
   and only adds a grant if that check fails.

**The one operational choice left: the settlement lag.** There is no lateness SLA
anywhere in the repository, so no value is derivable from it. It ships as an
explicit knob, with a default, and with a comment stating that the default is a
choice rather than a measurement. The first real extractions are what turn it into
a measured number.

**One decision needed from the human**, raised by Task 6: Phase 0 grants `SELECT`
on every table in schema `public`, so D18's inventory in trusted code is the
*only* thing enforcing "a new table is a human promotion". A dedicated
`forecast_extract` role granted `SELECT` on exactly the five tables would make
the grant a second, independent boundary. Not done unilaterally, because D4 names
`forecast_experiment` and narrowing that role would break the nightly trainer.
