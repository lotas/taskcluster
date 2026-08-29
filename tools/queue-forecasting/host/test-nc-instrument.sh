#!/usr/bin/env bash
# Unit test for nc-suite-phase2.sh's STATE INSTRUMENT.
#
# WHY THIS FILE EXISTS. A run of the suite reported `pass=49 fail=24` on a
# healthy host. Every one of the 24 failures, and at least three of the PASSES,
# came from `state_of` returning the empty string because `qf status` could not
# be reached -- the helper discarded stderr twice and had no way to say "I could
# not ask". The vacuous passes were the dangerous half:
#
#   ok  (exclusion) two heavy jobs are never both RUNNING
#   ok  (budget) a 22g heavy and a 4g light never run concurrently
#
# Both are `while ...; do if [ "$(state_of A)" = RUNNING ] && ...`, and an empty
# string never equals RUNNING. NC8's two most important properties passed having
# observed nothing at all.
#
# The suite itself cannot catch this: it needs a broken dispatcher to exercise
# the failure, and on a healthy host that path never runs. So the instrument is
# extracted and driven against a STUBBED `qf`, where every failure mode can be
# produced on demand.
#
#   ./test-nc-instrument.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUITE="$HERE/nc-suite-phase2.sh"
[ -f "$SUITE" ] || { echo "cannot find $SUITE" >&2; exit 2; }

# Extract the instrument block rather than sourcing the suite, which would run
# main() against the live host.
BLOCK="$(mktemp)"; trap 'rm -f "$BLOCK"' EXIT
# Two ranges: the instrument, and the stand-in nightly helpers further down.
# Sourcing the whole suite would run main() against the live host.
awk '/^# --- THE INSTRUMENT/{f=1} /^# .-c safe.directory=/{f=0} f' "$SUITE" > "$BLOCK"
awk '/^# A stand-in for the nightly:/{f=1} /^# NC8 -- the mutex/{f=0} f' \
  "$SUITE" >> "$BLOCK"
for fn in state_of field_of submit_as wait_state wait_terminal \
          require_state_for never_concurrent terminal_state note_blind \
          is_run_id \
          standin_nightly standin_acquired wait_standin_acquired; do
  grep -q "^$fn()" "$BLOCK" || { echo "extraction missed $fn()" >&2; exit 2; }
done

pass=0; fail=0; declare -a FAILED_NAMES=()
hpass=0; hfail=0
# Verdicts printed BY the code under test are this harness's INPUT, not its
# result. Kept apart so "harness: fail=0" means the instrument behaved.
snap()    { _sp=$pass; _sf=$fail; }
restore() { pass=$_sp; fail=$_sf; }
ok()   { echo "    (suite said) ok    $1"; pass=$((pass+1)); }
bad()  { echo "    (suite said) FAIL  $1"; fail=$((fail+1)); FAILED_NAMES+=("$1"); }
void() { echo "    (suite said) VOID  $1"; fail=$((fail+1)); FAILED_NAMES+=("VOID:$1"); }
HOK()  { echo "ok    $1"; hpass=$((hpass+1)); }
HBAD() { echo "FAIL  $1"; hfail=$((hfail+1)); }

RESEARCH_USER=research
declare -A STATES=()

# STUB. $MODE selects which way `qf status` misbehaves.
as() {
  local cmd="${*:2}"
  case "$cmd" in
    "qf --json status "*)
      case "$MODE" in
        die)     echo "qf: no dispatcher socket at /run/qf-dispatch/client/sock" >&2
                 return 2 ;;
        refused) echo '{"ok": false, "error": "no such run '\''x'\''"}' ;;
        nojob)   echo '{"ok": true, "stall": null}' ;;
        garbage) echo 'Traceback (most recent call last): boom' ;;
        good)    local rid="${cmd##qf --json status }"; rid="${rid%% *}"
                 echo "{\"ok\": true, \"job\": {\"state\": \"${STATES[$rid]:-QUEUED}\"}}" ;;
      esac ;;
    "qf submit "*)
      if [ "${SUBMIT_OK:-1}" = 1 ]; then echo "test-20260827T000000Z-abc-1"
      else echo "qf: submit refused: bad sha" >&2; return 2; fi ;;
    "qf cancel "*) : ;;
    "qf status "*)
      # argparse's actual behaviour for the trailing form, reproduced so that a
      # caller regressing to `qf status <rid> --json` fails here rather than
      # returning an empty state on a live host.
      echo "usage: qf [-h] [--json] {ping,submit,status,list,cancel,verify-chain,trusted-paths,logs} ..." >&2
      echo "qf: error: unrecognized arguments: --json" >&2
      return 2 ;;
  esac
}

