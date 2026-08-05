#!/usr/bin/env bash
# Walk-forward evaluation: run multiple configs across a range of as_of_dates.
# Skip-if-manifest-exists for resume safety.
#
# Usage:
#   ./scripts/walk_forward.sh --from 2026-04-19 --to 2026-04-24 \
#       [--configs configs/wait_time.yaml,configs/wait_time_residual.yaml,configs/wait_time_residual_throughput.yaml]
#
# Defaults: last 10 days ending today; three standard configs.

set -euo pipefail

usage() {
  cat >&2 <<USAGE
Usage: $0 --from YYYY-MM-DD --to YYYY-MM-DD [--configs c1,c2,...] [--step-days N]
  --from       inclusive start as_of_date
  --to         inclusive end as_of_date
  --step-days  stride between as_of_dates (default 1)
  --configs    comma-separated config paths (default: three standard wait configs)
USAGE
  exit "${1:-1}"
}

FROM=""
TO=""
STEP=1
CONFIGS="configs/wait_time.yaml,configs/wait_time_residual.yaml,configs/wait_time_residual_throughput.yaml"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from)       FROM="$2"; shift 2 ;;
    --to)         TO="$2"; shift 2 ;;
    --step-days)  STEP="$2"; shift 2 ;;
    --configs)    CONFIGS="$2"; shift 2 ;;
    -h|--help)    usage 0 ;;
    *)            echo "unknown arg: $1" >&2; usage 1 ;;
  esac
done

[[ -z "$FROM" || -z "$TO" ]] && usage 1
cd "$(dirname "$0")/.."

# Generate the list of as_of_dates (portable-ish date arithmetic).
gen_dates() {
  local from="$1" to="$2" step="$3"
  python3 - "$from" "$to" "$step" <<'PY'
import sys
from datetime import datetime, timedelta
f = datetime.fromisoformat(sys.argv[1])
t = datetime.fromisoformat(sys.argv[2])
s = int(sys.argv[3])
d = f
while d <= t:
    print(d.strftime("%Y-%m-%d"))
    d += timedelta(days=s)
PY
}

DATES=$(gen_dates "$FROM" "$TO" "$STEP")
IFS=',' read -ra CONFIG_LIST <<< "$CONFIGS"

# Pre-pass: pre-generate the aggregate baseline_predictions.ndjson once per
# unique (baseline_dir, exclude_dates) group across all residual configs in
# the sweep. Each cohort's run_training.sh will then find the file already
# in place and skip its own per-cohort generation.
#
# Two passes so a longer-lookback config in a group doesn't get its early
# rows silently truncated by an earlier-iterated shorter-lookback config:
#
#   pass 1 — collect each group's MAX (lookback + validation + holdout + 2)
#            across all configs in that group; also record group keys
#   pass 2 — for each unique group, generate one NDJSON sized to that max
echo ""
echo "[walk-forward] pre-pass: ensuring aggregate baseline NDJSONs"

declare -A GROUP_MAX_WINDOW
declare -A GROUP_BASELINE_REL
declare -A GROUP_EXCLUDES

for CFG in "${CONFIG_LIST[@]}"; do
  if ! grep -qE '^(residual|baseline_features):' "trainer/${CFG}"; then continue; fi

  baseline_rel=$(docker compose run --rm \
    --entrypoint uv \
    trainer \
    run python -m scripts.resolve_baseline_dir --config "$CFG" \
    | { grep -E '^[A-Za-z0-9_./-]+$' || true; } | tail -n 1)
  [[ -z "$baseline_rel" ]] && baseline_rel="data/baseline"

  excluded_dates=$(docker compose run --rm \
    --entrypoint uv \
    trainer \
    run python -m scripts.resolve_excluded_dates --config "$CFG" \
    | { grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' || true; } | tr '\n' ',' | sed 's/,$//')

  cfg_window=$(python3 -c "
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1]))
print(cfg['lookback_days'] + cfg['validation_days'] + cfg['holdout_days'] + 2)
" "trainer/${CFG}")

  group_key="${baseline_rel}|${excluded_dates}"
  current_max="${GROUP_MAX_WINDOW[$group_key]:-0}"
  if (( cfg_window > current_max )); then
    GROUP_MAX_WINDOW[$group_key]=$cfg_window
  fi
  GROUP_BASELINE_REL[$group_key]="$baseline_rel"
  GROUP_EXCLUDES[$group_key]="$excluded_dates"
done

for group_key in "${!GROUP_MAX_WINDOW[@]}"; do
  window=${GROUP_MAX_WINDOW[$group_key]}
  baseline_rel=${GROUP_BASELINE_REL[$group_key]}
  excluded_dates=${GROUP_EXCLUDES[$group_key]}

  ndjson_from=$(python3 -c "
import sys
from datetime import datetime, timedelta
print((datetime.fromisoformat(sys.argv[1]) - timedelta(days=int(sys.argv[2]))).strftime('%Y-%m-%d'))
" "$FROM" "$window")

  echo "[walk-forward] group baseline_dir=${baseline_rel} excludes=${excluded_dates:-none} window=${window}d"
  ./scripts/ensure_baseline_ndjson.sh \
    "$baseline_rel" "$excluded_dates" "$ndjson_from" "$TO"
done
echo ""

total=0
skipped=0
ran=0

for AS_OF in $DATES; do
  for CFG in "${CONFIG_LIST[@]}"; do
    total=$((total + 1))
    # Derive expected manifest path.
    # The manifest filename matches config stem.
    stem="$(basename "$CFG" .yaml)"
    manifest="trainer/data/models/${AS_OF}/${stem}_manifest.json"

    if [[ -f "$manifest" ]]; then
      echo "[walk-forward] SKIP ${AS_OF} ${stem} (manifest exists)"
      skipped=$((skipped + 1))
      continue
    fi

    echo "[walk-forward] RUN  ${AS_OF} ${stem}"
    ./scripts/run_training.sh "$CFG" --as-of-date "$AS_OF"
    ran=$((ran + 1))
  done
done

echo ""
echo "[walk-forward] complete: ${total} cells total, ${skipped} skipped, ${ran} ran"
echo "[walk-forward] run summarizer: uv run python trainer/scripts/summarize_walk_forward.py --from ${FROM} --to ${TO}"
