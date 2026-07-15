// Parity test for the ahead/allpending time-bound optimization.
//
// Unlike the `events` CTE's bound (provably lossless -- it matches the exact
// 15/60-minute windows the outer SELECT reads), the `ahead`/`allpending`
// bound is NOT lossless by construction: "still pending" backlog counts have
// no natural time window, so a row still pending after 72h is deliberately
// dropped (same "generous grace period, not a tight bound" tradeoff the
// Python trainer already makes in queue_context.py/data_loader.py). 72h
// (not a tighter 24h) specifically so a catch-up pass after a multi-day
// outage still sees the full backlog relative to each row's own pending_at
// (the leakage-safe T, not wall-clock "now"). This test proves:
//   1. The bound changes nothing for realistic "still pending" ages (<72h),
//      including a row older than a tighter 24h bound would have allowed.
//   2. The one case it DOES change (still pending, but pended >72h ago) is
//      exactly the documented tradeoff -- not an accident.
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

// The query as it was BEFORE this bound -- ahead/allpending with no lower
// bound on pending_at. Kept verbatim here as the parity baseline; do not
// "fix" it.
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
    AND r.pending_at <= $2::timestamptz
    AND (r.pending_at  > $2::timestamptz - INTERVAL '60 minutes'
         OR r.started_at > $2::timestamptz - INTERVAL '15 minutes')
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

test(
  'queue-context backlog: bounded ahead/allpending matches unbounded when all rows are within 72h',
  { skip: DSN ? false : 'set QCTX_TEST_DATABASE_URL (use a disposable/local DB) to run' },
  async () => {
    const client = new pg.Client({ connectionString: DSN });
    await client.connect();
    try {
      await client.query('BEGIN');
      await client.query('CREATE SCHEMA qctx_ahead_bound');
      await client.query('SET LOCAL search_path = qctx_ahead_bound');
      await client.query(`
        CREATE TABLE queue_forecast_tasks (
          task_id text PRIMARY KEY, task_queue_id text, repo_family text)`);
      await client.query(`
        CREATE TABLE queue_forecast_task_runs (
          task_id text, run_id int, priority_at_pending text,
          pending_at timestamptz, started_at timestamptz, resolved_at timestamptz,
          PRIMARY KEY (task_id, run_id))`);

      const tasks = [
        ['A',  'q/test', 'other'],
        ['B',  'q/test', 'other'],
        ['C',  'q/test', 'other'],
        ['TT', 'q/test', 'other'], // target
      ];
      // task_id, run_id, priority, pending_at, started_at, resolved_at
      const runs = [
        ['A',  0, 'high',   '2026-06-01T11:30:00Z', null, null], // 30m ago, still pending
        ['B',  0, 'low',    '2026-06-01T10:00:00Z', null, null], // 2h ago, still pending
        // 48h ago -- outside a tighter 24h bound but within 72h; this is
        // exactly the catch-up-backlog case the 72h choice exists for.
        ['C',  0, 'high',   '2026-05-30T12:00:00Z', null, null],
        ['TT', 0, 'medium', T,                      null, null],
      ];
      for (const [task_id, q, fam] of tasks) {
        await client.query(
          'INSERT INTO queue_forecast_tasks (task_id, task_queue_id, repo_family) VALUES ($1,$2,$3)',
          [task_id, q, fam]);
      }
      for (const [task_id, run_id, pr, p, s, r] of runs) {
        await client.query(
          `INSERT INTO queue_forecast_task_runs
             (task_id, run_id, priority_at_pending, pending_at, started_at, resolved_at)
           VALUES ($1,$2,$3,$4,$5,$6)`,
          [task_id, run_id, pr, p, s, r]);
      }

      const oldRow = (await client.query(OLD_BACKLOG_SQL, PARAMS)).rows[0];
      const newRow = (await client.query(BACKLOG_SQL, PARAMS)).rows[0];

      assert.deepEqual(newRow, oldRow,
        'bounded ahead/allpending must match unbounded when every row is within 72h');
      // Anchor: A (high) + C (high), both rnk 5 > 4 -> 2. B (low, rnk 3 < 4) -> 1.
      assert.equal(Number(newRow.pending_higher_priority_same_queue), 2);
      assert.equal(Number(newRow.pending_lower_priority_same_queue), 1);
    } finally {
      await client.query('ROLLBACK').catch(() => {});
      await client.end();
    }
  },
);

test(
  'queue-context backlog: bounded ahead/allpending deliberately drops a >72h-old still-pending row',
  { skip: DSN ? false : 'set QCTX_TEST_DATABASE_URL (use a disposable/local DB) to run' },
  async () => {
    const client = new pg.Client({ connectionString: DSN });
    await client.connect();
    try {
      await client.query('BEGIN');
      await client.query('CREATE SCHEMA qctx_ahead_bound2');
      await client.query('SET LOCAL search_path = qctx_ahead_bound2');
      await client.query(`
        CREATE TABLE queue_forecast_tasks (
          task_id text PRIMARY KEY, task_queue_id text, repo_family text)`);
      await client.query(`
        CREATE TABLE queue_forecast_task_runs (
          task_id text, run_id int, priority_at_pending text,
          pending_at timestamptz, started_at timestamptz, resolved_at timestamptz,
          PRIMARY KEY (task_id, run_id))`);

      const tasks = [
        ['OLD1', 'q/test', 'other'], // still pending, 80h before T
        ['OLD2', 'q/test', 'other'], // pending 80h before T, resolved 79h before T
        ['TT',   'q/test', 'other'], // target
      ];
      const runs = [
        ['OLD1', 0, 'high',   '2026-05-29T04:00:00Z', null, null],
        ['OLD2', 0, 'high',   '2026-05-29T04:00:00Z', null, '2026-05-29T05:00:00Z'],
        ['TT',   0, 'medium', T,                       null, null],
      ];
      for (const [task_id, q, fam] of tasks) {
        await client.query(
          'INSERT INTO queue_forecast_tasks (task_id, task_queue_id, repo_family) VALUES ($1,$2,$3)',
          [task_id, q, fam]);
      }
      for (const [task_id, run_id, pr, p, s, r] of runs) {
        await client.query(
          `INSERT INTO queue_forecast_task_runs
             (task_id, run_id, priority_at_pending, pending_at, started_at, resolved_at)
           VALUES ($1,$2,$3,$4,$5,$6)`,
          [task_id, run_id, pr, p, s, r]);
      }

      const oldRow = (await client.query(OLD_BACKLOG_SQL, PARAMS)).rows[0];
      const newRow = (await client.query(BACKLOG_SQL, PARAMS)).rows[0];

      // OLD counts OLD1 (still pending, higher priority than target) -> 1.
      // NEW drops it (pended >72h before T) -> 0. This IS the documented
      // tradeoff, not a bug -- if this assertion ever flips to equal, the
      // bound has been widened/removed and this test should be revisited.
      assert.equal(Number(oldRow.pending_higher_priority_same_queue), 1);
      assert.equal(Number(newRow.pending_higher_priority_same_queue), 0);
      // OLD2 is resolved either way -- both queries correctly exclude it
      // from "still pending", so this scenario isolates the tradeoff to
      // exactly the still-pending row.
    } finally {
      await client.query('ROLLBACK').catch(() => {});
      await client.end();
    }
  },
);
