#!/usr/bin/env bash
# Cron-safe backup for the queue forecasting experiment.
#
# Creates a consistent Postgres custom-format dump, uploads it to Cloud Storage,
# and syncs trainer/data so model outputs, baselines, and Parquet caches survive
# VM loss.

set -euo pipefail

export PATH="/usr/local/bin:/usr/bin:/bin:/snap/bin:${PATH:-}"

cd "$(dirname "${BASH_SOURCE[0]}")/.."

BACKUP_URI="${BACKUP_URI:-gs://queue-forecasting/backups}"
LOCAL_BACKUP_DIR="${LOCAL_BACKUP_DIR:-/tmp/queue-forecasting-backups}"
TRAINING_DATA_DIR="${TRAINING_DATA_DIR:-trainer/data}"
PG_SERVICE="${PG_SERVICE:-postgres}"
PGUSER="${PGUSER:-postgres}"
PGDATABASE="${PGDATABASE:-forecasting}"
DUMP_COMPRESSION="${DUMP_COMPRESSION:-6}"
LOCK_FILE="${LOCK_FILE:-/tmp/queue-forecasting-backup.lock}"

SKIP_DB=0
SKIP_TRAINING_DATA=0
KEEP_LOCAL=0
DRY_RUN=0
DELETE_EXTRA_TRAINING_DATA=0
TIMESTAMP=""

usage() {
  cat >&2 <<USAGE
Usage: $0 [options]

Options:
  --backup-uri URI                Cloud Storage destination prefix
                                  (default: ${BACKUP_URI}).
  --local-dir DIR                 Local staging dir for db dumps
                                  (default: ${LOCAL_BACKUP_DIR}).
  --training-data-dir DIR         Local trainer data directory to sync
                                  (default: ${TRAINING_DATA_DIR}).
  --skip-db                       Do not create/upload a Postgres dump.
  --skip-training-data            Do not sync trainer/data.
  --delete-extra-training-data    Delete remote trainer-data objects that are
                                  absent locally. Off by default.
  --keep-local                    Keep the local dump after upload.
  --dry-run                       Print actions without dumping or uploading.
  --timestamp YYYYMMDDTHHMMSSZ    Override backup timestamp.
  -h, --help                      Show this help.

Environment:
  BACKUP_URI                      Cloud Storage destination prefix.
  LOCAL_BACKUP_DIR                Local staging dir for db dumps.
  TRAINING_DATA_DIR               Training data directory to sync.
  PG_SERVICE                      Compose service for Postgres (default: postgres).
  PGUSER                          Postgres user (default: postgres).
  PGDATABASE                      Postgres database (default: forecasting).
  DUMP_COMPRESSION                pg_dump -Z compression level (default: 6).
  LOCK_FILE                       flock path for cron non-overlap.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backup-uri)
      BACKUP_URI="$2"
      shift 2
      ;;
    --local-dir)
      LOCAL_BACKUP_DIR="$2"
      shift 2
      ;;
    --training-data-dir)
      TRAINING_DATA_DIR="$2"
      shift 2
      ;;
    --skip-db)
      SKIP_DB=1
      shift
      ;;
    --skip-training-data)
      SKIP_TRAINING_DATA=1
      shift
      ;;
    --delete-extra-training-data)
      DELETE_EXTRA_TRAINING_DATA=1
      shift
      ;;
    --keep-local)
      KEEP_LOCAL=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --timestamp)
      TIMESTAMP="$2"
      shift 2
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

BACKUP_URI="${BACKUP_URI%/}"

section() {
  printf '\n== %s ==\n' "$1"
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "ERROR: required command not found: $1" >&2
    exit 1
  fi
}

compose() {
  docker compose "$@"
}

