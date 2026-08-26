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
DEPLOY_USER="${DEPLOY_USER:-$(stat -c '%U' "$TRUSTED" 2>/dev/null || echo deploy)}"
LOCK="${QFD_LOCK_FILE:-/var/lib/qf-locks/heavy-training.lock}"
INTENT_DIR="${QFD_INTENT_DIR:-/var/lib/qf-locks/intent.d}"
MIGRATED_MARKER="${QFD_LOCK_MIGRATED_MARKER:-/etc/qf-dispatch/lock-migrated}"
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

refuse_as() {  # refuse_as <user> <name> <command...> -> passes when it FAILS
  local user="$1" name="$2"; shift 2
  if as "$user" "$*" >/dev/null 2>&1; then
    bad "$name  (action was PERMITTED)"
  else
    ok "$name  (refused)"
  fi
}

canary_as() {  # canary_as <user> <name> <command...> -> passes when it SUCCEEDS
  local user="$1" name="$2"; shift 2
  if as "$user" "$*" >/dev/null 2>&1; then
    ok "$name  (canary: the attempt is possible)"
  else
    void "$name"
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

submit_as() {  # submit_as <user> <args...> -> prints the run id
  as "$1" "qf submit ${*:2}" 2>/dev/null | tail -1
}

state_of() { as "$RESEARCH_USER" "qf status $1 --json" 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["job"]["state"])' 2>/dev/null; }

field_of() { as "$RESEARCH_USER" "qf status $1 --json" 2>/dev/null \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['job'].get('$2'))" 2>/dev/null; }

wait_state() {  # wait_state <run_id> <state> <timeout_s>
  local rid="$1" want="$2" limit="$3" waited=0
  while [ "$waited" -lt "$limit" ]; do
    [ "$(state_of "$rid")" = "$want" ] && return 0
    sleep 2; waited=$((waited + 2))
  done
  return 1
}

wait_terminal() {
  local rid="$1" limit="$2" waited=0 st
  while [ "$waited" -lt "$limit" ]; do
    st="$(state_of "$rid")"
    case "$st" in
      SUCCEEDED|FAILED|TIMEOUT|CANCELLED|REFUSED) echo "$st"; return 0 ;;
    esac
    sleep 2; waited=$((waited + 2))
  done
  echo "TIMEOUT_WAITING"; return 1
}

require_state_for() {  # require_state_for <run_id> <state> <seconds>
  local rid="$1" want="$2" secs="$3" waited=0 st
  while [ "$waited" -lt "$secs" ]; do
    st="$(state_of "$rid")"
    if [ "$st" != "$want" ]; then
      echo "  (left $want for $st after ${waited}s)" >&2
      return 1
    fi
    sleep 3; waited=$((waited + 3))
  done
  return 0
}

# `-c safe.directory=` is load-bearing, not defensive. The mirror is owned by
# qfd and this runs as the deploy user, and modern git REFUSES a repository
# owned by someone else ("detected dubious ownership") -- with stderr discarded,
# that came back as an empty string, and an empty string here VOIDs NC13 and
# NC15 with "no mirror HEAD" on a perfectly healthy host. A blanket
# `--global safe.directory` would fix it too and is worse: it would leave the
# exception behind for everything the deploy user ever touches.
head_sha() { as "$DEPLOY_USER" \
  "git -c safe.directory=$STATE_DIR/mirror.git -C $STATE_DIR/mirror.git rev-parse refs/remotes/origin/main" \
  2>/dev/null; }

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

standin_nightly() {  # standin_nightly <wait_s> -> background PID that waits then holds
  ( exec 9>"$LOCK"; flock -w "$1" 9 && sleep 60 ) &
  echo $!
}

