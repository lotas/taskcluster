#!/usr/bin/env bash
# Every scored experiment, one row each, oldest first.
#
# WHY A SCRIPT AND NOT A `qf` SUBCOMMAND. The data is already reachable: `qf list
# --json` returns every job with its `spec_json` (so its note and its pinned
# inputs), and `qf status <id> --json` returns its pins (so its verdict and its
# scoreboard). What was missing is the JOIN -- and a join is not a new privilege.
# An op in `qfd` would mean a service restart to read numbers a client can
# already fetch.
#
#   ./results.sh              the last 200 jobs
#   LIMIT=1000 ./results.sh   more history
#   ./results.sh --json       the joined rows, for something else to read
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIMIT="${LIMIT:-200}"
# AN ARRAY, CALLED DIRECTLY, not a `qf()` wrapper function. The wrapper shape
# `first-probe.sh` uses is safe there only because its prefix is never empty --
# with an empty prefix, `qf() { "${AS_RESEARCH[@]}" qf "$@"; }` calls ITSELF and
# recurses. That is reachable here, because this script is meant to be runnable
# BY the research user as well as by an operator.
QF=(sudo -H -u research qf)
[ "$(id -un)" != "research" ] || QF=(qf)

JOBS="$("${QF[@]}" list --limit "$LIMIT" --json)" \
  || { echo "cannot list jobs" >&2; exit 1; }

# The successful evaluations, OLDEST FIRST so the table reads as a history.
EVAL_IDS="$(printf '%s' "$JOBS" | python3 -c '
import json, sys
jobs = json.load(sys.stdin).get("jobs") or []
print("\n".join(reversed([j["run_id"] for j in jobs
                          if j.get("kind") == "evaluate"
                          and j.get("state") == "SUCCEEDED"])))
')" || { echo "cannot read the job list" >&2; exit 1; }

# One `status` call per evaluation: `list` carries no pins, and an evaluation
# without its scoreboard is the row this whole file exists to avoid printing.
STATUSES=""
while IFS= read -r id; do
  [ -n "$id" ] || continue
  one="$("${QF[@]}" status "$id" --json 2>/dev/null)" || continue
  STATUSES="$STATUSES$one"$'\x1e'
done <<< "$EVAL_IDS"

printf '%s\x1d%s' "$JOBS" "$STATUSES" | python3 "$HERE/results.py" "$@"
