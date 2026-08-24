import { test } from 'node:test';
import assert from 'node:assert';
import { assertDisposableDatabaseUrl } from './smoke-guard.js';

const OK = 'postgresql://postgres@localhost:5544/forecasting_smoke';

test('accepts a disposable local database', () => {
  assert.doesNotThrow(() => assertDisposableDatabaseUrl(OK));
});

test('rejects an unset url', () => {
  assert.throws(() => assertDisposableDatabaseUrl(undefined), /SMOKE_DATABASE_URL is required/);
  assert.throws(() => assertDisposableDatabaseUrl(''), /SMOKE_DATABASE_URL is required/);
});

test('rejects the production host port 5433', () => {
  assert.throws(
    () => assertDisposableDatabaseUrl('postgresql://postgres@localhost:5433/forecasting_smoke'),
    /port 5433/,
  );
});

test('rejects a non-local host', () => {
  assert.throws(
    () => assertDisposableDatabaseUrl('postgresql://postgres@35.202.240.190:5544/forecasting_smoke'),
    /must be local/,
  );
});

test('rejects a database name that is not marked disposable', () => {
  assert.throws(
    () => assertDisposableDatabaseUrl('postgresql://postgres@localhost:5544/forecasting'),
    /database name/,
  );
});

test('accepts test-marked database names too', () => {
  assert.doesNotThrow(
    () => assertDisposableDatabaseUrl('postgresql://postgres@127.0.0.1:5544/qf_test'),
  );
});

test('rejects a malformed url', () => {
  assert.throws(() => assertDisposableDatabaseUrl('not-a-url'), /could not be parsed/);
});
