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
# NAMESPACED deliberately. `daily_walk_forward.sh` also reads a lock path, and
# the Phase 2a setup instructs the operator to put one on the crontab -- where a
# bare `LOCK_FILE=` assignment applies to EVERY entry below it. A shared name
# would make this script flock the heavy-training mutex and exit 1 whenever a
# training job held it: backups would die silently, and backups would become a
# contender on a mutex they have no business in.
BACKUP_LOCK_FILE="${BACKUP_LOCK_FILE:-/tmp/queue-forecasting-backup.lock}"
KEEP_DAILY="${KEEP_DAILY:-7}"
KEEP_WEEKLY="${KEEP_WEEKLY:-4}"
KEEP_MONTHLY="${KEEP_MONTHLY:-3}"

SKIP_DB=0
# Trainer-data is regenerable from the DB, so it is not synced by default.
SKIP_TRAINING_DATA=1
KEEP_LOCAL=0
DRY_RUN=0
PRUNE=1
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
  --sync-training-data            Sync trainer/data to Cloud Storage. Off by
                                  default (trainer-data is regenerable from the
                                  DB).
  --skip-training-data            Do not sync trainer/data. Now the default;
                                  kept as a no-op for backward compatibility.
  --delete-extra-training-data    With --sync-training-data, delete remote
                                  trainer-data objects that are absent locally.
                                  Off by default.
  --no-prune                      Do not prune old DB dumps after upload.
  --keep-local                    Keep the local dump after upload.
  --dry-run                       Print actions without dumping, uploading, or
                                  deleting.
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
  BACKUP_LOCK_FILE                flock path for cron non-overlap. Namespaced
                                  so a crontab-wide LOCK_FILE= (which the
                                  Phase 2a setup adds for the nightly
                                  trainer) cannot capture it.
  KEEP_DAILY                      GFS: newest dump per day, last N (default: 7).
  KEEP_WEEKLY                     GFS: newest dump per ISO week, last N (default: 4).
  KEEP_MONTHLY                    GFS: newest dump per month, last N (default: 3).
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
    --sync-training-data)
      SKIP_TRAINING_DATA=0
      shift
      ;;
    --no-prune)
      PRUNE=0
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

# Convert a 20260529T000001Z stamp into an ISO 8601 string GNU date understands.
ts_to_iso() {
  local ts="$1"
  printf '%s-%s-%sT%s:%s:%sZ' \
    "${ts:0:4}" "${ts:4:2}" "${ts:6:2}" "${ts:9:2}" "${ts:11:2}" "${ts:13:2}"
}

# GFS prune of DB dumps and their manifests. Runs after a fresh dump upload, so
# the newest dump is already in place and is always kept (it owns today's daily
# bucket). Internal failures are logged but never abort the backup.
prune_dumps() {
  section "Prune old dumps (GFS: ${KEEP_DAILY}d/${KEEP_WEEKLY}w/${KEEP_MONTHLY}m)"

  local listing
  if ! listing="$(gcloud storage ls "${BACKUP_URI}/db/" 2>/dev/null)"; then
    echo "WARN: could not list ${BACKUP_URI}/db/; skipping prune" >&2
    return 0
  fi

  # Collect dated-dump timestamps, excluding latest.dump and anything malformed.
  local -a stamps=()
  local line base ts
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    base="${line##*/}"
    case "$base" in
      "${PGDATABASE}-"*.dump) ;;
      *) continue ;;
    esac
    ts="${base#"${PGDATABASE}"-}"
    ts="${ts%.dump}"
    [[ "$ts" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || continue
    stamps+=("$ts")
  done <<< "$listing"

  if [[ "${#stamps[@]}" -eq 0 ]]; then
    echo "no dated dumps found; nothing to prune"
    return 0
  fi

  # Descending order; the fixed-width stamp sorts chronologically as text.
  local -a sorted
  mapfile -t sorted < <(printf '%s\n' "${stamps[@]}" | sort -r)

  # Keep set: newest dump in each of the most recent N day/week/month buckets.
  declare -A keep=() seen_day=() seen_week=() seen_month=()
  local iso daykey weekkey monthkey
  local n_day=0 n_week=0 n_month=0
  for ts in "${sorted[@]}"; do
    iso="$(ts_to_iso "$ts")"
    daykey="$(date -u -d "$iso" +%Y%m%d 2>/dev/null)" || continue
    weekkey="$(date -u -d "$iso" +%G%V 2>/dev/null)" || continue
    monthkey="$(date -u -d "$iso" +%Y%m 2>/dev/null)" || continue

    if [[ -z "${seen_day[$daykey]:-}" && "$n_day" -lt "$KEEP_DAILY" ]]; then
      seen_day[$daykey]=1; keep[$ts]=1; n_day=$((n_day + 1))
    fi
    if [[ -z "${seen_week[$weekkey]:-}" && "$n_week" -lt "$KEEP_WEEKLY" ]]; then
      seen_week[$weekkey]=1; keep[$ts]=1; n_week=$((n_week + 1))
    fi
    if [[ -z "${seen_month[$monthkey]:-}" && "$n_month" -lt "$KEEP_MONTHLY" ]]; then
      seen_month[$monthkey]=1; keep[$ts]=1; n_month=$((n_month + 1))
    fi
  done

  local -a to_delete=()
  local kept=0
  for ts in "${sorted[@]}"; do
    if [[ -n "${keep[$ts]:-}" ]]; then
      kept=$((kept + 1))
    else
      to_delete+=("$ts")
    fi
  done

  echo "dumps: ${#sorted[@]} total, ${kept} kept, ${#to_delete[@]} to delete"

  local dump_obj man_obj
  for ts in "${to_delete[@]}"; do
    dump_obj="${BACKUP_URI}/db/${PGDATABASE}-${ts}.dump"
    man_obj="${BACKUP_URI}/manifests/backup-${ts}.txt"
    if [[ "$DRY_RUN" == 1 ]]; then
      echo "would delete: $dump_obj"
      echo "would delete: $man_obj (if present)"
    else
      echo "deleting: $dump_obj"
      gcloud storage rm "$dump_obj" || echo "WARN: failed to delete $dump_obj" >&2
      # The matching manifest may not exist for every dump; ignore if absent.
      gcloud storage rm "$man_obj" 2>/dev/null || true
    fi
  done
}

if [[ "$BACKUP_URI" != gs://* ]]; then
  echo "ERROR: --backup-uri must be a gs:// Cloud Storage URI, got: $BACKUP_URI" >&2
  exit 1
fi

require_command docker
require_command gcloud

if command -v flock >/dev/null 2>&1; then
  exec 9>"$BACKUP_LOCK_FILE"
  if ! flock -n 9; then
    echo "ERROR: another backup is already running; lock file: $BACKUP_LOCK_FILE" >&2
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
sync_training_data: $([[ "$SKIP_TRAINING_DATA" == 1 ]] && echo no || echo yes)
prune:              $([[ "$PRUNE" == 1 ]] && echo "yes (${KEEP_DAILY}d/${KEEP_WEEKLY}w/${KEEP_MONTHLY}m)" || echo no)
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

if [[ "$PRUNE" == 1 && "$SKIP_DB" != 1 ]]; then
  # Pruning must never sink the backup: the fresh dump already succeeded.
  prune_dumps || echo "WARN: prune step failed; fresh backup is unaffected" >&2
else
  [[ "$PRUNE" != 1 ]] && echo "skipping prune (--no-prune)"
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
