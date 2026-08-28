# Auto-Research Loop Phase 2b Implementation Plan — the data plane

Design: `auto-research-phase2-design.md` §2 (the 2b row), D4, D6, D12, D14, §4.6.
Parent: `auto-research-loop-design.md` §3.4, §7, §8.5. Predecessor:
`auto-research-phase2a-plan.md` (the spine this builds on, delivered and
evidenced at fault-gates 32/0 and nc-suite 86/0).

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
- Publication is still `rename()` from
  `/var/lib/qf-extracts/.staging/<request_hash>.<pid>/` to
  `/var/lib/qf-extracts/<extract_hash>/`, with a `request_hash -> extract_hash`
  index so reuse is a lookup rather than a scan. A partially written extract is
  never visible under a real name — the same discipline as the nightly's marker
  publication (D10a).
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

`/extract/<extract_hash>` mounted **read-only** at `/extract`. The research
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

### Task 3: `extractor.py` — snapshot, files, manifest

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

### Task 4: the unit, the user, the credential

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