if [[ "$BACKUP_URI" != gs://* ]]; then
  echo "ERROR: --backup-uri must be a gs:// Cloud Storage URI, got: $BACKUP_URI" >&2
  exit 1
fi

require_command docker
require_command gcloud

if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    echo "ERROR: another backup is already running; lock file: $LOCK_FILE" >&2
    exit 1
  fi
fi

if [[ -z "$TIMESTAMP" ]]; then
  TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
fi

dump_name="${PGDATABASE}-${TIMESTAMP}.dump"
dump_path="${LOCAL_BACKUP_DIR}/${dump_name}"
manifest_name="backup-${TIMESTAMP}.txt"
manifest_path="${LOCAL_BACKUP_DIR}/${manifest_name}"

section "Backup configuration"
cat <<CONFIG
timestamp:          ${TIMESTAMP}
backup_uri:         ${BACKUP_URI}
local_backup_dir:   ${LOCAL_BACKUP_DIR}
training_data_dir:  ${TRAINING_DATA_DIR}
postgres_service:   ${PG_SERVICE}
postgres_database:  ${PGDATABASE}
dry_run:            ${DRY_RUN}
CONFIG

if [[ "$DRY_RUN" != 1 ]]; then
  mkdir -p "$LOCAL_BACKUP_DIR"
fi

if [[ "$SKIP_DB" != 1 ]]; then
  section "Postgres dump"
  if [[ "$DRY_RUN" == 1 ]]; then
    echo "would run: docker compose exec -T ${PG_SERVICE} pg_dump -U ${PGUSER} -d ${PGDATABASE} -Fc -Z ${DUMP_COMPRESSION} --no-owner --no-acl > ${dump_path}"
    echo "would upload: ${dump_path} -> ${BACKUP_URI}/db/${dump_name}"
    echo "would refresh: ${BACKUP_URI}/db/latest.dump"
  else
    tmp_dump="${dump_path}.tmp"
    rm -f "$tmp_dump"

    compose exec -T "$PG_SERVICE" \
      pg_dump -U "$PGUSER" -d "$PGDATABASE" \
        -Fc -Z "$DUMP_COMPRESSION" --no-owner --no-acl \
      > "$tmp_dump"

    mv "$tmp_dump" "$dump_path"

    echo "verifying dump TOC..."
    compose exec -T "$PG_SERVICE" pg_restore -l < "$dump_path" >/dev/null

    du -h "$dump_path"

    db_dest="${BACKUP_URI}/db/${dump_name}"
    latest_dest="${BACKUP_URI}/db/latest.dump"
    gcloud storage cp "$dump_path" "$db_dest"
    gcloud storage cp "$db_dest" "$latest_dest"
  fi
else
  echo "skipping Postgres dump"
fi

section "Backup manifest"
if [[ "$DRY_RUN" == 1 ]]; then
  echo "would write/upload manifest: ${manifest_path} -> ${BACKUP_URI}/manifests/${manifest_name}"
else
  {
    echo "timestamp=${TIMESTAMP}"
    echo "backup_uri=${BACKUP_URI}"
    echo "host=$(hostname)"
    echo "working_dir=$(pwd)"
    echo "postgres_service=${PG_SERVICE}"
    echo "postgres_database=${PGDATABASE}"
    if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      echo "git_commit=$(git rev-parse HEAD)"
      echo "git_dirty_files=$(git status --short | wc -l | tr -d '[:space:]')"
    fi
    if [[ -f "$dump_path" ]]; then
      echo "db_dump=${dump_name}"
      echo "db_dump_bytes=$(wc -c < "$dump_path" | tr -d '[:space:]')"
    fi
    if [[ -d "$TRAINING_DATA_DIR" ]]; then
      echo "training_data_du=$(du -sh "$TRAINING_DATA_DIR" | awk '{print $1}')"
    fi
  } > "$manifest_path"
  cat "$manifest_path"
  gcloud storage cp "$manifest_path" "${BACKUP_URI}/manifests/${manifest_name}"
fi

if [[ "$SKIP_TRAINING_DATA" != 1 ]]; then
  section "Training data sync"
  if [[ ! -d "$TRAINING_DATA_DIR" ]]; then
    echo "ERROR: training data directory does not exist: $TRAINING_DATA_DIR" >&2
    exit 1
  fi

  rsync_args=(storage rsync --recursive)
  if [[ "$DRY_RUN" == 1 ]]; then
    rsync_args+=(--dry-run)
  fi
  if [[ "$DELETE_EXTRA_TRAINING_DATA" == 1 ]]; then
    rsync_args+=(--delete-unmatched-destination-objects)
  fi
  rsync_args+=("$TRAINING_DATA_DIR" "${BACKUP_URI}/trainer-data")

  gcloud "${rsync_args[@]}"
else
  echo "skipping training data sync"
fi

if [[ "$DRY_RUN" != 1 && "$KEEP_LOCAL" != 1 && "$SKIP_DB" != 1 ]]; then
  section "Local cleanup"
  rm -f "$dump_path"
  echo "removed local dump: $dump_path"
fi

section "Done"
echo "backup complete: ${BACKUP_URI}"
