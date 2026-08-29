#!/usr/bin/env bash
# Negative-control suite, Phase 2a (auto-research-phase2a-plan.md Task 8).
#
# Run as root:  sudo ./host/nc-suite-phase2.sh
#
# Semantics are carried unchanged from nc-suite.sh, and the reason matters more
# here than anywhere else: a refusal is only meaningful if the action was
# POSSIBLE to attempt. Every refusal group is preceded by a canary that must
# SUCCEED, and a canary failure makes the group VOID -- which is a FAILURE, not
# a skip. A vacuous mutex reads exactly like a working one.
#
# Exit 0 = every control fails closed. Exit 1 = at least one is open or void.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=nc7-lib.sh
. "$HERE/nc7-lib.sh" 2>/dev/null || { echo "cannot source nc7-lib.sh" >&2; exit 2; }

TRUSTED="${TRUSTED:-/srv/queue-forecasting}"
DISPATCHER="$TRUSTED/tools/queue-forecasting/host/dispatcher"
RESEARCH_USER="${RESEARCH_USER:-research}"
# THE NIGHTLY IDENTITY, and deliberately NOT `stat -c %U "$TRUSTED"`. The
# trusted checkout is owned by root here, so deriving it that way made
# DEPLOY_USER=root -- and then `crontab -l -u root` held no nightly entry (both
# one-inode clauses VOIDed on a correctly configured host) and
# `refuse_as root "rm -f $LOCK"` SUCCEEDED, because root is exempt from DAC.
# That last one destroyed the mutex inode mid-run and every lock clause after it
# was measuring a host the suite had broken. `phase2-setup.sh` already carries
# this distinction; the suite did not.
#
# Detected from whichever crontab actually schedules the nightly, because that
# is the definition. Undetectable is a hard stop, not a guess: the whole of NC8
# is about the protocol between qfd and that user.
detect_nightly_user() {
  local f u
  for f in /var/spool/cron/crontabs/* /var/spool/cron/*; do
    [ -f "$f" ] || continue
    if grep -q 'daily_walk_forward' "$f" 2>/dev/null; then
      basename "$f"; return 0
    fi
  done
  for f in /etc/cron.d/*; do
    [ -f "$f" ] || continue
    u="$(awk '$1 !~ /^#/ && /daily_walk_forward/ {print $6; exit}' "$f")"
    if [ -n "$u" ]; then printf '%s' "$u"; return 0; fi
  done
  return 1
}
DEPLOY_USER="${DEPLOY_USER:-$(detect_nightly_user || true)}"
LOCK="${QFD_LOCK_FILE:-/var/lib/qf-locks/heavy-training.lock}"
INTENT_DIR="${QFD_INTENT_DIR:-/var/lib/qf-locks/intent.d}"
MIGRATED_MARKER="${QFD_LOCK_MIGRATED_MARKER:-/etc/qf-dispatch/lock-migrated}"
QFADMIN="${QFADMIN:-/usr/local/sbin/qfadmin}"
# Phase 2b-1: the extractor's privilege domain (D15).
DSN_FILE="${DSN_FILE:-/etc/qf-extract/dsn}"
EXTRACT_SOCK="${QFX_SOCKET:-/run/qf-extract/sock}"
QFD_USER="${QFD_USER:-qfd}"
QFEXTRACT_USER="${QFEXTRACT_USER:-qfextract}"
# A real extraction takes ~11 minutes (688s measured for 36 days), so the
# clauses that need one are OPT-IN. Not silently skipped: see nc18.
NC_SLOW="${NC_SLOW:-0}"
CLIENT_SOCK="${QFD_SOCKET:-/run/qf-dispatch/client/sock}"
ADMIN_SOCK="${QFD_ADMIN_SOCKET:-/run/qf-dispatch/admin/sock}"
RUNS_DIR="${QFD_RUNS_DIR:-/var/lib/qf-runs}"
STATE_DIR="${QFD_STATE_DIR:-/var/lib/qf-platform}"
LOG_CAP_MB="${QFD_LOG_CAP_MB:-16}"
HANDOFF_TIMEOUT_S="${QFD_HANDOFF_TIMEOUT_S:-120}"
BUILD_SETTLE_S="${QFD_BUILD_SETTLE_S:-30}"
KILL_CONFIRM_S="${QFD_KILL_CONFIRM_S:-300}"
EVIDENCE="$HERE/nc-evidence-phase2a.txt"
NC12_SHA_FILE="$HERE/nc12-sha.txt"
# The window NC18 uses. A SETTLED, already-published one, so the canary and the
# immutability clauses are reuse hits rather than eleven-minute extractions.
NC18_TRAIN_START="${NC18_TRAIN_START:-2026-07-21T00:00:00Z}"
NC18_AS_OF="${NC18_AS_OF:-2026-08-26T00:00:00Z}"

pass=0
fail=0
declare -a FAILED_NAMES=()

ok()   { echo "ok    $1"; pass=$((pass + 1)); }
bad()  { echo "FAIL  $1"; fail=$((fail + 1)); FAILED_NAMES+=("$1"); }
void() { echo "VOID  $1  (canary failed - refusals in this group prove nothing)";
         fail=$((fail + 1)); FAILED_NAMES+=("VOID:$1"); }

# Note the invocation: `sudo -H -u <user> bash -lc`, NOT `sudo -i`. With -i, sudo
# re-parses the command string, so a mangled command fails for the wrong reason
# and `refuse` reads that as a pass.
as() { sudo -H -u "$1" bash -lc "${*:2}"; }

# Both of these keep the command's output and print it on the UNEXPECTED
# outcome only. Every VOID in one full run of this suite arrived with no reason
# attached -- "canary: the attempt is possible" is a fine thing to print when a
# canary works, but "VOID (g4) deploy reaches the admin socket" with the reason
# thrown away is a line an operator can do nothing with.
refuse_as() {  # refuse_as <user> <name> <command...> -> passes when it FAILS
  local user="$1" name="$2"; shift 2
  # rc captured explicitly: `local out="$(cmd)"` would make $? the status of
  # `local` (always 0), which turns every refusal clause into a PERMITTED.
  local out rc
  out="$(as "$user" "$*" 2>&1)"; rc=$?
  if [ "$rc" -eq 0 ]; then
    bad "$name  (action was PERMITTED)"
    [ -n "$out" ] && printf '        it said: %s\n' \
      "$(printf '%s' "$out" | tr '\n' ' ' | cut -c1-160)"
  else
    ok "$name  (refused)"
  fi
}

canary_as() {  # canary_as <user> <name> <command...> -> passes when it SUCCEEDS
  local user="$1" name="$2"; shift 2
  local out rc
  out="$(as "$user" "$*" 2>&1)"; rc=$?
  if [ "$rc" -eq 0 ]; then
    ok "$name  (canary: the attempt is possible)"
  else
    void "$name  (rc=$rc: $(printf '%s' "$out" | tr '\n' ' ' | cut -c1-160))"
  fi
}

exists() {     # checked AS ROOT: the target is really there, so the refusal means something
  local name="$1" path="$2"
  if [ -e "$path" ]; then ok "$name  (target present)"; else void "$name  (target $path absent)"; fi
}

score() {      # score <name> <verdict-from-nc7-lib>
  case "$2" in
    refused) ok   "$1  (refused)" ;;
    fail)    bad  "$1  (action was PERMITTED)" ;;
    *)       void "$1  (inconclusive: $2)" ;;
  esac
}

assert_eq() {
  local name="$1" want="$2" got="$3"
  if [ "$want" = "$got" ]; then ok "$name"; else bad "$name  (want '$want', got '$got')"; fi
}

# --- dispatcher helpers ---------------------------------------------------
qf_as() { as "$1" "qf ${*:2}"; }

# --- THE INSTRUMENT -------------------------------------------------------
#
# WHY THIS IS THE MOST DEFENSIVE CODE IN THE SUITE. Every state observation used
# to be `qf status ... 2>/dev/null | python3 -c ... 2>/dev/null`, which has three
# possible outcomes collapsed into two: a state name, or an empty string that
# meant EITHER "the job has no state" (impossible) OR "I could not ask". A run of
# this suite reported pass=49 fail=24 where all 24 failures and at least three of
# the PASSES came from that empty string -- including
#
#   ok  (exclusion) two heavy jobs are never both RUNNING
#   ok  (budget) a 22g heavy and a 4g light never run concurrently
#
# both of which are `while ...; do if [ "$(state_of A)" = RUNNING ] && ...`. An
# empty string never equals RUNNING, so the two properties NC8 exists to prove
# passed because nothing whatsoever was observed. The failures were noisy and
# honest; the vacuous passes were the dangerous half.
#
# So state is now read through one function that can say "I could not ask", the
# reason is recorded, and no clause is allowed to draw a conclusion from it.
#
# THE COUNTER LIVES IN A FILE, not a variable. state_of is called almost
# exclusively inside `$(...)`, and a subshell's increment to a shell variable is
# discarded when it exits -- a counter kept in a variable here would read 0 no
# matter how blind the run had been.
BLIND_FILE="$(mktemp -t nc-blind.XXXXXX)"
trap 'rm -f "$BLIND_FILE"' EXIT

note_blind() {  # note_blind <reason>
  printf '%s\n' "$1" >> "$BLIND_FILE"
  # Once per distinct reason, like the daemon's transition logging: a poll loop
  # would otherwise print the same line 30 times and bury the first occurrence.
  if [ "$(grep -c -x -F "$1" "$BLIND_FILE" 2>/dev/null || echo 1)" = 1 ]; then
    echo "BLIND cannot read job state: $1" >&2
  fi
}

blind_count() { wc -l < "$BLIND_FILE" 2>/dev/null | tr -d ' '; }

# UNREADABLE, not "". A sentinel that is obviously not a state, so a clause that
# compares it against RUNNING and moves on is at least comparing something a
# reader can find in the output.
UNREADABLE=UNREADABLE

status_json() {  # status_json <run_id> -> payload on stdout, reason on stderr
  local rid="$1" out rc
  if [ -z "$rid" ]; then
    echo "no run id was ever produced (the submit failed)" >&2; return 1
  fi
  # `qf --json status`, NOT `qf status ... --json`. The flag is defined on the
  # top-level parser; the trailing form exited 2 with "unrecognized arguments:
  # --json" and this helper's discarded stderr turned that into an empty state
  # for every job in the suite. The client now accepts both orders, but the
  # global form is the one that also works against an older deployed client.
  out="$(as "$RESEARCH_USER" "qf --json status $rid" 2>&1)"; rc=$?
  if [ "$rc" -ne 0 ]; then
    printf 'qf status exited %s: %s\n' "$rc" \
      "$(printf '%s' "$out" | tr '\n' ' ' | cut -c1-200)" >&2
    return 1
  fi
  printf '%s' "$out"
}

# Extraction is a separate step from transport so the two failures do not share a
# message: "the dispatcher would not answer" and "the answer had no job in it"
# have completely different remedies.
_extract() {  # _extract <run_id> <jq-ish key> -- reads payload on stdin
  python3 -c '
import json, sys
key = sys.argv[1]
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception as e:
    sys.exit("payload is not JSON (%s): %.120s" % (e, raw))
if not d.get("ok", True):
    sys.exit("the dispatcher refused: %s" % d.get("error"))
job = d.get("job")
if job is None:
    sys.exit("the reply has no job key: %.120s" % raw)
value = job.get(key, "__MISSING__")
if value == "__MISSING__":
    sys.exit("the job has no %s field" % key)
print(value)
' "$1"
}

# Both streams are MERGED and the exit code does the discriminating, because
# status_json writes a payload on success and a reason on failure and never both.
# The first version routed stderr to a temp file, which worked and read as though
# something were being thrown away -- and a helper whose whole purpose is not
# throwing errors away should not have a `2>/dev/null` anywhere in it.
state_of() {  # state_of <run_id> -> a state name, or UNREADABLE
  local rid="$1" out st
  if ! out="$(status_json "$rid" 2>&1)"; then
    note_blind "$out"; printf '%s' "$UNREADABLE"; return 1
  fi
  if ! st="$(printf '%s' "$out" | _extract state 2>&1)"; then
    note_blind "$st"; printf '%s' "$UNREADABLE"; return 1
  fi
  printf '%s' "$st"
}

field_of() {  # field_of <run_id> <field> -> the value, or UNREADABLE
  local rid="$1" out value
  if ! out="$(status_json "$rid" 2>&1)"; then
    note_blind "$out"; printf '%s' "$UNREADABLE"; return 1
  fi
  if ! value="$(printf '%s' "$out" | _extract "$2" 2>&1)"; then
    note_blind "$value"; printf '%s' "$UNREADABLE"; return 1
  fi
  printf '%s' "$value"
}

is_run_id() {  # is_run_id <candidate>
  # A SHAPE check, never "does its directory exist". The run directory is
  # created by `prepare_run_dir` during execute, so between submit and the
  # first lease there is a perfectly good QUEUED job with no directory at all.
  # Four clauses guarded on the directory immediately after submitting, which
  # voided NC19's canary on a working submit -- and in the unpromoted-baseline
  # clause it printed `ok "never became a run"` for a job that HAD been created
  # and simply had not started. A positive claim about absence, resting on a
  # directory that does not exist yet.
  # The SHAPE qfd actually mints (`make_run_id`):
  #   <kind>-<YYYYmmddTHHMMSSZ>-<sha[:12]>-<seq>
  # Matched rather than length-checked, because the values this has to reject
  # are the client's error messages -- "qf: error: unrecognized arguments",
  # "no dispatcher socket at ..." -- and a length floor accepts most of those
  # once `tail -1` has trimmed them to one line.
  case "$1" in
    *[0-9]-[0-9]*) ;;
    *) return 1 ;;
  esac
  case "$1" in
    [a-z]*-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z-*) ;;
    *) return 1 ;;
  esac
  case "$1" in
    *[!A-Za-z0-9._-]*) return 1 ;;
  esac
  return 0
}

pin_of() {  # pin_of <run_id> <pin key> -> the value, or empty
  # A JOB PIN, not a column. Written out once here because NC18 carried two
  # copies of the same six lines and NC19 would have made four -- and a pin
  # helper that disagrees with itself across clauses is how a missing pin comes
  # to look like a different pin.
  local out
  if ! out="$(status_json "$1" 2>&1)"; then
    note_blind "$out"; return 1
  fi
  printf '%s' "$out" | python3 -c "
import json, sys
print((json.load(sys.stdin)['job'].get('pins') or {}).get(sys.argv[1], ''))
" "$2" 2>/dev/null
}

# THE GATE. Called at the top of every clause that is about to conclude
# something from a job's state. It runs in the MAIN shell (not a substitution),
# so unlike note_blind it can actually stop the run.
#
# A conclusion drawn while the instrument is broken is not a weaker conclusion,
# it is a false one, and it is indistinguishable in the output from a real pass.
readable() {  # readable <run_id> <clause-name> -> 0 if state is observable
  local st; st="$(state_of "$1")"
  if [ "$st" = "$UNREADABLE" ]; then
    void "$2  (state unreadable: $(tail -1 "$BLIND_FILE" 2>/dev/null))"
    return 1
  fi
  return 0
}

submit_as() {  # submit_as <user> <args...> -> prints the run id
  local out rc
  out="$(as "$1" "qf submit ${*:2}" 2>&1)"; rc=$?
  if [ "$rc" -ne 0 ]; then
    # Not silent, and not fatal: the caller checks for an empty id. But the
    # REASON is what turns "no run id" into something actionable -- nine of those
    # in one gate run cost an afternoon because the message was thrown away.
    echo "  submit failed (rc=$rc): $(printf '%s' "$out" | tr '\n' ' ' | cut -c1-200)" >&2
    return 1
  fi
  printf '%s' "$out" | tail -1
}

wait_state() {  # wait_state <run_id> <state> <timeout_s>
  local rid="$1" want="$2" limit="$3" waited=0 st
  while [ "$waited" -lt "$limit" ]; do
    st="$(state_of "$rid")"
    [ "$st" = "$want" ] && return 0
    # No point burning a 300s window polling an instrument that cannot answer.
    [ "$st" = "$UNREADABLE" ] && return 2
    sleep 2; waited=$((waited + 2))
  done
  return 1
}

spec_paths_of() {  # spec_paths_of <run_id> -> the probe's paths, space separated
  # THE ONLY PLACE THE EXPERIMENT PATH LIVES that a client can read. `qf list`
  # prints run_id, state, lane and submitted_at -- so "which probe ran which
  # fixture" cannot be answered by grepping a listing, which is what NC11's first
  # attempt did. `qf --json status` carries the submitted spec.
  local out
  if ! out="$(status_json "$1" 2>&1)"; then
    note_blind "$out"; return 1
  fi
  printf '%s' "$out" | python3 -c "
import json, sys
spec = json.load(sys.stdin)['job'].get('spec') or {}
print(' '.join((spec.get('args') or {}).get('paths') or []))
" 2>/dev/null
}

succeeded_probes() {  # -> SUCCEEDED probe run ids
  # The KIND comes from the run id's own prefix (`make_run_id`), because the
  # client has no --kind flag: two clauses passed one, every invocation exited 2
  # with "unrecognized arguments", and both voided with a message about their
  # subject being absent. A filter the client rejects is not a filter.
  as "$RESEARCH_USER" "qf list --state SUCCEEDED --limit 200" 2>/dev/null \
    | awk '$1 ~ /^probe-/ {print $1}'
}

wait_terminal() {
  local rid="$1" limit="$2" waited=0 st
  while [ "$waited" -lt "$limit" ]; do
    st="$(state_of "$rid")"
    case "$st" in
      SUCCEEDED|FAILED|TIMEOUT|CANCELLED|REFUSED) echo "$st"; return 0 ;;
      "$UNREADABLE") echo "$UNREADABLE"; return 2 ;;
    esac
    sleep 2; waited=$((waited + 2))
  done
  echo "TIMEOUT_WAITING"; return 1
}

# The state that made the last `require_state_for` return 1. A caller that says
# WHICH state it saw is the difference between a finding and a guess: the disk
# floor clause used to print "a job was admitted below the disk floor" for every
# non-QUEUED state, including the terminal ones that mean the opposite of
# admitted. This is a global rather than stdout because the function is called
# in `if`, not in `$(...)`, so an assignment here survives.
LAST_LEFT_FOR=""

require_state_for() {  # require_state_for <run_id> <state> <seconds>
  local rid="$1" want="$2" secs="$3" waited=0 st
  LAST_LEFT_FOR=""
  while [ "$waited" -lt "$secs" ]; do
    st="$(state_of "$rid")"
    if [ "$st" = "$UNREADABLE" ]; then
      echo "  (could not watch $rid: instrument blind)" >&2
      return 2
    fi
    if [ "$st" != "$want" ]; then
      LAST_LEFT_FOR="$st"
      echo "  (left $want for $st after ${waited}s)" >&2
      return 1
    fi
    sleep 3; waited=$((waited + 3))
  done
  return 0
}

terminal_state() {
  case "$1" in SUCCEEDED|FAILED|TIMEOUT|CANCELLED|REFUSED) return 0 ;;
                *) return 1 ;; esac
}

# never_concurrent <name> <rid_a> <rid_b> <window_s> <overlap-message>
#
# "They were never both RUNNING" is a claim satisfied by two very different
# worlds: one where a mutex serialised them, and one where the observer was blind
# or the jobs never started. The first is the property NC8 exists to prove. The
# second is how this clause printed `ok` while state_of returned "" sixty times.
#
# So a pass now requires POSITIVE evidence -- each job observed RUNNING on its
# own -- and an unobserved run is VOID rather than a pass. This is the same rule
# the refusal groups already follow (a refusal means nothing unless the action
# was possible); it just had never been applied to the concurrency clauses,
# which are the ones where a vacuous pass is most expensive.
never_concurrent() {
  local name="$1" a="$2" b="$3" window="$4" overlap_msg="$5"
  local i=0 both=0 seen_a=0 seen_b=0 sa sb
  if [ -z "$a" ] || [ -z "$b" ]; then
    void "$name  (one of the two jobs was never submitted)"; return
  fi
  while [ "$i" -lt "$window" ]; do
    sa="$(state_of "$a")"; sb="$(state_of "$b")"
    if [ "$sa" = "$UNREADABLE" ] || [ "$sb" = "$UNREADABLE" ]; then
      void "$name  (state unreadable: $(tail -1 "$BLIND_FILE" 2>/dev/null))"; return
    fi
    [ "$sa" = RUNNING ] && seen_a=1
    [ "$sb" = RUNNING ] && seen_b=1
    if [ "$sa" = RUNNING ] && [ "$sb" = RUNNING ]; then both=1; break; fi
    # Stop as soon as the answer cannot change: both seen separately (property
    # established) or both terminal (nothing further can happen). The old loop
    # always burned its full window, which is why it was set to a window too
    # short for two serialised test jobs to both start in.
    [ "$seen_a" = 1 ] && [ "$seen_b" = 1 ] && break
    terminal_state "$sa" && terminal_state "$sb" && break
    sleep 2; i=$((i + 2))
  done
  if [ "$both" = 1 ]; then
    bad "$overlap_msg"
  elif [ "$seen_a" = 1 ] && [ "$seen_b" = 1 ]; then
    ok "$name  (each was observed RUNNING separately)"
  else
    void "$name  (never observed either job RUNNING: a=$seen_a b=$seen_b; exclusion unproven)"
  fi
}

# `-c safe.directory=` is load-bearing, not defensive. The mirror is owned by
# qfd and this runs as the deploy user, and modern git REFUSES a repository
# owned by someone else ("detected dubious ownership") -- with stderr discarded,
# that came back as an empty string, and an empty string here VOIDs NC13 and
# NC15 with "no mirror HEAD" on a perfectly healthy host. A blanket
# `--global safe.directory` would fix it too and is worse: it would leave the
# exception behind for everything the deploy user ever touches.
# Run as root (the suite already is), not as the nightly user: reading the mirror
# is not a property of that identity, and coupling it there meant a suite that
# could not identify the nightly user could not even find a sha to submit.
head_sha() {
  git -c "safe.directory=$STATE_DIR/mirror.git" -C "$STATE_DIR/mirror.git" \
    rev-parse refs/remotes/origin/main 2>/dev/null
}

# The Task 13 fixture sha, validated as a sha before anything is submitted with
# it. A truncated or comment-laden nc12-sha.txt would otherwise reach `qf submit`
# and be refused for a reason that reads like containment.
fixture_sha() {
  [ -f "$NC12_SHA_FILE" ] || return 1
  local s; s="$(tr -d '[:space:]' < "$NC12_SHA_FILE")"
  [ "${#s}" -eq 40 ] || return 1
  case "$s" in *[!0-9a-f]*) return 1 ;; esac
  printf '%s' "$s"
}

# A stand-in for the nightly: opens the mutex, waits up to <wait_s> for it, then
# holds it for <hold_s>.
#
# SETS TWO VARIABLES rather than echoing the pid, and both reasons are defects
# this replaced. It used to be `( ... ) & echo $!`, read as
# `sp="$(standin_nightly 300)"`:
#
#   1. Command substitution reads the pipe to EOF, and the backgrounded subshell
#      INHERITS that pipe as its stdout. So `$(...)` did not return when the
#      function returned -- it blocked until the stand-in had waited for the
#      lock, slept, and exited. By the time `sp` was assigned the process was
#      already dead, and `kill -0` reported "exited instead of waiting".
#   2. Forked inside a substitution subshell, it was a GRANDCHILD of the suite,
#      and `wait` on a non-child returns 127 immediately -- so "(a) it never
#      acquired the lock" was printed without anything being waited for.
#
# Acquisition is reported through a MARKER FILE, not through the exit status, so
# the clause can time the wait itself instead of the wait plus the hold.
standin_nightly() {  # standin_nightly <wait_s> [hold_s]; sets STANDIN_PID/_ACQUIRED
  STANDIN_ACQUIRED="$(mktemp -u -t nc-standin.XXXXXX)"
  # stdout and stderr go to /dev/null so this can never hold a caller's pipe
  # open, whatever the call site looks like.
  ( exec 9>"$LOCK"
    flock -w "$1" 9 || exit 1
    date +%s > "$STANDIN_ACQUIRED"
    sleep "${2:-5}"
  ) >/dev/null 2>&1 &
  STANDIN_PID=$!
}

standin_acquired() { [ -s "$STANDIN_ACQUIRED" ]; }

# Waits for the stand-in to acquire, up to <limit>s. Distinguishes acquisition
# from exit: the process exiting without the marker means flock timed out.
wait_standin_acquired() {  # wait_standin_acquired <limit_s>
  local waited=0
  while [ "$waited" -lt "$1" ]; do
    standin_acquired && return 0
    kill -0 "$STANDIN_PID" 2>/dev/null || return 1   # exited without acquiring
    sleep 2; waited=$((waited + 2))
  done
  return 1
}

# =========================================================================
# NC8 -- the mutex, seventeen clauses, thirteen of them found by review.
# =========================================================================
# PRECONDITION, once, before any clause. This suite submits dozens of jobs, so
# one upstream condition -- admissions stopped, the nightly holding the mutex, a
# full per-uid queue, or a co-tenant Postgres spilling temp files into the disk
# floor -- turns into a screenful of unrelated VOIDs that each look like a
# containment failure. The fault gates learned this the expensive way: eleven
# reports from a single stuck queue.
#
# It REFUSES rather than warns. An evidence file is the output of this script, and
# evidence gathered against a host that could not run jobs is worse than no
# evidence, because it reads as coverage.
preflight() {
  local ping admit mutex queued free_mb cap floor_mb need_mb
  ping="$(as "$RESEARCH_USER" 'qf ping' 2>/dev/null)" || true
  if [ -z "$ping" ]; then
    echo "PREFLIGHT: cannot reach the dispatcher as $RESEARCH_USER." >&2
    exit 2
  fi
  admit="$(printf '%s\n' "$ping" | awk -F': ' '/^admit:/{print $2}')"
  mutex="$(printf '%s\n' "$ping" | awk -F': ' '/^mutex:/{print $2}')"
  queued="$(printf '%s\n' "$ping" | awk -F': ' '/^queued:/{print $2}')"
  free_mb="$(printf '%s\n' "$ping" | awk -F': ' '/^free_disk_mb:/{print $2}')"
  cap="${QFD_QUEUED_CAP_PER_UID:-20}"
  floor_mb=$(( ${QFD_DISK_FLOOR_GB:-20} * 1024 ))
  # NC15's disk-flood fixture writes up to OUT_QUOTA, and every other clause
  # needs the floor satisfied on top of that.
  need_mb=$(( floor_mb + ${QFD_ARTIFACT_CAP_MB:-2048} + 4096 ))

  if [ -z "$admit" ]; then
    echo "PREFLIGHT: this dispatcher predates the admit/mutex fields in ping;" >&2
    echo "refresh the trusted checkout before gathering evidence." >&2
    exit 2
  fi
  if [ "$admit" != "ok" ]; then
    echo "PREFLIGHT: not admitting ($admit). Clear it first." >&2
    exit 2
  fi
  if [ "$mutex" != "free" ]; then
    echo "PREFLIGHT: the training mutex reads '$mutex'. The nightly" >&2
    echo "walk-forward runs at 01:00 for roughly a quarter of an hour and holds" >&2
    echo "it exclusively; no light job is admitted while it does." >&2
    echo "holders: $(fuser "$LOCK" 2>/dev/null | tr -s ' ')" >&2
    exit 2
  fi
  if [ -n "$queued" ] && [ "$queued" -ge $(( cap / 2 )) ]; then
    echo "PREFLIGHT: $queued jobs already QUEUED against a per-uid cap of" >&2
    echo "$cap; submits would start being refused part-way through." >&2
    exit 2
  fi
  if [ -n "$free_mb" ] && [ "$free_mb" -lt "$need_mb" ]; then
    echo "PREFLIGHT: ${free_mb}MiB free, and this suite needs about" >&2
    echo "${need_mb}MiB (the ${floor_mb}MiB admission floor, NC15's output" >&2
    echo "quota, and headroom). Note that queue-forecasting_pgdata spills temp" >&2
    echo "files during large queries and can double transiently -- if that is" >&2
    echo "what you are seeing, wait for it rather than lowering the floor." >&2
    exit 2
  fi
  echo "preflight: admitting, mutex free, ${queued} queued (cap ${cap}), ${free_mb}MiB free"
  preflight_instrument
}

# THE INSTRUMENT CANARY, and the single most valuable check in this file.
#
# `qf ping` answering proves the socket, the client, sudo, the login shell and
# qfclient membership all work. It does NOT prove that `qf status <run_id>`
# answers, and that is the one call every state assertion in this suite is built
# on. A run where status was broken and ping was fine produced 73 clauses of
# output, 24 noisy failures and at least three PASSES that had observed nothing
# -- and it took about forty minutes to produce.
#
# So: submit a real job, read its state back through the same helper the clauses
# use, and refuse to continue if that round trip does not work. Thirty seconds
# instead of forty minutes, and no misleading evidence file.
preflight_instrument() {
  local rid st
  rid="$(submit_as "$RESEARCH_USER" --kind test --sha "$(head_sha)" --mem 2g)" || true
  if [ -z "$rid" ]; then
    echo "PREFLIGHT: could not submit a probe job, so no state assertion in this" >&2
    echo "suite could mean anything. The reason is printed above." >&2
    exit 2
  fi
  st="$(state_of "$rid")"
  as "$RESEARCH_USER" "qf cancel $rid" >/dev/null 2>&1 || true
  if [ "$st" = "$UNREADABLE" ]; then
    echo >&2
    echo "PREFLIGHT: submitted $rid but CANNOT READ ITS STATE:" >&2
    echo "  $(tail -1 "$BLIND_FILE" 2>/dev/null)" >&2
    echo >&2
    echo "Refusing to run the suite. 'qf ping' works, so this is not the socket" >&2
    echo "or group membership -- it is the status op specifically. Reproduce it" >&2
    echo "with the error visible:" >&2
    echo "  sudo -H -u $RESEARCH_USER qf --json status $rid" >&2
    echo "  sudo journalctl -u qf-dispatch -n 50 --no-pager" >&2
    echo >&2
    echo "Every state clause below would report TIMEOUT_WAITING, and the" >&2
    echo "concurrency clauses would report PASS having observed nothing." >&2
    exit 2
  fi
  echo "preflight: the status round trip works (probe $rid read back as $st)"
}

nc8() {
  echo
  echo "== NC8: no second heavy job, and the mutex protocol from both sides =="

  # --- (d) FIRST: does flock work on this filesystem at all? ---------------
  # On overlayfs and some network filesystems flock can be a no-op or
  # node-local, which would make every clause below vacuous.
  if ( exec 9>"$LOCK"; flock -n 9 && \
       ( exec 8>"$LOCK"; flock -n 8 ) && exit 1 || exit 0 ); then
    ok "(d) flock actually excludes on this filesystem"
  else
    void "(d) flock does not exclude on $(stat -f -c %T "$LOCK") -- every clause below would be vacuous"
    return
  fi

  # --- one inode -----------------------------------------------------------
  exists "(one-inode) migration marker" "$MIGRATED_MARKER"
  local cron_lock disp_id cron_id cron_intent
  # Word-anchored: 'LOCK_FILE=' also matches inside 'BACKUP_LOCK_FILE=', and
  # `head -1` would then stat the BACKUP lock and pass for the wrong reason.
  cron_lock="$(crontab -l -u "$DEPLOY_USER" 2>/dev/null \
    | grep -oE '(^|[[:space:]])LOCK_FILE=[^[:space:]]*' | head -1 | cut -d= -f2)"
  cron_intent="$(crontab -l -u "$DEPLOY_USER" 2>/dev/null \
    | grep -oE '(^|[[:space:]])INTENT_DIR=[^[:space:]]*' | head -1 | cut -d= -f2)"
  if [ -n "$cron_lock" ]; then
    disp_id="$(stat -c '%d:%i' "$LOCK" 2>/dev/null)"
    cron_id="$(stat -c '%d:%i' "$cron_lock" 2>/dev/null)"
    assert_eq "(one-inode) dispatcher and cron lock are the same inode" "$disp_id" "$cron_id"
  else
    void "(one-inode) no LOCK_FILE= in the deploy crontab"
  fi
  if [ -n "$cron_intent" ]; then
    assert_eq "(one-inode) dispatcher and cron intent dir are the same inode" \
      "$(stat -c '%d:%i' "$INTENT_DIR")" "$(stat -c '%d:%i' "$cron_intent")"
  else
    void "(one-inode) no INTENT_DIR= in the deploy crontab"
  fi

  # --- permission and immutability ----------------------------------------
  # The property is that the lock's DIRECTORY is 0755 root:root, so neither
  # runtime user can unlink or recreate the inode -- and qfd refuses to start
  # when it is missing.
  #
  # Asserted against the directory first, and the live `rm` only for users who
  # are actually subject to it. Two reasons, both learned the hard way:
  #
  #   * root is exempt from DAC, so "root cannot unlink the lock" is a claim
  #     that can only ever fail. Asserting it is not a strict test, it is a
  #     broken one.
  #   * the attempt is DESTRUCTIVE. When it succeeded it removed the mutex
  #     inode, so the qfd canary below then failed to open a file that no longer
  #     existed, the nightly script's `exec 9>` would have failed fatally, and
  #     the daemon would refuse to start. A gate that can destroy the thing it
  #     guards has to check afterwards and put it back.
  local lock_dir; lock_dir="$(dirname "$LOCK")"
  assert_eq "(perm) the lock directory is 0755" "755" "$(stat -c '%a' "$lock_dir")"
  assert_eq "(perm) the lock directory is owned by root:root" "root:root" \
    "$(stat -c '%U:%G' "$lock_dir")"

  local lock_id_before; lock_id_before="$(stat -c '%d:%i' "$LOCK")"
  for u in "$DEPLOY_USER" qfd; do
    [ -n "$u" ] || continue
    canary_as "$u" "(perm) $u can open the lock for write" "exec 9>$LOCK"
    if [ "$(id -u "$u" 2>/dev/null)" = "0" ]; then
      # Stated rather than skipped silently: an omitted clause reads as coverage.
      echo "      (perm) $u is uid 0 and exempt from DAC; the directory"\
           "assertions above are what constrain it"
    else
      # `rm`, NOT `rm -f`: -f exits 0 for a file that is not there, so once the
      # inode had been destroyed this clause reported PERMITTED for every user
      # regardless of permissions -- three of the four NC8 failures on the first
      # real run were that one deletion echoing forward.
      refuse_as "$u" "(perm) $u cannot unlink the lock" "rm $LOCK"
    fi
  done
  if [ ! -e "$LOCK" ] || [ "$(stat -c '%d:%i' "$LOCK")" != "$lock_id_before" ]; then
    bad "(perm) THE LOCK INODE WAS REPLACED OR DESTROYED by this gate; restoring it"
    systemd-tmpfiles --create /etc/tmpfiles.d/qf-locks.conf >/dev/null 2>&1 \
      || install -m 0660 -o root -g qfheavy /dev/null "$LOCK"
  fi
  # --- group: research must not be able to touch the mutex at all ----------
  if id -nG "$RESEARCH_USER" 2>/dev/null | tr ' ' '\n' | grep -qx qfheavy; then
    bad "(group) $RESEARCH_USER is in qfheavy -- it could stop nightly training at will"
  else
    ok "(group) $RESEARCH_USER is not in qfheavy"
  fi
  refuse_as "$RESEARCH_USER" "(group) $RESEARCH_USER cannot open the lock for write" \
    "exec 9>$LOCK"

  # --- (b3) marker permissions -------------------------------------------
  canary_as "$DEPLOY_USER" "(b3) deploy can create a marker" \
    "touch $INTENT_DIR/nightly.\$\$.\$(date +%s).intent && rm -f $INTENT_DIR/nightly.\$\$.*.intent"
  refuse_as "$RESEARCH_USER" "(b3) research cannot create a marker" \
    "touch $INTENT_DIR/nightly.9999.1.intent"
  local victim="$INTENT_DIR/nightly.1234.$(date +%s).intent"
  printf 'pid=1234\ndeadline=%d\n' "$(( $(date +%s) + 60 ))" > "$victim"
  refuse_as "$RESEARCH_USER" "(b3) research cannot delete an existing marker" "rm -f $victim"
  rm -f "$victim"

  # --- (h) marker readability under a hostile umask -----------------------
  # TWO clauses, because the mechanism has two halves and only one of them is
  # the directory's setgid bit.
  #
  # The old single clause wrote a marker with a bare `>` under umask 077 and
  # asserted qfd could read it, on the reasoning that setgid made it so. It
  # cannot: setgid sets the GROUP, never the mode, so that marker is 0600 and qfd
  # genuinely cannot read it. The clause failed on a correctly configured host
  # and the comment it was written from (in qf-locks.conf) claimed more for
  # setgid than setgid does.
  #
  # What actually makes this safe is the publisher: daily_walk_forward.sh writes
  # a temp file, `chmod 0640`s it, and `mv`s it into place. So (h1) tests that
  # sequence -- the one that ships -- and (h2) is its negative control, proving
  # the chmod is what does the work rather than the umask happening to be lax.
  local now_s; now_s="$(date +%s)"
  local m_good="$INTENT_DIR/nightly.4321.$now_s.intent"
  as "$DEPLOY_USER" "umask 077; t=$m_good.tmp; printf 'pid=4321\ndeadline=%d\n' $(( now_s + 60 )) > \$t && chmod 0640 \$t && mv -f \$t $m_good"
  if as qfd "cat $m_good" >/dev/null 2>&1; then
    ok "(h1) the nightly's publish sequence yields a marker qfd can read under umask 077"
  else
    bad "(h1) qfd cannot read a marker published the way the nightly publishes it -- it would admit straight through the declaration"
  fi
  assert_eq "(h1) the published marker's group is qfheavy" "qfheavy" \
    "$(stat -c '%G' "$m_good" 2>/dev/null)"
  rm -f "$m_good"

  local m_bare="$INTENT_DIR/nightly.4322.$now_s.intent"
  as "$DEPLOY_USER" "umask 077; printf 'pid=4322\ndeadline=%d\n' $(( now_s + 60 )) > $m_bare"
  if as qfd "cat $m_bare" >/dev/null 2>&1; then
    bad "(h2) a marker written WITHOUT the chmod was readable anyway, so (h1) proves nothing about the chmod -- check the deploy user's umask"
  else
    ok "(h2) the same write without the chmod is unreadable, so the chmod is what makes (h1) work"
  fi
  rm -f "$m_bare"

  # --- canary: a heavy job runs at all with the lock free -----------------
  local heavy rid_a rid_b
  heavy="$(submit_as "$RESEARCH_USER" --kind test --sha "$(head_sha)" --mem 8g)"
  if [ -n "$heavy" ] && wait_state "$heavy" RUNNING 300; then
    ok "canary: a heavy job reaches RUNNING with the lock free"
  else
    void "canary: a heavy job reaches RUNNING (got '$(state_of "$heavy")')"
  fi
  wait_terminal "$heavy" 600 >/dev/null

  # --- refusal: an unrelated holder keeps a heavy job QUEUED --------------
  ( flock -n "$LOCK" -c 'sleep 90' ) &
  local holder=$!
  sleep 2
  rid_a="$(submit_as "$RESEARCH_USER" --kind test --sha "$(head_sha)" --mem 8g)"
  # Three outcomes, because require_state_for now has three: it held (0), it
  # moved (1), or the suite could not watch it (2). Folding 2 into "it moved"
  # printed FAIL on a healthy host; folding it into "it held" would be worse.
  require_state_for "$rid_a" QUEUED 15
  case $? in
    0) ok  "(refusal) a heavy job stays QUEUED while the lock is held elsewhere" ;;
    1) bad "(refusal) a heavy job left QUEUED while the lock was held" ;;
    *) void "(refusal) a heavy job stays QUEUED  (could not watch it)" ;;
  esac
  wait "$holder" 2>/dev/null
  # POSITIVELY observed RUNNING. The old form was
  #   wait_state ... RUNNING 120 || [ "$(state_of ...)" != "QUEUED" ]
  # whose right-hand side is true for UNREADABLE, so a blind suite reported that
  # the mutex handed the job through. "Not still queued" is not "it started".
  wait_state "$rid_a" RUNNING 120
  case $? in
    0) ok  "(refusal) it starts once the lock is released" ;;
    2) void "(refusal) it starts once the lock is released  (state unreadable)" ;;
    *) if terminal_state "$(state_of "$rid_a")"; then
         ok "(refusal) it starts once the lock is released  (already terminal)"
       else
         bad "(refusal) it never started after the lock was released"
       fi ;;
  esac
  wait_terminal "$rid_a" 900 >/dev/null

  # --- exclusion: two heavy jobs are never both RUNNING -------------------
  rid_a="$(submit_as "$RESEARCH_USER" --kind test --sha "$(head_sha)" --mem 8g)"
  rid_b="$(submit_as "$RESEARCH_USER" --kind test --sha "$(head_sha)" --mem 8g)"
  never_concurrent "(exclusion) two heavy jobs are never both RUNNING" \
    "$rid_a" "$rid_b" 1200 "(exclusion) two heavy jobs ran concurrently"

  # --- budget: a 22g heavy and a 4g light never overlap -------------------
  local big small
  big="$(submit_as "$RESEARCH_USER" --kind test --sha "$(head_sha)" --mem 22g)"
  small="$(submit_as "$RESEARCH_USER" --kind test --sha "$(head_sha)" --mem 4g)"
  never_concurrent "(budget) a 22g heavy and a 4g light never run concurrently" \
    "$big" "$small" 1200 "(budget) 26g of admitted memory ran at once on a ~29g host"
  for r in "$rid_a" "$rid_b" "$big" "$small"; do
    as "$RESEARCH_USER" "qf cancel $r" >/dev/null 2>&1
  done

  nc8_protocol
}

nc8_protocol() {
  echo
  echo "-- NC8 protocol clauses (design D10a) --"
  local sha; sha="$(head_sha)"

  # (a) a stand-in nightly WAITS behind a light job's LOCK_SH, and proceeds.
  local light; light="$(submit_as "$RESEARCH_USER" --kind test --sha "$sha" --mem 2g)"
  if ! wait_state "$light" RUNNING 300; then
    void "(a) the light job never reached RUNNING, so there is nothing for the stand-in to wait behind"
    return
  fi
  local t0 t1
  t0=$(date +%s)
  standin_nightly 300
  sleep 5
  # Still alive AND has not acquired: that is what waiting looks like. Alive
  # alone would also be true of a stand-in that took the lock immediately,
  # which would mean the light job was not holding it.
  if kill -0 "$STANDIN_PID" 2>/dev/null && ! standin_acquired; then
    ok "(a) a stand-in nightly waits rather than exiting"
  elif standin_acquired; then
    bad "(a) the stand-in took the mutex while a light job was RUNNING"
  else
    bad "(a) the stand-in nightly exited instead of waiting"
  fi
  wait_terminal "$light" 900 >/dev/null
  if wait_standin_acquired 300; then
    t1="$(cat "$STANDIN_ACQUIRED")"
    ok "(a) it proceeded after $((t1 - t0))s"
  else
    bad "(a) it never acquired the lock"
  fi
  wait "$STANDIN_PID" 2>/dev/null
  rm -f "$STANDIN_ACQUIRED"

  # (b) STARVATION, tested by actively trying to barge while the waiter is queued.
  light="$(submit_as "$RESEARCH_USER" --kind test --sha "$sha" --mem 2g)"
  wait_state "$light" RUNNING 300
  local marker="$INTENT_DIR/nightly.$$.$(date +%s).intent"
  printf 'pid=%d\ndeadline=%d\n' "$$" "$(( $(date +%s) + 600 ))" > "$marker"
  chmod 0640 "$marker"
  standin_nightly 600
  local barged=0 j=0
  while [ "$j" -lt 5 ]; do
    local probe; probe="$(submit_as "$RESEARCH_USER" --kind test --sha "$sha" --mem 2g)"
    sleep 6
    [ "$(state_of "$probe")" != QUEUED ] && barged=1
    as "$RESEARCH_USER" "qf cancel $probe" >/dev/null 2>&1
    j=$((j + 1))
  done
  if [ "$barged" -eq 0 ]; then
    ok "(b) no admission barged past the queued nightly waiter"
  else
    bad "(b) an admission barged past a live intent marker"
  fi
  wait_terminal "$light" 900 >/dev/null
  rm -f "$marker"
  if wait_standin_acquired 300; then
    ok "(b) nightly entered once the running job drained"
  else
    bad "(b) nightly never entered"
  fi
  wait "$STANDIN_PID" 2>/dev/null
  rm -f "$STANDIN_ACQUIRED"

  # (b2) stale markers: dead PID, and expired deadline.
  local stale_dead="$INTENT_DIR/nightly.999999.$(date +%s).intent"
  printf 'pid=999999\ndeadline=%d\n' "$(( $(date +%s) + 600 ))" > "$stale_dead"
  local stale_old="$INTENT_DIR/nightly.$$.$(( $(date +%s) - 10 )).intent"
  printf 'pid=%d\ndeadline=%d\n' "$$" "$(( $(date +%s) - 60 ))" > "$stale_old"
  local probe; probe="$(submit_as "$RESEARCH_USER" --kind test --sha "$sha" --mem 2g)"
  if wait_state "$probe" RUNNING 180; then
    ok "(b2) a crashed nightly's markers do not wedge the dispatcher"
  else
    bad "(b2) stale markers blocked admission (state $(state_of "$probe"))"
  fi
  [ -e "$stale_dead" ] && bad "(b2) the dead-PID marker was not unlinked" \
    || ok "(b2) the dead-PID marker was unlinked"
  [ -e "$stale_old" ] && bad "(b2) the expired-deadline marker was not unlinked" \
    || ok "(b2) the expired-deadline marker was unlinked"
  wait_terminal "$probe" 900 >/dev/null

  # (h) a half-written / unreadable marker fails CLOSED.
  local corrupt="$INTENT_DIR/nightly.$$.$(date +%s).intent"
  printf 'pid=' > "$corrupt"
  probe="$(submit_as "$RESEARCH_USER" --kind test --sha "$sha" --mem 2g)"
  require_state_for "$probe" QUEUED 20
  case $? in
    0) ok  "(h) an unparseable marker fails closed" ;;
    1) bad "(h) an unparseable marker was admitted through (state $LAST_LEFT_FOR)" ;;
    *) void "(h) an unparseable marker fails closed  (could not watch it)" ;;
  esac
  rm -f "$corrupt"
  as "$RESEARCH_USER" "qf cancel $probe" >/dev/null 2>&1

  # (h) two concurrent invocations keep their own declarations.
  local m1="$INTENT_DIR/nightly.111.$(date +%s).intent"
  local m2="$INTENT_DIR/nightly.222.$(( $(date +%s) + 1 )).intent"
  printf 'pid=%d\ndeadline=%d\n' "$$" "$(( $(date +%s) + 300 ))" > "$m1"
  printf 'pid=%d\ndeadline=%d\n' "$$" "$(( $(date +%s) + 300 ))" > "$m2"
  rm -f "$m1"
  if [ -e "$m2" ]; then
    ok "(h) removing one invocation's marker leaves the other's"
  else
    bad "(h) one invocation's cleanup removed another's declaration"
  fi
  rm -f "$m2"

  # (c) PER-DESCRIPTOR ownership: the first light job finishing must not
  # release the second's LOCK_SH. A module-level shared descriptor passes (a)
  # and (b) and fails this.
  local l1 l2
  l1="$(submit_as "$RESEARCH_USER" --kind test --sha "$sha" --mem 2g)"
  l2="$(submit_as "$RESEARCH_USER" --kind test --sha "$sha" --mem 2g --timeout 600)"
  if ! wait_state "$l1" RUNNING 300 || ! wait_state "$l2" RUNNING 300; then
    void "(c) both light jobs never ran together, so nothing held two shared locks"
  else
    standin_nightly 600
    # CANCELLED, not waited for. Both jobs are ordinary test suites of roughly
    # equal duration, so waiting for l1 and hoping l2 outlives it is a coin
    # flip -- and the run that VOIDed reported `l2 was FAILED when l1 finished`,
    # which is that coin landing the other way. (Both FAIL: qf-research has five
    # known CWD-dependent test failures, which is irrelevant to the mutex and
    # exactly why the clause must not depend on how a job ended.)
    #
    # Cancelling l1 makes its exit something this clause SCHEDULES rather than
    # waits for, so l2 is still running with its whole remaining runtime to
    # spare.
    as "$RESEARCH_USER" "qf cancel $l1" >/dev/null 2>&1
    wait_terminal "$l1" 300 >/dev/null
    sleep 10
    # NOT ACQUIRED is the assertion; alive is only the precondition. The old
    # clause tested `kill -0` alone -- and a stand-in that HAD taken the mutex
    # (that is, l1's exit having released l2's LOCK_SH: precisely the bug this
    # clause exists to catch) is also alive, holding it, for the whole hold
    # window. The failure it was written to detect would have printed `ok`.
    # THE PRECONDITION IS RE-CHECKED AT THE MOMENT OF MEASUREMENT, and this is
    # what the clause was missing. It needs l2 to STILL hold its LOCK_SH when l1
    # finishes -- but both are ordinary test jobs of roughly equal duration, and
    # `--timeout 600` is a ceiling, not a length. When l2 happened to finish
    # first, no shared lock was held, the stand-in correctly acquired, and the
    # clause reported a dispatcher failure for a race in its own setup.
    local l2_state; l2_state="$(state_of "$l2")"
    if [ "$l2_state" != RUNNING ]; then
      void "(c) l2 was $l2_state when l1 finished, so no shared lock was held to survive -- the clause could not observe its subject"
    # ACQUIRED IS CHECKED BEFORE ALIVE, because the marker is DURABLE and the
    # process is not: a stand-in that took the lock, wrote its marker and
    # finished its hold is indistinguishable from one that never ran if liveness
    # is tested first. That inversion is what hid the real answer here.
    elif standin_acquired; then
      bad "(c) one job's exit released another's shared lock"
    elif ! kill -0 "$STANDIN_PID" 2>/dev/null; then
      bad "(c) the stand-in exited without acquiring; the clause could not observe the lock"
    else
      ok "(c) the second light job's LOCK_SH survived the first's exit"
    fi
    wait_terminal "$l2" 900 >/dev/null
    wait_standin_acquired 300 >/dev/null || true
    wait "$STANDIN_PID" 2>/dev/null
    rm -f "$STANDIN_ACQUIRED"
  fi

  # (f) the nightly wrapper fails CLOSED without flock.
  local script="$TRUSTED/tools/queue-forecasting/scripts/daily_walk_forward.sh"
  if [ -x "$script" ]; then
    if as "$DEPLOY_USER" "PATH=/nonexistent DRY_RUN=1 $script" >/dev/null 2>&1; then
      bad "(f) the nightly script ran with flock off its PATH"
    else
      ok "(f) the nightly script fails closed without flock"
    fi
  else
    void "(f) $script not found"
  fi

  # (g4) force-release authorisation, POSITIVE CANARIES FIRST -- a refusal
  # proves nothing if nothing can connect at all. Revision 9's 0750 qfd:qfd
  # runtime directory made both sockets unreachable.
  canary_as "$RESEARCH_USER" "(g4) research reaches the client socket" "qf ping"
  # ABSOLUTE PATH. qfadmin is installed in /usr/local/sbin (phase2-setup.sh),
  # and sbin is not on a non-root user's PATH, so by name this canary VOIDed with
  # "qfadmin: command not found" -- a report about $PATH dressed up as a report
  # about the admin socket. The two refusal clauses below connect to the socket
  # directly with python3 for the same reason: a missing binary exits 127, and
  # `refuse_as` would have scored that as a refusal it had not earned.
  canary_as "$DEPLOY_USER" "(g4) deploy reaches the admin socket" \
    "$QFADMIN --help"
  refuse_as "$RESEARCH_USER" "(g4) research cannot reach the admin socket" \
    "python3 -c \"import socket;s=socket.socket(socket.AF_UNIX);s.connect('$ADMIN_SOCK')\""
  refuse_as "$RESEARCH_USER" "(g4) force-release does not exist on the client socket" \
    "python3 -c \"
import json,socket,sys
s=socket.socket(socket.AF_UNIX); s.connect('$CLIENT_SOCK')
s.sendall(json.dumps({'op':'force-release','payload':{'run_id':'x','i_have_verified_nothing_is_running':True}}).encode()+b'\n')
r=json.loads(s.recv(65536).decode().split(chr(10))[0])
sys.exit(0 if r.get('ok') else 1)\""

  # (g6) BUILDING is a first-class state -- checked from the event chain, since
  # the window is short.
  if as "$RESEARCH_USER" "qf verify-chain" >/dev/null 2>&1; then
    ok "(g6) the event chain verifies after the protocol clauses"
  else
    bad "(g6) the event chain does not verify"
  fi

  echo "  note: clauses (e), (g), (g2), (g3), (g5), (g5b) and (i) require killing"
  echo "        the dispatcher mid-flight and are exercised by fault-gates-phase2.sh"
}

# =========================================================================
# NC10 -- trusted paths resolve only from the trusted checkout.
# =========================================================================
nc11() {
  echo
  echo "== NC11: a prediction set that is not a scorable row set is refused =="
  #
  # RESTRUCTURED AFTER A REVIEW, and the reason is worth stating because the
  # first version LOOKED like a working control. It mutated
  # `<run>/out/predictions.parquet` after a probe had succeeded and watched the
  # evaluation refuse. That worked -- because the relay staged from `out/`, which
  # has no recorded digest and is pruned after the handoff, so bytes could change
  # after a run finished and still be judged as that run. The control passed
  # BECAUSE OF the defect it should have found.
  #
  # Now the relay stages `artifacts/predictions.parquet` and requires its digest
  # to equal the one `add_artifact` recorded when the run finished. So post-hoc
  # mutation tests THAT BINDING, which is clause (b), and the row-set property
  # needs a candidate that legitimately emits a bad row set -- clause (c), from a
  # fixture experiment, voiding until the fixture branch carries one.

  local py="$TRUSTED/tools/queue-forecasting/host/evaluator/env/.venv/bin/python"
  if [ ! -x "$py" ]; then
    void "NC11 the evaluator venv is not built at $py. The 2c install step
  exists now (Task 24) and has not been run on this host:
      sudo ./phase2c-setup.sh discover     # what is outstanding
      sudo ./phase2c-setup.sh install
  Until it has, this whole group is void."
    return
  fi
  local ch listing
  listing="$(as "$RESEARCH_USER" "qf contracts" 2>&1)"
  ch="$(printf '%s' "$listing" | sed -n 's/^ *--contract \([0-9a-f]\{64\}\)$/\1/p' \
    | head -1)"
  if [ -z "$ch" ]; then
    void "NC11 no contract resolves; run instantiate-contract.sh against a
  promoted baseline (same precondition as NC9)"
    return
  fi

  # A SUCCEEDED probe that RECORDED a predictions artifact. Asked of the
  # dispatcher, because the recorded artifact is the subject now -- a file in a
  # run directory with no row in `artifacts` is exactly what must not be judged.
  local probe="" rid
  # NO `--kind` FLAG EXISTS. `qf list` takes `--state` and `--limit` and nothing
  # else, so the first version of this loop -- and of clause (c) -- passed
  # `--kind probe`, exited 2 with "unrecognized arguments", read nothing, and
  # voided with a message blaming the absence of a probe. A filter the client
  # rejects is not a filter; the kind is the run id's own prefix.
  for rid in $(succeeded_probes); do
    if [ -f "$RUNS_DIR/$rid/artifacts/predictions.parquet" ]; then
      probe="$rid"; break
    fi
  done
  if [ -z "$probe" ]; then
    void "NC11 no SUCCEEDED probe recorded an artifacts/predictions.parquet, so
  there is no prediction set to judge. Run one 2b-2 cohort first."
    return
  fi
  ok "NC11 subject: $probe"

  local art="$RUNS_DIR/$probe/artifacts/predictions.parquet"
  local out_copy="$RUNS_DIR/$probe/out/predictions.parquet"
  local scratch; scratch="$(mktemp -d)"
  cp -p "$art" "$scratch/artifact.parquet"
  [ -f "$out_copy" ] && cp -p "$out_copy" "$scratch/out.parquet"
  # RESTORED ON EVERY PATH, including a `return` from a failed clause. A mutated
  # artifact left behind would make every later evaluation of this probe fail for
  # a reason the suite caused.
  # shellcheck disable=SC2064
  trap "cp -p '$scratch/artifact.parquet' '$art'; chown qfd '$art' 2>/dev/null || true;
        [ -f '$scratch/out.parquet' ] && cp -p '$scratch/out.parquet' '$out_copy';
        rm -rf '$scratch'" RETURN

  _nc11_eval_of() {  # _nc11_eval_of <probe> -> "<state> <error_class> <verdict>"
    local subject="$1" r st
    r="$(as "$RESEARCH_USER" "qf evaluate --run $subject --contract $ch" 2>&1 | tail -1)"
    if ! is_run_id "$r"; then
      printf 'NOTSUBMITTED %s -' "$(printf '%s' "$r" | tr -d ' ' | cut -c1-40)"
      return
    fi
    st="$(wait_terminal "$r" 1200)"
    printf '%s %s %s' "$st" "$(field_of "$r" error_class)" \
      "$(pin_of "$r" verdict)"
  }

  _nc11_eval() { _nc11_eval_of "$probe"; }

  # (a) THE CANARY. Every refusal below is measured against a working
  # evaluation; without it, an evaluator that refused everything would satisfy
  # the whole group.
  local result verdict_seen
  result="$(_nc11_eval)"
  case "$result" in
    "SUCCEEDED  go"|"SUCCEEDED  no-go")
      verdict_seen="$(printf '%s' "$result" | awk '{print $3}')"
      ok "NC11 (a) the real prediction set is scored (verdict $verdict_seen)" ;;
    *)
      void "NC11 (a) canary: the unmutated prediction set did not produce a
  verdict: $result"
      return ;;
  esac

  # (b) THE JUDGED BYTES ARE THE RECORDED BYTES. Two halves, and the second is
  # the one that would have caught the original defect.
  #
  # (b1) Changing the RECORDED artifact is refused on the digest.
  "$py" -c '
import sys
import pyarrow.parquet as pq
t = pq.read_table(sys.argv[1])
pq.write_table(t.slice(0, max(1, t.num_rows - 1)), sys.argv[1])
' "$art" || { bad "NC11 (b1) could not rewrite the artifact"; return; }
  result="$(_nc11_eval)"
  case "$result" in
    "FAILED evaluate_input_missing "*)
      ok "NC11 (b1) an artifact changed since the run is refused on its digest" ;;
    *) bad "NC11 (b1) a changed artifact was not refused on its digest: $result" ;;
  esac
  cp -p "$scratch/artifact.parquet" "$art"; chown qfd "$art" 2>/dev/null || true

  # (b2) Changing `out/` changes NOTHING, because `out/` is not the input. This
  # is the direct control for the defect the first version of this group rested
  # on: it used to make the evaluation refuse.
  if [ -f "$out_copy" ]; then
    "$py" -c '
import sys
import pyarrow.parquet as pq
t = pq.read_table(sys.argv[1])
pq.write_table(t.slice(0, max(1, t.num_rows - 1)), sys.argv[1])
' "$out_copy" || { bad "NC11 (b2) could not rewrite out/"; return; }
    result="$(_nc11_eval)"
    case "$result" in
      "SUCCEEDED  $verdict_seen")
        ok "NC11 (b2) mutating out/ does not change the verdict: it is not the input" ;;
      *) bad "NC11 (b2) mutating out/ changed the outcome, so the relay is
  reading the candidate's own output directory rather than the recorded
  artifact: $result" ;;
    esac
    cp -p "$scratch/out.parquet" "$out_copy"
  else
    # Expected once D9 pruning has run -- and its absence is itself the point.
    ok "NC11 (b2) out/ has been pruned, so it cannot be the evaluator's input"
  fi

  # (c) THE ROW-SET PROPERTY, FROM A CANDIDATE rather than from a mutation.
  #
  # Post-hoc mutation can no longer reach it, and that is the digest binding in
  # (b) working: bytes that changed since the run are refused before the row set
  # is ever read. So this clause needs a probe that LEGITIMATELY emits an
  # unscorable prediction set. `nc-fixtures-phase2c.sh` writes five, the operator
  # pushes them to the fixture branch, and one probe per script runs them.
  #
  # THE HONEST FIXTURE IS THE CANARY and it is not optional. Four of the five
  # come back as `row_set_rejected`, which is also what a broken contract, a
  # holdout length that disagrees with `holdout_days`, or a slice value that
  # matches nothing would produce -- so without an accepted honest set, every
  # refusal here could be measuring the same unrelated mistake. If the honest one
  # is refused, the rest are VOIDED rather than counted.
  #
  # Each fixture's own outcome is proved in-repo, by running the generated script
  # against a synthetic extract and feeding the result to the real evaluator
  # (`evaluator/tests/test_nc11_fixtures.py`). This clause confirms it on the
  # host, against real data, through the dispatcher.
  local -a fixture_names=(nc11_honest nc11_relabelled nc11_ghost_row
                          nc11_cherry_picked nc11_easy_days)
  declare -A fixture_run=()
  local paths name
  for rid in $(succeeded_probes); do
    paths="$(spec_paths_of "$rid")"
    for name in "${fixture_names[@]}"; do
      case "$paths" in
        *"$name.py"*)
          [ -n "${fixture_run[$name]:-}" ] || fixture_run[$name]="$rid" ;;
      esac
    done
  done

  local canary_ok=0
  if [ -z "${fixture_run[nc11_honest]:-}" ]; then
    void "NC11 (c) no probe has run research/experiments/nc11_honest.py, so the
  row-set property is not exercised on this host, and the fixtures that violate
  it have nothing to be measured against:
      ./nc-fixtures-phase2c.sh ~/qf-research     # writes the five scripts
      # the operator pushes the fixture branch with the AGENT's credential, then
      # one probe per script against the same extract
  The property itself is covered in-repo by
  evaluator/tests/test_nc11_fixtures.py, which runs each generated script and
  feeds its output to the real evaluator."
  else
    result="$(_nc11_eval_of "${fixture_run[nc11_honest]}")"
    case "$result" in
      "SUCCEEDED  go"|"SUCCEEDED  no-go")
        canary_ok=1
        ok "NC11 (c) canary: the honest fixture's row set is scored ($result)" ;;
      *)
        void "NC11 (c) the HONEST fixture was REFUSED: $result. Every refusal
  below would then be measuring that rather than the property it names. Most
  likely the fixture and the contract disagree about the holdout: the scripts use
  NC11_HOLDOUT_DAYS (default 5) and PRIMARY_SLICE ('completed'), and the contract
  in the trusted checkout is authoritative for both. The evaluator's own message
  says which:  journalctl -u qf-eval -n 40" ;;
    esac
  fi

  if [ "$canary_ok" = 1 ]; then
    for name in "${fixture_names[@]}"; do
      [ "$name" = nc11_honest ] && continue
      if [ -z "${fixture_run[$name]:-}" ]; then
        void "NC11 (c) no probe has run research/experiments/$name.py"
        continue
      fi
      result="$(_nc11_eval_of "${fixture_run[$name]}")"
      case "$result" in
        "FAILED row_set_rejected "*)
          ok "NC11 (c) $name is refused as an unscorable row set" ;;
        *)
          bad "NC11 (c) $name was not refused as a row set: $result. The
  fixture violates exactly one part of the property and is proved to do so in
  evaluator/tests/test_nc11_fixtures.py, so a different outcome here is either
  the host's contract or the evaluator." ;;
      esac
    done
  fi

  # (d) THE STAGING ROOT IS THE HANDOVER, so its exact state is the clause.
  #
  # Two uids meet in one directory (D28): `qfd` creates `<run_id>/in/` and copies
  # the untrusted prediction set in, `qfeval` traverses in to read it. The
  # required state is `2770 qfd:qfeval`, and each of the three parts fails
  # somewhere different -- the owner stops the dispatcher creating the inbox, the
  # group stops the evaluator reading it, and the SETGID BIT is the one whose
  # absence looks like nothing at all: a file created in a setgid directory takes
  # the DIRECTORY's group, which is how the staged file reaches the evaluator
  # without `qfd` being in its group, and Linux permits `chown(-1, gid)` only for
  # a member of that group. So 0770 qfd:qfeval reads as correct, lets both
  # processes into the directory, and leaves the ONE FILE the directory exists for
  # in the wrong group. The dispatcher refuses to stage rather than hand over a
  # file the evaluator cannot open, and this is where that is checked on a host.
  local staged="${QFD_EVAL_DIR:-/var/lib/qf-eval}"
  if [ -d "$staged" ]; then
    local state; state="$(stat -c '%a %U %G' "$staged")"
    case "$state" in
      "2770 qfd qfeval") ok "NC11 (d) the staging root is $state" ;;
      "770 qfd qfeval"|"0770 qfd qfeval")
        bad "NC11 (d) the staging root is $state -- the SETGID bit is missing, so
  the staged prediction set lands in qfd's group and the evaluator cannot read
  the one file this directory exists for. phase2c-setup.sh install fixes it." ;;
      *) bad "NC11 (d) the staging root is $state, not 2770 qfd:qfeval:
  phase2c-setup.sh discover names which part is wrong and why" ;;
    esac
    refuse_as "$RESEARCH_USER" "NC11 (d) research cannot read the staged input" \
      "ls $staged"
    # AND A STAGED FILE, IF ONE IS THERE, IS ACTUALLY IN THE EVALUATOR'S GROUP.
    # The mode above is the mechanism; this is the outcome, and the outcome is
    # what the earlier version of the staging code got wrong while every mode it
    # set looked deliberate.
    local one; one="$(find "$staged" -mindepth 3 -maxdepth 3 -type f \
      -name predictions.parquet 2>/dev/null | head -1)"
    if [ -n "$one" ]; then
      local fstate; fstate="$(stat -c '%a %U:%G' "$one")"
      case "$fstate" in
        *":qfeval") ok "NC11 (d) a staged prediction set is $fstate" ;;
        *) bad "NC11 (d) a staged prediction set is $fstate: the evaluator reads
  it by GROUP, so this one is unreadable to the process that must judge it" ;;
      esac
    else
      # NOT A FAILURE. The relay removes the staged copy once it has a verdict --
      # nothing prunes this tree, and the copy's bytes are already in the run's
      # artifacts/, digest-recorded. So its absence after a completed evaluation
      # is the policy working.
      echo "note  NC11 (d) nothing staged in flight (the relay removes the
  copy once it has a verdict; nothing prunes this tree and the same bytes are
  in the run's artifacts/, digest-recorded)"
    fi
  else
    void "NC11 (d) $staged does not exist: run phase2c-setup.sh install"
  fi

  # (e) AND THE VERDICT IS RECOMPUTABLE. `eval.parquet` beside `verdict.json` is
  # what makes the numbers checkable rather than believable.
  local out_dir
  out_dir="$(find "$staged" -mindepth 2 -maxdepth 2 -type d -name out 2>/dev/null \
    | head -1)"
  if [ -n "$out_dir" ] && [ -f "$out_dir/verdict.json" ]; then
    [ -f "$out_dir/eval.parquet" ] \
      && ok "NC11 (e) the verdict is published beside its per-row file" \
      || bad "NC11 (e) $out_dir has a verdict.json and no eval.parquet"
    "$py" -c '
import hashlib, json, sys
d = json.load(open(sys.argv[1]))
body = {k: v for k, v in d.items() if k != "eval_hash"}
canon = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
rows = d.get("inputs", {}).get("eval_sha256")
h = hashlib.sha256()
with open(sys.argv[2], "rb") as fh:
    for chunk in iter(lambda: fh.read(1 << 20), b""):
        h.update(chunk)
ok = (hashlib.sha256(canon).hexdigest() == d.get("eval_hash")
      and rows == h.hexdigest())
sys.exit(0 if ok else 1)
' "$out_dir/verdict.json" "$out_dir/eval.parquet" \
      && ok "NC11 (e) the verdict hashes to its own eval_hash and pins its rows" \
      || bad "NC11 (e) the verdict does not verify against its own body and rows"
  else
    void "NC11 (e) no verdict.json under $staged"
  fi
}

nc10() {
  echo
  echo "== NC10: trusted paths resolve only from the trusted checkout =="
  local json
  # stderr KEPT. This canary voided with "returned nothing" on a host where the
  # client was printing a perfectly good explanation to stderr, and the suite
  # threw it away -- which is the same defect as the old state_of.
  local why
  json="$(as "$RESEARCH_USER" "qf --json trusted-paths" 2>/tmp/nc10.$$)"
  why="$(cat /tmp/nc10.$$ 2>/dev/null)"; rm -f /tmp/nc10.$$
  if [ -z "$json" ]; then
    void "NC10 canary: qf trusted-paths returned nothing${why:+ ($why)}"
    return
  fi
  ok "NC10 canary: qf trusted-paths responded"

  local name real digest recomputed
  for name in trainer-env.Dockerfile env/pyproject.toml env/uv.lock \
              nc13-inside.sh spec.py store.py sandbox.py qfd.py; do
    real="$(printf '%s' "$json" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(next((e.get('realpath','') for e in d['paths'] if e['name']=='$name'), ''))")"
    digest="$(printf '%s' "$json" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(next((e.get('sha256','') for e in d['paths'] if e['name']=='$name'), ''))")"
    case "$real" in
      "$TRUSTED"/*) ;;
      *) bad "NC10 $name realpath '$real' is outside \$TRUSTED"; continue ;;
    esac
    recomputed="$(sha256sum "$real" 2>/dev/null | cut -d' ' -f1)"
    assert_eq "NC10 $name digest recomputed independently" "$recomputed" "$digest"
  done

  # There is no field to redirect -- that is the control, and it is asserted.
  refuse_as "$RESEARCH_USER" "NC10 an invented path field is refused by name" \
    "python3 -c \"
import json,socket,sys
s=socket.socket(socket.AF_UNIX); s.connect('$CLIENT_SOCK')
spec={'schema':1,'kind':'test','source_sha':'$(head_sha)','dockerfile':'/home/$RESEARCH_USER/evil'}
s.sendall(json.dumps({'op':'submit','payload':{'spec':spec}}).encode()+b'\n')
r=json.loads(s.recv(65536).decode().split(chr(10))[0])
sys.exit(0 if r.get('ok') else 1)\""
}

# =========================================================================
# NC12 -- build provenance: no qf-research file participates in the build.
# =========================================================================
nc12() {
  echo
  echo "== NC12: build provenance =="
  local poisoned key_before key_after rid
  if ! poisoned="$(fixture_sha)"; then
    void "NC12 requires a 40-hex sha in $NC12_SHA_FILE (Task 13); missing or malformed is VOID, not skip"
    return
  fi
  key_before="$(as "$DEPLOY_USER" \
    "cd $DISPATCHER && python3 -c 'import image; print(image.content_key(\".\"))'" 2>/dev/null)"
  if [ -z "$key_before" ]; then
    void "NC12 canary: could not compute the image content key"
    return
  fi
  ok "NC12 canary: content key computed ($key_before)"

  rid="$(submit_as "$RESEARCH_USER" --kind test --sha "$poisoned")"
  local final; final="$(wait_terminal "$rid" 1800)"
  echo "  the poisoned-SHA job finished $final"
  # A fixture branch that was written but never PUSHED is the likely first
  # mistake, and without naming it here the operator gets a cascade of failed
  # containment clauses instead of "publish the branch". Every clause below and
  # all of NC15's hostile jobs run at this same sha.
  if [ "$(field_of "$rid" error_class)" = "source_not_published" ]; then
    void "NC12 the fixture sha $poisoned is not on the research remote: push the nc12-poisoned-manifest branch (NC12 and NC15's hostile clauses all run at it)"
    return
  fi
  key_after="$(as "$DEPLOY_USER" \
    "cd $DISPATCHER && python3 -c 'import image; print(image.content_key(\".\"))'" 2>/dev/null)"
  assert_eq "NC12 the image content key is unchanged by a poisoned manifest" \
    "$key_before" "$key_after"

  # The bogus dependency must not be installed. A selftest at the same SHA runs
  # in the same image and can list what is actually there.
  local sid
  sid="$(submit_as "$RESEARCH_USER" --kind selftest --sha "$poisoned")"
  wait_terminal "$sid" 900 >/dev/null
  if as "$RESEARCH_USER" "qf logs $sid" 2>/dev/null \
      | grep -qiE 'qf-nc12-bogus|this-package-does-not-exist'; then
    bad "NC12 the bogus dependency reached the image"
  else
    ok "NC12 the bogus dependency is absent from the image"
  fi

  # The build-context assertion is what makes this a fact rather than a claim.
  if journalctl -u qf-dispatch --since '-30 min' 2>/dev/null \
      | grep -q 'build context holds'; then
    bad "NC12 the build-context assertion FIRED (a file leaked into the context)"
  else
    ok "NC12 the build-context assertion did not fire"
  fi
}

# =========================================================================
# NC13 -- sandbox isolation, asserted from inside the sandbox that ran.
# =========================================================================
nc13() {
  echo
  echo "== NC13: sandbox isolation =="
  local sha rid out
  sha="$(head_sha)"
  if [ -z "$sha" ]; then void "NC13 canary: no mirror HEAD"; return; fi
  rid="$(submit_as "$RESEARCH_USER" --kind selftest --sha "$sha")"
  if [ -z "$rid" ]; then void "NC13 canary: submit produced no run id"; return; fi
  local final; final="$(wait_terminal "$rid" 900)"
  assert_eq "NC13 the selftest job succeeded" "SUCCEEDED" "$final"

  # An exit code alone must not certify it: grep the run's own output.
  #
  # stderr is NOT discarded. It was, and that hid the reason `qf logs` could
  # return nothing: runs_dir was not traversable by the client, so the read
  # failed and the client reported it as a missing file. The message is the only
  # thing that distinguishes "the suite was clean" from "we never read it".
  out="$(as "$RESEARCH_USER" "qf logs $rid")"
  # THE SUMMARY FIRST, and the FAIL/VOID grep only after it. In the other order
  # an EMPTY read reports "no FAIL or VOID line" -- the comfortable answer --
  # before anything establishes that there was output to grep at all.
  if ! printf '%s' "$out" | grep -q '== NC13: pass='; then
    void "NC13 no summary line -- the suite may not have run, and the FAIL/VOID scan below would have passed vacuously"
    return
  fi
  ok "NC13 the in-sandbox suite actually ran to its summary"
  if printf '%s' "$out" | grep -qE '^(FAIL|VOID) '; then
    bad "NC13 the in-sandbox suite reported FAIL/VOID lines"
    printf '%s\n' "$out" | grep -E '^(FAIL|VOID) ' | sed 's/^/    /'
  else
    ok "NC13 no FAIL or VOID line in the in-sandbox output"
  fi
}

nc17() {
  echo
  echo "== NC17: the database credential is unreachable from both other domains =="

  # THE POSITIVE CANARY, and it is not what the plan first said.
  #
  # The plan called for "qfextract can read the credential file". It cannot:
  # /etc/qf-extract/dsn is 0600 root:root and only systemd reads it, handing the
  # service a copy in a per-service credential store. So the canary that licenses
  # every refusal below is the SERVICE WORKING -- `ping` reporting ready, which is
  # only possible if qfextract received a usable credential and connected with it.
  # A TEMP SCRIPT, not an escaped one-liner. The first version tried to nest
  # python inside `bash -lc` inside `sudo` and produced `b'...' + b chr(10)`,
  # which is not Python at all -- a canary that cannot run is worse than none,
  # because its failure reads as the thing it was checking.
  local prober; prober="$(mktemp -t nc17-ping.XXXXXX.py)"
  chmod 0644 "$prober"
  cat > "$prober" <<'PROBE'
import json, socket, sys
sock = socket.socket(socket.AF_UNIX)
sock.settimeout(25)
sock.connect(sys.argv[1])
sock.sendall(json.dumps({"op": sys.argv[2]}).encode() + b"\n")
buf = b""
while b"\n" not in buf:
    chunk = sock.recv(65536)
    if not chunk:
        break
    buf += chunk
print(buf.split(b"\n")[0].decode())
PROBE
  local ping ready
  ping="$(as "$QFD_USER" "timeout 30 python3 $prober $EXTRACT_SOCK ping" 2>&1 || true)"
  ready="$(printf '%s' "$ping" | tail -1 | python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read().strip())
except Exception:
    print('unreadable'); raise SystemExit
if d.get('ready'):
    print('ready')
else:
    print('not-ready: ' + '; '.join(d.get('problems') or ['?']))
" 2>/dev/null || echo unreadable)"
  case "$ready" in
    ready) ok "NC17 canary: the extractor is ready, so it holds a usable credential" ;;
    *) void "NC17 canary: the extractor is not ready ($ready)"; return ;;
  esac

  # The credential itself. Root can read it -- root can read anything -- so these
  # are asserted for the two identities that must not.
  exists "NC17 the credential exists" "$DSN_FILE"
  local mode owner
  mode="$(stat -c %a "$DSN_FILE" 2>/dev/null || echo '?')"
  owner="$(stat -c %U:%G "$DSN_FILE" 2>/dev/null || echo '?')"
  assert_eq "NC17 the credential is 0600" "600" "$mode"
  assert_eq "NC17 the credential is root:root" "root:root" "$owner"

  refuse_as "$QFD_USER" "NC17 qfd cannot read the credential" \
    "cat $DSN_FILE"
  refuse_as "$RESEARCH_USER" "NC17 research cannot read the credential" \
    "cat $DSN_FILE"
  refuse_as "$QFEXTRACT_USER" \
    "NC17 not even qfextract reads the source (systemd hands it a copy)" \
    "cat $DSN_FILE"

  # The SOCKET. qfd is the only permitted client, and `research` must not get as
  # far as being refused by the peer check -- the mode should stop it first.
  # Reachability, via the same script: a `connect` that succeeds is the canary,
  # and the same invocation refused for `research` is the control. Using one
  # probe for both is what makes them comparable.
  canary_as "$QFD_USER" "NC17 qfd reaches the extractor socket" \
    "timeout 30 python3 $prober $EXTRACT_SOCK ping"
  refuse_as "$RESEARCH_USER" "NC17 research cannot reach the extractor socket" \
    "timeout 30 python3 $prober $EXTRACT_SOCK ping"
  rm -f "$prober"

  # THE GROUPS D15 FORBIDS. The service refuses to start if it holds any of
  # them; this asserts the host, so a drift is caught even while the service is
  # not running.
  local held; held=" $(id -nG "$QFEXTRACT_USER" 2>/dev/null) "
  for group in docker qfheavy qfclient; do
    if printf '%s' "$held" | grep -q " $group "; then
      bad "NC17 qfextract is in '$group' (D15 forbids it)"
    else
      ok "NC17 qfextract is not in '$group'"
    fi
  done

  # And the sandbox, re-asserted here because the data plane arriving is exactly
  # when this could stop being true.
  local sha; sha="$(head_sha)"
  if [ -z "$sha" ]; then
    void "NC17 no mirror HEAD, so the in-sandbox check cannot run"
  else
    local rid; rid="$(submit_as "$RESEARCH_USER" --kind selftest --sha "$sha")"
    if [ -z "$rid" ]; then
      void "NC17 selftest submit produced no run id"
    else
      local final; final="$(wait_terminal "$rid" 900)"
      assert_eq "NC17 the in-sandbox suite still passes with a data plane present" \
        "SUCCEEDED" "$final"
    fi
  fi
}

nc18() {
  echo
  echo "== NC18: the extraction request is closed-world, and extracts are immutable =="

  # CANARY: an extract exists at all. Every refusal below is measured against a
  # request that works -- and this one is free, because the request is already
  # published and reuse serves it.
  local rid final
  rid="$(as "$RESEARCH_USER" "qf extract --target wait_time \
      --train-start $NC18_TRAIN_START --as-of $NC18_AS_OF" 2>/dev/null | tail -1)"
  if [ -z "$rid" ]; then
    void "NC18 canary: an extract request produced no run id"
    return
  fi
  final="$(wait_terminal "$rid" 1800)"
  if [ "$final" != "SUCCEEDED" ]; then
    void "NC18 canary: the extract job did not succeed (got $final)"
    return
  fi
  ok "NC18 canary: an extract request reaches SUCCEEDED"

  local pins dir ehash
  pins="$(field_of "$rid" pins)"
  ehash="$(pin_of "$rid" extract_hash)"
  dir="$(pin_of "$rid" extract_dir)"
  if [ -n "$ehash" ]; then
    ok "NC18 the job records an extract_hash ($(printf '%s' "$ehash" | cut -c1-12))"
  else
    bad "NC18 the job records no extract_hash, so nothing points at the extract"
  fi

  # THE CLOSED WORLD. Each refused BY NAME at submit time, before anything runs.
  local out
  for probe in \
      "unknown-target:--target p90" \
      "lookback-zero:--lookback-days 0" \
      "lookback-huge:--lookback-days 999" \
  ; do
    local label="${probe%%:*}" flags="${probe#*:}"
    out="$(as "$RESEARCH_USER" "qf extract --train-start $NC18_TRAIN_START \
        --as-of $NC18_AS_OF --target wait_time $flags" 2>&1 || true)"
    if printf '%s' "$out" | grep -qiE 'error|refus|must|unknown|invalid'; then
      ok "NC18 ($label) refused  ($(printf '%s' "$out" | tail -1 | cut -c1-70))"
    else
      bad "NC18 ($label) was ACCEPTED: $out"
    fi
  done

  # A mid-day boundary, and a window inside the settlement lag. Both are D20
  # rules and both must name themselves.
  out="$(as "$RESEARCH_USER" "qf extract --target wait_time \
      --train-start $NC18_TRAIN_START --as-of 2026-08-20T06:00:00Z" 2>&1 || true)"
  if printf '%s' "$out" | grep -qi 'boundary'; then
    ok "NC18 a mid-day as_of is refused, naming the boundary rule"
  else
    bad "NC18 a mid-day as_of was not refused by name: $out"
  fi

  out="$(as "$RESEARCH_USER" "qf extract --target wait_time \
      --train-start $(date -u -d 'yesterday' +%Y-%m-%dT00:00:00Z) \
      --as-of $(date -u +%Y-%m-%dT00:00:00Z)" 2>&1 || true)"
  if printf '%s' "$out" | grep -qi 'settle'; then
    ok "NC18 a window inside the settlement lag is refused, naming the lag"
  else
    bad "NC18 an unsettled window was not refused by name: $out"
  fi

  # IMMUTABILITY. Re-request and require the SAME artifact, byte for byte.
  if [ -n "$dir" ] && [ -d "$dir" ]; then
    local before after
    before="$(cd "$dir" && sha256sum ./*.parquet MANIFEST.json 2>/dev/null | sort)"
    local rid2 final2
    rid2="$(as "$RESEARCH_USER" "qf extract --target wait_time \
        --train-start $NC18_TRAIN_START --as-of $NC18_AS_OF" 2>/dev/null | tail -1)"
    final2="$(wait_terminal "$rid2" 1800)"
    after="$(cd "$dir" && sha256sum ./*.parquet MANIFEST.json 2>/dev/null | sort)"
    if [ "$final2" = "SUCCEEDED" ] && [ "$before" = "$after" ]; then
      ok "NC18 re-requesting the same window serves the same bytes"
    else
      bad "NC18 a re-request changed the published extract (state $final2)"
    fi
    # ONE artifact per request, which is what makes the above true.
    local count
    count="$(ls -1d "$(dirname "$dir")"/*/ 2>/dev/null | wc -l)"
    if [ "$count" -ge 1 ]; then
      ok "NC18 $count published extract(s); a re-request added none"
    fi
  else
    void "NC18 the recorded extract_dir does not exist ($dir)"
  fi

  # THE PROTOCOL OFFERS NO RE-EXTRACTION AT ALL, which is stronger than the
  # planned clause ("a forced second extraction is refused"). `force` exists in
  # the extractor's own API and is deliberately unreachable from the wire, so a
  # caller cannot ask for it by any means.
  # `force=`, NOT `force`. Searching for the WORD matched five times, all of
  # them prose -- "in force on the live cluster", "enforced per process",
  # "unenforceable", "enforces_peer_uid" -- so a correctly written service was
  # reported as passing a force flag. Fifth time in this phase that a static scan
  # of mine has matched its own documentation; the durable fix is to grep for the
  # SYNTAX a caller would have to write, which prose cannot contain by accident.
  local svc="$TRUSTED/tools/queue-forecasting/host/extractor/service.py"
  if grep -q 'run(raw_request)' "$svc" && ! grep -q 'force=' "$svc"; then
    ok "NC18 the protocol exposes no way to force a re-extraction"
  else
    bad "NC18 the service passes a force flag from the wire"
    grep -n 'force=' "$svc" | sed 's/^/        /'
  fi

  # SLOW CLAUSES. A real extraction is ~11 minutes, so these are opt-in -- and
  # SAID SO rather than skipped quietly, because a suite that silently drops a
  # control reads as coverage.
  if [ "$NC_SLOW" = 1 ]; then
    local rid3 final3
    rid3="$(as "$RESEARCH_USER" "qf extract --target wait_time \
        --train-start $NC18_TRAIN_START --as-of $NC18_AS_OF --generation 2" \
        2>/dev/null | tail -1)"
    final3="$(wait_terminal "$rid3" 3600)"
    assert_eq "NC18 (slow) a new generation extracts again" "SUCCEEDED" "$final3"

    # SEPARATENESS, asserted on the IDENTITIES rather than on a directory count.
    #
    # The clause used to require the count to INCREASE, which made it valid
    # exactly once per host: extracts are immutable and reused by request_hash
    # (D20), so the second time this suite runs, generation 2 for this window is
    # already published, the extraction is a reuse hit, and no directory appears.
    # It passed on its first run and then reported a FAILURE the second time --
    # for the reason that the reuse it exists to protect was working.
    #
    # And the count was never the property anyway: it would have risen for any
    # unrelated extract published in the same interval, and it says nothing
    # about the two artifacts being DIFFERENT. Comparing the two runs' recorded
    # request hashes does, and it holds however many times this runs.
    local h1 h2 d1 d2
    h1="$(pin_of "$rid" request_hash)"
    h2="$(pin_of "$rid3" request_hash)"
    d1="$(pin_of "$rid" extract_dir)"
    d2="$(pin_of "$rid3" extract_dir)"
    if [ -z "$h1" ] || [ -z "$h2" ]; then
      void "NC18 (slow) a generation run recorded no request_hash (gen1='$h1' gen2='$h2')"
    elif [ "$h1" = "$h2" ]; then
      bad "NC18 (slow) generation 2 has the SAME request hash as generation 1 ($h1): generation is not part of the identity"
    elif [ ! -d "$d1" ] || [ ! -d "$d2" ]; then
      bad "NC18 (slow) the two generations do not both exist on disk ($d1, $d2)"
    elif [ "$d1" = "$d2" ]; then
      bad "NC18 (slow) both generations resolved to one directory ($d1)"
    else
      ok "NC18 (slow) generation 2 is a SEPARATE artifact, and generation 1 survives it"
    fi
  else
    echo "  note: NC18's generation and concurrency clauses need a real"
    echo "        ~11-minute extraction each and are OPT-IN: re-run with"
    echo "        NC_SLOW=1 to include them. They are covered by unit tests"
    echo "        (extractor: TestThereIsExactlyOneArtifactPerRequest;"
    echo "        dispatcher: TestAnExtractDoesNotHoldTheTrainingMutex)."
  fi
}

