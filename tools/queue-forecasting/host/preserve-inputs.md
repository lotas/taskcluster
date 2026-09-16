# Preserving the evaluation inputs (protocol §2.5)

Operator runbook, 2026-09-15. Everything here runs on the host from the
DEPLOY checkout (`~/dev/taskcluster/tools/queue-forecasting`, the one with
`.env` and `trainer/data`). Nothing here examines a reserved outcome.

The research loop's timer stays OFF throughout (`systemctl status qf-tick.timer`
should say inactive). With no agent running, nothing reads a reserved extract,
which is what "held unpublished" means in practice for this round: the extract
store has no staged state, so an extract is visible the moment it is cut.

## 0. What is at risk, and why no date is claimed

`src/retention.js` deletes tasks by `task_created` (runs cascade), at 60 days.
The baseline exporter needs the 7 days of resolved history before each pending
time. The development cohorts' baseline history starts 2026-07-25. Which of
those rows are already gone is a question for the database, not for
arithmetic, so step 1 asks it.

## 1. Inventory (read-only, minutes)

```sh
docker compose exec -T postgres psql -U postgres -d forecasting -f - < host/inventory-inputs.sql
docker compose config | grep RETENTION_DAYS
```

Record the output in the freeze commit later. Read off:

- the earliest surviving `task_created` and the created-to-resolved lag. These
  BOUND the risk; they certify nothing. A task deleted for its age may have
  resolved inside the 7-day history of a day whose own rows all survive, and
  the surviving rows carry no trace of it. History completeness is
  established only by step 2b, against a preserved input.
- the still-pending counts for 09-06..09-15 — they should be near zero before
  R3 and R4 are cut.

## 2. Export the baseline inputs NOW (hours; predictor has 6 GB)

Reuse the cutover recipe's pieces rather than the whole script: `cutover-v2.sh`
handles exactly two cohorts and the package needs six windows.

```sh
# Exclusion list, Policy B, resolved the way cutover-v2.sh does it. One list
# serves the whole window (load_anomalous_dates has no date bound).
EXCLUDED="$(docker compose run --rm --entrypoint uv trainer \
    run python -m scripts.resolve_excluded_dates \
    --config configs/wait_time_residual_throughput_filtered_baseline.yaml \
  | { grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' || true; } | sort -u | tr '\n' ',' | sed 's/,$//')"
echo "exclude: $EXCLUDED"

FROM=2026-07-25          # the development cohorts' baseline history start; step 2b decides whether it held
TO=2026-09-11            # EXCLUSIVE, as predictor.js --to is; covers through R3's holdout (09-06..09-10)
STAGE=trainer/data/baseline_v3_${FROM}_${TO}
mkdir -p "$STAGE"
IN="/app/tools/queue-forecasting/$STAGE"

# Same heavy-work mutex the cutover takes (a probe or the nightly must not overlap).
# With the loop off and no nightly running this is a formality, but take it.
exec 7>>/var/lib/qf-locks/heavy-training.lock; flock -w 9000 7

docker compose run --rm predictor node src/predictor.js \
  --export-baseline-predictions --from "$FROM" --to "$TO" \
  --output "$IN/baseline_predictions.ndjson" ${EXCLUDED:+--exclude-dates "$EXCLUDED"}

# Per-day JSONs for EVERY holdout day the package must serve:
#   development: 08-15..08-19, 08-22..08-26
#   reserved:    R1 08-27..08-31, R2 09-01..09-05, R3 09-06..09-10  (R4 in step 4)
for d in 2026-08-15 2026-08-16 2026-08-17 2026-08-18 2026-08-19 \
         2026-08-22 2026-08-23 2026-08-24 2026-08-25 2026-08-26 \
         2026-08-27 2026-08-28 2026-08-29 2026-08-30 2026-08-31 \
         2026-09-01 2026-09-02 2026-09-03 2026-09-04 2026-09-05 \
         2026-09-06 2026-09-07 2026-09-08 2026-09-09 2026-09-10; do
  [ -f "$STAGE/$d.json" ] && continue
  docker compose run --rm predictor node src/predictor.js \
    --pending-eval-date "$d" --output-json "$IN/$d.json" ${EXCLUDED:+--exclude-dates "$EXCLUDED"}
done
exec 7>&-

# ONE exclusion list for the whole package, persisted now and reused
# VERBATIM by every later export into this set (step 4). A package whose files
# were computed under two lists does not match its declared policy.
printf '%s' "$EXCLUDED" > "$STAGE.exclude_dates"
# Provenance beside the set (NOT inside it: the store is closed-world).
{ echo "from=$FROM to=$TO exclude=$EXCLUDED exported=$(date -u +%FT%TZ)";
  (cd "$STAGE" && sha256sum *); } > "$STAGE.provenance.txt"
```

