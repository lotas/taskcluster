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