# =========================================================================
# NC16 -- the container protocol: create, prove, THEN start.
#
# `docker run` was replaced by `docker create` + `docker start --attach` so that
# "the container exists" is a fact the dispatcher establishes while it still
# holds the phase gate (review round 6). Two things that change can only be
# checked against a real daemon:
#
#   * `docker start --attach` must still relay the CONTAINER's exit status. If
#     it did not, every failing candidate would read as SUCCEEDED -- the most
#     dangerous possible regression, because it is silent.
#   * `--rm` is now set at create time, so removal must still happen.
#
# The probe is a pytest path that does not exist: pytest exits non-zero for it
# whatever state the trainer's own suite is in, so the assertion does not depend
# on the repository being green.
# =========================================================================
nc16() {
  echo
  echo "== NC16: create-then-start relays the exit status =="
  local sha rid final code klass leftover live absent
  # FIRST, the probe itself, against a name that certainly does not exist.
  # On 2026-08-26 this was the whole failure: `is_running` read absence out of
  # the WORDING of docker inspect's stderr, Docker 29 words it differently, and
  # every run ended CLEANUP_BLOCKED with admissions frozen. The row-release
  # assertion at the end of this function would have caught it -- but only via a
  # run that could no longer complete, so the suite would have reported a
  # confusing downstream failure instead of the one-line cause. Asking the
  # primitive directly costs nothing and names the real thing.
  absent="$(PYTHONPATH="$DISPATCHER" python3 -c \
    'import qfd; print(qfd.Docker().is_running("qf-nc16-certainly-absent"))' \
    2>/dev/null)"
  assert_eq "NC16 the probe reads a nonexistent container as positively absent" \
    "False" "${absent:-unknown}"
  sha="$(head_sha)"
  if [ -z "$sha" ]; then void "NC16 canary: no mirror HEAD"; return; fi
  rid="$(submit_as "$RESEARCH_USER" --kind test --sha "$sha" \
          --path "tests/qf-nc16-no-such-path")"
  if [ -z "$rid" ]; then void "NC16 canary: submit produced no run id"; return; fi
  final="$(wait_terminal "$rid" 900)"
  if [ "$final" = "TIMEOUT_WAITING" ]; then
    void "NC16 canary: the probe never reached a terminal state"
    return
  fi

  code="$(field_of "$rid" exit_code)"
  klass="$(field_of "$rid" error_class)"
  assert_eq "NC16 the probe is FAILED" "FAILED" "$final"
  # bad_invocation, NOT nonzero_exit. The probe deliberately names a path that
  # does not exist, which is pytest's USAGE ERROR (exit 4) -- and exit 4 now
  # routes to `bad_invocation`, because "the experiment ran and failed" and "the
  # experiment never ran" send an operator to different places. This clause read
  # `nonzero_exit` until the classifier gained that distinction, so it was
  # asserting the old behaviour of the thing it tests.
  #
  # The exit-1 route (tests ran and failed -> nonzero_exit) is covered by
  # TestExitCodeClassification rather than here: producing a genuinely failing
  # test on the host would need another qf-research fixture, and the mapping is
  # pure arithmetic on the exit code.
  assert_eq "NC16 a usage error is classified bad_invocation" \
    "bad_invocation" "$klass"
  # The whole point: a status was relayed, and it was not zero. "None" here
  # means the client never reported one, which is exactly the failure mode
  # `docker start --attach` would introduce if it did not wait properly.
  if [ -n "$code" ] && [ "$code" != "None" ] && [ "$code" != "0" ]; then
    ok "NC16 the container's exit status was relayed (exit_code=$code)"
  else
    bad "NC16 no non-zero exit status was relayed (exit_code='$code')"
  fi

  # --rm at create time still removes: nothing of ours may survive a terminal
  # run, or the next incarnation of the same name would collide.
  leftover="$(docker ps -a --filter "name=qf-$rid-" --format '{{.Names}} {{.Status}}' 2>/dev/null)"
  if [ -z "$leftover" ]; then
    ok "NC16 no container survived the terminal run"
  else
    bad "NC16 containers left behind: $leftover"
    printf '%s\n' "$leftover" | sed 's/^/    /'
  fi

  live="$(sqlite3 "$STATE_DIR/state.db" \
    "SELECT count(*) FROM resources WHERE run_id='$rid' AND released_at IS NULL;" \
    2>/dev/null)"
  assert_eq "NC16 every resource row was released" "0" "${live:-unknown}"
}

