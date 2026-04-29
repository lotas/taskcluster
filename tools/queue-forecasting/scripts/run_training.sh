#!/usr/bin/env bash
# Two-step training orchestration:
#   1. Generate per-holdout-day baseline JSONs via the Node predictor.
#   2. Train + evaluate via the Python trainer.
#
# Usage (all three work):
#   ./scripts/run_training.sh configs/wait_time.yaml
#   ./scripts/run_training.sh trainer/configs/wait_time.yaml
#   ./scripts/run_training.sh tools/queue-forecasting/trainer/configs/wait_time.yaml
#
# Optional flag (must come after the config):
#   ./scripts/run_training.sh configs/wait_time.yaml --as-of-date 2026-04-21
#
# The trainer container's working_dir is /app/trainer (→ host ./trainer/),
# so the config path is passed in container-relative form. Any leading
# "trainer/" or "tools/queue-forecasting/trainer/" prefix is stripped.

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <config.yaml> [--as-of-date YYYY-MM-DD]" >&2
  echo "  e.g. $0 configs/wait_time.yaml" >&2
  echo "  e.g. $0 configs/wait_time.yaml --as-of-date 2026-04-21" >&2
  exit 1
fi
CONFIG="$1"
shift

# Parse optional --as-of-date flag from remaining args.
AS_OF_DATE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --as-of-date)
      AS_OF_DATE="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

# Normalize to container-relative path (trainer working_dir is /app/trainer).
CONFIG="${CONFIG#tools/queue-forecasting/}"
CONFIG="${CONFIG#trainer/}"

cd "$(dirname "$0")/.."

if [[ ! -f "trainer/$CONFIG" ]]; then
  echo "ERROR: config file not found at trainer/$CONFIG" >&2
  echo "  (normalized from original first arg)" >&2
  exit 1
fi

# Build --as-of-date flag fragments for passing through to sub-commands.
AS_OF_FLAG=()
if [[ -n "$AS_OF_DATE" ]]; then
  AS_OF_FLAG=(--as-of-date "$AS_OF_DATE")
fi

# Step 1: resolve holdout days from config (no DB access, pure config math).
# --entrypoint takes a single executable; the rest of the command becomes argv.
# Filter to date-shaped lines only: `docker compose run` can leak buildkit
# progress (e.g. "#12 resolving provenance...") into stdout, which would otherwise
# end up in HOLDOUT_DAYS and break the for-loop below.
HOLDOUT_DAYS=$(docker compose run --rm \
  --entrypoint uv \
  trainer \
  run python -m src.resolve_holdout_days --config "$CONFIG" "${AS_OF_FLAG[@]}" \
  | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}$')

if [[ -z "$HOLDOUT_DAYS" ]]; then
  echo "ERROR: resolve_holdout_days produced no dates." >&2
  echo "  Check: docker compose run --rm --entrypoint uv trainer run python -m src.resolve_holdout_days --config $CONFIG ${AS_OF_FLAG[*]:-}" >&2
  exit 1
fi

echo "Holdout days: $HOLDOUT_DAYS"

# Step 1.5: resolve excluded dates (Policy B/C only). Empty for A or unset.
# Also resolve the configured baseline directory so that each policy can
# keep its own per-day baseline cache.
#
# `|| true` is required: under `set -euo pipefail`, grep matching nothing
# (the by-design Policy A case) would propagate rc=1 and kill the script.
EXCLUDED_DATES=$(docker compose run --rm \
  --entrypoint uv \
  trainer \
  run python -m scripts.resolve_excluded_dates --config "$CONFIG" "${AS_OF_FLAG[@]}" \
  | { grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' || true; } | tr '\n' ',' | sed 's/,$//')

EXCLUDE_FLAG=()
if [[ -n "$EXCLUDED_DATES" ]]; then
  EXCLUDE_FLAG=(--exclude-dates "$EXCLUDED_DATES")
  echo "Excluding ${EXCLUDED_DATES} from baseline history"
fi

BASELINE_REL=$(docker compose run --rm \
  --entrypoint uv \
  trainer \
  run python -m scripts.resolve_baseline_dir --config "$CONFIG" \
  | { grep -E '^[A-Za-z0-9_./-]+$' || true; } | tail -n 1)
if [[ -z "$BASELINE_REL" ]]; then
  BASELINE_REL="data/baseline"
fi
echo "Baseline cache dir: trainer/${BASELINE_REL}"

mkdir -p "trainer/${BASELINE_REL}"

# Step 1.6: if config uses residual architecture, ensure the aggregate
# baseline-predictions NDJSON exists in the configured baseline dir. The
# trainer joins it on (task_id, run_id) for the residual feature, so it
# must cover every pending_at the cohort will train/eval over.
#
# Window: [as_of - lookback_days - validation_days - holdout_days, as_of)
# matches the trainer's compute_windows() math; +2 days padding for safety.
if grep -q '^residual:' "trainer/${CONFIG}"; then
  AS_OF_FOR_NDJSON="${AS_OF_DATE:-$(date -u +%F)}"
  NDJSON_FROM=$(python3 -c "
import sys, yaml
from datetime import datetime, timedelta
cfg = yaml.safe_load(open(sys.argv[1]))
window = cfg['lookback_days'] + cfg['validation_days'] + cfg['holdout_days'] + 2
print((datetime.fromisoformat(sys.argv[2]) - timedelta(days=window)).strftime('%Y-%m-%d'))
" "trainer/${CONFIG}" "$AS_OF_FOR_NDJSON")
  ./scripts/ensure_baseline_ndjson.sh \
    "$BASELINE_REL" "$EXCLUDED_DATES" "$NDJSON_FROM" "$AS_OF_FOR_NDJSON"
fi

# Step 2: generate per-day baselines (skip ones already present).
for d in $HOLDOUT_DAYS; do
  OUT="trainer/${BASELINE_REL}/${d}.json"
  if [[ -f "$OUT" ]]; then
    echo "baseline exists: $OUT"
    continue
  fi
  echo "baseline generate: $d"
  docker compose run --rm predictor \
    node src/predictor.js \
      --pending-eval-date "$d" \
      --output-json "/app/tools/queue-forecasting/${OUT}" \
      "${EXCLUDE_FLAG[@]}"
done

# Step 3: train + evaluate.
docker compose run --rm trainer --config "$CONFIG" "${AS_OF_FLAG[@]}"
