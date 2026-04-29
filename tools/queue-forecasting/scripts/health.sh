#!/usr/bin/env bash
# Queue forecasting runtime health check.
#
# Default mode is read-only: inspect Docker Compose state and summarize the
# Postgres tables. Use --restart-collector or --restart-stale for recovery.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

PG_SERVICE="${PG_SERVICE:-postgres}"
PGUSER="${PGUSER:-postgres}"
PGDATABASE="${PGDATABASE:-forecasting}"
STALE_MINUTES="${STALE_MINUTES:-30}"
LOG_LINES="${LOG_LINES:-80}"

SHOW_LOGS=0
RESTART_COLLECTOR=0
RESTART_STALE=0

usage() {
  cat >&2 <<USAGE
Usage: $0 [options]

Options:
  --logs                 Show recent collector logs after DB checks.
  --log-lines N          Number of collector log lines to show (default: ${LOG_LINES}).
  --stale-minutes N      Mark collector stale if no new pending task is seen for N minutes
                         (default: ${STALE_MINUTES}).
  --restart-collector    Restart the collector explicitly.
  --restart-stale        Restart the collector only if stopped or stale.
  -h, --help             Show this help.

Environment:
  PG_SERVICE             Compose service for Postgres (default: postgres).
  PGUSER                 Postgres user (default: postgres).
  PGDATABASE             Postgres database (default: forecasting).
  STALE_MINUTES          Default freshness threshold in minutes.
  LOG_LINES              Default --logs line count.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --logs)
      SHOW_LOGS=1
      shift
      ;;
    --log-lines)
      LOG_LINES="$2"
      shift 2
      ;;
    --stale-minutes)
      STALE_MINUTES="$2"
      shift 2
      ;;
    --restart-collector)
      RESTART_COLLECTOR=1
      shift
      ;;
    --restart-stale)
      RESTART_STALE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      usage
      exit 1
      ;;
  esac
done

compose() {
  docker compose "$@"
}

psql_db() {
  compose exec -T "$PG_SERVICE" \
    psql -U "$PGUSER" -d "$PGDATABASE" -v ON_ERROR_STOP=1 "$@"
}

section() {
  printf '\n== %s ==\n' "$1"
}

collector_container_id() {
  compose ps -q collector 2>/dev/null || true
}

collector_is_running() {
  local id running
  id="$(collector_container_id)"
  [[ -n "$id" ]] || return 1
  running="$(docker inspect --format '{{.State.Running}}' "$id" 2>/dev/null || true)"
  [[ "$running" == "true" ]]
}

restart_collector() {
  section "Restart collector"
  compose --profile collector up -d postgres

  if [[ -n "$(collector_container_id)" ]]; then
    compose restart collector
  else
    compose --profile collector up -d collector
  fi

  compose ps collector
}

section "Docker Compose services"
compose ps

section "Collector container"
collector_id="$(collector_container_id)"
if [[ -z "$collector_id" ]]; then
  echo "collector: no container found"
else
  docker inspect \
    --format 'collector: status={{.State.Status}} running={{.State.Running}} started={{.State.StartedAt}} restart_policy={{.HostConfig.RestartPolicy.Name}}' \
    "$collector_id"
fi

section "Table counts and freshest timestamp columns"
psql_db <<'SQL'
\pset pager off
\pset null '[null]'
CREATE TEMP TABLE _qf_table_health (
    table_name text,
    row_count bigint,
    newest_column text,
    newest_timestamp timestamptz
);

DO $do$
DECLARE
    tbl record;
    row_count bigint;
    newest_column text;
    newest_timestamp timestamptz;
    values_sql text;
BEGIN
    FOR tbl IN
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_type = 'BASE TABLE'
        ORDER BY table_name
    LOOP
        EXECUTE format('SELECT count(*)::bigint FROM %I.%I', tbl.table_schema, tbl.table_name)
            INTO row_count;

        SELECT string_agg(
                   format('(%L, max(%I)::timestamptz)', column_name, column_name),
                   ', ' ORDER BY ordinal_position
               )
        INTO values_sql
        FROM information_schema.columns
        WHERE table_schema = tbl.table_schema
          AND table_name = tbl.table_name
          AND data_type IN ('timestamp with time zone', 'timestamp without time zone', 'date');

        newest_column := NULL;
        newest_timestamp := NULL;
        IF values_sql IS NOT NULL THEN
            EXECUTE format(
                'SELECT col_name, ts_value
                   FROM (VALUES %s) AS v(col_name, ts_value)
                  WHERE ts_value IS NOT NULL
                  ORDER BY ts_value DESC
                  LIMIT 1',
                values_sql
            )
            INTO newest_column, newest_timestamp;
        END IF;

        INSERT INTO _qf_table_health
        VALUES (tbl.table_name, row_count, newest_column, newest_timestamp);
    END LOOP;
END
$do$;

SELECT
    table_name,
    to_char(row_count, 'FM999G999G999G999G990') AS rows,
    newest_column,
    newest_timestamp AS newest_seen_at,
    CASE
      WHEN newest_timestamp IS NULL THEN NULL
      ELSE now() - newest_timestamp
    END AS newest_age
FROM _qf_table_health
ORDER BY table_name;
SQL

