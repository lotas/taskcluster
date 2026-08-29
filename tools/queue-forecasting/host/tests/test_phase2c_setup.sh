#!/usr/bin/env bash
# Tests the two decidable functions in `phase2c-setup.sh`, and the agreement
# between the script's constants and the checked-in tmpfiles config.
#
#   ./tests/test_phase2c_setup.sh
#
# WHY THESE TWO. Most of an install script can only be judged on the host it
# installs to. These two cannot: `eval_dir_problem` decides whether the staging
# root is usable from three strings, and `forbidden_groups` READS the gate's own
# tuple instead of repeating it. Both are exactly the shape that fails silently
# -- one by accepting a directory that breaks the handover in a way nothing else
# reports, the other by parsing to nothing and looping over no groups while
# printing nothing at all. So both are tested without root, off the host.
#
# The extraction follows `test_unit_drift.sh`: sourcing the script itself would
# run `main`, and a copy of the function under test is not the function under
# test.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETUP="$HERE/../phase2c-setup.sh"
[ -f "$SETUP" ] || { echo "cannot find $SETUP" >&2; exit 2; }

# shellcheck disable=SC1090
source <(sed -n '/^EVAL_DIR_MODE=/,/^EVAL_DIR_GROUP=/p;
                 /^die()/p;
                 /^eval_dir_problem()/,/^}/p' "$SETUP")
declare -F eval_dir_problem >/dev/null \
  || { echo "extraction missed eval_dir_problem" >&2; exit 2; }
[ -n "${EVAL_DIR_MODE:-}" ] \
  || { echo "extraction missed the EVAL_DIR_* constants" >&2; exit 2; }

pass=0; fail=0
ok()  { echo "ok    $1"; pass=$((pass + 1)); }
bad() { echo "FAIL  $1"; fail=$((fail + 1)); }

expect_clean() {  # expect_clean <mode> <owner> <group> <label>
  local out; out="$(eval_dir_problem "$1" "$2" "$3")"
  if [ -z "$out" ]; then ok "$4"; else bad "$4 (said: $out)"; fi
}

expect_problem() {  # expect_problem <mode> <owner> <group> <needle> <label>
  local out; out="$(eval_dir_problem "$1" "$2" "$3")"
  if [ -z "$out" ]; then
    bad "$5 (accepted it)"
  elif printf '%s' "$out" | grep -qi -- "$4"; then
    ok "$5"
  else
    bad "$5 (wrong reason: $out)"
  fi
}

expect_clean 2770 qfd qfeval "the required state is accepted"

# THE ONE THAT MATTERS. 0770 qfd:qfeval passes every check somebody would think
# to make by eye -- the dispatcher can create the inbox, the evaluator can
# traverse it -- and the staged predictions.parquet inside it then belongs to
# group qfd, so the single file the directory exists to hand over is the one
# thing the evaluator cannot read.
expect_problem 770 qfd qfeval "setgid" "a missing setgid bit is named as such"
expect_problem 0770 qfd qfeval "setgid" "a zero-padded 0770 is caught too"

expect_problem 2750 qfd qfeval "mode is 2750" "a mode with no group write is refused"
expect_problem 2777 qfd qfeval "mode is 2777" "a world-writable staging root is refused"
expect_problem 2770 root qfeval "owner" "the wrong owner is refused, naming the writer"
expect_problem 2770 qfd qfd "group" "the wrong group is refused, naming the reader"
expect_problem 2770 qfeval qfeval "owner" "StateDirectory's ownership is refused"

# THE CONSTANTS AND THE CHECKED-IN CONFIG. The script provisions the directory by
# handing this file to systemd-tmpfiles and then verifies the result against its
# own constants -- so if the two disagree, install fails on a correct host and
# the message blames the host.
CONF="$HERE/../evaluator/qf-eval.conf"
if [ -f "$CONF" ]; then
  line="$(grep -v '^#' "$CONF" | grep -E '^d[[:space:]]+/var/lib/qf-eval' \
    | head -1)"
  # shellcheck disable=SC2086
  set -- $line
  if [ "${3:-}" = "$EVAL_DIR_MODE" ] && [ "${4:-}" = "$EVAL_DIR_OWNER" ] \
     && [ "${5:-}" = "$EVAL_DIR_GROUP" ]; then
    ok "qf-eval.conf provisions exactly what the script requires ($3 $4:$5)"
  else
    bad "qf-eval.conf says '${3:-} ${4:-} ${5:-}' and the script requires
     $EVAL_DIR_MODE $EVAL_DIR_OWNER $EVAL_DIR_GROUP"
  fi
else
  bad "$CONF is missing: nothing provisions the staging root"
