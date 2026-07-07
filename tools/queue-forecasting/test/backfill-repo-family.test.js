import { test } from 'node:test';
import assert from 'node:assert';
import { updateRepoFamily, selectTasksNeedingRepoFamily, resetStaleRepoFamily } from '../src/db.js';

test('updateRepoFamily maps rf fields to params $1..$5', async () => {
  let cap;
  const pool = { async query(sql, params){ cap = {sql, params}; return {rows:[]}; } };
  await updateRepoFamily(pool, 'T1', { family:'try', source:'source', evidence:'/try/', version:1 });
  assert.ok(/UPDATE queue_forecast_tasks/.test(cap.sql));
  assert.deepEqual(cap.params, ['T1','try','source','/try/',1]);
});

test('selectTasksNeedingRepoFamily returns task_id list', async () => {
  const pool = { async query(){ return {rows:[{task_id:'A'},{task_id:'B'}]}; } };
  const r = await selectTasksNeedingRepoFamily(pool, '2026-06-01', '2026-06-02', 500);
  assert.deepEqual(r, ['A','B']);
});

test('selectTasksNeedingRepoFamily uses the index-friendly IS NULL + EXISTS fast path', async () => {
  let cap;
  const pool = { async query(sql, params){ cap = {sql, params}; return {rows:[]}; } };
  await selectTasksNeedingRepoFamily(pool, '2026-06-01', '2026-06-02', 500);

  // "needs work" must be expressed as IS NULL so the partial index
  // idx_qf_tasks_needs_repo_family can drive selection (IS DISTINCT FROM is not
  // implied by the index predicate and would force a full scan).
  assert.ok(/repo_family_derivation_version\s+IS\s+NULL/i.test(cap.sql),
    'must select on repo_family_derivation_version IS NULL');
  assert.ok(!/IS\s+DISTINCT\s+FROM/i.test(cap.sql),
    'must NOT use IS DISTINCT FROM (defeats the partial index)');

  // Window membership must be a correlated EXISTS (uses the task_runs PK),
  // not IN (SELECT DISTINCT ...) which rebuilds a full-window hash every batch.
  assert.ok(/EXISTS\s*\(/i.test(cap.sql), 'must use EXISTS for window membership');
  assert.ok(!/SELECT\s+DISTINCT/i.test(cap.sql),
    'must NOT rebuild a DISTINCT set every batch');

  // Ordered scan so processed rows (which leave the partial index) never get
  // rescanned across batches.
  assert.ok(/ORDER\s+BY\s+t\.task_id/i.test(cap.sql), 'must ORDER BY task_id');

  // Params are now (from, to, limit) — no version param in the WHERE.
  assert.deepEqual(cap.params, ['2026-06-01', '2026-06-02', 500]);
});

test('resetStaleRepoFamily re-NULLs stale-version rows within the window and returns rowCount', async () => {
  let cap;
  const pool = { async query(sql, params){ cap = {sql, params}; return { rowCount: 7 }; } };
  const n = await resetStaleRepoFamily(pool, 2, '2026-06-01', '2026-06-02');
  assert.equal(n, 7);
  assert.ok(/UPDATE queue_forecast_tasks/i.test(cap.sql));
  assert.ok(/repo_family_derivation_version\s*=\s*NULL/i.test(cap.sql),
    'must NULL the derivation version so the row is reselected');
  // Only rows carrying a DIFFERENT (older) version get reset; current-version
  // and already-NULL rows are left alone.
  assert.ok(/repo_family_derivation_version\s+IS\s+NOT\s+NULL/i.test(cap.sql));
  assert.ok(/repo_family_derivation_version\s*<>\s*\$1/i.test(cap.sql));
  // Scoped to the same window as the backfill via EXISTS.
  assert.ok(/EXISTS\s*\(/i.test(cap.sql));
  assert.deepEqual(cap.params, [2, '2026-06-01', '2026-06-02']);
});
