#!/usr/bin/env bash
# Cron-safe daily walk-forward retraining for the queue forecasting experiment.
#
# This script is intentionally only a scheduler wrapper:
#   - scripts/walk_forward.sh owns the resume-safe training sweep.
#   - scripts/run_training.sh owns a single config/as-of training run.
#   - trainer/scripts/summarize_walk_forward.py owns summary CSV generation.

set -euo pipefail

export PATH="/usr/local/bin:/usr/local/sbin:/usr/bin:/bin:/snap/bin:/opt/homebrew/bin:${PATH:-}"

cd "$(dirname "${BASH_SOURCE[0]}")/.."

DEFAULT_CONFIGS="configs/wait_time_residual_throughput_filtered_baseline.yaml,configs/run_duration_residual.yaml"

LOCK_FILE="${LOCK_FILE:-/tmp/queue-forecasting-walk-forward.lock}"
AS_OF_DATE="${AS_OF_DATE:-}"
FROM_DATE="${FROM_DATE:-}"
TO_DATE="${TO_DATE:-}"
STEP_DAYS="${STEP_DAYS:-1}"
WALK_FORWARD_CONFIGS="${WALK_FORWARD_CONFIGS:-${CONFIGS:-$DEFAULT_CONFIGS}}"
SUMMARY_FROM="${SUMMARY_FROM:-2026-04-15}"
SUMMARY_TO="${SUMMARY_TO:-}"
SUMMARY_CONFIGS="${SUMMARY_CONFIGS:-}"
SUMMARY_OUTPUT="${SUMMARY_OUTPUT:-walk_forward_summary.csv}"
SKIP_SUMMARY=0
DRY_RUN=0

usage() {
  cat >&2 <<USAGE
Usage: $0 [options]

Daily default:
  Runs yesterday's UTC as_of_date, then refreshes ${SUMMARY_OUTPUT}.

Options:
  --as-of YYYY-MM-DD          Run one as_of_date. Defaults to yesterday UTC.
  --from YYYY-MM-DD           Inclusive start as_of_date for a backfill.
  --to YYYY-MM-DD             Inclusive end as_of_date for a backfill.
  --step-days N               Date stride for backfills (default: ${STEP_DAYS}).
  --configs c1,c2,...         Comma-separated trainer config paths.
  --summary-from YYYY-MM-DD   Summary start date (default: ${SUMMARY_FROM}).
  --summary-to YYYY-MM-DD     Summary end date (default: --to).
  --summary-configs names     Comma-separated config stems for the summary.
                              Defaults to stems derived from --configs.
  --summary-output PATH       Summary CSV output (default: ${SUMMARY_OUTPUT}).
  --skip-summary              Do not regenerate the summary CSV.
  --dry-run                   Print commands without running Docker/training.
  -h, --help                  Show this help.

Environment overrides:
  LOCK_FILE                   flock path for cron non-overlap.
  AS_OF_DATE                  Same as --as-of.
  FROM_DATE, TO_DATE          Same as --from/--to.
  STEP_DAYS                   Same as --step-days.
  WALK_FORWARD_CONFIGS        Same as --configs.
  CONFIGS                     Fallback alias for WALK_FORWARD_CONFIGS.
  SUMMARY_FROM, SUMMARY_TO    Summary date range.
  SUMMARY_CONFIGS             Summary config stems.
  SUMMARY_OUTPUT              Summary CSV path.
USAGE
}

section() {
  printf '\n== %s ==\n' "$1"
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: required command not found: $1" >&2
    exit 1
  fi
}

