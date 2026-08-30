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
          is_run_id spec_paths_of succeeded_probes \
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
declare -A PATHS=()
declare -A PATHS_LIST=()

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
                 # The SPEC is part of the payload when a test says so: it is
                 # where a probe's experiment path lives, and NC11 (c) reads it
                 # to tell which fixture a probe ran.
                 local spec="{}"
                 if [ -n "${PATHS[$rid]:-}" ]; then
                   # A PROBE's shape: `args.path`, one string. `test` jobs use
                   # `args.paths`, a list -- and reading only the list is what
                   # made NC11 hand its canary a fixture that refuses by design.
                   spec="{\"args\": {\"path\": \"${PATHS[$rid]}\", \"extract\": \"c179c7f5b961\"}}"
                 elif [ -n "${PATHS_LIST[$rid]:-}" ]; then
                   spec="{\"args\": {\"paths\": [\"${PATHS_LIST[$rid]}\"]}}"
                 elif [ "${SPEC_WITHOUT_ARGS:-0}" = 1 ]; then
                   spec="{\"kind\": \"probe\"}"
                 fi
                 local extra=""
                 if [ "${NULL_CLASS:-0}" = 1 ]; then
                   extra=", \"error_class\": null"
                 fi
                 echo "{\"ok\": true, \"job\": {\"state\":" \
                      "\"${STATES[$rid]:-QUEUED}\", \"spec\": $spec$extra}}" ;;
      esac ;;
    "qf list "*)
      # argparse's ACTUAL behaviour reproduced, flag by flag. `list` takes
      # --state and --limit and nothing else, and two NC11 clauses passed
      # `--kind probe` -- so every invocation exited 2 and both clauses voided
      # with a message about their subject being absent. If anything regresses to
      # a flag the client does not have, it fails HERE.
      case "$cmd" in
        *--kind*|*--lane*)
          echo "usage: qf [-h] [--json] {ping,submit,status,list,cancel} ..." >&2
          echo "qf: error: unrecognized arguments: ${cmd#*--}" >&2
          return 2 ;;
      esac
      printf '%s\n' "${LIST_OUTPUT:-}" ;;
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

# A JSON NULL IS AN EMPTY FIELD, at the transport rather than in each caller.
# `qf --json status` renders an unset `error_class` as `null`, and printing
# Python's `None` for it put that word in the middle of every result string a
# clause matched on -- which voided NC11 on a prediction set that HAD been
# scored. Every clause that compares a class by name was unaffected, which is
# exactly why it survived a day of runs.
MODE=good; : > "$BLIND_FILE"; STATES[r-null]=SUCCEEDED; NULL_CLASS=1
got="$(field_of r-null error_class)"
if [ -z "$got" ]; then
  HOK "a null error_class reads back as empty, not as the word None"
else
  HBAD "field_of returned '$got' for a JSON null"
fi
NULL_CLASS=0

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
# EVERY KIND, AND BOTH WAYS THE SHA CAN END. The three fixtures this list used to
# hold all had a 12-hex prefix ending in a DIGIT -- `9d54e39271d7`,
# `abcdef012345`, `000000000000` -- and `is_run_id`'s first clause was
# `*[0-9]-[0-9]*`, which is satisfied by exactly that coincidence. Six commits in
# sixteen end in a letter, and on one of them NC9 (d) reported "a trusted contract
# was refused at submit" for a run id the dispatcher had just minted. The fixtures
# below are the real one from that run plus one per kind, ending in a letter.
for good in "probe-20260829T123756Z-9d54e39271d7-4290" \
            "extract-20260829T000000Z-abcdef012345-1" \
            "test-20260829T235959Z-000000000000-999" \
            "evaluate-20260829T192144Z-f58141c0d68e-4448" \
            "probe-20260829T000000Z-aaaaaaaaaaaa-7" \
            "extract-20260829T000000Z-0123456789ab-12" \
            "test-20260829T000000Z-deadbeefcafe-1" \
            "selftest-20260829T000000Z-fffffffffffe-30"; do
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

echo "== a probe listing is filtered by the run id, not by a flag qf lacks =="
MODE=good; : > "$BLIND_FILE"
LIST_OUTPUT="probe-20260829T000000Z-aaaaaaaaaaaa-1    SUCCEEDED        heavy  2026-08-29T00:00:00Z
test-20260829T000100Z-aaaaaaaaaaaa-2     SUCCEEDED        light  2026-08-29T00:01:00Z
evaluate-20260829T000200Z-aaaaaaaaaaaa-3 SUCCEEDED        light  2026-08-29T00:02:00Z
probe-20260829T000300Z-bbbbbbbbbbbb-4    SUCCEEDED        heavy  2026-08-29T00:03:00Z"
got="$(succeeded_probes | tr '\n' ' ')"
case "$got" in
  "probe-20260829T000000Z-aaaaaaaaaaaa-1 probe-20260829T000300Z-bbbbbbbbbbbb-4 ")
    HOK "succeeded_probes returns the probes and only the probes" ;;
  *) HBAD "succeeded_probes returned '$got'" ;;
