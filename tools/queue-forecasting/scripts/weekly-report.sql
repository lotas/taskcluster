-- Weekly health + shadow report. Run by scripts/weekly_report.sh; by hand:
--   docker compose exec -T postgres psql -U postgres -d forecasting \
--     -v since="'2026-11-02 11:15'" -v days=8 < scripts/weekly-report.sql
--
-- `since` bounds the shadow comparison (default: the 2026-11-02 collector fix;
-- 10-29..11-02 is degraded, see the collector outage notes). `days` bounds the
-- collection-health table.

\if :{?since}
\else
  \set since '''2026-11-02 11:15'''
\endif
\if :{?days}
\else
  \set days 8
\endif
\pset footer off

\echo
\echo '## 1. Collection health (per pending day)'
\echo 'enriched should be ~1.000; qp_null is queue_pending missing; predicted counts live predictions.'
SELECT d.day::date AS day,
       to_char(d.day, 'Dy') AS dow,
       r.runs, r.enriched, r.qp_null, p.predicted, s.shadowed
FROM generate_series(date_trunc('day', now()) - (:days || ' days')::interval,
                     date_trunc('day', now()), '1 day') AS d(day)
LEFT JOIN LATERAL (
  SELECT count(*) AS runs,
         round(avg((t.metadata_name IS NOT NULL)::int), 3) AS enriched,
         round(avg((x.queue_pending IS NULL)::int), 3) AS qp_null
  FROM queue_forecast_task_runs x JOIN queue_forecast_tasks t USING (task_id)
  WHERE x.pending_at >= d.day AND x.pending_at < d.day + interval '1 day'
) r ON true
LEFT JOIN LATERAL (
  SELECT count(*) AS predicted FROM queue_forecast_run_predictions
  WHERE predicted_at >= d.day AND predicted_at < d.day + interval '1 day'
) p ON true
LEFT JOIN LATERAL (
  SELECT count(*) AS shadowed FROM queue_forecast_run_predictions_shadow
  WHERE predicted_at >= d.day AND predicted_at < d.day + interval '1 day'
) s ON true
ORDER BY 1;

SELECT count(*) AS unenriched_tasks_total FROM queue_forecast_tasks WHERE metadata_name IS NULL;

-- Shadow vs served on the same runs. Population mirrors the v3 primary slice
-- (completed, wait observed); both p90s are the guarded values actually served.
CREATE TEMP TABLE scored AS
SELECT date_trunc('day', r.pending_at)::date AS d,
       r.wait_duration_s AS a,
       p.wait_p50_s AS sp50, p.wait_p90_s AS sp90,
       s.wait_p50_s AS hp50, s.wait_p90_s AS hp90,
       p.wait_model_version AS sv, s.wait_model_version AS hv
FROM queue_forecast_run_predictions_shadow s
JOIN queue_forecast_run_predictions p USING (task_id, run_id)
JOIN queue_forecast_task_runs r USING (task_id, run_id)
WHERE r.reason_resolved = 'completed'
  AND r.started_at IS NOT NULL
  AND r.wait_duration_s >= 0
  AND s.predicted_at >= :since::timestamptz
  AND r.pending_at  >= :since::timestamptz;

\echo
\echo '## 2. Shadow vs served, aggregate since' :since
\echo 'v3 bars: mae_gain >= 0.15, w2x_gain_pp >= 5, pinball_gain > 0, coverage in [0.88, 0.93].'
SELECT count(*) AS n,
       string_agg(DISTINCT sv, ',') AS served_versions,
       string_agg(DISTINCT hv, ',') AS shadow_versions,
       round((1 - avg(abs(a - hp50)) / nullif(avg(abs(a - sp50)), 0))::numeric, 3) AS mae_gain,
       round((100 * (avg((a BETWEEN hp50 / 2 AND hp50 * 2)::int)
                   - avg((a BETWEEN sp50 / 2 AND sp50 * 2)::int)))::numeric, 2) AS w2x_gain_pp,
       round((1 - avg(CASE WHEN a >= hp90 THEN 0.9 * (a - hp90) ELSE 0.1 * (hp90 - a) END)
                / nullif(avg(CASE WHEN a >= sp90 THEN 0.9 * (a - sp90) ELSE 0.1 * (sp90 - a) END), 0))::numeric, 3) AS pinball_gain,
       round(avg((a <= sp90)::int)::numeric, 4) AS served_cov,
       round(avg((a <= hp90)::int)::numeric, 4) AS shadow_cov
FROM scored;