# =========================================================================
# NC14 -- the dispatcher's own token cannot write.
# =========================================================================
nc14() {
  echo
  echo "== NC14: the dispatcher's token is read-only =="
  local token_file="${QFD_TOKEN_FILE:-/etc/qf-dispatch/github-token}"
  if [ ! -r "$token_file" ]; then void "NC14 canary: $token_file unreadable"; return; fi

  # Read into a 0700 scratch dir, never argv.
  local scratch; scratch="$(mktemp -d)"; chmod 700 "$scratch"
  cp "$token_file" "$scratch/token"; chmod 400 "$scratch/token"
  local repo="${QFD_REPO:-lotas/qf-research}"
  local hdr="$scratch/hdr"
  printf 'Authorization: Bearer %s\n' "$(cat "$scratch/token")" > "$hdr"
  chmod 400 "$hdr"

  local code
  code="$(curl -sS -o /dev/null -w '%{http_code}' -H @"$hdr" \
    "https://api.github.com/repos/$repo" 2>/dev/null)"
  if [ "$code" = "200" ]; then
    ok "NC14 canary: authenticated GET returns 200 (the credential works)"
  else
    void "NC14 canary: GET returned $code -- refusals below would prove nothing"
    rm -rf "$scratch"; return
  fi

  # R1: a real push to a DISPOSABLE ref, never the deploy branch.
  local pushdir="$scratch/push"
  if as "$DEPLOY_USER" "git -C $STATE_DIR/mirror.git rev-parse HEAD" >/dev/null 2>&1; then :; fi
  git clone --quiet --depth 1 "https://api:$(cat "$scratch/token")@github.com/$repo" \
    "$pushdir" >/dev/null 2>&1
  if [ -d "$pushdir/.git" ]; then
    local r1 rc1
    r1="$(cd "$pushdir" && git push origin \
      "HEAD:refs/heads/nc14-disposable-$(date +%s)" 2>&1)"
    rc1=$?
    score "NC14 R1 smart-HTTP push to a disposable ref" "$(score_git "$rc1" "$r1")"
  else
    void "NC14 R1 canary: clone failed, so a push refusal proves nothing"
  fi

  # R2: POST /git/refs with BOTH required fields, so a 422 cannot masquerade.
  local body r2 r3
  body="$(curl -sS -w '\n%{http_code}' -X POST -H @"$hdr" \
    -H 'Accept: application/vnd.github+json' \
    -d "{\"ref\":\"refs/heads/nc14-r2-$(date +%s)\",\"sha\":\"$(head_sha)\"}" \
    "https://api.github.com/repos/$repo/git/refs" 2>&1)"
  r2="$(printf '%s' "$body" | tail -1)"
  score "NC14 R2 POST /git/refs with a valid payload" "$(score_http "$r2")"

  # R3: POST /pulls with both preflight conditions satisfied.
  body="$(curl -sS -w '\n%{http_code}' -X POST -H @"$hdr" \
    -H 'Accept: application/vnd.github+json' \
    -d '{"title":"nc14","head":"main","base":"main","body":"nc14"}' \
    "https://api.github.com/repos/$repo/pulls" 2>&1)"
  r3="$(printf '%s' "$body" | tail -1)"
  score "NC14 R3 POST /pulls (both preflight conditions satisfied)" "$(score_http "$r3")"

  rm -rf "$scratch"
}

