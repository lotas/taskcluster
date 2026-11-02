#!/usr/bin/env bash
# Weekly collection-health + shadow-vs-served report, written to a dated file.
#
#   scripts/weekly_report.sh                      # report to $REPORT_DIR/qf-report-<date>.md
#   SINCE='2026-11-09' scripts/weekly_report.sh   # bound the shadow comparison
#
# Read-only: queries Postgres and Docker; changes nothing. Cron-safe.

set -euo pipefail
export PATH="/usr/local/bin:/usr/local/sbin:/usr/bin:/bin:${PATH:-}"
cd "$(dirname "${BASH_SOURCE[0]}")/.."

SINCE="${SINCE:-2026-11-02 11:15}"
DAYS="${DAYS:-8}"
REPORT_DIR="${REPORT_DIR:-$HOME/qf-reports}"
mkdir -p "$REPORT_DIR"
out="$REPORT_DIR/qf-report-$(date -u +%F).md"
tmp="$out.tmp"

{
  echo "# queue-forecasting weekly report — $(date -u +%FT%TZ)"
  echo
  echo "shadow comparison since: $SINCE   health window: $DAYS days"

  echo
  echo "## 0. Host"
  df -h / | tail -1 | awk '{print "disk /: " $5 " used, " $4 " free"}'
  for svc in collector live-predictor; do
    st="$(docker inspect -f '{{.State.Status}} since {{.State.StartedAt}} restarts={{.RestartCount}}' "queue-forecasting-$svc-1" 2>&1 || true)"
    echo "$svc: $st"
  done
  # Count from the later of 24h ago and the container start: one container's log
  # spans restarts, so a plain --since 24h would count a previous process's flood.
  day_ago="$(date -u -d '24 hours ago' +%s)"
  started="$(date -u -d "$(docker inspect -f '{{.State.StartedAt}}' queue-forecasting-collector-1)" +%s)"
  win="$(( started > day_ago ? started : day_ago ))"
  win_iso="$(date -u -d "@$win" +%FT%TZ)"
  clog="$(docker logs --since "$win_iso" queue-forecasting-collector-1 2>&1 || true)"
  echo "collector errors since $win_iso: Seed failed=$(grep -c 'Seed failed' <<<"$clog" || true)  api-fetch=$(grep -c '^\[api-fetch\]' <<<"$clog" || true)  Sync summaries=$(grep -c 'Sync:' <<<"$clog" || true)  idle evictions=$(grep -c 'idle evicted' <<<"$clog" || true)"
  echo "collector error log: $(docker exec queue-forecasting-collector-1 sh -c 'du -ch /app/tools/queue-forecasting/collector-errors.log* | tail -1' 2>&1 | awk '{print $1}')"
  echo "live predictor models: $(docker logs queue-forecasting-live-predictor-1 2>&1 | grep -o '[a-z ]*wait model: v_[0-9a-f_-]*' | tail -2 | tr '\n' ';')"
  echo "latest trained day dir: $(ls -1 trainer/data/models | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' | sort | tail -1)"
  echo "last walk-forward: $(grep 'daily walk-forward complete' "$HOME/queue-forecasting-walk-forward.log" 2>/dev/null | tail -1 || echo none)"

  docker compose exec -T postgres psql -U postgres -d forecasting -X -q \
    -v since="'$SINCE'" -v days="$DAYS" < scripts/weekly-report.sql
} > "$tmp" 2>&1

mv -f "$tmp" "$out"
echo "report written: $out"
