import { test } from 'node:test';
import assert from 'node:assert';
import { updateRepoFamily, selectTasksNeedingRepoFamily } from '../src/db.js';

test('updateRepoFamily maps rf fields to params $1..$5', async () => {
  let cap;
  const pool = { async query(sql, params){ cap = {sql, params}; return {rows:[]}; } };
  await updateRepoFamily(pool, 'T1', { family:'try', source:'source', evidence:'/try/', version:1 });
  assert.ok(/UPDATE queue_forecast_tasks/.test(cap.sql));
  assert.deepEqual(cap.params, ['T1','try','source','/try/',1]);
});

test('selectTasksNeedingRepoFamily returns task_id list', async () => {
  const pool = { async query(){ return {rows:[{task_id:'A'},{task_id:'B'}]}; } };
  const r = await selectTasksNeedingRepoFamily(pool, 1, '2026-06-01', '2026-06-02', 500);
  assert.deepEqual(r, ['A','B']);
});