# =========================================================================
# NC15 -- disk containment.
# =========================================================================
nc15() {
  echo
  echo "== NC15: disk containment =="
  local sha; sha="$(head_sha)"

  # The CANARY needs the fixture branch too, and that is a correction rather
  # than a tidy-up. It used to submit a plain `test` job and ask whether
  # artifacts appeared -- but an ordinary pytest run writes nothing to /out, and
  # the 2a allowlist is `result.json` and nothing else, so the canary voided on a
  # perfectly working handoff. Producing an artifact takes a fixture that writes
  # one.
  local fx
  if ! fx="$(fixture_sha)"; then
    void "NC15 needs the Task 13 fixture branch: a 40-hex sha in $NC12_SHA_FILE; missing or malformed is VOID, not skip"
  else
    local rid final art

    # Canary: a well-behaved job's artifact lands at 0640 qfd:qfclient and is
    # readable as research -- only true if the pre-create / --group-add / chmod
    # sequence of Task 6 is right. Without it every refusal below could be
    # measuring a handoff that never produced anything.
    local good; good="$(submit_as "$RESEARCH_USER" --kind test --sha "$fx" \
      --path research/experiments/artifact_good.py)"
    wait_terminal "$good" 1800 >/dev/null
    art="$RUNS_DIR/$good/artifacts"
    if [ -d "$art" ] && [ -n "$(ls -A "$art" 2>/dev/null)" ]; then
      local mode owner group
      mode="$(stat -c '%a' "$art"/* | head -1)"
      owner="$(stat -c '%U' "$art"/* | head -1)"
      group="$(stat -c '%G' "$art"/* | head -1)"
      assert_eq "NC15 canary: artifact mode" "640" "$mode"
      assert_eq "NC15 canary: artifact owner" "qfd" "$owner"
      assert_eq "NC15 canary: artifact group" "qfclient" "$group"
      canary_as "$RESEARCH_USER" "NC15 canary: research can read an artifact" \
        "cat $art/* > /dev/null"
    else
      void "NC15 canary: no artifacts were produced"
    fi

    rid="$(submit_as "$RESEARCH_USER" --kind test --sha "$fx" \
      --path research/experiments/log_flood.py)"
    final="$(wait_terminal "$rid" 1800)"
    assert_eq "NC15 log flood is killed" "FAILED" "$final"
    assert_eq "NC15 log flood error_class" "log_overflow" "$(field_of "$rid" error_class)"
    local biggest
    biggest="$(find "$RUNS_DIR/$rid/logs" -type f -printf '%s\n' 2>/dev/null | sort -n | tail -1)"
    if [ -n "$biggest" ] && [ "$biggest" -le $(( (LOG_CAP_MB + 1) * 1024 * 1024 )) ]; then
      ok "NC15 no log file exceeded ${LOG_CAP_MB}MiB (largest ${biggest}B)"
    else
      bad "NC15 a log file exceeded the cap (${biggest}B)"
    fi

    rid="$(submit_as "$RESEARCH_USER" --kind test --sha "$fx" \
      --path research/experiments/disk_flood.py)"
    final="$(wait_terminal "$rid" 1800)"
    echo "  disk flood finished $final ($(field_of "$rid" error_class))"
    local used
    used="$(du -sm "$RUNS_DIR/$rid" 2>/dev/null | cut -f1)"
    echo "  run directory is ${used}MiB"
    # ASSERTED AGAINST THE DISK FLOOR, not against a multiple of the quota.
    #
    # The old bound was `cap * 3`, and across five runs the run directory
    # finished at 1.9x, 2.1x, 2.9x, 3.4x and 3.7x the 2048 MiB quota -- so the
    # clause failed on a run where containment had worked exactly as designed.
    # Raising the multiple to fit the observation would have been fitting the
    # test to the data.
    #
    # The property that actually matters is that the host survives: OUT_QUOTA
    # stops a runaway (asserted above, by the job being FAILED with
    # `out_quota_exceeded`), and the dispatcher's 20 GiB floor is what keeps the
    # filesystem usable while it is being stopped. A sampled quota cannot be
    # exact; the floor does not depend on sampling.
    #
    # The overshoot is PRINTED rather than asserted, so the quota's real meaning
    # stays visible instead of being hidden behind a tolerance.
    local floor_mb_nc15=$(( ${QFD_DISK_FLOOR_GB:-20} * 1024 ))
    local cap_mb=${QFD_ARTIFACT_CAP_MB:-2048}
    if [ -n "$used" ]; then
      echo "  overshoot: ${used}MiB written against a ${cap_mb}MiB quota"       \
           "($(( used * 10 / cap_mb ))/10 x), sampled every"                    \
           "${QFD_OUT_SAMPLE_INTERVAL_S:-0.5}s"
    fi
    if [ -n "$used" ] && [ "$used" -lt "$floor_mb_nc15" ]; then
      ok "NC15 the flood stayed well below the ${floor_mb_nc15}MiB disk floor"
    else
      bad "NC15 the run directory reached ${used}MiB, at or past the disk floor"
    fi
    # Reclaim the flood FILE only, after the assertion has read its size. The
    # prune timer is 90 days, and leaving ~2GiB of deliberate garbage on a host
    # whose disk floor gates admissions would mean this suite degrades the thing
    # it just measured. Logs, artifacts and the event record all stay.
    rm -f "$RUNS_DIR/$rid/out/nc15-flood.bin"

    # HOSTILE MODES. A candidate can leave its artifact 0600, which qfd -- owner
    # of the directory but not of the file -- can neither read nor chmod. The
    # handoff runs as the candidate's own uid precisely so it always can
    # (design D9), so this must still yield a readable 0640 copy.
    rid="$(submit_as "$RESEARCH_USER" --kind test --sha "$fx" \
      --path research/experiments/artifact_mode_0600.py)"
    final="$(wait_terminal "$rid" 1800)"
    assert_eq "NC15 a 0600 artifact still SUCCEEDS" "SUCCEEDED" "$final"
    art="$RUNS_DIR/$rid/artifacts/result.json"
    if [ -s "$art" ]; then
      assert_eq "NC15 the 0600 artifact was normalised to 0640" "640" \
        "$(stat -c '%a' "$art")"
      canary_as "$RESEARCH_USER" "NC15 research can read the 0600 artifact" \
        "cat $art > /dev/null"
    else
      bad "NC15 a 0600 artifact produced no readable copy"
    fi

    # HOSTILE FILE TYPES. Both must be refused, and -- the part worth measuring
    # -- refused by the TYPE GUARD rather than by the timeout. `wc -c` and `cat`
    # on a FIFO both block for ever, so if the guard were removed these would
    # wedge until HANDOFF_TIMEOUT_S. Asserting the class AND the elapsed time
    # separately is what distinguishes "the guard worked" from "the backstop
    # caught it": the plan's text expected the FIFO case to terminate AT the
    # timeout, which would mean the guard had not run.
    for fixture in artifact_symlink artifact_fifo; do
      rid="$(submit_as "$RESEARCH_USER" --kind test --sha "$fx" \
        --path "research/experiments/$fixture.py")"
      final="$(wait_terminal "$rid" 1800)"
      assert_eq "NC15 $fixture is FAILED" "FAILED" "$final"
      assert_eq "NC15 $fixture is refused as a bad type" "handoff_bad_type" \
        "$(field_of "$rid" error_class)"
      local wall; wall="$(field_of "$rid" wall_s)"
      # Integer compare on a float field: cut at the point.
      if [ -n "$wall" ] && [ "${wall%%.*}" -lt "$HANDOFF_TIMEOUT_S" ]; then
        ok "NC15 $fixture was refused by the type guard, not the timeout (${wall%%.*}s < ${HANDOFF_TIMEOUT_S}s)"
      else
        bad "NC15 $fixture took ${wall}s, at or past HANDOFF_TIMEOUT_S; the type guard did not do the refusing"
      fi
      if [ -s "$RUNS_DIR/$rid/artifacts/result.json" ]; then
        bad "NC15 $fixture still produced artifact CONTENT"
      else
        ok "NC15 $fixture copied nothing into artifacts/"
      fi
    done
  fi

  # Admission floor: with the floor above actual free space, nothing is admitted.
  local free_mb floor_gb
  free_mb="$(df -m --output=avail "$RUNS_DIR" | tail -1 | tr -d ' ')"
  floor_gb=$(( free_mb / 1024 + 10 ))
  mkdir -p /etc/systemd/system/qf-dispatch.service.d
  printf '[Service]\nEnvironment=QFD_DISK_FLOOR_GB=%d\n' "$floor_gb" \
    > /etc/systemd/system/qf-dispatch.service.d/nc15-floor.conf
  systemctl daemon-reload && systemctl restart qf-dispatch && sleep 5

  # CANARY THAT GATES. This clause raises the floor by writing a drop-in and
  # restarting -- two steps, either of which can silently not happen (a stale
  # unit, a restart that came back on the old environment, a daemon-reload that
  # did not). If the floor is NOT in force, a job is admitted for the ordinary
  # reason that there is plenty of disk, and the clause used to report that as
  # "a job was admitted below the disk floor" -- naming a control as broken on
  # evidence that never involved it.
  #
  # `ping` reports the resource gate at the smallest reservation any kind can
  # ask for, so a blocking floor is visible from outside. Nothing is asserted
  # about admission unless the daemon first agrees the floor is blocking.
  local gate; gate="$(as "$RESEARCH_USER" "qf --json ping" 2>/dev/null \
    | sed -n 's/.*"resource": *"\([^"]*\)".*/\1/p' | head -1)"
  echo "  floor raised to ${floor_gb}GiB; ping resource: ${gate:-<unreadable>}"
  case "$gate" in
    "disk floor"*)
      local blocked
      blocked="$(submit_as "$RESEARCH_USER" --kind test --sha "$sha")"
      require_state_for "$blocked" QUEUED 20
      case $? in
        0) ok  "NC15 a job stays QUEUED with free space below the floor" ;;
        1) bad "NC15 a job left QUEUED for $LAST_LEFT_FOR below the disk floor" ;;
        *) void "NC15 a job stays QUEUED below the floor  (could not watch it)" ;;
      esac
      as "$RESEARCH_USER" "qf cancel $blocked" >/dev/null 2>&1
      ;;
    ok)
      void "NC15 the raised floor never took effect (ping says resources are ok)"
      ;;
    *)
      void "NC15 could not read the resource gate from ping (${gate:-empty})"
      ;;
  esac
  rm -f /etc/systemd/system/qf-dispatch.service.d/nc15-floor.conf
  systemctl daemon-reload && systemctl restart qf-dispatch && sleep 5
}


