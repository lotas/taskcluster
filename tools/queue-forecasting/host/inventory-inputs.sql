-- Inventory of the inputs the evaluation package depends on. READ-ONLY.
-- Protocol §2.5 (evaluation-protocol.md). Run as:
--   docker compose exec -T postgres psql -U postgres -d forecasting -f - < host/inventory-inputs.sql
--
-- Retention (src/retention.js) deletes queue_forecast_tasks by task_created
-- (cascading to task_runs), run_predictions by predicted_at, worker_counts by
-- sampled_at, all at RETENTION_DAYS (60) back from now. The baseline exporter
-- reads the 7 days of resolved history preceding each pending time
-- (src/predictor.js: resolved_at > $date - INTERVAL '7 days').
--
-- WHAT THIS CANNOT TELL YOU. Whether a day's 7-day history is complete. A task
-- deleted for its task_created age may have RESOLVED much later, inside the
-- history window of a day whose own rows all survive. The surviving rows carry
-- no record of what was deleted, so history coverage is UNKNOWN from this
-- inventory. It prints the earliest surviving task_created and the
-- created-to-resolved lag among surviving runs so the operator can bound the
-- risk; completeness is established only by host/check-baseline-coverage.py
-- against a preserved input (the promoted baseline 9c150d75), before promotion.
\echo '== retention setting on the running service (compare with docker-compose.yml RETENTION_DAYS)'
\echo '   (not readable from SQL; check `docker compose config | grep RETENTION_DAYS`)'

\echo '== earliest retained rows, by the column retention actually keys on'
SELECT 'queue_forecast_tasks.task_created'      AS what, min(task_created)::date AS earliest, max(task_created)::date AS latest, count(*) AS rows FROM queue_forecast_tasks
UNION ALL
SELECT 'queue_forecast_task_runs.pending_at',        min(pending_at)::date,        max(pending_at)::date,        count(*) FROM queue_forecast_task_runs
UNION ALL
SELECT 'queue_forecast_task_runs.resolved_at',       min(resolved_at)::date,       max(resolved_at)::date,       count(*) FROM queue_forecast_task_runs WHERE resolved_at IS NOT NULL
UNION ALL
SELECT 'queue_forecast_worker_counts.sampled_at',    min(sampled_at)::date,        max(sampled_at)::date,        count(*) FROM queue_forecast_worker_counts
UNION ALL
SELECT 'queue_forecast_daily_health.sample_date',    min(sample_date),             max(sample_date),             count(*) FROM queue_forecast_daily_health;

\echo '== created-to-resolved lag among SURVIVING runs (days). A deleted task could have resolved this late.'
SELECT percentile_cont(0.5)  WITHIN GROUP (ORDER BY lag) AS p50_days,
       percentile_cont(0.99) WITHIN GROUP (ORDER BY lag) AS p99_days,
       max(lag) AS max_days
FROM (SELECT extract(epoch FROM (r.resolved_at - t.task_created)) / 86400.0 AS lag
      FROM queue_forecast_task_runs r JOIN queue_forecast_tasks t USING (task_id)
      WHERE r.resolved_at IS NOT NULL) x;

\echo '== RISK HORIZON, not a certification: days before (earliest task_created + max lag + 7d) may have lost history rows'
SELECT (min(task_created)::date) AS earliest_surviving_created,
       (min(task_created)::date + INTERVAL '7 days')::date AS naive_first_day_DO_NOT_USE,
       'history completeness is UNKNOWN from surviving rows; see check-baseline-coverage.py' AS note
FROM queue_forecast_tasks;

\echo '== rows per pending day at the OLD edge (a partial first day means retention is mid-way through it)'
SELECT pending_at::date AS day, count(*) AS runs
FROM queue_forecast_task_runs
WHERE pending_at < (SELECT min(pending_at) FROM queue_forecast_task_runs) + INTERVAL '12 days'
GROUP BY 1 ORDER BY 1;

\echo '== rows per pending day at the NEW edge (R3/R4 holdout days 09-06..09-15 must be complete before their extract)'
SELECT pending_at::date AS day, count(*) AS runs,
       count(*) FILTER (WHERE started_at IS NULL AND resolved_at IS NULL) AS still_pending
FROM queue_forecast_task_runs
WHERE pending_at >= '2026-09-01'
GROUP BY 1 ORDER BY 1;

\echo '== anomalous days flagged so far (Policy B exclusion list is derived from these; the package records it)'
SELECT sample_date, is_anomalous
FROM queue_forecast_daily_health
WHERE is_anomalous
ORDER BY 1;

\echo '== worker-count sampler coverage per day at the old edge (capacity features need it)'
SELECT sampled_at::date AS day, count(*) AS samples
FROM queue_forecast_worker_counts
WHERE sampled_at < (SELECT min(sampled_at) FROM queue_forecast_worker_counts) + INTERVAL '10 days'
GROUP BY 1 ORDER BY 1;
