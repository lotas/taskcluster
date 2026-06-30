// Parity test for the queue-context backlog query optimization.
//
// The `events` CTE in src/live-predictor/queue-context.js originally had NO
// time bound — it materialized a queue's entire run history on every
// prediction, only to count the last 15/60 minutes. That spilled to pgsql_tmp
// and took minutes. The fix bounds `events` to the windows it actually uses.
//
// This test proves the bound preserves results: it runs the ORIGINAL
// (unbounded) query and the CURRENT (bounded, imported) query against the same
// adversarially-seeded data and asserts identical output. If a future edit
// over-tightens the bound (e.g. drops the long-wait row whose started_at is
// recent but pending_at is old), old !== new and this fails.
//
// SAFETY: requires an explicit QCTX_TEST_DATABASE_URL (NOT DATABASE_URL, to
// avoid the prod default). Everything runs in an isolated schema inside a
// transaction that is ALWAYS rolled back, so it leaves zero trace even if
// pointed at a database with real data. Use a disposable/local DB regardless.
import { test } from 'node:test';
import assert from 'node:assert';
import pg from 'pg';
import { BACKLOG_SQL } from '../../src/live-predictor/queue-context.js';

const DSN = process.env.QCTX_TEST_DATABASE_URL;

// The query as it was BEFORE the optimization — `events` with no time filter.
// Kept verbatim here as the parity baseline; do not "fix" it.
const OLD_BACKLOG_SQL = `-- queue_context_backlog
WITH ahead AS (
  SELECT r.priority_at_pending AS pr, r.pending_at, r.started_at, t.repo_family,
         r.task_id, r.run_id,
         CASE r.priority_at_pending
           WHEN 'highest' THEN 7 WHEN 'very-high' THEN 6 WHEN 'high' THEN 5
           WHEN 'medium' THEN 4 WHEN 'low' THEN 3 WHEN 'very-low' THEN 2
           WHEN 'lowest' THEN 1 WHEN 'normal' THEN 1 ELSE 0 END AS rnk
  FROM queue_forecast_task_runs r
  JOIN queue_forecast_tasks t ON r.task_id = t.task_id
  WHERE t.task_queue_id = $1
    AND r.pending_at <= $2::timestamptz
    AND (COALESCE(r.started_at, r.resolved_at) IS NULL
         OR COALESCE(r.started_at, r.resolved_at) > $2::timestamptz)
    AND NOT (r.task_id = $4 AND r.run_id = $5)
),
events AS (
  SELECT r.pending_at, r.started_at,
         CASE r.priority_at_pending
           WHEN 'highest' THEN 7 WHEN 'very-high' THEN 6 WHEN 'high' THEN 5
           WHEN 'medium' THEN 4 WHEN 'low' THEN 3 WHEN 'very-low' THEN 2
           WHEN 'lowest' THEN 1 WHEN 'normal' THEN 1 ELSE 0 END AS rnk
  FROM queue_forecast_task_runs r
  JOIN queue_forecast_tasks t ON r.task_id = t.task_id
  WHERE t.task_queue_id = $1
    AND NOT (r.task_id = $4 AND r.run_id = $5)
),
allpending AS (
  SELECT 1 AS one
  FROM queue_forecast_task_runs r
  JOIN queue_forecast_tasks t ON r.task_id = t.task_id
  WHERE t.task_queue_id = $1
    AND r.pending_at <= $2::timestamptz
    AND (COALESCE(r.started_at, r.resolved_at) IS NULL
         OR COALESCE(r.started_at, r.resolved_at) > $2::timestamptz)
)
SELECT
  count(*) FILTER (WHERE rnk > $3)                                  AS pending_higher_priority_same_queue,
  count(*) FILTER (WHERE rnk = $3 AND (pending_at < $2::timestamptz
                   OR (pending_at = $2::timestamptz AND (
                        (task_id COLLATE "C") < ($4 COLLATE "C")
                        OR (task_id = $4 AND run_id < $5)
                      )))) AS pending_same_priority_same_queue,
  count(*) FILTER (WHERE rnk < $3)                                  AS pending_lower_priority_same_queue,
  EXTRACT(EPOCH FROM ($2::timestamptz - min(pending_at) FILTER (WHERE rnk >= $3))) AS oldest_higher_or_equal_pending_age_same_queue,
  count(*) FILTER (WHERE rnk >= $3)                                 AS pending_higher_or_equal_excl_target,
  count(*) FILTER (WHERE rnk >= $3 AND repo_family = 'try')         AS pending_try_higher_or_equal_same_queue,
  count(*) FILTER (WHERE rnk >= $3 AND repo_family = 'autoland')    AS pending_autoland_higher_or_equal_same_queue,
  count(*) FILTER (WHERE rnk >= $3 AND repo_family = 'release_beta') AS pending_release_beta_higher_or_equal_same_queue,
  (SELECT count(*) FILTER (WHERE pending_at > $2::timestamptz - INTERVAL '15 minutes' AND pending_at <= $2::timestamptz) FROM events)
                                                                    AS arrivals_15m_same_queue,
  (SELECT count(*) FILTER (WHERE pending_at > $2::timestamptz - INTERVAL '60 minutes' AND pending_at <= $2::timestamptz) FROM events)
                                                                    AS arrivals_60m_same_queue,
  (SELECT count(*) FILTER (WHERE pending_at > $2::timestamptz - INTERVAL '15 minutes' AND pending_at <= $2::timestamptz AND rnk >= $3) FROM events)
                                                                    AS arrivals_higher_or_equal_15m_same_queue,
  (SELECT count(*) FILTER (WHERE pending_at > $2::timestamptz - INTERVAL '60 minutes' AND pending_at <= $2::timestamptz AND rnk >= $3) FROM events)
                                                                    AS arrivals_higher_or_equal_60m_same_queue,
  (SELECT count(*) FILTER (WHERE started_at > $2::timestamptz - INTERVAL '15 minutes' AND started_at <= $2::timestamptz AND rnk >= $3) FROM events)
                                                                    AS starts_higher_or_equal_15m_same_queue,
  (SELECT count(*) FROM allpending)                                AS pending_total_incl_target
FROM ahead;`;