# =========================================================================
# NC19: a promoted baseline is immutable, and a probe records which one it read.
#
# The extract half of this argument is NC18's. This is the other input: a
# baseline is what a residual cohort's numbers are MEASURED AGAINST, so a
# baseline that can change after publication does not corrupt a prediction --
# it corrupts a comparison, which is worse, because the prediction still looks
# right.
# =========================================================================
nc19() {
  echo
  echo "== NC19: the promoted baseline is immutable, and probes record it =="

  local store="${QF_BASELINE_STORE:-/var/lib/qf-baselines}"
  local promoter="$HERE/promote-baseline.sh"

  local fx
  if ! fx="$(fixture_sha)"; then
    void "NC19 needs the fixture branch: a 40-hex sha in $NC12_SHA_FILE"
    return
  fi

  # An extract to probe against. Reuse serves it: NC18 published this window.
  local ex
  ex="$(as "$RESEARCH_USER" "qf --json extracts" 2>/dev/null | python3 -c "
import json, sys
rows = json.load(sys.stdin).get('extracts') or []
print(rows[0]['request_hash'] if rows else '')" 2>/dev/null)"
  if [ -z "$ex" ]; then
    void "NC19 canary: no extract is published, so no probe can run"
    return
  fi

  # CANARY: THE FIXTURE IS DEPLOYED, and this doubles as clause (e).
  #
  # It runs FIRST, before anything is promoted, because if
  # `baseline_contract.py` is not on the fixture branch every probe below fails
  # for that reason -- and the clauses would report it as the baseline mount
  # being broken. A control blamed for a fixture nobody pushed is the same
  # false-failure shape as the mutex probe's sixteen unrelated voids, and the
  # cost is an investigation into working code.
  #
  # The no-baseline run is the right canary because it needs nothing this group
  # sets up: no promotion, no hash. And it is a clause in its own right --
  # absence is a control too, since a baseline must not be AMBIENT, or a cohort
  # could compare against data its record does not name.
  local rid final log
  rid="$(as "$RESEARCH_USER" "qf probe --sha $fx \
      --path research/experiments/baseline_contract.py --extract $ex" 2>&1 \
      | tail -1)"
  if ! is_run_id "$rid"; then
    void "NC19 canary: the no-baseline probe produced no run id: $rid"
    return
  fi
  final="$(wait_terminal "$rid" 3600)"
  log="$(cat "$RUNS_DIR/$rid/logs/"* 2>/dev/null)"
  if ! printf '%s' "$log" | grep -q "== BASELINE-CONTRACT:"; then
    # NO SUMMARY AT ALL means the script never ran to completion -- almost
    # always because the fixture branch does not carry it. Named as such, with
    # the remedy, rather than reported as a failed control.
    void "NC19 canary: the probe printed no BASELINE-CONTRACT summary (state
  $final). If research/experiments/baseline_contract.py is not on the fixture
  branch, re-run host/nc-fixtures-phase2b.sh, push it, and update
  $NC12_SHA_FILE."
    printf '%s\n' "$log" | tail -5 | sed 's/^/    /'
    return
  fi
  assert_eq "NC19 (e) a probe without a baseline SUCCEEDS" "SUCCEEDED" "$final"
  if printf '%s' "$log" | grep -q "BASELINE-CONTRACT: present=0 pass=[0-9]* fail=0 "; then
    ok "NC19 (e) no baseline was mounted, and none was lying around"
  else
    bad "NC19 (e) a probe that asked for no baseline saw one, or failed a clause"
    printf '%s\n' "$log" | grep -E '^(FAIL|== BASELINE)' | sed 's/^/    /'
  fi
  assert_eq "NC19 (e) and it is pinned as none, not left absent" "none" \
    "$(pin_of "$rid" baseline)"

  # --- promote a small baseline ------------------------------------------
  # SYNTHESISED rather than taken from the nightly's output, and the reason is
  # scope: the promoter's own suite already validates real-shaped sets against
  # `describe`. What NC19 is about is the boundary AFTER promotion -- immutable,
  # mounted read-only, recorded in the run -- and a two-day set exercises every
  # one of those in seconds.
  local src; src="$(mktemp -d -t nc19-baseline.XXXXXX)"
  python3 - "$src" <<'PYEOF'