### 2b. Validate before believing it (minutes)

The database cannot say whether the July/August history behind the export was
complete. The promoted v2 baseline `9c150d75` can: its NDJSON was computed on
2026-09-11 when that history was intact, so every row of it must be reproduced
exactly by the new export, on every row whose 7-day history contains no date
where the two exclusion lists differ. And every train+validation row of every
cohort the package serves must have a baseline row.

```sh
# Extract directories are named by REQUEST hash (`qf extracts --json` -> "dir"),
# not by the extract_hash the `qf extract` command prints on success.
X=/var/lib/qf-extracts
REF=/var/lib/qf-baselines/9c150d75a8ea3ba49f113f4d04c30f514f89a19ef5aa5168f6fc6d011b7bafca
REF_EXCL="$(python3 -c 'import json,sys; print(",".join(json.load(open(sys.argv[1]))["exclude_dates"]))' $REF/MANIFEST.json)"
# The evaluator venv lives in the TRUSTED checkout, not this one.
PY=/srv/queue-forecasting/tools/queue-forecasting/host/evaluator/env/.venv/bin/python
sudo $PY host/check-baseline-coverage.py \
  --ndjson "$STAGE/baseline_predictions.ndjson" --exclude-dates "$(cat $STAGE.exclude_dates)" \
  --reference "$REF/baseline_predictions.ndjson" --reference-exclude-dates "$REF_EXCL" \
  --cohort 2026-08-20=$X/975aea71d759b83b199cdb697bf2ead204dbae04baa6681268d6ce56e7178c01/runs.parquet \
  --cohort 2026-08-27=$X/bd29b39ab6254a3cf5de6a7413c1476a6caa178a0685f88aaa7d489c9a2db91f/runs.parquet \
  --cohort 2026-09-01=$X/d3c5330dc78170c89daaedbd58b303076707b0f62f3769909e0fe87dc1c55d54/runs.parquet \
  --cohort 2026-09-06=$X/8a0f96cb5d8b1bfbbba7ccd8ac248c186d9176b9f667da1d47b9f9538638280b/runs.parquet \
  --json "$STAGE.coverage.json"
```

Result on 2026-09-16 (R2 cohort = the extract cut in step 3 the same day):
all four cohorts 0 missing rows; 8,816,035 reference rows comparable, 0
disagree, 0 absent; exclusion lists differ only on 2026-09-12..09-15 (flagged
after 9c150d75 was cut). PROMOTABLE. Recorded in `$STAGE.coverage.json`.

Read the last line. `NOT PROMOTABLE` with disagreeing reference rows means the
early history had already been thinned when the export ran: the development
windows cannot be reproduced under this package, and the freeze commit says so
instead of shrinking the window silently. Do not substitute the reference's
rows into the new set; that would be two histories under one declared policy.
Re-run this check after step 4 with R3 and R4 added as cohorts.

Sanity checks before calling it preserved:

```sh
# every day from FROM to TO-1 has rows, and the level columns are present
awk -F'"' '{c[substr($10,1,10)]++} END {for (k in c) print k, c[k]}' "$STAGE/baseline_predictions.ndjson" | sort | head -3
head -1 "$STAGE/baseline_predictions.ndjson" | grep -o 'bl_wait_level\|bl_wait_sample_size' | sort -u
ls "$STAGE"/*.json | wc -l     # 25
```

Do NOT promote yet. Promotion is once, with R4 included (protocol §6).

## 3. Cut the reserved extracts that are due (R2 now)

Settlement lag is 48 h (`extract_spec.DEFAULT_SETTLEMENT_LAG_S`), so an
as_of of 2026-09-06 has been extractable since 09-08; the protocol's own
minimum delay (as_of + 7 d) was met on 09-13. Copy the flags `d3c5330d` was
cut with (`qf extracts --json` shows its `lookback_days` and `generation`);
the qctx configs need `task_created` in `qctx_runs`, which `experiment.py plan`
checks by column, not by generation.