# =========================================================================
# NC8 -- the mutex, seventeen clauses, thirteen of them found by review.
# =========================================================================
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
  # The lock's directory is 0755 root:root, so neither runtime user can unlink
  # or recreate the inode, and qfd refuses to start when it is missing.
  for u in "$DEPLOY_USER" qfd; do
    canary_as "$u" "(perm) $u can open the lock for write" "exec 9>$LOCK"
    refuse_as "$u" "(perm) $u cannot unlink the lock" "rm -f $LOCK"
  done
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
  # The directory's setgid bit is what makes this work.
  local umask_marker="$INTENT_DIR/nightly.4321.$(date +%s).intent"
  as "$DEPLOY_USER" "umask 077; printf 'pid=4321\ndeadline=%d\n' $(( $(date +%s) + 60 )) > $umask_marker"
  if as qfd "cat $umask_marker" >/dev/null 2>&1; then
    ok "(h) a marker written under umask 077 is still readable by qfd"
  else
    bad "(h) qfd cannot read a umask-077 marker -- it would admit straight through it"
  fi
  rm -f "$umask_marker"

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
  if require_state_for "$rid_a" QUEUED 15; then
    ok "(refusal) a heavy job stays QUEUED while the lock is held elsewhere"
  else
    bad "(refusal) a heavy job left QUEUED while the lock was held"
  fi
  wait "$holder" 2>/dev/null
  if wait_state "$rid_a" RUNNING 120 || [ "$(state_of "$rid_a")" != "QUEUED" ]; then
    ok "(refusal) it starts once the lock is released"
  else
    bad "(refusal) it never started after the lock was released"
  fi
  wait_terminal "$rid_a" 900 >/dev/null

  # --- exclusion: two heavy jobs are never both RUNNING -------------------
  rid_a="$(submit_as "$RESEARCH_USER" --kind test --sha "$(head_sha)" --mem 8g)"
  rid_b="$(submit_as "$RESEARCH_USER" --kind test --sha "$(head_sha)" --mem 8g)"
  local both=0 i=0
  while [ "$i" -lt 60 ]; do
    if [ "$(state_of "$rid_a")" = RUNNING ] && [ "$(state_of "$rid_b")" = RUNNING ]; then
      both=1; break
    fi
    sleep 2; i=$((i + 2))
  done
  if [ "$both" -eq 0 ]; then
    ok "(exclusion) two heavy jobs are never both RUNNING"
  else
    bad "(exclusion) two heavy jobs ran concurrently"
  fi

  # --- budget: a 22g heavy and a 4g light never overlap -------------------
  local big small
  big="$(submit_as "$RESEARCH_USER" --kind test --sha "$(head_sha)" --mem 22g)"
  small="$(submit_as "$RESEARCH_USER" --kind test --sha "$(head_sha)" --mem 4g)"
  both=0; i=0
  while [ "$i" -lt 60 ]; do
    if [ "$(state_of "$big")" = RUNNING ] && [ "$(state_of "$small")" = RUNNING ]; then
      both=1; break
    fi
    sleep 2; i=$((i + 2))
  done
  if [ "$both" -eq 0 ]; then
    ok "(budget) a 22g heavy and a 4g light never run concurrently"
  else
    bad "(budget) 26g of admitted memory ran at once on a ~29g host"
  fi
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
  wait_state "$light" RUNNING 300
  local t0 t1 sp
  t0=$(date +%s)
  sp="$(standin_nightly 300)"
  sleep 5
  if kill -0 "$sp" 2>/dev/null; then
    ok "(a) a stand-in nightly waits rather than exiting"
  else
    bad "(a) the stand-in nightly exited instead of waiting"
  fi
  wait_terminal "$light" 900 >/dev/null
  if wait "$sp" 2>/dev/null; then t1=$(date +%s); ok "(a) it proceeded after $((t1 - t0))s"; else
    bad "(a) it never acquired the lock"; fi

  # (b) STARVATION, tested by actively trying to barge while the waiter is queued.
  light="$(submit_as "$RESEARCH_USER" --kind test --sha "$sha" --mem 2g)"
  wait_state "$light" RUNNING 300
  local marker="$INTENT_DIR/nightly.$$.$(date +%s).intent"
  printf 'pid=%d\ndeadline=%d\n' "$$" "$(( $(date +%s) + 600 ))" > "$marker"
  chmod 0640 "$marker"
  sp="$(standin_nightly 600)"
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
  if wait "$sp" 2>/dev/null; then ok "(b) nightly entered once the running job drained"; else
    bad "(b) nightly never entered"; fi

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
  if require_state_for "$probe" QUEUED 20; then
    ok "(h) an unparseable marker fails closed"
  else
    bad "(h) an unparseable marker was admitted through"
  fi
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
  wait_state "$l1" RUNNING 300; wait_state "$l2" RUNNING 300
  sp="$(standin_nightly 600)"
  wait_terminal "$l1" 900 >/dev/null
  sleep 10
  if kill -0 "$sp" 2>/dev/null; then
    ok "(c) the second light job's LOCK_SH survived the first's exit"
  else
    bad "(c) one job's exit released another's shared lock"
  fi
  wait_terminal "$l2" 900 >/dev/null
  wait "$sp" 2>/dev/null

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
  canary_as "$DEPLOY_USER" "(g4) deploy reaches the admin socket" \
    "qfadmin --help"
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
nc10() {
  echo
  echo "== NC10: trusted paths resolve only from the trusted checkout =="
  local json
  json="$(as "$RESEARCH_USER" "qf trusted-paths --json" 2>/dev/null)"
  if [ -z "$json" ]; then
    void "NC10 canary: qf trusted-paths returned nothing"
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
  assert_eq "NC16 it is classified nonzero_exit" "nonzero_exit" "$klass"
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
    if [ -n "$used" ] && [ "$used" -le $(( ${QFD_ARTIFACT_CAP_MB:-2048} * 3 )) ]; then
      ok "NC15 the output quota bound held"
    else
      bad "NC15 the run directory grew past its bound (${used}MiB)"
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
  local blocked; blocked="$(submit_as "$RESEARCH_USER" --kind test --sha "$sha")"
  if require_state_for "$blocked" QUEUED 20; then
    ok "NC15 a job stays QUEUED with free space below the floor"
  else
    bad "NC15 a job was admitted below the disk floor"
  fi
  rm -f /etc/systemd/system/qf-dispatch.service.d/nc15-floor.conf
  systemctl daemon-reload && systemctl restart qf-dispatch && sleep 5
  as "$RESEARCH_USER" "qf cancel $blocked" >/dev/null 2>&1
}