import json, os, sys
d = sys.argv[1]
with open(os.path.join(d, "baseline_predictions.ndjson"), "w") as fh:
    for i in range(4):
        fh.write(json.dumps({"task_id": f"nc19-{i}", "run_id": 0, "p50": 1.0,
                             "pending_at": f"2026-08-0{i + 1}T00:00:00+00:00"})
                 + "\n")
for day in ("2026-08-01", "2026-08-02"):
    with open(os.path.join(d, day + ".json"), "w") as fh:
        json.dump({"day": day, "rows": 2}, fh)
PYEOF
  chmod -R a+rX "$src"

  local out bhash
  out="$("$promoter" "$src" 2>&1)"
  bhash="$(printf '%s' "$out" | sed -n 's/.*published \([0-9a-f]\{12\}\).*/\1/p' \
    | head -1)"
  if [ -z "$bhash" ]; then
    void "NC19 canary: the promoter published nothing: $(printf '%s' "$out" | tr '\n' ' ' | cut -c1-200)"
    rm -rf "$src"
    return
  fi
  local full; full="$(find "$store" -mindepth 1 -maxdepth 1 -type d \
    -name "$bhash*" -printf '%f\n' 2>/dev/null | head -1)"
  if [ -z "$full" ]; then
    void "NC19 canary: nothing under $store matches $bhash"
    rm -rf "$src"
    return
  fi
  ok "NC19 canary: a baseline promotes ($bhash)"

  # (a) DOUBLE PROMOTION IS ONE ARTIFACT. The identity is a content key, so the
  # second run has nothing to publish -- and rewriting would open a window in
  # which a mounted baseline was incomplete.
  local before after
  before="$(find "$store/$full" -type f -exec sha256sum {} \; | sort)"
  "$promoter" "$src" >/dev/null 2>&1 || true
  after="$(find "$store/$full" -type f -exec sha256sum {} \; | sort)"
  local dirs; dirs="$(find "$store" -mindepth 1 -maxdepth 1 -type d \
    -name "$bhash*" | wc -l)"
  [ "$dirs" = 1 ] && ok "NC19 (a) promoting the same files twice is one artifact" \
    || bad "NC19 (a) a second promotion created $dirs artifacts"
  [ "$before" = "$after" ] \
    && ok "NC19 (a) the published bytes were not rewritten" \
    || bad "NC19 (a) a re-promotion rewrote the artifact"

  # (b) THE STORE IS NOT WRITABLE by either non-root domain. This is the
  # boundary the promoter refuses to run without, asserted from outside it.
  refuse_as "$RESEARCH_USER" "NC19 (b) research cannot write to the store" \
    "touch $store/.nc19-research"
  refuse_as "$DEPLOY_USER" "NC19 (b) the nightly user cannot write to the store" \
    "touch $store/.nc19-deploy"
  refuse_as "$RESEARCH_USER" "NC19 (b) research cannot alter a published manifest" \
    "printf x >> $store/$full/MANIFEST.json"

  # (c) `qf baselines` LISTS IT, with the full hash on a copyable line. The same
  # defect as `qf extracts` had: printing only the short form makes the natural
  # copy-paste the value the validator refuses.
  local listing; listing="$(as "$RESEARCH_USER" "qf baselines" 2>&1)"
  if printf '%s' "$listing" | grep -q -- "--baseline $full"; then
    ok "NC19 (c) qf baselines prints the full hash on its own line"
  else
    bad "NC19 (c) qf baselines did not print a copyable --baseline line"
  fi

  # (d) A PROBE WITH A BASELINE. The fixture reports what it SAW; this clause
  # asserts that against what it ASKED FOR, which is why the fixture prints
  # `present=`. A fixture that assumed a baseline would pass for the wrong
  # reason on the run that asks for none.
  rid="$(as "$RESEARCH_USER" "qf probe --sha $fx \
      --path research/experiments/baseline_contract.py \
      --extract $ex --baseline $full" 2>&1 | tail -1)"
  if ! is_run_id "$rid"; then
    void "NC19 (d) the probe produced no run id: $rid"
  else
    final="$(wait_terminal "$rid" 3600)"
    assert_eq "NC19 (d) a probe with a baseline SUCCEEDS" "SUCCEEDED" "$final"
    log="$(cat "$RUNS_DIR/$rid/logs/"* 2>/dev/null)"
    if printf '%s' "$log" | grep -q "BASELINE-CONTRACT: present=1 pass=[0-9]* fail=0 "; then
      ok "NC19 (d) the in-sandbox contract holds with a baseline mounted"
    else
      bad "NC19 (d) the in-sandbox contract failed: $(printf '%s' "$log" | grep -c '^FAIL') FAIL line(s)"
      printf '%s\n' "$log" | grep '^FAIL' | sed 's/^/    /'
    fi
    # THE PROVENANCE. Promised and not recorded is worse than absent: the record
    # looks complete. This is the same gap the extract pins had.
    local pinned; pinned="$(pin_of "$rid" baseline_hash)"
    assert_eq "NC19 (d) the run records which baseline it read" "$full" "$pinned"
    local pdir; pdir="$(pin_of "$rid" baseline_dir)"
    [ "$pdir" = "$store/$full" ] \
      && ok "NC19 (d) and the directory it was mounted from" \
      || bad "NC19 (d) baseline_dir is '$pdir', not $store/$full"
  fi

  # (f) AN UNPROMOTED BASELINE IS REFUSED BEFORE ANYTHING STARTS. Its own error
  # class, because "the baseline is not promoted" is a request to fix and
  # "the extraction failed" is a subsystem to look at.
  rid="$(as "$RESEARCH_USER" "qf probe --sha $fx \
      --path research/experiments/baseline_contract.py \
      --extract $ex --baseline $(printf 'f%.0s' $(seq 64))" 2>&1 | tail -1)"
  if ! is_run_id "$rid"; then
    # The client resolves a prefix and refuses an unknown FULL hash only at the
    # dispatcher, so an absent run id here means the client refused first --
    # which is also correct, and is what this clause is about either way.
    #
    # NOT "its directory is missing": that was true of every job that had not
    # been leased yet, so this branch claimed the refusal for jobs the
    # dispatcher had accepted and was about to run.
    ok "NC19 (f) an unpromoted baseline never became a run"
  else
    final="$(wait_terminal "$rid" 600)"
    assert_eq "NC19 (f) an unpromoted baseline is FAILED" "FAILED" "$final"
    assert_eq "NC19 (f) with its own error class" "baseline_not_published" \
      "$(field_of "$rid" error_class)"
    # BEFORE THE IMAGE BUILD, which is the whole reason the pin happens early.
    # A build takes minutes; a refusal must cost seconds.
    local wall; wall="$(field_of "$rid" wall_s)"
    if [ -n "$wall" ] && [ "$wall" != "$UNREADABLE" ] \
        && [ "${wall%%.*}" -lt "$BUILD_SETTLE_S" ]; then
      ok "NC19 (f) refused in ${wall%%.*}s, before any image work"
    else
      bad "NC19 (f) took ${wall}s: the reference was checked too late"
    fi
  fi

  # (g) AN EDITED BASELINE IS REFUSED. The hash is a content key, so this is the
  # one input whose declared identity can be VERIFIED rather than trusted -- and
  # an edit that leaves `baseline_hash` alone is invisible to any check that
  # only compares the directory name to the manifest's field.
  local manifest="$store/$full/MANIFEST.json"
  local saved; saved="$(mktemp -t nc19-manifest.XXXXXX)"
  cp -p "$manifest" "$saved"
  python3 - "$manifest" <<'PYEOF'