section "Collector freshness"
psql_db <<'SQL'
\pset pager off
\pset null '[null]'
WITH freshness AS (
    SELECT
        now() AS checked_at,
        (SELECT max(pending_at) FROM queue_forecast_task_runs) AS latest_pending_at,
        (SELECT max(started_at) FROM queue_forecast_task_runs) AS latest_started_at,
        (SELECT max(resolved_at) FROM queue_forecast_task_runs) AS latest_resolved_at,
        (SELECT max(enriched_at) FROM queue_forecast_tasks) AS latest_enriched_at,
        (SELECT max(sampled_at) FROM queue_forecast_worker_counts) AS latest_worker_sample_at,
        (SELECT max(computed_at) FROM queue_forecast_daily_health) AS latest_daily_health_at
)
SELECT
    checked_at,
    latest_pending_at,
    now() - latest_pending_at AS pending_age,
    latest_started_at,
    now() - latest_started_at AS started_age,
    latest_resolved_at,
    now() - latest_resolved_at AS resolved_age,
    latest_enriched_at,
    now() - latest_enriched_at AS enriched_age,
    latest_worker_sample_at,
    now() - latest_worker_sample_at AS worker_sample_age,
    latest_daily_health_at,
    now() - latest_daily_health_at AS daily_health_age
FROM freshness;
SQL

section "Recent ingestion windows"
psql_db <<'SQL'
\pset pager off
WITH windows(label, span) AS (
    VALUES
        ('5m',  interval '5 minutes'),
        ('15m', interval '15 minutes'),
        ('1h',  interval '1 hour'),
        ('6h',  interval '6 hours'),
        ('24h', interval '24 hours')
)
SELECT
    w.label,
    count(*) FILTER (WHERE r.pending_at >= now() - w.span) AS pending_rows,
    count(*) FILTER (WHERE r.started_at >= now() - w.span) AS started_rows,
    count(*) FILTER (WHERE r.resolved_at >= now() - w.span) AS resolved_rows
FROM windows w
CROSS JOIN queue_forecast_task_runs r
GROUP BY w.label, w.span
ORDER BY w.span;
SQL

section "Open issues"
psql_db <<'SQL'
\pset pager off
SELECT 'unenriched_tasks' AS metric, count(*)::bigint AS value
  FROM queue_forecast_tasks
 WHERE metadata_name IS NULL
UNION ALL
SELECT 'unresolved_runs', count(*)::bigint
  FROM queue_forecast_task_runs
 WHERE resolved_at IS NULL
UNION ALL
SELECT 'unresolved_runs_older_than_2h', count(*)::bigint
  FROM queue_forecast_task_runs
 WHERE resolved_at IS NULL
   AND pending_at < now() - interval '2 hours'
UNION ALL
SELECT 'completed_missing_queue_pending', count(*)::bigint
  FROM queue_forecast_task_runs
 WHERE queue_pending IS NULL
   AND reason_resolved = 'completed'
   AND started_at IS NOT NULL
UNION ALL
SELECT 'worker_sample_timestamps_last_30m', count(DISTINCT sampled_at)::bigint
  FROM queue_forecast_worker_counts
 WHERE sampled_at >= now() - interval '30 minutes'
UNION ALL
SELECT 'daily_health_rows_last_7d', count(*)::bigint
  FROM queue_forecast_daily_health
 WHERE sample_date >= current_date - 7
ORDER BY metric;
SQL

section "Recent resolution reasons"
psql_db <<'SQL'
\pset pager off
\pset null '[null]'
SELECT
    reason_resolved,
    count(*) AS rows
FROM queue_forecast_task_runs
WHERE resolved_at >= now() - interval '1 hour'
GROUP BY reason_resolved
ORDER BY rows DESC, reason_resolved
LIMIT 20;
SQL

section "Top queues in last 15m"
psql_db <<'SQL'
\pset pager off
\pset null '[null]'
SELECT
    t.task_queue_id,
    count(*) AS pending_rows,
    max(r.pending_at) AS latest_pending_at,
    count(*) FILTER (WHERE r.resolved_at IS NOT NULL) AS resolved_rows
FROM queue_forecast_task_runs r
JOIN queue_forecast_tasks t ON t.task_id = r.task_id
WHERE r.pending_at >= now() - interval '15 minutes'
GROUP BY t.task_queue_id
ORDER BY pending_rows DESC
LIMIT 15;
SQL

pending_age_s="$(
  psql_db -Atc "SELECT COALESCE(EXTRACT(EPOCH FROM now() - max(pending_at))::bigint, -1) FROM queue_forecast_task_runs;" \
    | tr -d '[:space:]'
)"
stale_seconds=$((STALE_MINUTES * 60))
collector_unhealthy=0

section "Health verdict"
if ! collector_is_running; then
  echo "WARN: collector container is not running."
  collector_unhealthy=1
elif [[ "$pending_age_s" =~ ^-?[0-9]+$ ]] && (( pending_age_s < 0 )); then
  echo "WARN: no task-run pending_at rows exist yet."
  collector_unhealthy=1
elif [[ "$pending_age_s" =~ ^[0-9]+$ ]] && (( pending_age_s > stale_seconds )); then
  echo "WARN: latest pending_at is ${pending_age_s}s old; threshold is ${stale_seconds}s."
  collector_unhealthy=1
else
  echo "OK: collector is running and recent task-run data is present."
fi

did_restart=0
if [[ "$RESTART_COLLECTOR" == 1 ]]; then
  restart_collector
  did_restart=1
elif [[ "$RESTART_STALE" == 1 && "$collector_unhealthy" == 1 ]]; then
  restart_collector
  did_restart=1
fi

if [[ "$SHOW_LOGS" == 1 ]]; then
  section "Collector logs"
  compose logs --tail "$LOG_LINES" collector
fi

if [[ "$did_restart" == 0 ]]; then
  echo
  echo "To restart manually: docker compose --profile collector restart collector"
  echo "To start if missing:   docker compose --profile collector up -d collector"
fi