```sh
sudo -H -u research qf extract --target wait_time \
    --train-start 2026-08-17T00:00:00Z --as-of 2026-09-06T00:00:00Z \
    --lookback-days 30 --note 'protocol R2: holdout 2026-09-01..09-05' --wait
```

R3 on or after 2026-09-18 (`--train-start 2026-08-22 --as-of 2026-09-11`),
R4 on or after 2026-09-23 (`--train-start 2026-08-27 --as-of 2026-09-16`).
One extract per day is the research loop's own cap (`MAX_EXTRACTS` in
`tick.sh`, enforced by a shim), not the dispatcher's; an operator extract does
not compete with it.

**R1 provenance (record in the freeze commit).** `d3c5330d` (as_of 2026-09-01,
holdout 08-27..08-31) was not cut by an operator: the research loop cut it at
2026-09-12T02:12Z as its daily extract, then submitted five probes against it
(09-12 03:08, 09-13 04:05, 09-14 03:05, 17:12, 19:13). All five FAILED before
training: `_require_baselines` in `trainer/src/train.py` refuses a cohort
whose holdout days have no per-day JSON in the pinned baseline, and `9c150d75`
carries only 08-15..19 and 08-22..26. No model was trained or scored on R1's
holdout, so its outcome is still unexamined -- but by a gate, not by policy.
The loop auto-paused itself on 2026-09-15T14:09Z (three consecutive
verification disagreements; `~research/qf-research/PAUSE`, issue
lotas/qf-research#11) and the timer was stopped 2026-09-16T13:01Z.

**Why the timer stays off until promotion.** Extract directories are
world-readable (`/var/lib/qf-extracts/*/runs.parquet`, mode 0644), so a running
leader can read a reserved holdout without a probe. And the tick prompt's
highest-value action is "confirm on a cohort whose holdout does not overlap",
which R2 (09-01..09-05) and R1 are, exactly; today only the per-day JSON gate
stops that, and the v3 package removes the gate for both. The loop also has
nothing else to do: its 2026-09-15 escalation says every PROMISING row is
blocked on precisely the per-day files this package will promote.

Whether the extractor supports an observed-at cutoff (protocol §2.3): it does
not. `extract_spec` has a settlement lag and no snapshot cutoff; the extract
reads the tables as they are at cut time. Record "minimum delay only" in the
freeze commit.

## 4. On 2026-09-23: extend for R4, then build the package once

```sh
# Try the full range first; if the early rows are gone, the step-2 export is
# the record and you append instead.
docker compose run --rm predictor node src/predictor.js \
  --export-baseline-predictions --from 2026-09-11 --to 2026-09-16 \
  --output "$IN/baseline_predictions_r4.ndjson" ${EXCLUDED:+--exclude-dates "$EXCLUDED"}
for d in 2026-09-11 2026-09-12 2026-09-13 2026-09-14 2026-09-15; do
  docker compose run --rm predictor node src/predictor.js \
    --pending-eval-date "$d" --output-json "$IN/$d.json" ${EXCLUDED:+--exclude-dates "$EXCLUDED"}
done
# The store accepts ONE NDJSON. Ranges do not overlap (TO is exclusive), so
# concatenation adds no duplicate rows.
cat "$STAGE/baseline_predictions_r4.ndjson" >> "$STAGE/baseline_predictions.ndjson"
rm "$STAGE/baseline_predictions_r4.ndjson"
```

The exclusion list is the one persisted in step 2 (`$STAGE.exclude_dates`),
reused verbatim: `EXCLUDED="$(cat $STAGE.exclude_dates)"` before the commands
above. Days flagged anomalous after that list was taken are NOT excluded from
this package, and the provenance records which ones they are, so the package's
declared policy is "Policy B with the list as of <step-2 date>" and every file
in it matches that policy. If a newly flagged day matters enough to exclude,
the whole set is regenerated under the new list and step 2b is re-run; a set
is never patched file by file.

Then, and only after the protocol is frozen (§7):

```sh
sudo ./host/promote-baseline.sh "$STAGE" ${EXCLUDED:+--exclude "$EXCLUDED"}
sudo ./host/instantiate-contract.sh wait_time.v3 <NEW_HASH> --activate   # once wait_time.v3.json.in exists
```

## 5. What this does not do

It does not run any probe, does not evaluate anything, and does not touch the
published baselines or contracts. The development-only diagnostic
(`host/floor-diagnostic.py`) reads existing `eval.parquet` files and needs
none of the above.
