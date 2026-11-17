import { test } from 'node:test';
import assert from 'node:assert';
import { enrichTask } from '../src/db.js';

test('enrichTask includes repo_family columns and maps params to $12..$15', async () => {
  let captured;
  const pool = { async query(sql, params) { captured = { sql, params }; return { rows: [] }; } };
  await enrichTask(pool, 'TASK1', {
    metadata_name: 'x', repo_family: 'try', repo_family_source: 'source',
    repo_family_evidence: '/try/', repo_family_derivation_version: 1,
  });
  assert.ok(captured.sql.includes('repo_family_source') && captured.sql.includes('repo_family_derivation_version'),
    'SQL must reference the repo_family columns');
  assert.ok(/repo_family\s*=\s*COALESCE\(\$12/.test(captured.sql), 'repo_family must be $12');
  assert.ok(/repo_family_source\s*=\s*COALESCE\(\$13/.test(captured.sql));
  assert.ok(/repo_family_evidence\s*=\s*COALESCE\(\$14/.test(captured.sql));
  assert.ok(/repo_family_derivation_version\s*=\s*COALESCE\(\$15/.test(captured.sql));
  assert.equal(captured.params[0], 'TASK1');
  assert.equal(captured.params[11], 'try');   // $12
  assert.equal(captured.params[12], 'source'); // $13
  assert.equal(captured.params[13], '/try/');  // $14
  assert.equal(captured.params[14], 1);        // $15
});

test('enrichTask leaves repo_family params null when not provided', async () => {
  let captured;
  const pool = { async query(sql, params) { captured = { sql, params }; return { rows: [] }; } };
  await enrichTask(pool, 'T2', { metadata_name: 'x' });
  assert.equal(captured.params[11], null);
  assert.equal(captured.params[14], null);
});

function capturingPool(rowCount = 1) {
  const calls = [];
  return { calls, async query(sql, params) { calls.push({ sql, params }); return { rows: [], rowCount }; } };
}

test('enrichTask writes requires/deadline/dependency_count as $16..$18', async () => {
  const pool = capturingPool();
  await enrichTask(pool, 'T3', {
    metadata_name: 'x', requires: 'all-completed', deadline: '2026-11-17T00:00:00.000Z',
    dependencies: ['D1', 'D2', 'D1'],
  });
  const [upd] = pool.calls;
  assert.ok(/requires\s*=\s*COALESCE\(\$16/.test(upd.sql));
  assert.ok(/deadline\s*=\s*COALESCE\(\$17/.test(upd.sql));
  assert.ok(/dependency_count\s*=\s*COALESCE\(\$18/.test(upd.sql));
  assert.equal(upd.params[15], 'all-completed');
  assert.equal(upd.params[16], '2026-11-17T00:00:00.000Z');
  assert.equal(upd.params[17], 2, 'dependency_count counts unique deps');
});

test('enrichTask stores deduplicated dependencies inline as $19', async () => {
  const pool = capturingPool(1);
  await enrichTask(pool, 'T4', { metadata_name: 'x', dependencies: ['D1', 'D2', 'D1', 'T4'] });
  assert.equal(pool.calls.length, 1, 'single UPDATE, no edge-table insert');
  assert.ok(/dependencies\s*=\s*COALESCE\(\$19/.test(pool.calls[0].sql));
  assert.deepEqual(pool.calls[0].params[18], ['D1', 'D2']);
  assert.equal(pool.calls[0].params[17], 2);
});

test('enrichTask records dependency_count 0 and an empty array for no deps', async () => {
  const pool = capturingPool(1);
  await enrichTask(pool, 'T5', { metadata_name: 'x', dependencies: [] });
  assert.equal(pool.calls[0].params[17], 0);
  assert.deepEqual(pool.calls[0].params[18], []);
});

test('enrichTask leaves dependency fields NULL when deps were not fetched', async () => {
  const pool = capturingPool(1);
  await enrichTask(pool, 'T6', { metadata_name: 'x' });
  assert.equal(pool.calls[0].params[17], null);
  assert.equal(pool.calls[0].params[18], null);
});
