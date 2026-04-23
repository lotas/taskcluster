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
# The trainer container's working_dir is /app/trainer (→ host ./trainer/),
# so the config path is passed in container-relative form. Any leading
# "trainer/" or "tools/queue-forecasting/trainer/" prefix is stripped.

set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <config.yaml>" >&2
  echo "  e.g. $0 configs/wait_time.yaml" >&2
  exit 1
fi
CONFIG="$1"
# Normalize to container-relative path (trainer working_dir is /app/trainer).
CONFIG="${CONFIG#tools/queue-forecasting/}"
CONFIG="${CONFIG#trainer/}"

cd "$(dirname "$0")/.."

if [[ ! -f "trainer/$CONFIG" ]]; then
  echo "ERROR: config file not found at trainer/$CONFIG" >&2
  echo "  (normalized from: $1)" >&2
  exit 1
fi

mkdir -p trainer/data/baseline

# Step 1: resolve holdout days from config (no DB access, pure config math).
# --entrypoint takes a single executable; the rest of the command becomes argv.
# Filter to date-shaped lines only: `docker compose run` can leak buildkit
# progress (e.g. "#12 resolving provenance...") into stdout, which would otherwise
# end up in HOLDOUT_DAYS and break the for-loop below.
HOLDOUT_DAYS=$(docker compose run --rm \
  --entrypoint uv \
  trainer \
  run python -m src.resolve_holdout_days --config "$CONFIG" \
  | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}$')

if [[ -z "$HOLDOUT_DAYS" ]]; then
  echo "ERROR: resolve_holdout_days produced no dates." >&2
  echo "  Check: docker compose run --rm --entrypoint uv trainer run python -m src.resolve_holdout_days --config $CONFIG" >&2
  exit 1
fi

echo "Holdout days: $HOLDOUT_DAYS"

# Step 2: generate per-day baselines (skip ones already present).
for d in $HOLDOUT_DAYS; do
  OUT="trainer/data/baseline/${d}.json"
  if [[ -f "$OUT" ]]; then
    echo "baseline exists: $OUT"
    continue
  fi
  echo "baseline generate: $d"
  docker compose run --rm predictor \
    node src/predictor.js \
      --pending-eval-date "$d" \
      --output-json "/app/tools/queue-forecasting/${OUT}"
done

# Step 3: train + evaluate.
docker compose run --rm trainer --config "$CONFIG"
