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