esac

# THE REGRESSION GUARD. The stub refuses a flag the real client refuses, so a
# clause that reintroduces `--kind` reads nothing here instead of on a host.
if as research "qf list --state SUCCEEDED --kind probe --limit 200" >/dev/null 2>&1; then
  HBAD "the stub accepted --kind, so this harness cannot catch that regression"
else
  HOK "a listing flag the client does not have fails loudly"
fi
if grep -q -- "qf list[^\"']*--kind" "$SUITE"; then
  HBAD "the suite still passes --kind to qf list somewhere"
else
  HOK "no clause passes --kind to qf list"
fi

echo "== a probe's own spec is where its experiment path is read from =="
# BOTH SHAPES. A probe's spec carries `args.path` (a string); a test job carries
# `args.paths` (a list). The first version of this helper read only the list, so
# it returned nothing for every probe -- and NC11's subject discovery, which uses
# it to skip the fixtures that refuse by design, skipped nothing and voided the
# group on the wrong probe.
PATHS[probe-x]="research/experiments/nc11_honest.py"
STATES[probe-x]=SUCCEEDED
got="$(spec_paths_of probe-x)"
if [ "$got" = "research/experiments/nc11_honest.py" ]; then
  HOK "spec_paths_of reads a PROBE's args.path (a string)"
else
  HBAD "spec_paths_of returned '$got' for a probe"
fi
# And the hash beside it is not mistaken for a path.
case "$got" in
  *c179c7f5b961*) HBAD "spec_paths_of returned args.extract as a path" ;;
  *) HOK "a hash in args is not read as a path" ;;
esac
PATHS_LIST[test-x]="test/test_thing.py"
STATES[test-x]=SUCCEEDED
got="$(spec_paths_of test-x)"
if [ "$got" = "test/test_thing.py" ]; then
  HOK "and a TEST job's args.paths (a list)"
else
  HBAD "spec_paths_of returned '$got' for a test job"
fi

SPEC_WITHOUT_ARGS=1
got="$(spec_paths_of probe-y)"
if [ -z "$got" ]; then
  HOK "a spec with no args yields no path rather than an error"
else
  HBAD "spec_paths_of invented a path: '$got'"
fi
SPEC_WITHOUT_ARGS=0

MODE=die; : > "$BLIND_FILE"
got="$(spec_paths_of probe-x)"; rc=$?
if [ "$rc" -ne 0 ] && [ -n "$(cat "$BLIND_FILE")" ]; then
  HOK "a failure to ASK for a spec is recorded as blindness, not an empty path"
else
  HBAD "spec_paths_of rc=$rc blind='$(cat "$BLIND_FILE")' -- the pass=49 defect"
fi
MODE=good

# ONE SED PER FUNCTION. A single script with several ranges prints any line that
# two ranges share TWICE -- `sed` applies each `p` independently -- so the sourced
# text came back with duplicated lines inside a function body, and `declare -F`
# said the function existed while its body was garbage. The tests below then
# failed on the code under test for a defect in how they had loaded it.
for _fn in nc11_restore nc11_outcome_line nc11_scored nc11_refused_as \
           nc11_verdict_of qfd_pythonpath; do
  # shellcheck disable=SC1090
  source <(sed -n "/^$_fn()/,/^}/p" "$SUITE")
  declare -F "$_fn" >/dev/null || HBAD "extraction missed $_fn()"
done

echo "== a probe of the daemon imports what the daemon imports =="
# WHY: `qfd.py` imports `baseline` and `contract` at module scope, and both live
# in `host/shared/`, which the SERVICE gets from `Environment=PYTHONPATH=`. NC16
# probed `qfd.Docker().is_running` with only the dispatcher directory on the
# path, so after `baseline.py` moved to `shared/` in 2c-2 the import failed, the
# clause's `2>/dev/null` ate the ModuleNotFoundError, and an empty answer was
# reported as "Docker cannot confirm an absence" -- a far more alarming claim
# than the truth. So the path comes from the unit, and this checks that it does.
UTMP="$(mktemp -d)"
TRUSTED="$UTMP/trusted"; DISPATCHER="$UTMP/trusted/tools/queue-forecasting/host/dispatcher"
cat > "$UTMP/unit" <<'UNIT'
[Service]
Environment=PYTHONPATH=/srv/queue-forecasting/tools/queue-forecasting/host/shared
UNIT
got="$(qfd_pythonpath "$UTMP/unit")"
case "$got" in
  "$DISPATCHER:/srv/queue-forecasting/tools/queue-forecasting/host/shared")
    HOK "the probe's path is the unit's PYTHONPATH plus the dispatcher directory" ;;
  *) HBAD "qfd_pythonpath returned '$got'" ;;
