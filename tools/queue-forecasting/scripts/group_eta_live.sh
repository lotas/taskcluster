#!/usr/bin/env bash
# Live shadow task-group ETA (group-eta-plan.md P2). Internal only.
#
#   scripts/group_eta_live.sh fit       # daily: refresh cache + refit (~5-10 min)
#   scripts/group_eta_live.sh predict   # every 10 min: score newly-defined groups
#
# Runs as the `group-eta` compose service: trainer image, 6g cap, separate from
# the 22g heavy trainer, and does not take the heavy-training lock. Model + day cache live under
# trainer/data/group_eta/.
#
# Suggested crontab:
#   15 4 * * *    ~/dev/taskcluster/tools/queue-forecasting/scripts/group_eta_live.sh fit     >> ~/qf-reports/group-eta.log 2>&1
#   */10 * * * *  ~/dev/taskcluster/tools/queue-forecasting/scripts/group_eta_live.sh predict >> ~/qf-reports/group-eta.log 2>&1
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

cmd="${1:?usage: group_eta_live.sh fit|predict [args...]}"
shift || true

# flock: never overlap two runs of the same subcommand (a slow fit must not stack).
exec 9>"/tmp/qf-group-eta-$cmd.lock"
if ! flock -n 9; then
  echo "$(date -u +%FT%TZ) group-eta $cmd: previous run still active, skipping"
  exit 0
fi

echo "$(date -u +%FT%TZ) group-eta $cmd start"
docker compose run --rm -T group-eta "$cmd" --data-dir data/group_eta "$@"
echo "$(date -u +%FT%TZ) group-eta $cmd done"
