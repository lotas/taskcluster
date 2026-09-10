import { test } from 'node:test';
import assert from 'node:assert';
import { baselineExportRecord } from '../src/utils.js';

const row = { task_id: 'T1', run_id: 0, pending_at: new Date('2026-08-01T00:00:00Z') };

test('baselineExportRecord carries p50/p90 AND level/sample_size for both targets', () => {
  const blD = { level: 'metadata_name', p50: '10.5', p90: '20', sample_size: 41 };
  const blW = { level: 'queue+priority+bucket', p50: '3', p90: '9.25', sample_size: 7 };
  assert.deepEqual(baselineExportRecord(row, blD, blW), {
    task_id: 'T1', run_id: 0, pending_at: '2026-08-01T00:00:00.000Z',
    bl_duration_p50: 10.5, bl_duration_p90: 20,
    bl_duration_level: 'metadata_name', bl_duration_sample_size: 41,
    bl_wait_p50: 3, bl_wait_p90: 9.25,
    bl_wait_level: 'queue+priority+bucket', bl_wait_sample_size: 7,
  });
});

test('baselineExportRecord writes nulls, not undefined, when a baseline is missing', () => {
  const rec = baselineExportRecord(row, null, null);
  for (const k of ['bl_duration_p50', 'bl_duration_p90', 'bl_duration_level', 'bl_duration_sample_size',
                   'bl_wait_p50', 'bl_wait_p90', 'bl_wait_level', 'bl_wait_sample_size']) {
    assert.strictEqual(rec[k], null, k);
  }
  // JSON.stringify drops undefined keys; null survives, so the NDJSON schema is stable.
  assert.equal(Object.keys(JSON.parse(JSON.stringify(rec))).length, 11);
});

test('baselineExportRecord accepts a pending_at that is already a string', () => {
  const rec = baselineExportRecord({ ...row, pending_at: '2026-08-01T00:00:00+00:00' }, null, null);
  assert.equal(rec.pending_at, '2026-08-01T00:00:00+00:00');
});
