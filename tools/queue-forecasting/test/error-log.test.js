import { test } from 'node:test';
import assert from 'node:assert';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { createErrorLog } from '../src/error-log.js';

function end(log) {
  return new Promise(resolve => log.end(resolve));
}

test('rotates to .1 when the cap is reached and keeps disk use bounded', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'qf-errlog-'));
  const file = path.join(dir, 'errors.log');
  const log = createErrorLog(file, { maxBytes: 1000 });
  const line = 'x'.repeat(99) + '\n';
  for (let i = 0; i < 100; i++) log.write(line);
  await end(log);
  const cur = fs.statSync(file).size;
  const old = fs.statSync(`${file}.1`).size;
  assert.ok(cur <= 1000, `current ${cur}`);
  assert.ok(old <= 1000, `rotated ${old}`);
  assert.deepEqual(fs.readdirSync(dir).sort(), ['errors.log', 'errors.log.1']);
  fs.rmSync(dir, { recursive: true });
});

test('counts an existing file toward the cap', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'qf-errlog-'));
  const file = path.join(dir, 'errors.log');
  fs.writeFileSync(file, 'y'.repeat(950));
  const log = createErrorLog(file, { maxBytes: 1000 });
  log.write('z'.repeat(100));
  await end(log);
  assert.equal(fs.statSync(`${file}.1`).size, 950);
  assert.equal(fs.readFileSync(file, 'utf8'), 'z'.repeat(100));
  fs.rmSync(dir, { recursive: true });
});