// T = the prediction anchor. All seed timestamps are expressed relative to it.
const T = '2026-06-01T12:00:00Z';
const TARGET_RNK = 4; // 'medium'
const PARAMS = ['q/test', T, TARGET_RNK, 'TT', 0];

// Adversarial seed. The two rows that give the test teeth:
//   D — pending 90m ago (outside the 60m window) but STARTED 5m ago: must be
//       kept by the bound (via the started_at OR) so starts_he_15m stays 1.
//   E — fully ancient (pending/started/resolved all >60m ago): contributes to
//       nothing, so dropping it must not change any output.
const TASKS = [
  // task_id, queue, repo_family
  ['A',  'q/test',  'other'],
  ['B',  'q/test',  'other'],
  ['C',  'q/test',  'other'],
  ['D',  'q/test',  'other'],
  ['E',  'q/test',  'other'],
  ['F',  'q/test',  'try'],
  ['G',  'q/test',  'autoland'],
  ['AA', 'q/test',  'other'],
  ['ZZ', 'q/test',  'other'],
  ['TT', 'q/test',  'other'],   // the target itself
  ['X',  'q/other', 'other'],   // different queue — must be ignored
];

// task_id, run_id, priority, pending_at, started_at, resolved_at  (UTC)
const RUNS = [
  ['A',  0, 'high',   '2026-06-01T11:30:00Z', null,                   null],
  ['B',  0, 'medium', '2026-06-01T11:50:00Z', null,                   null],
  ['C',  0, 'low',    '2026-06-01T11:55:00Z', null,                   null],
  ['D',  0, 'high',   '2026-06-01T10:30:00Z', '2026-06-01T11:55:00Z', null],
  ['E',  0, 'high',   '2026-06-01T09:00:00Z', '2026-06-01T09:10:00Z', '2026-06-01T09:20:00Z'],
  ['F',  0, 'high',   '2026-06-01T11:40:00Z', null,                   null],
  ['G',  0, 'medium', '2026-06-01T11:15:00Z', null,                   null],
  ['AA', 0, 'medium', '2026-06-01T12:00:00Z', null,                   null], // same instant, id < TT
  ['ZZ', 0, 'medium', '2026-06-01T12:00:00Z', null,                   null], // same instant, id > TT
  ['TT', 0, 'medium', '2026-06-01T12:00:00Z', null,                   null], // target
  ['X',  0, 'high',   '2026-06-01T11:55:00Z', null,                   null], // other queue
];

test(
  'queue-context backlog: bounded events produces identical output to unbounded',
  { skip: DSN ? false : 'set QCTX_TEST_DATABASE_URL (use a disposable/local DB) to run' },
  async () => {
    const client = new pg.Client({ connectionString: DSN });
    await client.connect();
    try {
      await client.query('BEGIN');
      await client.query('CREATE SCHEMA qctx_parity');
      await client.query('SET LOCAL search_path = qctx_parity');
      await client.query(`
        CREATE TABLE queue_forecast_tasks (
          task_id text PRIMARY KEY, task_queue_id text, repo_family text)`);
      await client.query(`
        CREATE TABLE queue_forecast_task_runs (
          task_id text, run_id int, priority_at_pending text,
          pending_at timestamptz, started_at timestamptz, resolved_at timestamptz,
          PRIMARY KEY (task_id, run_id))`);

      for (const [task_id, q, fam] of TASKS) {
        await client.query(
          'INSERT INTO queue_forecast_tasks (task_id, task_queue_id, repo_family) VALUES ($1,$2,$3)',
          [task_id, q, fam]);
      }
      for (const [task_id, run_id, pr, p, s, r] of RUNS) {
        await client.query(
          `INSERT INTO queue_forecast_task_runs
             (task_id, run_id, priority_at_pending, pending_at, started_at, resolved_at)
           VALUES ($1,$2,$3,$4,$5,$6)`,
          [task_id, run_id, pr, p, s, r]);
      }

      const oldRow = (await client.query(OLD_BACKLOG_SQL, PARAMS)).rows[0];
      const newRow = (await client.query(BACKLOG_SQL, PARAMS)).rows[0];

      // Core guarantee: the optimization changes nothing observable.
      assert.deepEqual(newRow, oldRow,
        'bounded events query must return identical columns to the unbounded baseline');

      // Anchor a couple of absolute values so the seed is proven to exercise
      // the window boundaries (not just that two identical-but-wrong queries
      // agree). arrivals_60m: A,B,C,F,G,AA,ZZ (7); D & E pending outside 60m,
      // X on another queue. starts_he_15m: only D (started 5m ago, rnk>=4).
      assert.equal(Number(newRow.arrivals_60m_same_queue), 7);
      assert.equal(Number(newRow.starts_higher_or_equal_15m_same_queue), 1);
    } finally {
      await client.query('ROLLBACK').catch(() => {});
      await client.end();
    }
  },
);