\echo
\echo '## 3. Shadow vs served, per day'
SELECT d, count(*) AS n, max(hv) AS shadow_ver,
       round(avg(abs(a - sp50))::numeric, 1) AS srv_mae,
       round(avg(abs(a - hp50))::numeric, 1) AS shd_mae,
       round((1 - avg(abs(a - hp50)) / nullif(avg(abs(a - sp50)), 0))::numeric, 3) AS mae_gain,
       round(avg((a BETWEEN sp50 / 2 AND sp50 * 2)::int)::numeric, 3) AS srv_w2x,
       round(avg((a BETWEEN hp50 / 2 AND hp50 * 2)::int)::numeric, 3) AS shd_w2x,
       round(avg((a <= sp90)::int)::numeric, 4) AS srv_cov,
       round(avg((a <= hp90)::int)::numeric, 4) AS shd_cov,
       round(avg(CASE WHEN a >= sp90 THEN 0.9 * (a - sp90) ELSE 0.1 * (sp90 - a) END)::numeric, 1) AS srv_pin,
       round(avg(CASE WHEN a >= hp90 THEN 0.9 * (a - hp90) ELSE 0.1 * (hp90 - a) END)::numeric, 1) AS shd_pin
FROM scored GROUP BY d ORDER BY d;

\echo
\echo '## 4. p90 coverage by actual-wait bucket'
SELECT CASE WHEN a < 60 THEN '1:<1m' WHEN a < 300 THEN '2:1-5m'
            WHEN a < 1800 THEN '3:5-30m' ELSE '4:30m+' END AS bucket,
       count(*) AS n,
       round(avg((a <= sp90)::int)::numeric, 4) AS served_cov,
       round(avg((a <= hp90)::int)::numeric, 4) AS shadow_cov,
       round(avg(sp90)::numeric, 1) AS served_mean_p90_s,
       round(avg(hp90)::numeric, 1) AS shadow_mean_p90_s
FROM scored GROUP BY 1 ORDER BY 1;

\echo
\echo '## 5. Dependency-edge collection (group-eta-plan.md P0, live since 2026-11-16 14:38Z; inline TEXT[] since 2026-11-17 09:31Z)'
\echo 'deps_cov = share of created tasks with edges collected (dependency_count NOT NULL); should be ~1.0 after 11-16.'
SELECT d.day::date AS day,
       x.tasks, x.deps_cov, x.zero_deps, x.avg_deps, x.max_deps, x.edges
FROM generate_series(date_trunc('day', now()) - (:days || ' days')::interval,
                     date_trunc('day', now()), '1 day') AS d(day)
LEFT JOIN LATERAL (
  SELECT count(*) AS tasks,
         round(avg((t.dependency_count IS NOT NULL)::int), 3) AS deps_cov,
         count(*) FILTER (WHERE t.dependency_count = 0) AS zero_deps,
         round(avg(t.dependency_count), 2) AS avg_deps,
         max(t.dependency_count) AS max_deps,
         sum(t.dependency_count) AS edges
  FROM queue_forecast_tasks t
  WHERE t.task_created >= d.day AND t.task_created < d.day + interval '1 day'
) x ON true
ORDER BY 1;

SELECT pg_size_pretty(pg_total_relation_size('queue_forecast_tasks')) AS tasks_table_size;

\echo
\echo '## 6. Shadow task-group ETA (group-eta-plan.md P2), groups predicted since' :since
\echo 'label = completion of the original graph (tasks created <= t_pred) - t0; only fully-resolved groups older than 1 day.'
\echo 'internal bars (try): cov90 in [0.85, 0.93], w2x >= 0.70.'
WITH pg AS (
  SELECT DISTINCT task_group_id, t0, t_pred
  FROM queue_forecast_group_predictions_shadow
  WHERE t_pred >= :since::timestamptz AND t_pred < now() - interval '1 day'
), lab AS (
  SELECT pg.task_group_id,
         extract(epoch FROM max(r.resolved_at) - pg.t0) AS span,
         bool_or(r.task_id IS NULL OR r.resolved_at IS NULL) AS incomplete
  FROM pg
  JOIN queue_forecast_tasks t
    ON t.task_group_id = pg.task_group_id
   AND t.task_created >= pg.t0 AND t.task_created <= pg.t_pred
  LEFT JOIN queue_forecast_task_runs r ON r.task_id = t.task_id
  GROUP BY pg.task_group_id, pg.t0
), s AS (
  SELECT p.repo_family, p.estimator, l.span AS y, p.p50_s AS p50, p.p90_s AS p90
  FROM queue_forecast_group_predictions_shadow p
  JOIN lab l USING (task_group_id)
  WHERE NOT l.incomplete AND l.span > 0
)
SELECT repo_family, estimator, count(*) AS n,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY y) / 60)::numeric, 1) AS med_span_min,
       round((avg(abs(y - p50)) / 60)::numeric, 1) AS mae_min,
       round(avg((y BETWEEN p50 / 2 AND p50 * 2)::int)::numeric, 3) AS w2x,
       round(avg((y <= p90)::int)::numeric, 3) AS cov90,
       round((avg(CASE WHEN y >= p90 THEN 0.9 * (y - p90) ELSE 0.1 * (p90 - y) END) / 60)::numeric, 1) AS pin90_min
FROM s GROUP BY 1, 2 ORDER BY 1, 2;
