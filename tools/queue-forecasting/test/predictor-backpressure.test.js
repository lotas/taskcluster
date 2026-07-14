import { test } from 'node:test';
import assert from 'node:assert';
import { Writable } from 'node:stream';
import { writeLineWithBackpressure } from '../src/utils.js';

// Real Writable with a tiny highWaterMark so write() deterministically
// returns false, plus a delayed drain -- proves the caller actually waits
// for 'drain' instead of the bug this guards against (writing straight
// through backpressure until the process runs out of heap).
function makeSlowStream({ drainDelayMs = 20 } = {}) {
  const received = [];
  let drainScheduled = false;
  const stream = new Writable({
    highWaterMark: 1, // smallest possible buffer -> next write() returns false
    write(chunk, enc, cb) {
      received.push(chunk.toString());
      if (!drainScheduled) {
        drainScheduled = true;
        setTimeout(() => { drainScheduled = false; cb(); }, drainDelayMs);
      } else {
        cb();
      }
    },
  });
  return { stream, received };
}

test('writeLineWithBackpressure resolves immediately when the buffer has room', async () => {
  const { stream, received } = makeSlowStream({ drainDelayMs: 0 });
  await writeLineWithBackpressure(stream, 'a\n');
  assert.deepEqual(received, ['a\n']);
});

test('writeLineWithBackpressure waits for drain before resolving under backpressure', async () => {
  const { stream, received } = makeSlowStream({ drainDelayMs: 30 });

  const t0 = Date.now();
  // First write starts the slow underlying write (cb only fires after
  // drainDelayMs); highWaterMark=1 means the SECOND write is guaranteed to
  // return false (buffer already occupied by the first, un-acked write).
  await writeLineWithBackpressure(stream, 'a\n');
  const p = writeLineWithBackpressure(stream, 'b\n');
  let resolved = false;
  p.then(() => { resolved = true; });

  // Must NOT have resolved yet -- proves it's actually waiting on 'drain',
  // not racing past backpressure the way the pre-fix code did.
  await new Promise(r => setTimeout(r, 5));
  assert.equal(resolved, false, 'resolved before drain fired');

  await p;
  const elapsed = Date.now() - t0;
  assert.ok(elapsed >= 25, `resolved too fast (${elapsed}ms) -- did not actually wait for drain`);
  assert.deepEqual(received, ['a\n', 'b\n']);
});

test('writeLineWithBackpressure rejects if the stream errors while waiting', async () => {
  const { stream } = makeSlowStream({ drainDelayMs: 200 }); // long enough to not drain before destroy() below
  const first = writeLineWithBackpressure(stream, 'a\n'); // occupies the buffer
  const second = writeLineWithBackpressure(stream, 'b\n'); // will be waiting on drain
  stream.destroy(new Error('disk full'));
  await assert.rejects(second, /disk full/);
  await first.catch(() => {}); // first may also reject on destroy; not under test here
});

test('writeLineWithBackpressure does not leak listeners across many backpressure events', async () => {
  // Regression test: the first fix waited for 'drain' correctly but left the
  // sibling 'error' (or 'drain') once-listener registered forever whenever
  // the other one fired -- an unbounded leak across a multi-million-row
  // export that surfaced live as MaxListenersExceededWarning and eventual
  // heap OOM, despite backpressure being awaited correctly.
  const { stream } = makeSlowStream({ drainDelayMs: 0 });
  for (let i = 0; i < 200; i++) {
    await writeLineWithBackpressure(stream, `${i}\n`);
  }
  assert.equal(stream.listenerCount('drain'), 0);
  assert.equal(stream.listenerCount('error'), 0);
});