default_as_of_date() {
  python3 - <<'PY'
from datetime import datetime, timedelta, timezone

print((datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat())
PY
}

config_stems() {
  local configs="$1"
  local old_ifs="$IFS"
  local cfg
  local base
  local stems=()

  IFS=','
  read -ra config_list <<<"$configs"
  IFS="$old_ifs"

  for cfg in "${config_list[@]}"; do
    base="${cfg##*/}"
    stems+=("${base%.yaml}")
  done

  local joined=""
  for cfg in "${stems[@]}"; do
    if [[ -n "$joined" ]]; then
      joined+=","
    fi
    joined+="$cfg"
  done
  printf '%s\n' "$joined"
}

print_cmd() {
  printf '  '
  printf '%q ' "$@"
  printf '\n'
}

run_cmd() {
  print_cmd "$@"
  if [[ "$DRY_RUN" != 1 ]]; then
    "$@"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
  --as-of)
    AS_OF_DATE="$2"
    shift 2
    ;;
  --from)
    FROM_DATE="$2"
    shift 2
    ;;
  --to)
    TO_DATE="$2"
    shift 2
    ;;
  --step-days)
    STEP_DAYS="$2"
    shift 2
    ;;
  --configs)
    WALK_FORWARD_CONFIGS="$2"
    shift 2
    ;;
  --summary-from)
    SUMMARY_FROM="$2"
    shift 2
    ;;
  --summary-to)
    SUMMARY_TO="$2"
    shift 2
    ;;
  --summary-configs)
    SUMMARY_CONFIGS="$2"
    shift 2
    ;;
  --summary-output)
    SUMMARY_OUTPUT="$2"
    shift 2
    ;;
  --skip-summary)
    SKIP_SUMMARY=1
    shift
    ;;
  --dry-run)
    DRY_RUN=1
    shift
    ;;
  -h | --help)
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

if [[ -n "$AS_OF_DATE" ]] && { [[ -n "$FROM_DATE" ]] || [[ -n "$TO_DATE" ]]; }; then
  echo "ERROR: use either --as-of or --from/--to, not both." >&2
  exit 1
fi

if [[ -n "$AS_OF_DATE" ]]; then
  FROM_DATE="$AS_OF_DATE"
  TO_DATE="$AS_OF_DATE"
fi

if [[ -z "$FROM_DATE" && -z "$TO_DATE" ]]; then
  FROM_DATE="$(default_as_of_date)"
  TO_DATE="$FROM_DATE"
elif [[ -z "$FROM_DATE" || -z "$TO_DATE" ]]; then
  echo "ERROR: --from and --to must be provided together." >&2
  exit 1
fi

if [[ -z "$SUMMARY_TO" ]]; then
  SUMMARY_TO="$TO_DATE"
fi

if [[ -z "$SUMMARY_CONFIGS" ]]; then
  SUMMARY_CONFIGS="$(config_stems "$WALK_FORWARD_CONFIGS")"
fi

require_command python3
if [[ "$DRY_RUN" != 1 ]]; then
  require_command docker
fi

if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    echo "ERROR: another walk-forward run is already active; lock file: $LOCK_FILE" >&2
    exit 1
  fi
else
  echo "WARN: flock not found; running without a cron overlap lock." >&2
fi

section "Daily walk-forward configuration"
cat <<CONFIG
working_dir:       $(pwd)
from_date:         ${FROM_DATE}
to_date:           ${TO_DATE}
step_days:         ${STEP_DAYS}
configs:           ${WALK_FORWARD_CONFIGS}
summary_from:      ${SUMMARY_FROM}
summary_to:        ${SUMMARY_TO}
summary_configs:   ${SUMMARY_CONFIGS}
summary_output:    ${SUMMARY_OUTPUT}
lock_file:         ${LOCK_FILE}
dry_run:           ${DRY_RUN}
CONFIG

section "Postgres"
run_cmd docker compose up -d postgres

section "Walk-forward"
run_cmd ./scripts/walk_forward.sh \
  --from "$FROM_DATE" \
  --to "$TO_DATE" \
  --step-days "$STEP_DAYS" \
  --configs "$WALK_FORWARD_CONFIGS"

if [[ "$SKIP_SUMMARY" != 1 ]]; then
  section "Summary"
  run_cmd docker compose run --rm --entrypoint uv trainer \
    run python -m scripts.summarize_walk_forward \
    --from "$SUMMARY_FROM" \
    --to "$SUMMARY_TO" \
    --configs "$SUMMARY_CONFIGS" \
    --output "$SUMMARY_OUTPUT"
else
  section "Summary"
  echo "skipped"
fi

section "Done"
echo "daily walk-forward complete: ${FROM_DATE} to ${TO_DATE}"