import json, sys
p = sys.argv[1]
with open(p) as fh:
    m = json.load(fh)
m["ndjson_rows"] = (m.get("ndjson_rows") or 0) + 1000   # leaves baseline_hash
with open(p, "w") as fh:
    json.dump(m, fh)
PYEOF
  rid="$(as "$RESEARCH_USER" "qf probe --sha $fx \
      --path research/experiments/baseline_contract.py \
      --extract $ex --baseline $full" 2>&1 | tail -1)"
  if ! is_run_id "$rid"; then
    void "NC19 (g) the edited-baseline probe produced no run id: $rid"
  else
    final="$(wait_terminal "$rid" 600)"
    assert_eq "NC19 (g) a baseline edited after promotion is FAILED" "FAILED" \
      "$final"
    assert_eq "NC19 (g) as a baseline that is not published" \
      "baseline_not_published" "$(field_of "$rid" error_class)"
  fi
  cp -p "$saved" "$manifest"
  rm -f "$saved"
  # And the restore has to have worked, or every later run of this suite fails
  # for a reason this clause caused.
  rid="$(as "$RESEARCH_USER" "qf --json baselines" 2>/dev/null | python3 -c "
import json, sys
rows = json.load(sys.stdin).get('baselines') or []
print(sum(1 for r in rows if r.get('broken')))" 2>/dev/null)"
  [ "$rid" = "0" ] \
    && ok "NC19 (g) the store was left intact" \
    || bad "NC19 (g) $rid baseline(s) are still broken after the restore"

  rm -rf "$src"
}

