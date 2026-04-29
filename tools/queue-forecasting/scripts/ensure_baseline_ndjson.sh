#!/usr/bin/env bash
# Ensure trainer/<baseline_dir>/baseline_predictions.ndjson exists and covers
# the requested date range / exclude_dates. Validated via a .meta.json sidecar.
#
# Usage:
#   ensure_baseline_ndjson.sh <baseline_rel_dir> <exclude_dates_csv> <from_date> <to_date>
#
#   <baseline_rel_dir>   e.g. "data/baseline" or "data/baseline_filtered"
#                        (relative to trainer/)
#   <exclude_dates_csv>  comma-separated YYYY-MM-DD dates, or empty string
#   <from_date>          YYYY-MM-DD inclusive
#   <to_date>            YYYY-MM-DD exclusive
#
# Behavior:
#   - file present + sidecar covers requested coverage and excludes match  -> skip
#   - file present + sidecar missing/stale/insufficient                    -> regenerate
#   - file absent                                                          -> generate
#
# Sidecar lives at "<NDJSON>.meta.json".  Run from tools/queue-forecasting/.

set -euo pipefail

BASELINE_REL="$1"
EXCLUDED_DATES="$2"
FROM_DATE="$3"
TO_DATE="$4"

NDJSON="trainer/${BASELINE_REL}/baseline_predictions.ndjson"
META="${NDJSON}.meta.json"

needs_regen=1
if [[ -f "$NDJSON" ]]; then
  if python3 scripts/check_baseline_ndjson_meta.py \
       "$META" "$FROM_DATE" "$TO_DATE" "$EXCLUDED_DATES" \
       | sed 's/^/[ensure-baseline] /'; then
    needs_regen=0
  fi
fi

if [[ $needs_regen -eq 0 ]]; then
  echo "[ensure-baseline] reuse: $NDJSON"
  exit 0
fi

# Stale or missing -> regenerate cleanly.
rm -f "$NDJSON" "$META"
mkdir -p "trainer/${BASELINE_REL}"

EXCLUDE_FLAG=()
if [[ -n "$EXCLUDED_DATES" ]]; then
  EXCLUDE_FLAG=(--exclude-dates "$EXCLUDED_DATES")
  echo "[ensure-baseline] generating: $NDJSON  range=[${FROM_DATE}..${TO_DATE})  excluding=${EXCLUDED_DATES}"
else
  echo "[ensure-baseline] generating: $NDJSON  range=[${FROM_DATE}..${TO_DATE})  no-exclusions"
fi

docker compose run --rm predictor \
  node src/predictor.js \
  --export-baseline-predictions \
  --from "$FROM_DATE" --to "$TO_DATE" \
  --output "/app/tools/queue-forecasting/${NDJSON}" \
  "${EXCLUDE_FLAG[@]}"

# Write the sidecar only after the predictor reports success — otherwise a
# half-generated NDJSON would be incorrectly trusted on the next run.
python3 - "$META" "$FROM_DATE" "$TO_DATE" "$EXCLUDED_DATES" <<'PY'
import json, sys
from datetime import datetime, timezone
meta_path, from_d, to_d, exclude_csv = sys.argv[1:]
exclude_dates = sorted({x.strip() for x in exclude_csv.split(",") if x.strip()})
with open(meta_path, "w") as fh:
    json.dump({
        "from_date": from_d,
        "to_date": to_d,
        "exclude_dates": exclude_dates,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }, fh, indent=2)
    fh.write("\n")
PY

echo "[ensure-baseline] done: $NDJSON (sidecar: $META)"
