#!/usr/bin/env bash
# Tests `results.py`, the join that turns runs into a comparable history.
#
# WHY THIS FILE EXISTS. A results table is read as though its rows can be
# compared, so its failure mode is not a crash -- it is printing two experiments
# under one label, or printing rows from two extracts as if they were a series.
# Both of those look fine. The assertions below are mostly about that.
#
#   ./tests/test_results.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS="$HERE/../results.py"
[ -f "$RESULTS" ] || { echo "cannot find $RESULTS" >&2; exit 2; }

pass=0; fail=0
ok()  { echo "ok    $1"; pass=$((pass + 1)); }
bad() { echo "FAIL  $1"; fail=$((fail + 1)); }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# One board builder, so a fixture cannot disagree with itself about the shape
# `qfd` writes. `bar` is an OBJECT here for the same reason it is one there.
board() {  # board <measured-mae> <mae-passed> <tail>
  python3 -c '
import json, sys
mae, passed, tail = float(sys.argv[1]), sys.argv[2] == "true", float(sys.argv[3])
print(json.dumps({"metrics": {
    "mae": {"measured": mae, "passed": passed, "value": 425.0,
            "baseline": 455.0, "direction": "lower_is_better",
            "bar": {"kind": "relative_improvement", "value": 0.15}},
    "p90_miss_tail": {"measured": tail, "passed": tail < 0.3, "value": tail,
                      "baseline": 0.34, "bucket": "30m+",
                      "direction": "lower_is_better",
                      "bar": {"kind": "absolute", "value": 0.3}}},
    "consistency": {"days_required": 3, "days_passed": 4}},
    separators=(",", ":"), sort_keys=True))' "$1" "$2" "$3"
}

# `<jobs json>\x1d<status json>\x1e<status json>...`, which is what results.sh
# pipes in. Built by a python helper because the payload is JSON in JSON.
fixture() {  # fixture <extract-of-row-2>
  python3 -c '
import json, sys
extract_2 = sys.argv[1]
boards = sys.argv[2], sys.argv[3]
E1 = "cd467b4bd869" + "c" * 52
B = "e51a321057ca" + "e" * 52
C = "f740716d32b8" + "f" * 52
LONG = "configs/wait_time_residual_throughput_filtered_baseline"
runs = [
    ("probe-20260830T140000Z-aaaaaaaaaaaa-1", "evaluate-20260830T141000Z-bbbbbbbbbbbb-2",
     "2026-08-30T14:10:00Z", E1, f"cfg={LONG}.yaml", "no-go", boards[0]),
    ("probe-20260830T150000Z-cccccccccccc-3", "evaluate-20260830T151000Z-dddddddddddd-4",
     "2026-08-30T15:10:00Z", extract_2, f"cfg={LONG}_qctx.yaml | qctx features",
     "go", boards[1]),
]
jobs, statuses = [], []
for probe, ev, when, extract, note, verdict, board in runs:
    jobs.append({"run_id": probe, "kind": "probe", "state": "SUCCEEDED",
                 "submitted_at": when, "spec_json": json.dumps({"note": note})})
    jobs.append({"run_id": ev, "kind": "evaluate", "state": "SUCCEEDED",
                 "submitted_at": when,
                 "spec_json": json.dumps({"note": note, "args": {"run": probe}})})
    statuses.append(json.dumps({"job": {
        "run_id": ev, "submitted_at": when,
        "pins": {"verdict": verdict, "scoreboard": board, "judged_run": probe,
                 "request_hash": extract, "baseline_hash": B,
                 "contract_hash": C}}}))
# A FAILED probe and a RUNNING one. Neither is a result, and a table that showed
# them would be a table where "no row" and "no verdict" look the same.
jobs.append({"run_id": "probe-20260830T160000Z-eeeeeeeeeeee-5", "kind": "probe",
             "state": "FAILED", "submitted_at": "2026-08-30T16:00:00Z",
             "spec_json": json.dumps({"note": "cfg=configs/oom.yaml"})})
jobs.append({"run_id": "evaluate-20260830T170000Z-ffffffffffff-6",
             "kind": "evaluate", "state": "RUNNING",
             "submitted_at": "2026-08-30T17:00:00Z",
             "spec_json": json.dumps({"note": "cfg=configs/pending.yaml"})})
jobs.sort(key=lambda j: j["submitted_at"], reverse=True)
sys.stdout.write(json.dumps({"ok": True, "jobs": jobs}) + "\x1d"
                 + "\x1e".join(statuses))' "$1" "$(board 0.0671 false 0.247)" \
    "$(board 0.163 true 0.221)"
}

