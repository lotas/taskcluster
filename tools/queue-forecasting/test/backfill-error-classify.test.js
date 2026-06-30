import { test } from 'node:test'; import assert from 'node:assert';
import { isPermanentTaskError } from '../scripts/backfill_repo_family.js';
test('404 is permanent', () => assert.equal(isPermanentTaskError({statusCode:404}), true));
test('5xx is transient', () => assert.equal(isPermanentTaskError({statusCode:503}), false));
test('429 is transient', () => assert.equal(isPermanentTaskError({statusCode:429}), false));
test('auth is transient', () => { assert.equal(isPermanentTaskError({statusCode:403}), false); assert.equal(isPermanentTaskError({statusCode:401}), false); });
test('network error is transient', () => assert.equal(isPermanentTaskError({code:'ECONNRESET'}), false));
test('null/undefined safe', () => { assert.equal(isPermanentTaskError(null), false); assert.equal(isPermanentTaskError(undefined), false); });