# =========================================================================
main() {
  [ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 2; }
  echo "== Phase 2a negative controls =="
  echo "trusted=$TRUSTED deploy=$DEPLOY_USER research=$RESEARCH_USER"
  echo "lock=$LOCK intent=$INTENT_DIR"

  nc8
  nc10
  nc12
  nc13
  nc14
  nc15
  nc16

  echo
  echo "== totals: pass=$pass fail=$fail =="
  if [ "$fail" -gt 0 ]; then
    printf 'failed: %s\n' "${FAILED_NAMES[@]}"
  fi

  {
    echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) nc-suite-phase2.sh ==="
    echo "host=$(hostname) trusted=$TRUSTED"
    echo "dispatcher commit: $(as "$RESEARCH_USER" 'qf ping' 2>/dev/null | grep -i commit || echo unknown)"
    echo "pass=$pass fail=$fail"
    [ "$fail" -gt 0 ] && printf 'failed: %s\n' "${FAILED_NAMES[@]}"
    echo
  } >> "$EVIDENCE"

  # Phase 1 §7.2: the suite checks its OWN output for secrets before exiting.
  if grep -qE 'gh[pousr]_[A-Za-z0-9]{20,}|://[^/[:space:]]+:[^@[:space:]]+@' "$EVIDENCE"; then
    echo "REFUSING TO EXIT CLEAN: the evidence file contains a secret-shaped string" >&2
    exit 3
  fi

  [ "$fail" -eq 0 ]
}

main "$@"