SAME="$(fixture "cd467b4bd869$(printf 'c%.0s' {1..52})")"
CROSS="$(fixture "bd29b39ab625$(printf 'd%.0s' {1..52})")"

printf '%s' "$SAME" > "$TMP/same"
printf '%s' "$CROSS" > "$TMP/cross"
python3 "$RESULTS" < "$TMP/same" > "$TMP/out" 2>"$TMP/err" \
  || { echo "results.py failed: $(cat "$TMP/err")" >&2; exit 2; }
OUT="$(cat "$TMP/out")"

grep -q "0.0671!" <<< "$OUT" \
  && ok "a metric that missed its bar is marked" \
  || bad "a metric that missed its bar is marked"

grep -q "0.247 " <<< "$OUT" \
  && ok "a metric that met its bar is not marked" \
  || bad "a metric that met its bar is not marked"

# THE ONE THAT MATTERS. The two configs differ in their last five characters, so
# a table that truncates from the right prints both rows as the same experiment.
grep -q "_baseline_qctx" <<< "$OUT" \
  && ok "a long config name keeps the part that distinguishes it" \
  || bad "a long config name keeps the part that distinguishes it"

[ "$(grep -c "^2026-08-30" <<< "$OUT")" = 2 ] \
  && ok "only scored runs are rows (a failed and a running job are not)" \
  || bad "only scored runs are rows: $(grep -c '^2026-08-30' <<< "$OUT") row(s)"

# Oldest first: the 14:10 row above the 15:10 one, so the table reads forward.
first="$(grep "^2026-08-30" <<< "$OUT" | head -1)"
grep -q "14:10" <<< "$first" \
  && ok "oldest first" || bad "oldest first (got '$first')"

grep -q "WARNING" <<< "$OUT" \
  && bad "one input set must not warn" \
  || ok "one input set does not warn"

CROSS_OUT="$(python3 "$RESULTS" < "$TMP/cross")"
grep -q "WARNING: 2 different input sets" <<< "$CROSS_OUT" \
  && ok "two extracts in one table warns" \
  || bad "two extracts in one table warns"

# `--json` must not gain display-only keys, or two callers of the same function
# disagree about the shape depending on which ran first.
KEYS="$(python3 "$RESULTS" --json < "$TMP/same" \
  | python3 -c 'import json,sys; print(",".join(sorted(json.load(sys.stdin)[0])))')"
[ "$KEYS" = "baseline,contract,evaluation,extract,metrics,note,passed,probe,verdict,when" ] \
  && ok "--json carries the joined fields and nothing display-only" \
  || bad "--json keys: $KEYS"

# A run from before the scoreboard existed: verdict, no metrics. It has to
# render, because the alternative is a history that starts at today.
OLD="$(python3 -c '
import json, sys
sys.stdout.write(json.dumps({"jobs": [{"run_id": "evaluate-1", "kind": "evaluate",
  "state": "SUCCEEDED", "submitted_at": "2026-08-01T00:00:00Z",
  "spec_json": json.dumps({"note": "cfg=configs/old.yaml"})}]}) + "\x1d"
  + json.dumps({"job": {"run_id": "evaluate-1",
      "submitted_at": "2026-08-01T00:00:00Z",
      "pins": {"verdict": "no-go", "request_hash": "aa" * 32}}}))')"
if OLD_OUT="$(printf '%s' "$OLD" | python3 "$RESULTS" 2>&1)"; then
  grep -q "no-go" <<< "$OLD_OUT" \
    && ok "a run with no scoreboard still renders its verdict" \
    || bad "a run with no scoreboard still renders its verdict"
else
  bad "a run with no scoreboard crashed: $OLD_OUT"
fi

for junk in 'not json' '{"jobs": []}'; do
  if printf '%s\x1dgarbage' "$junk" | python3 "$RESULTS" >/dev/null 2>&1; then
    ok "unreadable input is reported, not a traceback ($junk)"
  else
    bad "unreadable input crashed ($junk)"
  fi
done

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
