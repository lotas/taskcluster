# Bet 1 — Queue-Context Features Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the wait model "what is ahead of the task" (priority/flow/capacity/repo-family backlog at pending time) so it can separate long-waiters from short-waiters and drive the conditional 30m+ wait-p90 miss down, validated via walk-forward ablation.

**Architecture:** Queue-context features mirror the existing throughput-feature pattern: a Python builder (`trainer/src/queue_context.py`) computes them historically via a per-queue event-sweep over a reference `task_runs` frame during `data_loader.load()`; a JS builder (`src/live-predictor/queue-context.js`) computes the same values from current DB state and merges them into `liveFeatures`. Both are versioned by `QUEUE_CONTEXT_FEATURE_VERSION` and listed in the model's `feature_schema.json`. Repo-family is a derived enrichment column on `queue_forecast_tasks`, captured forward in the collector and backfilled over the training window via the TC API.

**Tech Stack:** Node.js (`node:test`), Python 3 + LightGBM + pandas (`uv run pytest`), Postgres, ONNX, docker-compose trainer.

**Spec:** `tools/queue-forecasting/bet1-queue-context-features-design.md`. Read it before starting.

**Conventions:**
- Commits are handled by the user — do **not** run `git commit`. Where a step says "Commit", stage with `git add` and stop, or just note the logical checkpoint.
- JS tests run per-file: `node test/<path>.js`. Python tests: `cd trainer && uv run pytest`.
- DB target is the single `forecasting` DB; one-offs run via `docker compose ... run --rm` so `DATABASE_URL` resolves (see `reference_qf_deployment_ops`).
- Serving (JS) and training (Python) feature definitions MUST stay identical — this is the Phase 4 lesson. The versioned schema enforces comparability.

---

## File Structure

**Create:**
- `migrate-queue-context.sql` — additive schema: repo-family columns on `queue_forecast_tasks`.
- `src/repo-family.js` — pure derivation `deriveRepoFamily({routes, metadataSource, schedulerId})` (shared by collector + backfill).
- `scripts/backfill_repo_family.js` — Node one-off: re-fetch task defs over the window, derive, UPDATE.
- `trainer/src/queue_context.py` — `add_queue_context_features(...)` event-sweep builder + `PRIORITY_RANK` + `QUEUE_CONTEXT_FEATURE_VERSION`.
- `src/live-predictor/queue-context.js` — `getQueueContext(pool, row)` live builder + matching version constant.
- `trainer/configs/wait_time_residual_throughput_filtered_baseline_qctx.yaml` (+ per-ablation-step configs).
- `test/repo-family.test.js`, `test/live-predictor/queue-context.test.js`, `trainer/tests/test_queue_context.py`.

**Modify:**
- `init.sql` — add the same repo-family columns to the `queue_forecast_tasks` CREATE TABLE (fresh installs).
- `src/collector.js:259-270` — capture `routes` + `metadata.source`, derive repo-family in `backgroundApiFetch`.
- `src/db.js:88-122` — extend `ENRICH_TASK_SQL` + `enrichTask` with repo-family params; add `updateRepoFamily`.
- `src/live-predictor/predict.js:37-45,120-126,185-197` — fetch repo_family, merge queue-context into `liveFeatures`, stamp audit.
- `trainer/src/data_loader.py:130-178,373-443` — add `repo_family` to `candidate_cols`; load reference runs + wire `add_queue_context_features`.
- `trainer/src/train.py:186-204` — write `queue_context_features` block into `feature_schema.json`.
- `src/dashboard-gen.js` — add a first-class 30m+ wait-p90 miss-rate section.
- `trainer/scripts/summarize_walk_forward.py:27-36,182-215` — add a `30mplus_wait_p90_miss` column.

---

## Phase 0 — Schema: repo-family enrichment columns

### Task 1: Migration + init.sql columns

**Files:**
- Create: `tools/queue-forecasting/migrate-queue-context.sql`
- Modify: `tools/queue-forecasting/init.sql:7-25`

- [ ] **Step 1: Write the migration**

Create `migrate-queue-context.sql`:

```sql
-- Bet 1: repo-family enrichment columns on queue_forecast_tasks.
-- Derivation happens at enrichment time (collector) and via backfill; we
-- store only the result + minimal evidence, NOT raw route arrays.
BEGIN;
ALTER TABLE queue_forecast_tasks
    ADD COLUMN IF NOT EXISTS repo_family                    TEXT,
    ADD COLUMN IF NOT EXISTS repo_family_source             TEXT,   -- source|route|scheduler|unknown
    ADD COLUMN IF NOT EXISTS repo_family_evidence           TEXT,   -- short matched token/path
    ADD COLUMN IF NOT EXISTS repo_family_derivation_version INTEGER;
COMMIT;
```

- [ ] **Step 2: Mirror the columns in init.sql**

In `init.sql`, inside `CREATE TABLE IF NOT EXISTS queue_forecast_tasks (...)` (after `tags JSONB`), add:

```sql
    tags               JSONB,
    repo_family        TEXT,
    repo_family_source TEXT,
    repo_family_evidence TEXT,
    repo_family_derivation_version INTEGER
```

(Move the comma onto the `tags` line; the four new columns are variable-length, so they belong in that block.)

- [ ] **Step 3: Apply the migration to the running DB**

