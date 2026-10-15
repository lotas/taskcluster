-- Served wait model vs shadow wait model, on the same runs.
--
--   docker compose exec -T postgres psql -U postgres -d forecasting < shadow-compare.sql
--   (optionally: psql -v since="'2026-10-20'" ...  to bound the window)
--
-- Population mirrors the v3 contract's primary slice: reason_resolved =
-- 'completed', anchored at pending_at, wait observed. Only runs scored by BOTH
-- models count, which the shadow table guarantees (it is written in the same
-- pass as the served row). Both p90s are the GUARDED values actually served.
--
-- Targets from wait_time_v3: p90 coverage in [0.88, 0.93]; the gate that nearly
-- failed on R4 was 0.9286. Pinball is the p90 quantile loss (lower is better).
--
-- Read `n` first: a coverage figure on a few thousand runs is not evidence
-- against a 0.93 ceiling it clears by 0.0014.

\if :{?since}
\else
  \set since '''1970-01-01'''
\endif

WITH scored AS (
  SELECT
    r.wait_duration_s                              AS actual,
    p.wait_p50_s                                   AS served_p50,
    p.wait_p90_s                                   AS served_p90,
    s.wait_p50_s                                   AS shadow_p50,
    s.wait_p90_s                                   AS shadow_p90,
    p.wait_model_version                           AS served_version,
    s.wait_model_version                           AS shadow_version,
    s.wait_artifact_hash                           AS shadow_artifact
  FROM queue_forecast_run_predictions_shadow s
  JOIN queue_forecast_run_predictions p USING (task_id, run_id)
  JOIN queue_forecast_task_runs r        USING (task_id, run_id)
  WHERE r.reason_resolved = 'completed'
    AND r.started_at IS NOT NULL
    AND r.wait_duration_s IS NOT NULL
    AND r.wait_duration_s >= 0
    AND s.predicted_at >= :since::timestamptz
),
long AS (
  SELECT 'served' AS model, served_version AS version, actual, served_p50 AS p50, served_p90 AS p90 FROM scored
  UNION ALL
  SELECT 'shadow',          shadow_version,            actual, shadow_p50,        shadow_p90        FROM scored
)
SELECT
  model,
  max(version)                                                        AS version,
  count(*)                                                            AS n,
  round(avg((actual <= p90)::int)::numeric, 4)                        AS p90_coverage,
  round(avg(abs(actual - p50))::numeric, 1)                           AS mae_p50_s,
  round(avg(CASE WHEN actual >= p90 THEN 0.9 * (actual - p90)
                 ELSE 0.1 * (p90 - actual) END)::numeric, 2)          AS pinball_p90,
  round(avg(p90)::numeric, 1)                                         AS mean_p90_width_s,
  round(avg((actual BETWEEN p50 / 2 AND p50 * 2)::int)::numeric, 4)   AS within_2x_of_p50
FROM long
GROUP BY model
ORDER BY model DESC;

-- Same, split by the wait bucket the contract reports on (actual wait).
WITH scored AS (
  SELECT r.wait_duration_s AS actual,
         p.wait_p90_s AS served_p90, s.wait_p90_s AS shadow_p90
  FROM queue_forecast_run_predictions_shadow s
  JOIN queue_forecast_run_predictions p USING (task_id, run_id)
  JOIN queue_forecast_task_runs r        USING (task_id, run_id)
  WHERE r.reason_resolved = 'completed'
    AND r.started_at IS NOT NULL
    AND r.wait_duration_s >= 0
    AND s.predicted_at >= :since::timestamptz
)
SELECT
  CASE WHEN actual < 60 THEN '1:<1m'
       WHEN actual < 300 THEN '2:1-5m'
       WHEN actual < 1800 THEN '3:5-30m'
       ELSE '4:30m+' END                                      AS bucket,
  count(*)                                                    AS n,
  round(avg((actual <= served_p90)::int)::numeric, 4)         AS served_cov,
  round(avg((actual <= shadow_p90)::int)::numeric, 4)         AS shadow_cov
FROM scored
GROUP BY 1
ORDER BY 1;
