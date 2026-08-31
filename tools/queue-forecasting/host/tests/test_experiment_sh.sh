#!/usr/bin/env bash
# `experiment.sh` picks the code another identity can actually execute.
#
# WHY THIS IS A SUITE. Both bugs this script has had were path-and-identity
# assumptions that looked fine from the operator's shell: running
# `$HERE/experiment.py` (unreadable by the research user, because a home
# directory is not traversable by another account) and testing `-r` as the
# CALLER rather than as the identity that runs the file. Neither is visible
# without actually resolving the path as somebody else, so `sudo` is stubbed
# here and the resolution is asserted.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../experiment.sh"
PASS=0 FAIL=0

check() {  # check <label> <expected-substring> <actual>
  if [[ "$3" == *"$2"* ]]; then PASS=$((PASS+1)); echo "  ok   $1"
  else FAIL=$((FAIL+1)); echo "  FAIL $1"; echo "       want: $2"
       echo "       got:  ${3//$'\n'/ | }"; fi
}
refute() {
  if [[ "$3" != *"$2"* ]]; then PASS=$((PASS+1)); echo "  ok   $1"
  else FAIL=$((FAIL+1)); echo "  FAIL $1 (unexpectedly contains: $2)"; fi
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# A `sudo` that drops `-H -u <user>` and runs the rest as whoever we are. The
# point is not to test sudo -- it is that the file the script chooses is the
# one that gets executed, and by whom is exactly what the real sudo decides.
mkdir -p "$WORK/bin"
cat > "$WORK/bin/sudo" <<'STUB'
#!/usr/bin/env bash
while [ $# -gt 0 ]; do
  case "$1" in
    -H) shift ;;
    -u) shift 2 ;;
    *) break ;;
  esac
done
exec "$@"
STUB
chmod +x "$WORK/bin/sudo"
export PATH="$WORK/bin:$PATH"

# A trusted mirror, and an operator checkout whose copy DIFFERS from it.
mkdir -p "$WORK/srv/host" "$WORK/checkout"
printf 'import sys\nprint("TRUSTED", *sys.argv[1:])\n' > "$WORK/srv/host/experiment.py"
cp "$SCRIPT" "$WORK/checkout/experiment.sh"
printf 'import sys\nprint("LOCAL EDIT", *sys.argv[1:])\n' > "$WORK/checkout/experiment.py"

echo "== it runs the trusted copy, not the one beside it"
out="$(QF_TRUSTED_HOST="$WORK/srv/host" "$WORK/checkout/experiment.sh" doctor 2>&1)"
check "executes the deployed file" "TRUSTED doctor" "$out"
refute "does not execute the local edit" "LOCAL EDIT" "$out"

echo "== it says the local copy differs"
check "warns about undeployed edits" "differs from" "$out"
check "names the local path in the warning" "$WORK/checkout/experiment.py" "$out"

echo "== identical copies produce no note"
cp "$WORK/srv/host/experiment.py" "$WORK/checkout/experiment.py"
out="$(QF_TRUSTED_HOST="$WORK/srv/host" "$WORK/checkout/experiment.sh" plan x 2>&1)"
check "still runs the trusted copy" "TRUSTED plan x" "$out"
refute "no spurious drift note" "differs from" "$out"

echo "== arguments reach the program unchanged"
out="$(QF_TRUSTED_HOST="$WORK/srv/host" "$WORK/checkout/experiment.sh" \
        run configs/a.yaml --note "two words" 2>&1)"
check "argv is preserved, including a quoted note" \
  "TRUSTED run configs/a.yaml --note two words" "$out"

echo "== an unreadable trusted copy refuses with the deploy command"
out="$(QF_TRUSTED_HOST="$WORK/srv/nothing" "$WORK/checkout/experiment.sh" doctor 2>&1)"
code=$?
check "names the missing file" "cannot read" "$out"
check "names mirror-refresh as the fix" "mirror-refresh" "$out"
check "offers the override" "QF_TRUSTED_HOST" "$out"
[ "$code" != "0" ] && { PASS=$((PASS+1)); echo "  ok   exits nonzero"; } \
  || { FAIL=$((FAIL+1)); echo "  FAIL exits nonzero"; }

echo "== the operator's own copy is used when IT is the trusted one"
# The research user running the deployed script: HERE == TRUSTED_HOST, so
# there is nothing to elevate to and nothing to compare against.
cp "$SCRIPT" "$WORK/srv/host/experiment.sh"
out="$(QF_TRUSTED_HOST="$WORK/srv/host" "$WORK/srv/host/experiment.sh" doctor 2>&1)"
check "runs in place" "TRUSTED doctor" "$out"
refute "no drift note against itself" "differs from" "$out"

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
