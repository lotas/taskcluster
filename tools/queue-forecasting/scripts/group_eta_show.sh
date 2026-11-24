#!/usr/bin/env bash
# Show recent shadow task-group ETAs (group-eta-plan.md P2) next to actuals.
#   scripts/group_eta_show.sh [hours=6] [family=try] [estimator=B1c]
# finish_p50/p90 are wall-clock UTC estimates; actual is filled once every task
# of the original graph has resolved (blank = still running).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
hours="${1:-6}"; fam="${2:-try}"; est="${3:-B1c}"
docker compose exec -T postgres psql -U postgres -d forecasting -X -q \
  -v hours="$hours" -v fam="$fam" -v est="$est" <<'SQL'
\pset footer off
WITH p AS (
  SELECT * FROM queue_forecast_group_predictions_shadow
  WHERE estimator = :'est' AND repo_family = :'fam'
    AND t_pred > now() - make_interval(hours => :hours)
), lab AS (
  SELECT p.task_group_id,
         max(r.resolved_at) AS done_at,
         bool_or(r.task_id IS NULL OR r.resolved_at IS NULL) AS running
  FROM p
  JOIN queue_forecast_tasks t ON t.task_group_id = p.task_group_id
   AND t.task_created >= p.t0 AND t.task_created <= p.t_pred
  LEFT JOIN queue_forecast_task_runs r ON r.task_id = t.task_id
  GROUP BY 1
)
SELECT p.task_group_id, p.n_known AS tasks,
       to_char(p.t0, 'DD HH24:MI') AS pushed,
       to_char(p.t0 + make_interval(secs => p.p50_s), 'DD HH24:MI') AS finish_p50,
       to_char(p.t0 + make_interval(secs => p.p90_s), 'DD HH24:MI') AS finish_p90,
       CASE WHEN l.running THEN '' ELSE to_char(l.done_at, 'DD HH24:MI') END AS actual,
       CASE WHEN l.running THEN ''
            WHEN l.done_at <= p.t0 + make_interval(secs => p.p90_s) THEN 'in' ELSE 'LATE' END AS vs_p90
FROM p JOIN lab l USING (task_group_id)
ORDER BY p.t0 DESC;
SQL