# shellcheck source=/dev/null
. "$BLOCK"

echo "== a failure to ASK is distinguishable from an ANSWER =="
for m in die refused nojob garbage; do
  MODE=$m; : > "$BLIND_FILE"
  got="$(state_of somerun)"; reason="$(cat "$BLIND_FILE")"
  if [ "$got" = UNREADABLE ] && [ -n "$reason" ]; then
    HOK "$m -> UNREADABLE, reason recorded: $reason"
  else
    HBAD "$m -> got '$got' with reason '$reason' (the pass=49 defect)"
  fi
done

MODE=good; : > "$BLIND_FILE"; STATES[r1]=RUNNING
got="$(state_of r1)"
if [ "$got" = RUNNING ] && [ "$(blind_count)" = 0 ]; then
  HOK "a healthy answer reads through and records no blindness"
else
  HBAD "healthy answer -> '$got', blind=$(blind_count)"
fi

echo "== a failed submit says why =="
SUBMIT_OK=0
err="$(submit_as research --kind test --sha deadbeef 2>&1 >/dev/null)"
case "$err" in
  *"submit refused"*) HOK "submit failure prints the reason" ;;
  *) HBAD "submit failure was silent: '$err' -- 'no run id' x9 cost an afternoon" ;;
esac
SUBMIT_OK=1

echo "== a negative property is not provable by an observer that sees nothing =="
MODE=die; : > "$BLIND_FILE"
snap
never_concurrent "(exclusion) never both RUNNING" a b 10 "OVERLAP"
[ "$pass" -eq "$_sp" ] && HOK "a blind observer does NOT pass the exclusion clause" \
  || HBAD "a blind observer PASSED the exclusion clause (the original defect)"
restore

MODE=good; : > "$BLIND_FILE"; STATES[a]=QUEUED; STATES[b]=QUEUED
snap
never_concurrent "(exclusion) never both RUNNING" a b 4 "OVERLAP"
[ "$pass" -eq "$_sp" ] && HOK "jobs never seen RUNNING -> VOID, not a pass" \
  || HBAD "jobs that never ran PASSED the exclusion clause"
restore

echo "== and it still passes when the property genuinely holds =="
# The counter is a FILE: the first version of this stub used a shell variable and
# every increment vanished, because state_of runs inside $(...). That is the same
# trap BLIND_FILE exists to avoid in the code under test.
SEQ="$(mktemp)"; echo 0 > "$SEQ"
state_of_orig="$(declare -f state_of)"
state_of() {
  local n; n=$(( $(cat "$SEQ") + 1 )); echo "$n" > "$SEQ"
  if [ "$n" -le 2 ]; then [ "$1" = a ] && echo RUNNING || echo QUEUED
  else [ "$1" = b ] && echo RUNNING || echo SUCCEEDED; fi
}
snap
never_concurrent "(exclusion) never both RUNNING" a b 20 "OVERLAP"
[ "$pass" -gt "$_sp" ] && HOK "two serialised jobs, each seen RUNNING -> pass" \
  || HBAD "serialised jobs did not pass"
restore

state_of() { echo RUNNING; }
snap
never_concurrent "(exclusion) never both RUNNING" a b 20 "OVERLAP DETECTED"
if [ "${FAILED_NAMES[-1]}" = "OVERLAP DETECTED" ]; then
  HOK "a real overlap -> FAIL, with the caller's message"
else
  HBAD "overlap -> '${FAILED_NAMES[-1]}'"
fi
restore
eval "$state_of_orig"
rm -f "$SEQ"