esac
# NO UNIT INSTALLED: fall back to the checkout, rather than to a path that
# imports nothing.
got="$(qfd_pythonpath "$UTMP/absent-unit")"
case "$got" in
  *"/host/shared") HOK "with no unit it falls back to the checkout's shared/" ;;
  *) HBAD "qfd_pythonpath returned '$got' with no unit" ;;
esac
# A UNIT WITH NO PYTHONPATH -- the 2b-1 state -- must not yield a trailing colon,
# which python reads as the current directory.
printf '[Service]\nUser=qfd\n' > "$UTMP/bare"
got="$(qfd_pythonpath "$UTMP/bare")"
case "$got" in
  *:) HBAD "qfd_pythonpath ended in a colon, so '.' is on the path" ;;
  *"/host/shared") HOK "a unit with no PYTHONPATH falls back the same way" ;;
  *) HBAD "qfd_pythonpath returned '$got'" ;;
esac
rm -rf "$UTMP"
unset TRUSTED DISPATCHER

echo "== NC11 reads an outcome the way the dispatcher writes one =="
# THE STRINGS ARE THE ONES THE HOST PRODUCED, verbatim. Two live rounds were
# spent on this: `SUCCEEDED None no-go` -- a prediction set that WAS scored, with
# a null error_class rendered as Python's `None` -- matched no pattern, so the
# clause reported that no verdict had been produced and voided the group. The
# transport is fixed (a JSON null prints as empty now) and the vocabulary is
# normalised on top of it, so both spellings mean the same thing here.
if declare -F nc11_outcome_line >/dev/null && declare -F nc11_scored >/dev/null; then
  for raw in "SUCCEEDED::no-go" "SUCCEEDED:None:no-go" "SUCCEEDED:null:no-go" \
             "SUCCEEDED::go"; do
    IFS=: read -r st kl vd <<<"$raw"
    line="$(nc11_outcome_line "$st" "$kl" "$vd")"
    if nc11_scored "$line"; then
      HOK "'$raw' -> '$line' reads as scored"
    else
      HBAD "'$raw' -> '$line' did NOT read as scored (the void that cost two rounds)"
    fi
  done
  # And the verdict is recoverable from the line, because (b2) compares against it.
  line="$(nc11_outcome_line SUCCEEDED None no-go)"
  [ "$(nc11_verdict_of "$line")" = "no-go" ] \
    && HOK "the verdict is the last field of the line" \
    || HBAD "nc11_verdict_of returned '$(nc11_verdict_of "$line")'"

  # A REFUSAL IS NOT A SCORE, and a refusal that also carried a verdict is the
  # worse of the two failures -- so `nc11_refused_as` requires no verdict.
  for raw in "FAILED:row_set_rejected:" "FAILED:evaluate_input_missing:" \
             "FAILED:eval_staging_denied:" "FAILED:evaluator_internal:"; do
    IFS=: read -r st kl vd <<<"$raw"
    line="$(nc11_outcome_line "$st" "$kl" "$vd")"
    if nc11_scored "$line"; then
      HBAD "'$line' read as scored"
    elif nc11_refused_as "$line" "$kl"; then
      HOK "'$line' reads as refused with class $kl"
    else
      HBAD "'$line' did not read as refused with $kl"
    fi
  done
  line="$(nc11_outcome_line FAILED row_set_rejected go)"
  if nc11_refused_as "$line" row_set_rejected; then
    HBAD "a refusal carrying a verdict was accepted as a clean refusal"
  else
    HOK "a refusal that also recorded a verdict is not accepted as one"
  fi
  # The classes must not be interchangeable: (b1) and (c) assert different ones.
  line="$(nc11_outcome_line FAILED evaluate_input_missing "")"
  nc11_refused_as "$line" row_set_rejected \
    && HBAD "one refusal class matched another" \
    || HOK "a refusal class is not matched by a different one"
  # NOTSUBMITTED, which `_nc11_eval_of` emits when the submit never returned an id.
  line="$(nc11_outcome_line NOTSUBMITTED "qf:error" "")"
  nc11_scored "$line" && HBAD "a failed submit read as scored" \
    || HOK "a failed submit is neither scored nor a refusal"
else
  HBAD "extraction missed NC11's outcome predicates"
fi