Run:
```bash
cd tools/queue-forecasting && docker compose run --rm predictor \
  node -e "import('./src/db.js').then(async ({createPool})=>{const p=createPool(process.env.DATABASE_URL);const fs=await import('node:fs');await p.query(fs.readFileSync('/app/migrate-queue-context.sql','utf8'));console.log('applied');await p.end();})"
```
Expected: prints `applied`. (If the predictor image doesn't mount the repo at `/app`, instead pipe the file to psql in the postgres service: `docker compose exec -T postgres psql -U postgres -d forecasting < migrate-queue-context.sql`.)

- [ ] **Step 4: Verify columns exist**

Run:
```bash
docker compose exec postgres psql -U postgres -d forecasting -c "\d queue_forecast_tasks" | grep repo_family
```
Expected: four `repo_family*` rows listed.

- [ ] **Step 5: Checkpoint** — `git add migrate-queue-context.sql init.sql`

---

## Phase 1 — Repo-family derivation, forward capture, backfill

### Task 2: Pure derivation module (`src/repo-family.js`)

**Files:**
- Create: `tools/queue-forecasting/src/repo-family.js`
- Test: `tools/queue-forecasting/test/repo-family.test.js`

- [ ] **Step 1: Write the failing test**

`test/repo-family.test.js`:

```javascript
import { test } from 'node:test';
import assert from 'node:assert';
import { deriveRepoFamily, REPO_FAMILY_DERIVATION_VERSION } from '../src/repo-family.js';

test('metadata.source hg path wins: try', () => {
  const r = deriveRepoFamily({ metadataSource: 'https://hg.mozilla.org/try/file/abc/taskcluster/ci', routes: [], schedulerId: 'gecko-level-1' });
  assert.equal(r.family, 'try');
  assert.equal(r.source, 'source');
  assert.equal(r.version, REPO_FAMILY_DERIVATION_VERSION);
});

test('metadata.source autoland', () => {
  const r = deriveRepoFamily({ metadataSource: 'https://hg.mozilla.org/integration/autoland/file/tip/x', routes: [] });
  assert.equal(r.family, 'autoland');
});

test('metadata.source beta -> release_beta', () => {
  const r = deriveRepoFamily({ metadataSource: 'https://hg.mozilla.org/releases/mozilla-beta/x', routes: [] });
  assert.equal(r.family, 'release_beta');
});

test('routes fallback when source missing', () => {
  const r = deriveRepoFamily({ metadataSource: null, routes: ['tc-treeherder.v2.mozilla-central.abc', 'index.gecko.v2.mozilla-central.x'] });
  assert.equal(r.family, 'central');
  assert.equal(r.source, 'route');
  assert.ok(r.evidence.includes('mozilla-central'));
});

test('scheduler coarse fallback: level-1 -> try', () => {
  const r = deriveRepoFamily({ metadataSource: null, routes: [], schedulerId: 'gecko-level-1' });
  assert.equal(r.family, 'try');
  assert.equal(r.source, 'scheduler');
});

test('unknown when nothing matches', () => {
  const r = deriveRepoFamily({ metadataSource: null, routes: [], schedulerId: null });
  assert.equal(r.family, 'unknown');
  assert.equal(r.source, 'unknown');
});

test('evidence is short, never a full route array', () => {
  const r = deriveRepoFamily({ metadataSource: null, routes: ['x'.repeat(500), 'index.gecko.v2.try.1'] });
  assert.ok(r.evidence.length <= 64);
});
```

- [ ] **Step 2: Run it, expect failure**

Run: `node test/repo-family.test.js`
Expected: FAIL — cannot find module `../src/repo-family.js`.

- [ ] **Step 3: Implement the module**

`src/repo-family.js`:

```javascript
// Pure repo-family derivation, shared by the collector (forward) and the
// backfill script (historical). Stores only the result + a short evidence
// token, never raw route arrays.
export const REPO_FAMILY_DERIVATION_VERSION = 1;

// hg path fragment -> family. Order matters (most specific first).
const SOURCE_PATTERNS = [
  [/\/try(\/|$)/,                         'try'],
  [/\/integration\/autoland(\/|$)/,       'autoland'],
  [/\/releases\/mozilla-beta(\/|$)/,      'release_beta'],
  [/\/releases\/mozilla-release(\/|$)/,   'release_beta'], // release + beta share a band
  [/\/mozilla-central(\/|$)/,             'central'],
];

// route token -> family.
const ROUTE_PATTERNS = [
  [/\.v2\.try(\.|$)/,             'try'],
  [/\.v2\.autoland(\.|$)/,        'autoland'],
  [/\.v2\.mozilla-beta(\.|$)/,    'release_beta'],
  [/\.v2\.mozilla-release(\.|$)/, 'release_beta'],
  [/\.v2\.mozilla-central(\.|$)/, 'central'],
];

function short(s) {
  const str = String(s ?? '');
  return str.length <= 64 ? str : str.slice(0, 64);
}

export function deriveRepoFamily({ routes = [], metadataSource = null, schedulerId = null } = {}) {
  const v = REPO_FAMILY_DERIVATION_VERSION;

  if (typeof metadataSource === 'string') {
    for (const [re, fam] of SOURCE_PATTERNS) {
      const m = metadataSource.match(re);
      if (m) return { family: fam, source: 'source', evidence: short(m[0]), version: v };
    }
  }

  const routeList = Array.isArray(routes) ? routes : [];
  for (const route of routeList) {
    if (typeof route !== 'string') continue;
    for (const [re, fam] of ROUTE_PATTERNS) {
      const m = route.match(re);
      if (m) return { family: fam, source: 'route', evidence: short(m[0]), version: v };
    }
  }

  // Coarse scheduler fallback. Only level-1 maps reliably (try-dominated);
  // level-3 is mixed (autoland/central/release) -> 'other'. Audited via source.
  if (typeof schedulerId === 'string') {
    if (/-level-1$/.test(schedulerId)) return { family: 'try',   source: 'scheduler', evidence: short(schedulerId), version: v };
    if (/-level-3$/.test(schedulerId)) return { family: 'other', source: 'scheduler', evidence: short(schedulerId), version: v };
  }

  return { family: 'unknown', source: 'unknown', evidence: null, version: v };
}
```

- [ ] **Step 4: Run tests, expect pass**

Run: `node test/repo-family.test.js`
Expected: PASS (all 7).

- [ ] **Step 5: Checkpoint** — `git add src/repo-family.js test/repo-family.test.js`

### Task 3: Capture repo-family forward in the collector

**Files:**
- Modify: `tools/queue-forecasting/src/collector.js` (`backgroundApiFetch`, lines 256-271)
- Modify: `tools/queue-forecasting/src/db.js` (`ENRICH_TASK_SQL` + `enrichTask`, lines 88-122)

- [ ] **Step 1: Extend `enrichTask` + SQL in db.js**

In `src/db.js`, change `ENRICH_TASK_SQL` to add the four columns (renumber params; `enriched_at` stays last):

```sql
const ENRICH_TASK_SQL = `
UPDATE queue_forecast_tasks SET
  metadata_name     = COALESCE($2, metadata_name),
  normalized_name   = COALESCE($3, normalized_name),
  tags              = COALESCE($4, tags),
  task_created      = COALESCE($5, task_created),
  original_priority = COALESCE(original_priority, $6),
  task_queue_id     = COALESCE($7, task_queue_id),
  task_group_id     = COALESCE($8, task_group_id),
  scheduler_id      = COALESCE($9, scheduler_id),
  project_id        = COALESCE($10, project_id),
  max_run_time_s    = COALESCE($11, max_run_time_s),
  repo_family                    = COALESCE($12, repo_family),
  repo_family_source             = COALESCE($13, repo_family_source),
  repo_family_evidence           = COALESCE($14, repo_family_evidence),
  repo_family_derivation_version = COALESCE($15, repo_family_derivation_version),
  enriched_at       = COALESCE(enriched_at, now())
WHERE task_id = $1;
`;
```

And in `enrichTask`, destructure + append the four params:

```javascript
export async function enrichTask(pool, taskId, enrichment) {
  const {
    metadata_name = null, normalized_name = null,
    tags = null, task_created = null, original_priority = null,
    task_queue_id = null, task_group_id = null,
    scheduler_id = null, project_id = null,
    max_run_time_s = null,
    repo_family = null, repo_family_source = null,
    repo_family_evidence = null, repo_family_derivation_version = null,
  } = enrichment;

  await pool.query(ENRICH_TASK_SQL, [
    taskId,
    metadata_name, normalized_name,
    tags ? JSON.stringify(tags) : null,
    task_created, original_priority,
    task_queue_id, task_group_id,
    scheduler_id, project_id,
    max_run_time_s,
    repo_family, repo_family_source,
    repo_family_evidence, repo_family_derivation_version,
  ]);
}
```

- [ ] **Step 2: Derive + populate in collector `backgroundApiFetch`**

In `src/collector.js`, add the import near the top (next to `normalizeMetadataName`):

```javascript
import { deriveRepoFamily } from './repo-family.js';
```

Inside `backgroundApiFetch`, after `const taskDef = await queueClient.task(taskId);`, add:

```javascript
    const rf = deriveRepoFamily({
      routes: taskDef.routes || [],
      metadataSource: taskDef.metadata?.source ?? null,
      schedulerId: taskDef.schedulerId || status.schedulerId || null,
    });
```

Then extend the `enrichment` object with:

```javascript
      repo_family: rf.family,
      repo_family_source: rf.source,
      repo_family_evidence: rf.evidence,
      repo_family_derivation_version: rf.version,
```

- [ ] **Step 3: Add an enrichment smoke assertion**

In `test/smoke.js`, locate the enrichment test (search for `enrichTask`) and add an assertion after an enrich call with a `repo_family` enrichment that the column round-trips. Mirror the existing assert style:

```javascript
  await enrichTask(pool, taskId, { repo_family: 'try', repo_family_source: 'source', repo_family_evidence: '/try/', repo_family_derivation_version: 1 });
  const { rows: rf } = await pool.query('SELECT repo_family, repo_family_source FROM queue_forecast_tasks WHERE task_id=$1', [taskId]);
  assert(rf[0].repo_family === 'try', `repo_family should be try, got ${rf[0].repo_family}`);
```

- [ ] **Step 4: Run smoke (requires DB)**

Run: `docker compose run --rm predictor node test/smoke.js`
Expected: all smoke cases pass, including the new repo_family round-trip.

- [ ] **Step 5: Checkpoint** — `git add src/collector.js src/db.js test/smoke.js`

### Task 4: Historical backfill of repo-family

**Files:**
- Create: `tools/queue-forecasting/scripts/backfill_repo_family.js`
- Modify: `tools/queue-forecasting/src/db.js` (add `updateRepoFamily`)

- [ ] **Step 1: Add `updateRepoFamily` to db.js**

```javascript
const UPDATE_REPO_FAMILY_SQL = `
UPDATE queue_forecast_tasks SET
  repo_family = $2, repo_family_source = $3,
  repo_family_evidence = $4, repo_family_derivation_version = $5
WHERE task_id = $1;
`;

export async function updateRepoFamily(pool, taskId, rf) {
  await pool.query(UPDATE_REPO_FAMILY_SQL, [taskId, rf.family, rf.source, rf.evidence, rf.version]);
}

const SELECT_REPO_FAMILY_BACKFILL_SQL = `
SELECT task_id FROM queue_forecast_tasks
WHERE repo_family_derivation_version IS DISTINCT FROM $1
  AND task_id IN (
    SELECT DISTINCT task_id FROM queue_forecast_task_runs
    WHERE pending_at >= $2::timestamptz AND pending_at < $3::timestamptz
  )
LIMIT $4;
`;

export async function selectTasksNeedingRepoFamily(pool, version, fromTs, toTs, limit) {
  const { rows } = await pool.query(SELECT_REPO_FAMILY_BACKFILL_SQL, [version, fromTs, toTs, limit]);
  return rows.map(r => r.task_id);
}
```

- [ ] **Step 2: Write the backfill script**

`scripts/backfill_repo_family.js`:

```javascript
// One-off: re-fetch task definitions over a window and derive repo_family.
// Bounded to non-expired definitions; 404/expired tasks are marked unknown
// so we don't re-fetch them forever. Concurrency-limited.
//
// Usage: node scripts/backfill_repo_family.js --from 2026-05-26 --to 2026-06-26
import taskcluster from 'taskcluster-client';
import { createPool, updateRepoFamily, selectTasksNeedingRepoFamily } from '../src/db.js';
import { deriveRepoFamily, REPO_FAMILY_DERIVATION_VERSION } from '../src/repo-family.js';

const MAX_CONCURRENT = 20;
const BATCH = 500;

function parseArgs() {
  const a = process.argv.slice(2);
  let from = null, to = null;
  for (let i = 0; i < a.length; i++) {
    if (a[i] === '--from') from = a[++i];
    else if (a[i] === '--to') to = a[++i];
  }
  if (!from || !to) { console.error('Usage: --from YYYY-MM-DD --to YYYY-MM-DD'); process.exit(1); }
  return { from: `${from}T00:00:00Z`, to: `${to}T00:00:00Z` };
}

async function main() {
  const { from, to } = parseArgs();
  const pool = createPool(process.env.DATABASE_URL);
  const queue = new taskcluster.Queue({ rootUrl: process.env.TASKCLUSTER_ROOT_URL });

  let done = 0, unknown = 0, expired = 0;
  for (;;) {
    const ids = await selectTasksNeedingRepoFamily(pool, REPO_FAMILY_DERIVATION_VERSION, from, to, BATCH);
    if (ids.length === 0) break;
    for (let i = 0; i < ids.length; i += MAX_CONCURRENT) {
      const chunk = ids.slice(i, i + MAX_CONCURRENT);
      await Promise.all(chunk.map(async (taskId) => {
        try {
          const def = await queue.task(taskId);
          const rf = deriveRepoFamily({ routes: def.routes || [], metadataSource: def.metadata?.source ?? null, schedulerId: def.schedulerId ?? null });
          await updateRepoFamily(pool, taskId, rf);
          if (rf.family === 'unknown') unknown++;
        } catch (err) {
          // Expired/deleted definition: mark unknown at this version so it's not retried.
          await updateRepoFamily(pool, taskId, { family: 'unknown', source: 'unknown', evidence: null, version: REPO_FAMILY_DERIVATION_VERSION });
          expired++;
        }
        done++;
      }));
      process.stderr.write(`\r[backfill] done=${done} unknown=${unknown} expired=${expired}`);
    }
  }
  process.stderr.write('\n');
  console.log(`Backfilled repo_family: done=${done} unknown=${unknown} expired=${expired}`);
  await pool.end();
}

main().catch(e => { console.error(e); process.exit(1); });
```

- [ ] **Step 3: Dry-run on a small recent window**

Run:
```bash
docker compose run --rm predictor node scripts/backfill_repo_family.js --from 2026-06-24 --to 2026-06-26
```
Expected: prints `Backfilled repo_family: done=N ...` with N > 0.

- [ ] **Step 4: Verify distribution + audit coverage**

Run:
```bash
docker compose exec postgres psql -U postgres -d forecasting -c \
"SELECT repo_family, repo_family_source, count(*) FROM queue_forecast_tasks WHERE repo_family IS NOT NULL GROUP BY 1,2 ORDER BY 3 DESC;"
```
Expected: a sane distribution (try/autoland/central/release_beta present; `unknown`/`scheduler` minority). Record the `unknown%` — it feeds the Tier-D hygiene gate. **If `scheduler` or `unknown` dominate, stop and revisit `SOURCE_PATTERNS`/`ROUTE_PATTERNS` before proceeding.**

- [ ] **Step 5: Backfill the full training window** (after Step 4 looks right)

Run:
```bash
docker compose run --rm predictor node scripts/backfill_repo_family.js --from 2026-05-26 --to 2026-06-26
```

- [ ] **Step 6: Checkpoint** — `git add scripts/backfill_repo_family.js src/db.js`

---

## Phase 2 — Python queue-context builder (historical reconstruction)

### Task 5: Priority rank + version constants + reference-runs loader

**Files:**
- Create: `tools/queue-forecasting/trainer/src/queue_context.py`
- Modify: `tools/queue-forecasting/trainer/src/data_loader.py`

- [ ] **Step 1: Write the failing test for ranks + version**

`trainer/tests/test_queue_context.py` (start):

```python
import numpy as np
import pandas as pd
from src.queue_context import (
    add_queue_context_features, PRIORITY_RANK, QUEUE_CONTEXT_FEATURE_VERSION,
)

def test_priority_rank_ordering():
    assert PRIORITY_RANK["highest"] > PRIORITY_RANK["very-high"] > PRIORITY_RANK["high"]
    assert PRIORITY_RANK["medium"] > PRIORITY_RANK["low"] > PRIORITY_RANK["very-low"] > PRIORITY_RANK["lowest"]
    assert PRIORITY_RANK["normal"] == PRIORITY_RANK["lowest"]

def test_version_is_int():
    assert isinstance(QUEUE_CONTEXT_FEATURE_VERSION, int)
```

- [ ] **Step 2: Run, expect failure**

Run: `cd trainer && uv run pytest tests/test_queue_context.py -q`
Expected: FAIL — module `src.queue_context` not found.

- [ ] **Step 3: Create the module skeleton with constants**

`trainer/src/queue_context.py` (constants + signature; body filled in Task 6):

```python
"""Queue-context features: what is pending ahead of a task at its pending_at.

Mirrors src/live-predictor/queue-context.js. Computed historically here via a
per-queue event sweep over a reference task_runs frame; identical definitions
must hold on both sides (enforced by QUEUE_CONTEXT_FEATURE_VERSION in the
feature schema)."""
from __future__ import annotations
import numpy as np
import pandas as pd

QUEUE_CONTEXT_FEATURE_VERSION = 1

PRIORITY_RANK = {
    "highest": 7, "very-high": 6, "high": 5, "medium": 4,
    "low": 3, "very-low": 2, "lowest": 1, "normal": 1,
}
UNKNOWN_RANK = 0  # null/unknown priority; never "ahead" of a ranked task.

REPO_FAMILIES = ["try", "autoland", "central", "release_beta", "other", "unknown"]

# Output columns this builder produces (the contract with the config).
FEATURE_COLUMNS = [
    "pending_higher_priority_same_queue",
    "pending_same_priority_same_queue",
    "pending_lower_priority_same_queue",
    "oldest_higher_or_equal_pending_age_same_queue",
    "arrivals_15m_same_queue", "arrivals_60m_same_queue",
    "arrivals_higher_or_equal_15m_same_queue", "arrivals_higher_or_equal_60m_same_queue",
    "starts_higher_or_equal_15m_same_queue",
    "pending_total_per_capacity", "pending_higher_or_equal_per_capacity", "running_per_capacity",
    "running_workers", "existing_capacity", "claimed_tasks",
    "capacity_sample_age_s",
    "capacity_null_reason",   # categorical in the config; ok|no_sample|static_pool_null|zero_capacity
    "backlog_coverage_ratio",
    "pending_try_higher_or_equal_same_queue",
    "pending_autoland_higher_or_equal_same_queue",
    "pending_release_beta_higher_or_equal_same_queue",
]

def _rank(priority) -> int:
    return PRIORITY_RANK.get(priority, UNKNOWN_RANK)

def add_queue_context_features(
    df: pd.DataFrame,
    runs_df: pd.DataFrame,
    worker_counts: pd.DataFrame,
    *,
    capacity_staleness_s: int = 900,
) -> pd.DataFrame:
    raise NotImplementedError  # Task 6
```

- [ ] **Step 4: Run rank/version tests, expect pass**

Run: `cd trainer && uv run pytest tests/test_queue_context.py -q -k "rank or version"`
Expected: PASS (2).

- [ ] **Step 5: Add the reference-runs loader to data_loader.py**

In `trainer/src/data_loader.py`, add (near `load_task_runs_for_throughput`):

```python
def load_task_runs_for_queue_context(c, window_start, as_of_date) -> pd.DataFrame:
    """Reference runs whose pending interval can overlap any training row's
    pending_at in [window_start, as_of_date): pending before as_of_date AND
    (still pending OR started after window_start). Includes priority + family."""
    dsn = os.environ["DATABASE_URL"]
    sql = """
        SELECT r.task_id, r.run_id, r.pending_at, r.started_at,
               r.priority_at_pending, t.task_queue_id, t.repo_family
        FROM queue_forecast_task_runs r
        JOIN queue_forecast_tasks t ON r.task_id = t.task_id
        WHERE r.pending_at < %(as_of)s
          AND (r.started_at IS NULL OR r.started_at > %(wstart)s)
          AND t.task_queue_id IS NOT NULL
    """
    with psycopg.connect(dsn) as conn:
        return pd.read_sql_query(sql, conn, params={"as_of": as_of_date, "wstart": window_start})
```

- [ ] **Step 6: Checkpoint** — `git add trainer/src/queue_context.py trainer/tests/test_queue_context.py trainer/src/data_loader.py`

### Task 6: Event-sweep implementation

**Files:**
- Modify: `tools/queue-forecasting/trainer/src/queue_context.py`
- Modify: `tools/queue-forecasting/trainer/tests/test_queue_context.py`

- [ ] **Step 1: Write the failing fixture tests** (the spec-mandated cases)

Append to `trainer/tests/test_queue_context.py`:

```python
def _runs(rows):
    return pd.DataFrame(rows, columns=["task_id","run_id","pending_at","started_at","priority_at_pending","task_queue_id","repo_family"])

def _t(s):  # short timestamp helper
    return pd.Timestamp(f"2026-06-01T{s}Z")

EMPTY_WC = pd.DataFrame(columns=["task_queue_id","sampled_at","running_workers","existing_capacity","claimed_tasks"])

def test_higher_priority_ahead_includes_same_instant():
    # target T at 00:10; one higher-priority peer pending at exactly T (still pending) -> counts as ahead.
    runs = _runs([
        ["target",0,_t("00:10:00"),None,"low","q/a","try"],
        ["hi",0,_t("00:10:00"),None,"high","q/a","try"],
    ])
    df = runs[runs.task_id=="target"][["task_id","run_id","pending_at","priority_at_pending","task_queue_id","repo_family"]].copy()
    out = add_queue_context_features(df, runs, EMPTY_WC)
    assert out["pending_higher_priority_same_queue"].iloc[0] == 1

def test_same_priority_fifo_excludes_self_and_later():
    # two same-priority peers: one earlier (ahead), one later (not ahead), plus target.
    runs = _runs([
        ["early",0,_t("00:00:00"),None,"medium","q/a","try"],
        ["target",0,_t("00:05:00"),None,"medium","q/a","try"],
        ["late",0,_t("00:09:00"),None,"medium","q/a","try"],
    ])
    df = runs[runs.task_id=="target"][["task_id","run_id","pending_at","priority_at_pending","task_queue_id","repo_family"]].copy()
    out = add_queue_context_features(df, runs, EMPTY_WC)
    assert out["pending_same_priority_same_queue"].iloc[0] == 1  # only 'early'

def test_started_peer_removed():
    # a higher peer that already started before T must not count.
    runs = _runs([
        ["gone",0,_t("00:00:00"),_t("00:03:00"),"high","q/a","try"],
        ["target",0,_t("00:05:00"),None,"low","q/a","try"],
    ])
    df = runs[runs.task_id=="target"][["task_id","run_id","pending_at","priority_at_pending","task_queue_id","repo_family"]].copy()
    out = add_queue_context_features(df, runs, EMPTY_WC)
    assert out["pending_higher_priority_same_queue"].iloc[0] == 0

def test_same_timestamp_tie_order():
    # three same-priority at identical T; tie order by (task_id, run_id). target='b'.
    runs = _runs([
        ["a",0,_t("00:10:00"),None,"medium","q/a","try"],
        ["b",0,_t("00:10:00"),None,"medium","q/a","try"],
        ["c",0,_t("00:10:00"),None,"medium","q/a","try"],
    ])
    df = runs[runs.task_id=="b"][["task_id","run_id","pending_at","priority_at_pending","task_queue_id","repo_family"]].copy()
    out = add_queue_context_features(df, runs, EMPTY_WC)
    assert out["pending_same_priority_same_queue"].iloc[0] == 1  # only 'a' is before 'b'

def test_oldest_higher_or_equal_age():
    runs = _runs([
        ["old",0,_t("00:00:00"),None,"high","q/a","try"],
        ["target",0,_t("00:10:00"),None,"low","q/a","try"],
    ])
    df = runs[runs.task_id=="target"][["task_id","run_id","pending_at","priority_at_pending","task_queue_id","repo_family"]].copy()
    out = add_queue_context_features(df, runs, EMPTY_WC)
    assert out["oldest_higher_or_equal_pending_age_same_queue"].iloc[0] == 600.0  # 10 min

def test_repo_family_blocking_composition():
    runs = _runs([
        ["beta",0,_t("00:00:00"),None,"high","q/a","release_beta"],
        ["target",0,_t("00:10:00"),None,"low","q/a","try"],
    ])
    df = runs[runs.task_id=="target"][["task_id","run_id","pending_at","priority_at_pending","task_queue_id","repo_family"]].copy()
    out = add_queue_context_features(df, runs, EMPTY_WC)
    assert out["pending_release_beta_higher_or_equal_same_queue"].iloc[0] == 1
```

- [ ] **Step 2: Run, expect failure (NotImplementedError)**

Run: `cd trainer && uv run pytest tests/test_queue_context.py -q`
Expected: FAIL — `NotImplementedError`.

- [ ] **Step 3: Implement the sweep** (replace the `raise NotImplementedError` body)

```python
def add_queue_context_features(df, runs_df, worker_counts, *, capacity_staleness_s=900):
    df = df.copy().reset_index(drop=True)
    n = len(df)
    cols = {c: np.full(n, np.nan) for c in FEATURE_COLUMNS}

    # Pre-rank everything once.
    df["_rank"] = df["priority_at_pending"].map(_rank).fillna(UNKNOWN_RANK).astype(int)
    runs = runs_df.copy()
    runs["_rank"] = runs["priority_at_pending"].map(_rank).fillna(UNKNOWN_RANK).astype(int)
    runs["_fam"] = runs["repo_family"].where(runs["repo_family"].isin(REPO_FAMILIES), "unknown")
    # numeric epoch seconds for arithmetic + tie ordering
    for frame in (df, runs):
        frame["_p"] = frame["pending_at"].astype("int64") / 1e9
    runs["_s"] = runs["started_at"].astype("int64") / 1e9  # NaT -> large negative; fix below
    runs.loc[runs["started_at"].isna(), "_s"] = np.inf

    # Group both sides by queue and sweep each queue independently.
    df_by_q = {q: g for q, g in df.groupby("task_queue_id")}
    runs_by_q = {q: g for q, g in runs.groupby("task_queue_id")}

    for q, targets in df_by_q.items():
        rq = runs_by_q.get(q)
        if rq is None:
            continue
        # Sort reference runs by pending time; precompute arrays.
        rq = rq.sort_values(["_p", "task_id", "run_id"]).reset_index(drop=True)
        rp = rq["_p"].to_numpy(); rs = rq["_s"].to_numpy()
        rr = rq["_rank"].to_numpy(); rf = rq["_fam"].to_numpy()
        rid = list(zip(rq["task_id"], rq["run_id"]))

        for ti in targets.index:
            T = df.at[ti, "_p"]; r = df.at[ti, "_rank"]
            self_key = (df.at[ti, "task_id"], df.at[ti, "run_id"])

            active = (rp <= T) & (rs > T)          # pending at T (started_at>T or inf)
            higher = active & (rr > r) & ~_is_self(rid, self_key)
            # same-priority FIFO: strictly-earlier OR same-instant tie before target
            same = active & (rr == r) & ~_is_self(rid, self_key)
            earlier = same & (rp < T)
            tie = same & (rp == T) & _tie_before(rid, self_key)
            lower = active & (rr < r) & ~_is_self(rid, self_key)

            cols["pending_higher_priority_same_queue"][_pos(df,ti)] = int(higher.sum())
            cols["pending_same_priority_same_queue"][_pos(df,ti)] = int((earlier | tie).sum())
            cols["pending_lower_priority_same_queue"][_pos(df,ti)] = int(lower.sum())

            hoe = active & (rr >= r) & ~_is_self(rid, self_key)
            oldest = rp[hoe].min() if hoe.any() else np.nan
            cols["oldest_higher_or_equal_pending_age_same_queue"][_pos(df,ti)] = (T - oldest) if not np.isnan(oldest) else np.nan

            # repo-family blocking composition (higher-or-equal, pending at T)
            for fam, col in (("try","pending_try_higher_or_equal_same_queue"),
                             ("autoland","pending_autoland_higher_or_equal_same_queue"),
                             ("release_beta","pending_release_beta_higher_or_equal_same_queue")):
                cols[col][_pos(df,ti)] = int((hoe & (rf == fam)).sum())

            # flow windows
            for w, secs in ((15,900),(60,3600)):
                arr = (rp > T - secs) & (rp <= T) & ~_is_self(rid, self_key)
                cols[f"arrivals_{w}m_same_queue"][_pos(df,ti)] = int(arr.sum())
                cols[f"arrivals_higher_or_equal_{w}m_same_queue"][_pos(df,ti)] = int((arr & (rr >= r)).sum())
            starts15 = (rs > T - 900) & (rs <= T) & (rr >= r)
            cols["starts_higher_or_equal_15m_same_queue"][_pos(df,ti)] = int(starts15.sum())

            # coverage: reconstructed pending INCLUDING target vs queue_pending
            recon_incl = int(((rp <= T) & (rs > T)).sum())  # self is in rq, so included
            qp = df.at[ti, "queue_pending"] if "queue_pending" in df.columns else np.nan
            cols["backlog_coverage_ratio"][_pos(df,ti)] = (recon_incl / qp) if (qp and qp > 0) else np.nan

    _attach_capacity(df, worker_counts, cols, capacity_staleness_s)
    for c, arr in cols.items():
        df[c] = arr
    return df.drop(columns=["_rank","_p"])
```

Plus the small helpers at module scope:

```python
def _pos(df, idx):  # position in reset-index frame == idx
    return idx

def _is_self(rid_list, self_key):
    return np.array([k == self_key for k in rid_list])

def _tie_before(rid_list, self_key):
    return np.array([k < self_key for k in rid_list])
```

> **Implementer note:** the above is O(n_q) per target (vectorized masks). It is correct and fine for fixture tests and moderate queues; for the full backfill, the slowest single queues (`gecko-t/*`) may need the heap/Fenwick optimization described in the spec §"Backfill strategy". Make the tests pass first, then profile Task 9's NDJSON/feature build; optimize only if a queue exceeds a few seconds. Keep the masked version as the reference oracle the optimized version is tested against.

- [ ] **Step 4: Implement `_attach_capacity`**

```python
def _attach_capacity(df, worker_counts, cols, staleness_s):
    n = len(df)
    cols["capacity_null_reason"] = np.array(["no_sample"] * n, dtype=object)
    if worker_counts is None or worker_counts.empty:
        return
    wc = worker_counts.sort_values("sampled_at")
    for q, targets in df.groupby("task_queue_id"):
        qwc = wc[wc["task_queue_id"] == q]
        if qwc.empty:
            continue
        st = qwc["sampled_at"].astype("int64").to_numpy() / 1e9
        for ti in targets.index:
            T = df.at[ti, "_p"]
            j = np.searchsorted(st, T, side="right") - 1  # latest sample at/before T
            if j < 0:
                continue
            row = qwc.iloc[j]
            age = T - st[j]
            cols["capacity_sample_age_s"][ti] = age
            running = row["running_workers"]; cap = row["existing_capacity"]; claimed = row["claimed_tasks"]
            cols["running_workers"][ti] = running if pd.notna(running) else np.nan
            cols["existing_capacity"][ti] = cap if pd.notna(cap) else np.nan
            cols["claimed_tasks"][ti] = claimed if pd.notna(claimed) else np.nan
            if pd.isna(cap) or cap == 0:
                cols["capacity_null_reason"][ti] = "static_pool_null" if pd.isna(cap) else "zero_capacity"
                continue  # *_per_capacity stays NaN (never impute 0)
            cols["capacity_null_reason"][ti] = "ok"
            qp = df.at[ti, "queue_pending"] if "queue_pending" in df.columns else np.nan
            hoe = cols["pending_higher_or_equal_per_capacity"]  # placeholder; computed below
            cols["pending_total_per_capacity"][ti] = (qp / cap) if (qp and qp > 0) else np.nan
            cols["running_per_capacity"][ti] = (running / cap) if pd.notna(running) else np.nan
```

> Note: `pending_higher_or_equal_per_capacity` needs (higher+same+self) ÷ cap. Compute it in the sweep where you already know `hoe.sum()` and store into `cols["pending_higher_or_equal_per_capacity"]`, then in `_attach_capacity` divide by `cap`. Wire this in Step 3 by stashing the raw higher-or-equal count, then dividing here. Add a fixture test that with cap=10 and 5 higher-or-equal pending the ratio is 0.5.

- [ ] **Step 5: Run all queue_context tests, expect pass**

Run: `cd trainer && uv run pytest tests/test_queue_context.py -q`
Expected: PASS (all). Fix until green.

- [ ] **Step 6: Lint + checkpoint**

Run: `cd trainer && uv run ruff check src/queue_context.py`
Then `git add trainer/src/queue_context.py trainer/tests/test_queue_context.py`

---

## Phase 3 — JS live queue-context builder (must match Python)

### Task 7: `src/live-predictor/queue-context.js`

**Files:**
- Create: `tools/queue-forecasting/src/live-predictor/queue-context.js`
- Test: `tools/queue-forecasting/test/live-predictor/queue-context.test.js`

- [ ] **Step 1: Write the failing test** (fakePool idiom)

`test/live-predictor/queue-context.test.js`:

```javascript
import { test } from 'node:test';
import assert from 'node:assert';
import { getQueueContext, QUEUE_CONTEXT_FEATURE_VERSION } from '../../src/live-predictor/queue-context.js';

function fakePool(rowsByNeedle) {
  return { async query(sql) {
    for (const [needle, rows] of Object.entries(rowsByNeedle)) if (sql.includes(needle)) return { rows };
    return { rows: [] };
  }};
}

test('version matches contract', () => { assert.equal(typeof QUEUE_CONTEXT_FEATURE_VERSION, 'number'); });

test('maps backlog query rows into named features', async () => {
  const pool = fakePool({
    'queue_context_backlog': [{
      pending_higher_priority_same_queue: 3,
      pending_same_priority_same_queue: 2,
      pending_lower_priority_same_queue: 5,
      oldest_higher_or_equal_pending_age_same_queue: 600,
      arrivals_15m_same_queue: 4, arrivals_60m_same_queue: 9,
      arrivals_higher_or_equal_15m_same_queue: 1, arrivals_higher_or_equal_60m_same_queue: 2,
      starts_higher_or_equal_15m_same_queue: 1,
      pending_total_incl_target: 11,
      pending_higher_or_equal_incl_target: 6,
      pending_try_higher_or_equal_same_queue: 1,
      pending_autoland_higher_or_equal_same_queue: 2,
      pending_release_beta_higher_or_equal_same_queue: 0,
    }],
    'queue_context_capacity': [{ running_workers: 8, existing_capacity: 10, claimed_tasks: 7, capacity_sample_age_s: 42 }],
  });
  const row = { task_id: 't', run_id: 0, task_queue_id: 'q/a', pending_at: new Date('2026-06-01T00:10:00Z'),
                priority_at_pending: 'low', queue_pending: 12, repo_family: 'try' };
  const f = await getQueueContext(pool, row);
  assert.equal(f.pending_higher_priority_same_queue, 3);
  assert.equal(f.running_per_capacity, 0.8);
  assert.equal(f.pending_total_per_capacity, 12 / 10);
  assert.equal(f.backlog_coverage_ratio, 11 / 12);
});

test('static-pool null capacity -> per_capacity NaN, reason flag', async () => {
  const pool = fakePool({
    'queue_context_backlog': [{ pending_total_incl_target: 3, pending_higher_or_equal_incl_target: 1 }],
    'queue_context_capacity': [{ running_workers: null, existing_capacity: null, claimed_tasks: 4, capacity_sample_age_s: 30 }],
  });
  const row = { task_queue_id: 'q/a', pending_at: new Date(), priority_at_pending: 'low', queue_pending: 3, repo_family: 'try' };
  const f = await getQueueContext(pool, row);
  assert.ok(Number.isNaN(f.pending_total_per_capacity));
  assert.equal(f.capacity_null_reason, 'static_pool_null');
});
```

- [ ] **Step 2: Run, expect failure** — `node test/live-predictor/queue-context.test.js` → cannot find module.

- [ ] **Step 3: Implement the module**

The two SQL queries reconstruct the same point-in-time sets the Python sweep computes, but for a single target row. Both are tagged with a comment needle for the test mock.

`src/live-predictor/queue-context.js`:

```javascript
// Live queue-context features for ONE target row. Mirrors
// trainer/src/queue_context.py exactly (same definitions, same version).
export const QUEUE_CONTEXT_FEATURE_VERSION = 1;

const PRIORITY_RANK = { highest:7,'very-high':6,high:5,medium:4,low:3,'very-low':2,lowest:1,normal:1 };
const rank = (p) => PRIORITY_RANK[p] ?? 0;

// Backlog + flow in one query. $1=queue $2=T $3=rank $4=task_id $5=run_id.
const BACKLOG_SQL = `-- queue_context_backlog
WITH ahead AS (
  SELECT r.priority_at_pending AS pr, r.pending_at, r.started_at, t.repo_family,
         CASE r.priority_at_pending
           WHEN 'highest' THEN 7 WHEN 'very-high' THEN 6 WHEN 'high' THEN 5
           WHEN 'medium' THEN 4 WHEN 'low' THEN 3 WHEN 'very-low' THEN 2
           WHEN 'lowest' THEN 1 WHEN 'normal' THEN 1 ELSE 0 END AS rnk
  FROM queue_forecast_task_runs r
  JOIN queue_forecast_tasks t ON r.task_id = t.task_id
  WHERE t.task_queue_id = $1
    AND r.pending_at <= $2::timestamptz
    AND (r.started_at IS NULL OR r.started_at > $2::timestamptz)
)
SELECT
  count(*) FILTER (WHERE rnk > $3 AND NOT (pending_at = $2::timestamptz AND FALSE)) AS pending_higher_priority_same_queue,
  count(*) FILTER (WHERE rnk = $3 AND pending_at < $2::timestamptz)                 AS pending_same_priority_same_queue,
  count(*) FILTER (WHERE rnk < $3)                                                  AS pending_lower_priority_same_queue,
  EXTRACT(EPOCH FROM ($2::timestamptz - min(pending_at) FILTER (WHERE rnk >= $3)))  AS oldest_higher_or_equal_pending_age_same_queue,
  count(*)                                                                          AS pending_total_incl_target,
  count(*) FILTER (WHERE rnk >= $3)                                                 AS pending_higher_or_equal_incl_target,
  count(*) FILTER (WHERE rnk >= $3 AND repo_family = 'try')                         AS pending_try_higher_or_equal_same_queue,
  count(*) FILTER (WHERE rnk >= $3 AND repo_family = 'autoland')                    AS pending_autoland_higher_or_equal_same_queue,
  count(*) FILTER (WHERE rnk >= $3 AND repo_family = 'release_beta')                AS pending_release_beta_higher_or_equal_same_queue,
  (SELECT count(*) FROM queue_forecast_task_runs r2 JOIN queue_forecast_tasks t2 ON r2.task_id=t2.task_id
     WHERE t2.task_queue_id=$1 AND r2.pending_at > $2::timestamptz - INTERVAL '15 minutes' AND r2.pending_at <= $2::timestamptz) AS arrivals_15m_same_queue,
  (SELECT count(*) FROM queue_forecast_task_runs r2 JOIN queue_forecast_tasks t2 ON r2.task_id=t2.task_id
     WHERE t2.task_queue_id=$1 AND r2.pending_at > $2::timestamptz - INTERVAL '60 minutes' AND r2.pending_at <= $2::timestamptz) AS arrivals_60m_same_queue
FROM ahead;`;
```

> **Note on FIFO/self-exclusion in SQL:** at live time the target row is already in `queue_forecast_task_runs`, so the same-priority count uses `pending_at < $2` (strictly earlier), which naturally excludes the target and same-instant-but-later peers; same-instant-earlier peers are conservatively excluded live (the watermark in Task 8 ensures the cohort has landed; residual difference is captured by `backlog_coverage_ratio`). Higher-priority uses `pending_at <= $2` and excludes the target via `rnk > $3` (the target is rank `$3`, not `> $3`). Keep this comment in the file.

Continue the module:

```javascript
const CAPACITY_SQL = `-- queue_context_capacity
SELECT running_workers, existing_capacity, claimed_tasks,
       EXTRACT(EPOCH FROM ($2::timestamptz - sampled_at)) AS capacity_sample_age_s
FROM queue_forecast_worker_counts
WHERE task_queue_id = $1 AND sampled_at <= $2::timestamptz
ORDER BY sampled_at DESC LIMIT 1;`;

const ALL_NAN = Object.freeze({
  pending_higher_priority_same_queue: NaN, pending_same_priority_same_queue: NaN,
  pending_lower_priority_same_queue: NaN, oldest_higher_or_equal_pending_age_same_queue: NaN,
  arrivals_15m_same_queue: NaN, arrivals_60m_same_queue: NaN,
  arrivals_higher_or_equal_15m_same_queue: NaN, arrivals_higher_or_equal_60m_same_queue: NaN,
  starts_higher_or_equal_15m_same_queue: NaN,
  pending_total_per_capacity: NaN, pending_higher_or_equal_per_capacity: NaN, running_per_capacity: NaN,
  running_workers: NaN, existing_capacity: NaN, claimed_tasks: NaN,
  capacity_sample_age_s: NaN, capacity_null_reason: 'no_sample',
  backlog_coverage_ratio: NaN,
  pending_try_higher_or_equal_same_queue: NaN,
  pending_autoland_higher_or_equal_same_queue: NaN,
  pending_release_beta_higher_or_equal_same_queue: NaN,
});

const num = (v) => (v === null || v === undefined ? NaN : Number(v));

export async function getQueueContext(pool, row) {
  if (!row.task_queue_id || !row.pending_at) return { ...ALL_NAN };
  const r = rank(row.priority_at_pending);
  const args = [row.task_queue_id, row.pending_at, r, row.task_id ?? '', row.run_id ?? -1];
  const [bRes, cRes] = await Promise.all([pool.query(BACKLOG_SQL, args), pool.query(CAPACITY_SQL, args)]);
  const b = bRes.rows[0] || {};
  const c = cRes.rows[0] || null;

  const out = { ...ALL_NAN };
  for (const k of ['pending_higher_priority_same_queue','pending_same_priority_same_queue',
    'pending_lower_priority_same_queue','oldest_higher_or_equal_pending_age_same_queue',
    'arrivals_15m_same_queue','arrivals_60m_same_queue',
    'pending_try_higher_or_equal_same_queue','pending_autoland_higher_or_equal_same_queue',
    'pending_release_beta_higher_or_equal_same_queue']) out[k] = num(b[k]);
  // arrivals_higher_or_equal + starts_higher_or_equal: add to BACKLOG_SQL the same way
  // (omitted here for brevity in the snippet — add the two FILTER subqueries and map them).

  const qp = num(row.queue_pending);
  const totalIncl = num(b.pending_total_incl_target);
  out.backlog_coverage_ratio = qp > 0 ? totalIncl / qp : NaN;

  if (c) {
    out.running_workers = num(c.running_workers);
    out.existing_capacity = num(c.existing_capacity);
    out.claimed_tasks = num(c.claimed_tasks);
    out.capacity_sample_age_s = num(c.capacity_sample_age_s);
    const cap = out.existing_capacity;
    if (!Number.isFinite(cap) || cap === 0) {
      out.capacity_null_reason = !Number.isFinite(cap) ? 'static_pool_null' : 'zero_capacity';
    } else {
      out.capacity_null_reason = 'ok';
      out.pending_total_per_capacity = qp > 0 ? qp / cap : NaN;
      out.pending_higher_or_equal_per_capacity = num(b.pending_higher_or_equal_incl_target) / cap;
      out.running_per_capacity = Number.isFinite(out.running_workers) ? out.running_workers / cap : NaN;
    }
  }
  return out;
}
```

> **Implementer task:** finish the two omitted `arrivals_higher_or_equal_*` + `starts_higher_or_equal_15m_same_queue` columns in `BACKLOG_SQL` (FILTER on `rnk >= $3` plus the started_at window), and map them in the loop. Add a fixture test asserting them, mirroring the Python test.

- [ ] **Step 4: Run tests, expect pass** — `node test/live-predictor/queue-context.test.js`

- [ ] **Step 5: Cross-check parity with Python**

Add a comment block at the top of both modules listing `FEATURE_COLUMNS` and assert (by eye + a checklist in the PR) that the two lists are identical. Add a tiny test in each that the produced object keys === the expected feature set.

- [ ] **Step 6: Checkpoint** — `git add src/live-predictor/queue-context.js test/live-predictor/queue-context.test.js`

---

## Phase 4 — Wire features into serving + config + schema

### Task 8: Merge into predict.js + prediction watermark + audit

**Files:**
- Modify: `tools/queue-forecasting/src/live-predictor/predict.js` (37-45, 120-126, 169-197)
- Modify: `tools/queue-forecasting/src/live-predictor/index.js` (POLL_SQL or processOne)

- [ ] **Step 1: Add repo_family to FETCH_ROW_SQL**

In `predict.js` `FETCH_ROW_SQL`, add `t.repo_family` to the selected `t.` columns.

- [ ] **Step 2: Call getQueueContext + merge into liveFeatures**

Import at top: `import { getQueueContext, QUEUE_CONTEXT_FEATURE_VERSION } from './queue-context.js';`

After the throughput fetch, add:
```javascript
  const queueContext = await getQueueContext(pool, row);
```
Extend the `liveFeatures` object literal with `...queueContext,`.

- [ ] **Step 3: Stamp the audit object**

In `inputFeaturesAudit`, add:
```javascript
    queue_context_at_pending: {
      feature_version: QUEUE_CONTEXT_FEATURE_VERSION,
      ...queueContext,
    },
```

- [ ] **Step 4: Add the prediction watermark**

In `index.js`, change the poll selection so a row is only scored once `now() - pending_at >= W`. Add to `POLL_SQL`'s WHERE:
```sql
  AND r.pending_at <= now() - (($4)::int || ' seconds')::interval
```
and pass `QUEUE_CONTEXT_PREDICT_DELAY_S` (default e.g. 30, env-overridable) as `$4`. Document: this lets same-instant sibling pending events land before scoring, matching the trainer's full as-of-T visibility (spec P2).

- [ ] **Step 5: Run live-predictor unit tests**

Run: `node test/live-predictor/predict.test.js` (if present) and `node test/live-predictor/queue-context.test.js`.
Expected: PASS. If `predict.test.js` mocks the pool, add `queue_context_backlog`/`queue_context_capacity` needles returning zeros so it still passes.

- [ ] **Step 6: Checkpoint** — `git add src/live-predictor/predict.js src/live-predictor/index.js`

### Task 9: Wire into trainer data_loader + config + schema

**Files:**
- Modify: `trainer/src/data_loader.py` (`candidate_cols` ~144, `load()` ~373-443)
- Modify: `trainer/src/train.py` (~186-204)
- Create: `trainer/configs/wait_time_residual_throughput_filtered_baseline_qctx.yaml`

- [ ] **Step 1: Add repo_family to candidate_cols + queue-context to load()**

In `data_loader.py` `_build_query` `candidate_cols`, add:
```python
        "repo_family":         "t.repo_family",
```

In `load()`, after the throughput block, add a queue-context block gated on config:
```python
    if getattr(c, "queue_context_features", None) and c.queue_context_features.get("enabled"):
        from src.queue_context import add_queue_context_features
        w = compute_windows(c)
        runs_qc = load_task_runs_for_queue_context(c, w.train_start - pd.Timedelta(minutes=90), w.as_of_date)
        wc = load_worker_counts(c, w.train_start - pd.Timedelta(minutes=30), w.as_of_date)
        df = add_queue_context_features(df, runs_qc, wc)
```
Add `queue_context_features` to the `Config` dataclass in `trainer/src/config.py` (mirror how `throughput_features` is declared) and to `serving_hash` inputs so cache keys change when it toggles.

- [ ] **Step 2: Write the qctx config**

Copy `wait_time_residual_throughput_filtered_baseline.yaml` to `..._qctx.yaml` and: (a) append all `FEATURE_COLUMNS` numeric features to `numeric_features` (except `capacity_null_reason`, which is categorical — add it to `categorical_features`), and (b) add:
```yaml
queue_context_features:
  enabled: true
  version: 1
```
List exactly (numeric): `pending_higher_priority_same_queue, pending_same_priority_same_queue, pending_lower_priority_same_queue, oldest_higher_or_equal_pending_age_same_queue, arrivals_15m_same_queue, arrivals_60m_same_queue, arrivals_higher_or_equal_15m_same_queue, arrivals_higher_or_equal_60m_same_queue, starts_higher_or_equal_15m_same_queue, pending_total_per_capacity, pending_higher_or_equal_per_capacity, running_per_capacity, running_workers, existing_capacity, claimed_tasks, capacity_sample_age_s, backlog_coverage_ratio, pending_try_higher_or_equal_same_queue, pending_autoland_higher_or_equal_same_queue, pending_release_beta_higher_or_equal_same_queue`. Categorical: `repo_family, capacity_null_reason`.

- [ ] **Step 3: Write queue_context block into feature_schema.json**

In `train.py` `feature_schema` dict, add:
```python
        "queue_context_features": getattr(c, "queue_context_features", None),
```

- [ ] **Step 4: Unit test the loader wiring**

Add `trainer/tests/test_data_loader_qctx.py` that builds a tiny in-memory `df` + `runs` + `wc` and asserts `load`-style wiring calls `add_queue_context_features` (or test `add_queue_context_features` is invoked by monkeypatching). Keep it light — the heavy logic is already tested in Task 6.

Run: `cd trainer && uv run pytest tests/test_data_loader_qctx.py -q` → PASS.

- [ ] **Step 5: Checkpoint** — `git add trainer/src/data_loader.py trainer/src/config.py trainer/src/train.py trainer/configs/wait_time_residual_throughput_filtered_baseline_qctx.yaml`

---

## Phase 5 — Ablation configs, retrain, walk-forward

### Task 10: Create the ablation-step configs

**Files:** Create under `trainer/configs/`:
- `wait_qctx_a_capacity.yaml` — base + capacity/coverage features only.
- `wait_qctx_b_priority.yaml` — + Tier A priority-ahead + oldest-age.
- `wait_qctx_c_flow.yaml` — + Tier C flow.
- `wait_qctx_d_repofamily.yaml` — + Tier D repo-family (== full `_qctx.yaml`).

- [ ] **Step 1:** For each, start from the production wait config and add only that step's feature names + the `queue_context_features` block. The ablation order is `current → +capacity → +priority-ahead → +flow → +repo-family-blocking → all` (the production `wait_time_residual_throughput_filtered_baseline.yaml` is "current").

- [ ] **Step 2: Checkpoint** — `git add trainer/configs/wait_qctx_*.yaml`

### Task 11: Run the walk-forward ablation

- [ ] **Step 1: Quick single-cohort train to validate end-to-end**

Run:
```bash
cd tools/queue-forecasting && ./scripts/run_training.sh configs/wait_qctx_d_repofamily.yaml --as-of-date 2026-06-20
```
Expected: produces `trainer/data/models/2026-06-20/wait_time_residual_throughput_filtered_baseline_qctx_*` artifacts + manifest, no crash. Confirms reconstruction + retrain wire up.

- [ ] **Step 2: Run the sweep over all ablation configs**

Run:
```bash
./scripts/walk_forward.sh --from 2026-06-05 --to 2026-06-19 \
  --configs configs/wait_time_residual_throughput_filtered_baseline.yaml,configs/wait_qctx_a_capacity.yaml,configs/wait_qctx_b_priority.yaml,configs/wait_qctx_c_flow.yaml,configs/wait_qctx_d_repofamily.yaml
```

- [ ] **Step 3: Summarize**

Run:
```bash
cd trainer && uv run python scripts/summarize_walk_forward.py --from 2026-06-05 --to 2026-06-19 --configs '*'
```
Expected: per-config MAE Δ%, within-2x, p90 in-band, 30m+ within-2x, and (after Task 12) the new `30mplus_wait_p90_miss` column. **Decision gates:** primary = global 30m+ wait p90 miss materially down, no regression on overall p90 / p50 / within-2x / MAE, stable across cohorts; Tier D ships only if it lifts the shared-pool slice.

- [ ] **Step 4:** Record results in `next-steps.md` §4.1 and the memory `project_tail_accuracy_program`.

---

## Phase 6 — Instrumentation (tail-miss as a first-class metric)

### Task 12: summarize_walk_forward 30m+ wait p90 miss column

**Files:** Modify `trainer/scripts/summarize_walk_forward.py` (27-36, 182-215, and the per-cohort metric extraction).

- [ ] **Step 1: Add the field**

In `FIELDNAMES`, add `"30mplus_wait_p90_miss"`. In the per-cohort metric computation (where bucket within-2x is read from the eval JSON), also read the 30m+ wait bucket p90 miss rate (= `bad / n` for the 30m+ actual-wait bucket, completed-only). If the eval JSON doesn't already emit it, add it to `trainer/src/evaluate.py` alongside the existing bucket metrics.

- [ ] **Step 2: Print it in the target block**

In `_print_target_block`, add a line mirroring the `30m+ w/in2x` line:
```python
        miss30 = [r["30mplus_wait_p90_miss"] for r in cfg_rows if r.get("30mplus_wait_p90_miss") is not None]
        if miss30:
            under_35 = sum(1 for m in miss30 if m < 0.35)
            print(f"  30m+ wait p90 miss: mean={_fmt(stats.mean(miss30)*100,'%')}  <35%: {under_35}/{len(miss30)}", file=sys.stderr)
```

- [ ] **Step 3: Add a unit test** in `trainer/tests/` that feeds a synthetic eval row and asserts the column is computed. Run `uv run pytest -q`.

- [ ] **Step 4: Checkpoint** — `git add trainer/scripts/summarize_walk_forward.py trainer/src/evaluate.py trainer/tests/`

### Task 13: Dashboard 30m+ wait p90 miss section

**Files:** Modify `tools/queue-forecasting/src/dashboard-gen.js` (add a function near `queryAggregationsByWaitBucket` ~479; add a section near ~1667).

- [ ] **Step 1: Add a miss-rate query function**

```javascript
async function queryWaitTailMissRate(pool, windowDays) {
  const { rows } = await pool.query(`
    SELECT ${WAIT_DURATION_BUCKET_SQL} AS dim,
      count(*) FILTER (WHERE r.wait_duration_s IS NOT NULL AND p.wait_p90_s IS NOT NULL AND r.reason_resolved='completed') AS n,
      count(*) FILTER (WHERE r.reason_resolved='completed' AND r.wait_duration_s > p.wait_p90_s) AS bad
    FROM queue_forecast_run_predictions p
    JOIN queue_forecast_task_runs r USING (task_id, run_id)
    WHERE r.resolved_at IS NOT NULL AND r.resolved_at >= now() - ($1 || ' days')::interval
    GROUP BY 1
    ORDER BY CASE dim WHEN '<1m' THEN 1 WHEN '1-5m' THEN 2 WHEN '5-30m' THEN 3 WHEN '30m+' THEN 4 ELSE 9 END;
  `, [windowDays]);
  return rows;
}
```

- [ ] **Step 2: Render a small table** (mirror an existing section's HTML build) showing `dim | n | miss% = bad/n`, with the `30m+` row highlighted against the <35%/<30% gate. Wire its output into the page near the existing "By Actual Wait Bucket" section.

- [ ] **Step 3: Generate locally + eyeball**

Run (against the DB):
```bash
docker compose run --rm dashboard-gen node src/dashboard-gen.js --once   # or the project's one-shot invocation
```
Expected: the new section renders with a sane 30m+ miss%.

- [ ] **Step 4: Checkpoint** — `git add src/dashboard-gen.js`

---

## Phase 7 — Deploy gating (only if ablation passes)

### Task 14: Promote + deploy (manual, gated)

- [ ] **Step 1:** If the ablation passes the primary gate, set the qctx config as the production wait config (update `WAIT_STEM` in `index.js` only if the stem name changes; prefer keeping the production stem name and folding qctx features into the existing production config so `WAIT_STEM` is unchanged).
- [ ] **Step 2:** Retrain the production wait + duration on the latest `--as-of-date`, ensuring BOTH stems land in the same `trainer/data/models/<date>/` dir (live-predictor crash-loops otherwise — see `reference_qf_deployment_ops`).
- [ ] **Step 3:** Deploy: ensure the collector is capturing repo_family forward and the watermark env is set; restart `live-predictor` to load the new model. **Serving feature builder + trained model must share `QUEUE_CONTEXT_FEATURE_VERSION`.**
- [ ] **Step 4:** Verify on the next dashboard refresh: 30m+ wait p90 miss trends down; `repo_family` unknown% and `capacity_null_reason` distribution look sane; no overall p90 regression. (User handles the actual deploy + commit.)

---

## Self-Review notes (for the implementer)

- **Parity is the #1 risk.** Before retrain, diff the Python `FEATURE_COLUMNS` against the JS `getQueueContext` output keys and against the config `numeric_features`/`categorical_features` — all three must agree. A mismatch silently corrupts the log-ratio anchor.
- **Leakage:** every reconstructed value uses only timestamps ≤ T; capacity uses the latest sample at/before T. The fixture tests pin this — do not "fix" a failing leakage test by relaxing the bound.
- **Coverage is hygiene, not a correction:** `backlog_coverage_ratio` is a feature + flag; never rescale other features by it.
- **Optimize the sweep only if needed:** keep the masked reference implementation as the oracle; if you add a heap/Fenwick fast path, test it against the oracle on random fixtures.