# =========================================================================
# THE STAND-IN NIGHTLY. Real flock, real background processes, no dispatcher.
# =========================================================================
#
# The original was `( exec 9>"$LOCK"; flock -w "$1" 9 && sleep 60 ) & echo $!`,
# read as `sp="$(standin_nightly 300)"`, and it had two bugs feeding four FAILs:
#
#   1. Command substitution reads its pipe to EOF and the backgrounded subshell
#      inherits that pipe as stdout, so `$(...)` blocked until the stand-in had
#      waited for the lock, slept 60s and exited -- measured at 67s in a local
#      reproduction. By the time `sp` was assigned the process was gone.
#   2. It was a GRANDCHILD, so `wait "$sp"` returned 127 ("not a child of this
#      shell") without waiting, printing "it never acquired the lock".
#
# Whether clause (a) then said "waits rather than exiting" or "exited instead of
# waiting" was a RACE: at that moment the process is a zombie, and `kill -0`
# succeeds for a zombie until init reaps it. Two hosts disagreed for that reason.
echo "== the stand-in nightly, against real flock =="
SLOCK="$(mktemp)"
LOCK="$SLOCK"          # the helpers read $LOCK

( exec 8>"$SLOCK"; flock -s 8; sleep 12 ) &   # a shared holder, like a light job
holder=$!
sleep 1
t0=$(date +%s); standin_nightly 60 3; t1=$(date +%s)
[ $((t1 - t0)) -le 1 ]   && HOK "standin_nightly returns immediately ($((t1 - t0))s)"   || HBAD "standin_nightly blocked for $((t1 - t0))s -- the substitution bug is back"

kill -0 "$STANDIN_PID" 2>/dev/null   && HOK "it is a direct child, visible as pid $STANDIN_PID"   || HBAD "the stand-in is not visible to kill -0"

sleep 3
if kill -0 "$STANDIN_PID" 2>/dev/null && ! standin_acquired; then
  HOK "while a shared holder exists it WAITS and has not acquired"
else
  HBAD "it did not wait behind the shared holder"
fi

wait "$holder" 2>/dev/null
if wait_standin_acquired 60; then
  HOK "it acquires once the holder drains, and the marker records when"
else
  HBAD "it never acquired after the holder drained"
fi
if wait "$STANDIN_PID" 2>/dev/null; then
  HOK "wait reaps it (a grandchild would return 127 immediately)"
else
  HBAD "wait failed -- it is not a child of this shell"
fi
rm -f "$STANDIN_ACQUIRED"

( exec 8>"$SLOCK"; flock -x 8; sleep 20 ) &   # EXCLUSIVE: it must never get in
blocker=$!
sleep 1
standin_nightly 3 2
if wait_standin_acquired 12; then
  HBAD "it claimed acquisition through an exclusive holder"
else
  HOK "a flock timeout reports 'never acquired', not success"
fi
kill "$blocker" 2>/dev/null; wait "$blocker" 2>/dev/null
rm -f "$STANDIN_ACQUIRED" "$SLOCK"

# --- is_run_id: a SHAPE check, never "does its directory exist" -----------
#
# The guard it replaced was `[ -z "$rid" ] || ! [ -d "$RUNS_DIR/$rid" ]`, run
# immediately after submit. A submitted job is QUEUED and has no run directory
# until `prepare_run_dir` runs during execute, so that guard fired on every
# healthy submit: NC19's canary voided on a working probe, and the
# unpromoted-baseline clause printed `ok "never became a run"` for a job the
# dispatcher had accepted and was about to start.
for good in "probe-20260829T123756Z-9d54e39271d7-4290" \
            "extract-20260829T000000Z-abcdef012345-1" \
            "test-20260829T235959Z-000000000000-999"; do
  if is_run_id "$good"; then
    HOK "is_run_id accepts $good"
  else
    HBAD "is_run_id rejected a real run id: $good"
  fi
done
for bad in "" "short" "abcdefgh" "qf: error: unrecognized arguments: --baseline" \
           "no dispatcher socket at /run/qf-dispatch/client/sock" \
           "../../etc/passwd" "probe 20260829"; do
  if is_run_id "$bad"; then
    HBAD "is_run_id accepted something that is not a run id: '$bad'"
  else
    HOK "is_run_id rejects '$(printf '%.30s' "$bad")'"
  fi
done

# The property that matters: acceptance must not depend on a directory.
if is_run_id "probe-20260829T123756Z-9d54e39271d7-4290"; then
  HOK "a run id with no directory anywhere is still a run id"
else
  HBAD "is_run_id consulted the filesystem"
fi

echo
echo "harness: pass=$hpass fail=$hfail"
[ "$hfail" -eq 0 ]