echo "== the NC11 restore fires once and is a no-op the second time =="
# WHY THIS IS A UNIT TEST. A RETURN trap set inside a function is not scoped to
# it: it fires when the clause returns and AGAIN when its caller returns. The
# first version of NC11's cleanup was an inline command list ending in `rm -rf
# "$scratch"`, so the second firing printed
#   cp: cannot stat '/tmp/tmp.XXXX/artifact.parquet': No such file or directory
# after the suite's totals -- a restore that HAD happened, reported as a failure
# of the code that cleans up after failures. On a host, with a mutated artifact
# in play, "did the restore run?" is not a question to answer by reading.
if declare -F nc11_restore >/dev/null; then
  RTMP="$(mktemp -d)"
  mkdir -p "$RTMP/scratch" "$RTMP/run"
  printf 'original\n' > "$RTMP/scratch/artifact.parquet"
  printf 'original\n' > "$RTMP/scratch/out.parquet"
  printf 'MUTATED\n'  > "$RTMP/run/artifact"
  printf 'MUTATED\n'  > "$RTMP/run/out"
  err="$(nc11_restore "$RTMP/scratch" "$RTMP/run/artifact" "$RTMP/run/out" 2>&1)"
  if [ "$(cat "$RTMP/run/artifact")" = original ] \
     && [ "$(cat "$RTMP/run/out")" = original ]; then
    HOK "nc11_restore puts both files back"
  else
    HBAD "nc11_restore did not restore: artifact='$(cat "$RTMP/run/artifact")' out='$(cat "$RTMP/run/out")'"
  fi
  [ -z "$err" ] && HOK "and says nothing on the happy path" \
    || HBAD "nc11_restore printed: $err"
  [ ! -d "$RTMP/scratch" ] && HOK "and removes its scratch directory" \
    || HBAD "the scratch directory survived"
  # THE SECOND FIRING. Silent, and it must not report a failure for work that
  # has already been done.
  err="$(nc11_restore "$RTMP/scratch" "$RTMP/run/artifact" "$RTMP/run/out" 2>&1)"
  if [ -z "$err" ]; then
    HOK "a second firing is silent rather than an error about a missing copy"
  else
    HBAD "the second firing printed: $err"
  fi
  rm -rf "$RTMP"
else
  HBAD "extraction missed nc11_restore()"
fi

echo "== every control group defined is a group that RUNS =="
# THREE hand-written lists have to agree: the `ncN()` functions, the default set
# `main` iterates, and the `case` that validates a name from the command line. A
# group defined but not listed is a control that exists and never executes --
# indistinguishable, in an evidence file, from one that passed. A group listed but
# not accepted by the validator cannot be asked for by name.
#
# THE FIRST VERSION OF THIS CLAUSE PASSED WHEN IT SHOULD NOT HAVE, and the reason
# is worth keeping: it read the default list with `sed -n '/groups=(nc8/,/)/p'`,
# and in sed a range whose end pattern matches the START line does not close
# there -- it runs on to the NEXT line containing `)`, which is the validating
# `case` a few lines below. So removing nc11 from the default list still showed
# nc11, because the clause was reading the other list. A check that quietly reads
# a different subject is the failure this whole file exists for.
defined="$(grep -oE '^nc[0-9]+\(\)' "$SUITE" | tr -d '()' | sort -u)"
listed="$(grep -oE 'groups=\(nc8[^)]*\)' "$SUITE" | grep -oE 'nc[0-9]+' | sort -u)"
accepted="$(grep -oE '^ *nc8\|nc9\|[a-z0-9|]*\)' "$SUITE" | grep -oE 'nc[0-9]+' \
  | sort -u)"
if [ -z "$defined" ] || [ -z "$listed" ] || [ -z "$accepted" ]; then
  HBAD "could not read one of the three lists: defined='$(echo $defined)' listed='$(echo $listed)' accepted='$(echo $accepted)'"
else
  n="$(printf '%s\n' "$defined" | grep -c .)"
  if [ "$defined" = "$listed" ]; then
    HOK "all $n defined groups are in the default set that runs"
  else
    HBAD "defined vs default set: $(comm -3 <(printf '%s\n' "$defined") <(printf '%s\n' "$listed") | tr '\n' ' ')"
  fi
  if [ "$defined" = "$accepted" ]; then
    HOK "all $n defined groups can be asked for by name"
  else
    HBAD "defined vs accepted names: $(comm -3 <(printf '%s\n' "$defined") <(printf '%s\n' "$accepted") | tr '\n' ' ')"
  fi
fi

if grep -q "unknown group" "$SUITE"; then
  HOK "an unknown group name is refused rather than silently running nothing"
else
  HBAD "a mistyped group name would run no controls and report pass=0 fail=0"
fi

if grep -q "PARTIAL RUN" "$SUITE"; then
  HOK "a partial run labels itself in the totals and the evidence file"
else
  HBAD "a partial run would write totals that read like a full clean run"
fi

echo
echo "harness: pass=$hpass fail=$hfail"
[ "$hfail" -eq 0 ]