# =========================================================================
# NC9: the rule a result is judged by lives in the trusted checkout.
#
# Design negative control 9, deferred from 2a to 2c because it needs a contract
# to exist. The property: a job cannot choose the bar it clears. Everything about
# HOW a result is judged -- the slice, the metrics, the thresholds, the baseline
# -- lives in a root-owned file the candidate cannot edit, named by a content
# hash that both qfd and the evaluator resolve independently.
# =========================================================================
nc9() {
  echo
  echo "== NC9: the evaluation contract is trusted, not supplied =="

  local contracts_dir="${QFD_CONTRACTS_DIR:-$TRUSTED/tools/queue-forecasting/host/contracts}"

  # CANARY: at least one contract resolves. Every refusal below is measured
  # against a contract that works -- without this, a resolver that returned
  # nothing would satisfy all of them.
  local listing ch
  listing="$(as "$RESEARCH_USER" "qf contracts" 2>&1)"
  ch="$(printf '%s' "$listing" | sed -n 's/^ *--contract \([0-9a-f]\{64\}\)$/\1/p' \
    | head -1)"
  if [ -z "$ch" ]; then
    void "NC9 canary: no contract resolves. Templates in $contracts_dir carry an
  unpinned baseline and are refused by design -- run instantiate-contract.sh
  against a promoted baseline and commit the result. Listing was:
  $(printf '%s' "$listing" | tr '\n' ' ' | cut -c1-200)"
    return
  fi
  ok "NC9 canary: a contract resolves ($(printf '%s' "$ch" | cut -c1-12))"

  # (a) THE STORE IS NOT WRITABLE by either non-root domain. A rule the
  # candidate can edit is not a rule, and this is the layer beneath the hash.
  refuse_as "$RESEARCH_USER" "NC9 (a) research cannot add a contract" \
    "touch $contracts_dir/nc9.json"
  refuse_as "$RESEARCH_USER" "NC9 (a) research cannot edit a contract" \
    "printf x >> $contracts_dir/$(cd "$contracts_dir" && ls *.json 2>/dev/null | head -1)"
  refuse_as "$DEPLOY_USER" "NC9 (a) the nightly user cannot add a contract" \
    "touch $contracts_dir/nc9-deploy.json"

  # (b) A CONTRACT THAT IS NOT IN THE CHECKOUT IS REFUSED AT SUBMIT, before a
  # job exists. The client resolves a prefix, so a full unknown hash is what
  # reaches the dispatcher.
  local out
  out="$(as "$RESEARCH_USER" "qf evaluate --run probe-20260829T000000Z-000000000000-1 \
      --contract $(printf 'f%.0s' $(seq 64))" 2>&1 || true)"
  if printf '%s' "$out" | grep -qi 'trusted checkout'; then
    ok "NC9 (b) an untrusted contract hash is refused at submit"
  else
    bad "NC9 (b) an untrusted contract was not refused by name: $(printf '%s' "$out" | tr '\n' ' ' | cut -c1-200)"
  fi
  # And it names what IS available, because "unknown contract" with no list is
  # unactionable.
  printf '%s' "$out" | grep -q "$(printf '%s' "$ch" | cut -c1-12)" \
    && ok "NC9 (b) the refusal lists what the checkout carries" \
    || bad "NC9 (b) the refusal did not list the available contracts"

  # (c) NO POLICY CROSSES THE WIRE. A caller that could pass a bar could pass
  # its own bar, so the spec accepts exactly `run` and `contract`.
  for field in baseline bar mae threshold metrics holdout_days; do
    out="$(as "$RESEARCH_USER" "python3 - <<'EOF'
import json, socket
s = socket.socket(socket.AF_UNIX); s.connect('$CLIENT_SOCK')
spec = {'schema': 1, 'kind': 'evaluate',
        'args': {'run': 'probe-20260829T000000Z-000000000000-1',
                 'contract': '$ch', '$field': 1}}
s.sendall(json.dumps({'op': 'submit', 'payload': {'spec': spec}}).encode() + b'\n')
print(s.recv(65536).decode())
EOF" 2>&1 || true)"
    if printf '%s' "$out" | grep -q "$field"; then
      ok "NC9 (c) args.$field is refused by name"
    else
      bad "NC9 (c) args.$field was not refused: $(printf '%s' "$out" | tr '\n' ' ' | cut -c1-160)"
    fi
  done

  # (d) A TRUSTED CONTRACT IS ACCEPTED. The canary for (b) and (c) at the submit
  # boundary rather than at the resolver: a submit path that refused every
  # evaluate job would pass both of them. The job is expected to FAIL -- the run
  # it names does not exist -- and the point is WHICH failure.
  local rid
  rid="$(as "$RESEARCH_USER" "qf evaluate \
      --run probe-20260829T000000Z-000000000000-1 --contract $ch" 2>&1 | tail -1)"
  if ! is_run_id "$rid"; then
    bad "NC9 (d) a trusted contract was refused at submit: $(printf '%s' "$rid" | cut -c1-160)"
  else
    ok "NC9 (d) a trusted contract is accepted at submit"
    local final; final="$(wait_terminal "$rid" 600)"
    assert_eq "NC9 (d) and the job fails on the RUN, not the contract" \
      "evaluate_input_missing" "$(field_of "$rid" error_class)"
    assert_eq "NC9 (d) the job reached a terminal state" "FAILED" "$final"
    # THE CONTRACT IS PINNED even on a failure: a verdict-less run still has to
    # say what rule it was going to be judged by, or the record cannot be read.
    assert_eq "NC9 (d) the contract is pinned regardless" "$ch" \
      "$(pin_of "$rid" contract_hash)"
  fi

  # (e) THE EVALUATOR RESOLVES IT INDEPENDENTLY. qfd's check is for legibility;
  # the authoritative one is in the other domain, and a control enforced only by
  # the process in the `docker` group is not a control.
  out="$(as "$DEPLOY_USER" "python3 - <<'EOF'
import json, socket
try:
    s = socket.socket(socket.AF_UNIX); s.connect('/run/qf-eval/sock')
    s.sendall(json.dumps({'op': 'ping'}).encode() + b'\n')
    print(s.recv(65536).decode())
except Exception as e:
    print('UNREACHABLE', e)
EOF" 2>&1 || true)"
  if printf '%s' "$out" | grep -q 'UNREACHABLE\|permission denied\|Permission denied'; then
    ok "NC9 (e) the nightly user cannot reach the evaluator socket"
  else
    bad "NC9 (e) a non-dispatcher uid reached the evaluator: $(printf '%s' "$out" | tr '\n' ' ' | cut -c1-160)"
  fi
  refuse_as "$RESEARCH_USER" "NC9 (e) research cannot reach the evaluator socket" \
    "python3 -c \"import socket; socket.socket(socket.AF_UNIX).connect('/run/qf-eval/sock')\""

  # (f) THE EVALUATOR CANNOT WRITE WHAT IT JUDGES BY. Asserted from outside, as
  # the unit's ReadWritePaths and the store's mode together.
  local unit=/etc/systemd/system/qf-eval.service
  if [ -f "$unit" ]; then
    local rw; rw="$(grep '^ReadWritePaths=' "$unit" | head -1)"
    case "$rw" in
      *qf-extracts*|*qf-baselines*|*contracts*)
        bad "NC9 (f) qf-eval.service can write an input it judges by: $rw" ;;
      *) ok "NC9 (f) qf-eval.service writes only its own output ($rw)" ;;
    esac
  else
    void "NC9 (f) qf-eval.service is not installed"
  fi
}
# =========================================================================
main() {
  [ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 2; }
  echo "== Phase 2a negative controls =="
  if [ -z "$DEPLOY_USER" ]; then
    echo "cannot identify the nightly user: no crontab schedules" >&2
    echo "daily_walk_forward.sh. NC8 is entirely about the protocol between qfd" >&2
    echo "and that account, so guessing one would produce clauses that pass or" >&2
    echo "fail for reasons unrelated to the mutex. Set DEPLOY_USER=<user> and" >&2
    echo "re-run." >&2
    exit 2
  fi
  echo "trusted=$TRUSTED deploy=$DEPLOY_USER research=$RESEARCH_USER"
  preflight
  echo "lock=$LOCK intent=$INTENT_DIR"

  nc8
  nc9
  nc10
  nc11
  nc12
  nc13
  nc14
  nc15
  nc16
  nc17
  nc18
  nc19

  echo
  echo "== totals: pass=$pass fail=$fail =="
  if [ "$fail" -gt 0 ]; then
    printf 'failed: %s\n' "${FAILED_NAMES[@]}"
  fi

  # A PARTLY BLIND RUN HAS NO TOTALS. If the instrument failed even once, the
  # pass count is not a weaker result than a clean one -- it is a different kind
  # of thing, because a clause that could not observe its subject reports `ok`
  # for every negative property it was asked about. Saying so here, and in the
  # evidence file, is the difference between a result and a misleading artifact.
  local blind; blind="$(blind_count)"
  if [ -n "$blind" ] && [ "$blind" -gt 0 ]; then
    echo
    echo "== THE INSTRUMENT WAS BLIND $blind TIME(S); THESE TOTALS DO NOT STAND ==" >&2
    echo "distinct reasons:" >&2
    sort -u "$BLIND_FILE" | sed 's/^/  /' >&2
    echo "Fix the reason above and re-run. Do not record this as evidence." >&2
  fi

  {
    echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) nc-suite-phase2.sh ==="
    echo "host=$(hostname) trusted=$TRUSTED"
    echo "dispatcher commit: $(as "$RESEARCH_USER" 'qf ping' 2>/dev/null | grep -i commit || echo unknown)"
    if [ -n "$blind" ] && [ "$blind" -gt 0 ]; then
      echo "VOID RUN: the state instrument failed $blind time(s); totals below are"
      echo "  not evidence. Distinct reasons:"
      sort -u "$BLIND_FILE" | sed 's/^/    /'
    fi
    echo "pass=$pass fail=$fail"
    [ "$fail" -gt 0 ] && printf 'failed: %s\n' "${FAILED_NAMES[@]}"
    echo
  } >> "$EVIDENCE"

  # Phase 1 §7.2: the suite checks its OWN output for secrets before exiting.
  if grep -qE 'gh[pousr]_[A-Za-z0-9]{20,}|://[^/[:space:]]+:[^@[:space:]]+@' "$EVIDENCE"; then
    echo "REFUSING TO EXIT CLEAN: the evidence file contains a secret-shaped string" >&2
    exit 3
  fi

  # Blindness is a failure of the run even if every clause happened to pass.
  [ "$fail" -eq 0 ] && [ "${blind:-0}" -eq 0 ]
}

main "$@"