fi

# --- forbidden_groups, in a subshell because it dies ------------------------
# Run as a fresh bash so `die`'s `exit 2` is observable rather than fatal here.
run_forbidden() {  # run_forbidden <dir holding a service.py>
  EVALUATOR="$1" bash -c '
    set -Eeuo pipefail
    source <(sed -n "/^die()/p; /^forbidden_groups()/,/^}/p" "$0")
    forbidden_groups
  ' "$SETUP" 2>&1
}

real="$(run_forbidden "$HERE/../evaluator")"
if [ -z "$real" ]; then
  bad "forbidden_groups read nothing from the real service.py"
else
  # AGAINST THE SOURCE, not against a list written here. A literal copy in this
  # test would drift with the same silence the script avoids.
  want="$(python3 - "$HERE/../evaluator/service.py" <<'PY'
import ast, sys
tree = ast.parse(open(sys.argv[1]).read())
for node in tree.body:
    if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "FORBIDDEN_GROUPS" for t in node.targets):
        print(" ".join(ast.literal_eval(node.value)))
PY
)"
  if [ "$real" = "$want" ]; then
    ok "forbidden_groups is the gate's own tuple ($real)"
  else
    bad "forbidden_groups said '$real', the gate says '$want'"
  fi
  case "$real" in
    *qfclient*) ok "qfclient is in it -- the access D28 exists to avoid needing" ;;
    *) bad "qfclient is not in the forbidden list" ;;
  esac
fi

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

printf 'FORBIDDEN_GROUPS = ()\n' > "$TMP/service.py"
out="$(run_forbidden "$TMP")"
case "$out" in
  *empty*) ok "an empty tuple is a refusal, not a loop over nothing" ;;
  *) bad "an empty FORBIDDEN_GROUPS did not refuse (said: $out)" ;;
esac

printf 'X = 1\n' > "$TMP/service.py"
out="$(run_forbidden "$TMP")"
case "$out" in
  *"no FORBIDDEN_GROUPS"*) ok "a service with no such tuple is a refusal" ;;
  *) bad "a missing FORBIDDEN_GROUPS did not refuse (said: $out)" ;;
esac

printf 'FORBIDDEN_GROUPS = (\n' > "$TMP/service.py"
out="$(run_forbidden "$TMP")"
if [ -n "$out" ] && ! printf '%s' "$out" | grep -q '^docker'; then
  ok "a service.py that does not parse is a refusal"
else
  bad "an unparseable service.py did not refuse (said: $out)"
fi

# --- the drift comparison actually runs -------------------------------------
# WHY THIS IS TESTED AT ALL. `discover` compares each installed unit against the
# checkout with `unit_matches`, extracted from phase2-setup.sh rather than
# copied. If the extraction silently produced nothing there would be no
# comparison to make -- and the version of that loop this test was written
# against printed "installed and matches the checkout" in exactly that case,
# which is the claim `test_unit_drift.sh` exists because somebody once believed.
#
# DRIVEN THROUGH THE REAL SCRIPT, in place. `BASH_SOURCE[0]` inside a function
# names the file the function was DEFINED in, so sourcing the loader out of a
# process substitution would make it look for phase2-setup.sh in /dev/fd -- a
# test harness failing for a reason the host never would.
UNITS_TMP="$(mktemp -d)"; trap 'rm -rf "$TMP" "$UNITS_TMP"' EXIT
for unit in qf-eval.socket qf-eval.service; do
  sed 's/%%QFD_UID%%/4242/g' "$HERE/../evaluator/$unit" > "$UNITS_TMP/$unit"
done
drift_run() {
  TRUSTED="$(cd "$HERE/../../../.." && pwd)" UNIT_DIR="$UNITS_TMP" \
    EVAL_DIR="$UNITS_TMP/nonexistent" \
    bash "$HERE/../phase2c-setup.sh" discover 2>&1
}

out="$(drift_run)"
if printf '%s' "$out" | grep -q "NOT COMPARED"; then
  bad "the drift comparison did not load, so nothing compares the units"
elif printf '%s' "$out" | grep -q "qf-eval.service installed and matches"; then
  ok "an installed unit is compared against the checkout, substitution aside"
else
  bad "discover said neither: $(printf '%s' "$out" | grep -i 'qf-eval.service')"
fi

printf 'Environment=QFE_EVAL_DIR=/somewhere/else\n' >> "$UNITS_TMP/qf-eval.service"
out="$(drift_run)"
if printf '%s' "$out" | grep -q "differs from the checkout"; then
  ok "an edited installed unit is reported as drift"
else
  bad "an edited unit was not reported as drift"
fi

echo
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
