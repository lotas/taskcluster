// One-off: re-fetch task definitions over a window and derive repo_family.
// Bounded to non-expired definitions; only genuinely-gone (HTTP 404) task
// definitions are permanently marked unknown so we don't re-fetch them forever.
// Transient errors (5xx, 429, auth, network) are left untouched for retry on a
// later invocation. Concurrency-limited. Safe to re-run.
//
// Usage: node scripts/backfill_repo_family.js --from 2026-05-26 --to 2026-06-26
import { fileURLToPath } from 'node:url';
import taskcluster from '@taskcluster/client';
import { createPool, updateRepoFamily, selectTasksNeedingRepoFamily } from '../src/db.js';
import { deriveRepoFamily, REPO_FAMILY_DERIVATION_VERSION } from '../src/repo-family.js';

const MAX_CONCURRENT = 20;
const BATCH = 500;

/**
 * A task fetch error is PERMANENT only when the TC Queue API returns 404,
 * i.e. the task definition has expired / never existed — re-fetching will
 * never succeed, so the row should be marked unknown once and skipped forever.
 *
 * Everything else is treated as TRANSIENT and must be left for retry:
 *   - 5xx (server), 429 (rate-limit), 401/403 (auth/creds not yet ready)
 *   - network errors with no statusCode (ECONNRESET/ETIMEDOUT/ENOTFOUND, etc.)
 * Writing 'unknown' for those would permanently lose recoverable rows.
 */
export function isPermanentTaskError(err) {
  return !!err && err.statusCode === 404;
}

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

  let done = 0, derived = 0, gone = 0, transient = 0;
  let loggedTransient = false;
  for (;;) {
    const ids = await selectTasksNeedingRepoFamily(pool, REPO_FAMILY_DERIVATION_VERSION, from, to, BATCH);
    if (ids.length === 0) break;

    // Track DB writes this batch. Transient rows keep derivation_version NULL,
    // so selectTasksNeedingRepoFamily will re-return them on the next loop. If
    // an ENTIRE batch produced zero writes (every row transient), looping again
    // re-selects the identical failing rows forever — so we break and let the
    // next invocation retry them.
    let batchWrites = 0;

    for (let i = 0; i < ids.length; i += MAX_CONCURRENT) {
      const chunk = ids.slice(i, i + MAX_CONCURRENT);
      await Promise.all(chunk.map(async (taskId) => {
        try {
          const def = await queue.task(taskId);
          const rf = deriveRepoFamily({ routes: def.routes || [], metadataSource: def.metadata?.source ?? null, schedulerId: def.schedulerId ?? null });
          await updateRepoFamily(pool, taskId, rf);
          batchWrites++;
          derived++;
          done++;
        } catch (err) {
          if (isPermanentTaskError(err)) {
            // 404: definition is gone for good — record unknown so we stop retrying.
            await updateRepoFamily(pool, taskId, { family: 'unknown', source: 'unknown', evidence: null, version: REPO_FAMILY_DERIVATION_VERSION });
            batchWrites++;
            gone++;
            done++;
          } else {
            // Transient: leave the row (NULL version) for a later retry. Do NOT write.
            transient++;
            if (!loggedTransient) {
              console.error(`[backfill] transient error (left for retry), e.g. task ${taskId}: ${err.statusCode || err.code || err.message}`);
              loggedTransient = true;
            }
          }
        }
      }));
      process.stderr.write(`\r[backfill] done=${done} derived=${derived} gone=${gone} transient=${transient}`);
    }

    if (batchWrites === 0) {
      process.stderr.write('\n');
      console.warn(`[backfill] entire batch of ${ids.length} failed transiently; stopping this run (rerun to retry).`);
      break;
    }
  }
  process.stderr.write('\n');
  console.log(`Backfilled repo_family: done=${done} derived=${derived} gone(404)=${gone} transient=${transient}`);
  await pool.end();
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  main().catch(e => { console.error(e); process.exit(1); });
}
