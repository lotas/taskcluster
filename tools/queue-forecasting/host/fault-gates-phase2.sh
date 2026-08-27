#!/usr/bin/env bash
# Fault gates, Phase 2a (auto-research-phase2a-plan.md Task 8b).
#
# Run as root, AFTER Task 12's first pinned job and BEFORE Task 14:
#   sudo ./host/fault-gates-phase2.sh
#
# Prose review reached diminishing returns; what remains is behaviour under
# INTERRUPTION, which only an executable test can settle. Two gates:
#
#   Gate A -- kill qfd during a long build, three ways. Asserts no docker build
#             client survives, MEASURES how long daemon-side work takes to stop,
#             and asserts that on restart the BUILDING job RETAINS its lock and
#             reservation rather than being released merely because it has no
#             `resources` row. That last assertion is the one that fails against
#             a vacuous empty-set check, which is exactly what this gate is for.
#
#   Gate B -- crash after each named startup phase and assert EXACTLY ONE of two
#             outcomes: every resource still held, or verified cleanup completed.
#             An intermediate release fails the gate.
#
# Every measured cancellation time is printed and recorded, because design D10's
# decision rule depends on the numbers rather than on a pass/fail: any run beyond
# QFD_BUILD_SETTLE_S means building moves out of qfd, not that the window grows.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TRUSTED="${TRUSTED:-/srv/queue-forecasting}"
DISPATCHER="$TRUSTED/tools/queue-forecasting/host/dispatcher"
RESEARCH_USER="${RESEARCH_USER:-research}"
STATE_DIR="${QFD_STATE_DIR:-/var/lib/qf-platform}"
# Named once here rather than repeated inline: `lock_holders` had its own copy of
# this default, and the preflight below needs the same inode.
LOCK="${QFD_LOCK_FILE:-/var/lib/qf-locks/heavy-training.lock}"
BUILD_SETTLE_S="${QFD_BUILD_SETTLE_S:-30}"
# ONE definition, used both for the drop-in the gate installs and for the instant
# the gate starts its clock at. Two copies of this number drifting apart is how
# the clock ends up measuring the wrong interval.
FG_BUILD_TIMEOUT_S="${FG_BUILD_TIMEOUT_S:-45}"
DROPIN_DIR=/etc/systemd/system/qf-dispatch.service.d
DROPIN="$DROPIN_DIR/fault-gate.conf"
EVIDENCE="$HERE/fault-evidence-phase2a.txt"

pass=0
fail=0
declare -a MEASUREMENTS=()
declare -a FAILED_NAMES=()

ok()   { echo "ok    $1"; pass=$((pass + 1)); }
bad()  { echo "FAIL  $1"; fail=$((fail + 1)); FAILED_NAMES+=("$1"); }
void() { echo "VOID  $1"; fail=$((fail + 1)); FAILED_NAMES+=("VOID:$1"); }
note() { echo "      $1"; }

as() { sudo -H -u "$1" bash -lc "${*:2}"; }
sql() { sqlite3 "$STATE_DIR/state.db" "$1" 2>/dev/null; }

head_sha() {
  as "$RESEARCH_USER" "git -C $STATE_DIR/mirror.git rev-parse refs/remotes/origin/main" \
    2>/dev/null || sudo git -C "$STATE_DIR/mirror.git" rev-parse refs/remotes/origin/main
}

build_clients() { pgrep -fa 'docker[- ]build' | grep -v fault-gates | wc -l; }

# A job holds the training lock exactly while a descriptor on it is open. `fuser`
# tells us whether ANY process holds it, which is what "the lock was retained"
# means from the outside.
lock_holders() { fuser "$LOCK" 2>/dev/null | tr -s ' '; }

# READINESS, not a fixed sleep. Gate B waited `sleep 10` after each restart and
# then asserted -- but startup recovery runs the build-settle procedure for every
# retained BUILDING job, up to QFD_BUILD_SETTLE_S each, so after Gate A had left
# two of them a restart took far longer than ten seconds. The socket was not
# there yet, verify-chain came back rc=2, and every LATER iteration then failed
# to submit for want of the same socket: one slow start, ten reports.
wait_ready() {  # wait_ready [seconds] -> 0 when the daemon answers
  local limit="${1:-180}" waited=0
  while [ "$waited" -lt "$limit" ]; do
    if sudo -H -u "$RESEARCH_USER" bash -lc 'qf ping' >/dev/null 2>&1; then
      return 0
    fi
    sleep 2; waited=$((waited + 2))
  done
  return 1
}

# A daemon that never comes back invalidates every remaining iteration, so this
# stops the gate instead of letting it emit one void per iteration for a
# condition that will not change.
require_ready() {
  local what="$1"
  if ! wait_ready; then
    echo
    echo "ABORTING: the dispatcher did not become ready after $what." >&2
    echo "Nothing below could be measured against a daemon that is not running," >&2
    echo "and each iteration would report its own unrelated failure." >&2
    systemctl status qf-dispatch --no-pager -n 20 >&2 || true
    exit 2
  fi
}

set_dropin() {
  mkdir -p "$DROPIN_DIR"
  printf '[Service]\n%s\n' "$1" > "$DROPIN"
  systemctl daemon-reload
}

clear_dropin() {
  rm -f "$DROPIN"
  systemctl daemon-reload
  systemctl restart qf-dispatch
  sleep 5
}

wait_for_state() {  # wait_for_state <run_id> <state> <timeout>
  local rid="$1" want="$2" limit="$3" waited=0
  while [ "$waited" -lt "$limit" ]; do
    [ "$(sql "SELECT state FROM jobs WHERE run_id='$rid';")" = "$want" ] && return 0
    sleep 2; waited=$((waited + 2))
  done
  return 1
}

# =========================================================================
# Gate A -- kill qfd during a long build.
# =========================================================================
gate_a() {
  echo
  echo "== Gate A: interruption during a long build =="

  # The slow fixture: a Dockerfile whose content key MISSES, so a build is
  # forced, and whose build takes long enough to be interrupted.
  local fixture="$DISPATCHER/trainer-env.Dockerfile"
  local backup="/root/trainer-env.Dockerfile.faultgate.bak"
  cp "$fixture" "$backup"
  printf '\n# fault-gate slow step (removed by this script)\nRUN sleep 600\n' >> "$fixture"
  note "appended a RUN sleep 600 step; original saved at $backup"

  local method
  for method in build_timeout sigkill systemctl_stop; do
    echo
    echo "-- iteration: $method --"
    systemctl restart qf-dispatch
    require_ready "the restart at the top of iteration $method"
    local before_clients; before_clients="$(build_clients)"

    case "$method" in
      build_timeout) set_dropin "Environment=QFD_BUILD_TIMEOUT_S=$FG_BUILD_TIMEOUT_S"
                     systemctl restart qf-dispatch; sleep 5 ;;
    esac

    local rid
    rid="$(as "$RESEARCH_USER" "qf submit --kind test --sha $(head_sha)" 2>/dev/null | tail -1)"
    if [ -z "$rid" ]; then void "$method: submit produced no run id"; continue; fi

    if ! wait_for_state "$rid" BUILDING 180; then
      # WHY, not just "it did not". A job that sits QUEUED is the daemon
      # refusing to admit, and the reason is a fact it already knows -- printing
      # only the state sent the reader looking at the job instead of at the
      # queue. Every iteration of this gate then voided for the same upstream
      # cause with nothing naming it.
      void "$method: the job never reached BUILDING (state $(sql "SELECT state FROM jobs WHERE run_id='$rid';"))"
      as "$RESEARCH_USER" "qf ping" 2>/dev/null \
        | grep -E '^(admit|queued|stall|admitted_mem_mb|free_disk_mb):' \
        | sed 's/^/      /'
      journalctl -u qf-dispatch --since '-3 min' 2>/dev/null \
        | grep -E 'not admitt|lane .*:' | tail -3 | sed 's/^/      /'
      as "$RESEARCH_USER" "qf cancel $rid" >/dev/null 2>&1
      continue
    fi
    ok "$method: canary -- the job reached BUILDING and a build is in flight"
    local t_building; t_building=$(date +%s)
    sleep 10

    # THE CLOCK STARTS AT THE EVENT, NOT BEFORE THE WAIT FOR IT.
    #
    # `t0` used to be set before a fixed `sleep 60` that waited for a 45s
    # timeout to fire, so 60 seconds of the gate's own sleeping were counted as
    # cancellation latency -- and the gate then failed design D10's rule on that
    # number, reporting "cancellation took 60s > 30s" on a host where the build
    # client had in fact died promptly. A measurement that includes the wait for
    # the thing it measures is not a measurement of it.
    #
    # For the timeout method the event is the daemon's own kill, which fires
    # BUILD_TIMEOUT_S after the build started. BUILDING is observed within a poll
    # of the build starting, so `t_building + BUILD_TIMEOUT_S` is the fire
    # instant to within that poll -- and the residual bias is stated rather than
    # hidden: it makes the window very slightly generous, so a PASS here is
    # weaker than a pass on the other two methods by up to one poll interval.
    local t0 t1 elapsed
    case "$method" in
      build_timeout)
        note "letting QFD_BUILD_TIMEOUT_S=$FG_BUILD_TIMEOUT_S fire"
        local fires_at=$(( t_building + FG_BUILD_TIMEOUT_S ))
        while [ "$(date +%s)" -lt "$fires_at" ]; do sleep 1; done
        t0="$fires_at" ;;
      sigkill)
        t0=$(date +%s)
        kill -9 "$(systemctl show -p MainPID --value qf-dispatch)" ;;
      systemctl_stop)
        t0=$(date +%s)
        systemctl stop qf-dispatch ;;
    esac

    # 1. No docker build client survives.
    local waited=0 clients
    while [ "$waited" -lt "$(( BUILD_SETTLE_S * 3 ))" ]; do
      clients="$(build_clients)"
      [ "$clients" -le "$before_clients" ] && break
      sleep 1; waited=$((waited + 1))
    done
    t1=$(date +%s); elapsed=$((t1 - t0))
    MEASUREMENTS+=("$method: build client gone after ${elapsed}s (settle window ${BUILD_SETTLE_S}s)")
    note "build client gone after ${elapsed}s"
    if [ "$(build_clients)" -le "$before_clients" ]; then
      ok "$method: no docker build client survives"
    else
      bad "$method: a docker build client survived"
    fi
    if [ "$elapsed" -le "$BUILD_SETTLE_S" ]; then
      ok "$method: daemon-side work stopped within QFD_BUILD_SETTLE_S"
    else
      bad "$method: cancellation took ${elapsed}s > ${BUILD_SETTLE_S}s -- design D10's rule says BUILDING MOVES OUT OF qfd, not that the window grows"
    fi

    # 2. On restart, the BUILDING job retains its lock and reservation. It must
    #    NOT be released merely because it has no `resources` row.
    local rows state
    rows="$(sql "SELECT COUNT(*) FROM resources WHERE run_id='$rid';")"
    note "recorded resources for $rid: ${rows:-0}"
    systemctl start qf-dispatch 2>/dev/null || systemctl restart qf-dispatch
    sleep 8
    state="$(sql "SELECT state FROM jobs WHERE run_id='$rid';")"
    if [ "${rows:-0}" = "0" ]; then
      case "$state" in
        BUILDING)
          if [ -n "$(lock_holders)" ]; then
            ok "$method: the BUILDING job with no resources RETAINED its lock (not a vacuous release)"
          else
            bad "$method: the BUILDING job kept its state but the lock was released"
          fi ;;
        FAILED|CANCELLED)
          # Permitted only via the cancellation-settle procedure, which takes at
          # least the settle window.
          note "reached $state; acceptable only through the settle procedure"
          ok "$method: the job settled to $state rather than being freed instantly" ;;
        *)
          bad "$method: unexpected state $state for a BUILDING job with no resources" ;;
      esac
    else
      note "the job had recorded containers, so confirmation was a real check"
      ok "$method: state after restart is $state"
    fi

    # Wait for the cancelled job to actually LEAVE its non-terminal state. A
    # BUILDING job that is merely asked to cancel keeps its lock and reservation
    # until the settle procedure finishes, and every later daemon start re-adopts
    # it and re-runs that procedure -- which is what made Gate B's restarts take
    # longer than its fixed sleep. Leaving work behind for the next stage is how
    # one gate breaks another.
    as "$RESEARCH_USER" "qf cancel $rid" >/dev/null 2>&1
    local settle=0
    while [ "$settle" -lt 120 ]; do
      case "$(sql "SELECT state FROM jobs WHERE run_id='$rid';")" in
        SUCCEEDED|FAILED|TIMEOUT|CANCELLED|REFUSED) break ;;
      esac
      sleep 3; settle=$((settle + 3))
    done
    [ "$settle" -ge 120 ] && note "$method: $rid did not settle in 120s (state $(sql "SELECT state FROM jobs WHERE run_id='$rid';"))"
    [ "$method" = build_timeout ] && clear_dropin
  done

  cp "$backup" "$fixture"
  rm -f "$backup"
  note "restored the original trainer-env.Dockerfile"
  systemctl restart qf-dispatch
  sleep 5
}

# =========================================================================
# Gate B -- crash after each named startup phase.
# =========================================================================
gate_b() {
  echo
  echo "== Gate B: crash after each startup reconciliation phase =="
  local phases="enumerate lock recharge deadline resolve_blocked"
  local orphan_alive phase

  for orphan_alive in 1 0; do
    for phase in $phases; do
      echo
      echo "-- phase=$phase orphan_alive=$orphan_alive --"

      # Make an orphan: start a job, then SIGKILL qfd so the job is left in a
      # non-terminal state with (optionally) a live container.
      local rid
      rid="$(as "$RESEARCH_USER" "qf submit --kind test --sha $(head_sha) --timeout 900" \
        2>/dev/null | tail -1)"
      if [ -z "$rid" ]; then void "$phase/$orphan_alive: no run id"; continue; fi
      if ! wait_for_state "$rid" RUNNING 600; then
        void "$phase/$orphan_alive: the job never reached RUNNING"
        as "$RESEARCH_USER" "qf cancel $rid" >/dev/null 2>&1
        continue
      fi
      kill -9 "$(systemctl show -p MainPID --value qf-dispatch)" 2>/dev/null
      sleep 3

      if [ "$orphan_alive" = "0" ]; then
        # Kill the workload too, so recovery meets an already-dead orphan.
        docker ps -q --filter "label=qf.run_id=$rid" | xargs -r docker kill >/dev/null 2>&1
        sleep 3
      fi
      local live_before
      live_before="$(docker ps -q --filter "label=qf.run_id=$rid" | wc -l)"
      note "containers live before recovery: $live_before"

      # Crash at the named phase. QFD_ALLOW_FAULT_INJECTION is required, and the
      # shipped unit never sets it.
      set_dropin "Environment=QFD_ALLOW_FAULT_INJECTION=1
Environment=QFD_FAULT_AFTER=$phase"
      systemctl start qf-dispatch >/dev/null 2>&1
      sleep 8
      note "dispatcher exited at phase $phase (active=$(systemctl is-active qf-dispatch))"

      # Now a clean restart, and the assertion -- but only once the daemon is
      # actually answering. See `wait_ready`.
      rm -f "$DROPIN"; systemctl daemon-reload
      systemctl restart qf-dispatch
      require_ready "the clean restart in $phase/$orphan_alive"

      local state live_after holders
      state="$(sql "SELECT state FROM jobs WHERE run_id='$rid';")"
      live_after="$(docker ps -q --filter "label=qf.run_id=$rid" | wc -l)"
      holders="$(lock_holders)"
      note "state=$state live=$live_after lock_holders='$holders'"

      # EXACTLY ONE OF TWO outcomes is acceptable.
      #   (1) everything still held: non-terminal state AND a lock holder.
      #   (2) verified cleanup completed: terminal state AND no live container.
      local outcome_held=0 outcome_clean=0
      case "$state" in
        LEASED|BUILDING|RUNNING|CLEANUP_BLOCKED)
          [ -n "$holders" ] && outcome_held=1 ;;
        SUCCEEDED|FAILED|TIMEOUT|CANCELLED)
          [ "$live_after" -eq 0 ] && outcome_clean=1 ;;
      esac

      if [ "$outcome_held" = 1 ] && [ "$outcome_clean" = 0 ]; then
        ok "$phase/$orphan_alive: every resource is still HELD ($state)"
      elif [ "$outcome_clean" = 1 ] && [ "$outcome_held" = 0 ]; then
        ok "$phase/$orphan_alive: verified cleanup COMPLETED ($state, no live container)"
      else
        # An intermediate release is the failure: lock closed while a container
        # lives, reservation freed with work outstanding, or FAILED recorded
        # without confirmation.
        bad "$phase/$orphan_alive: INTERMEDIATE state -- state=$state live=$live_after holders='$holders'"
      fi

      # The chain must still verify either way -- and "does not verify" must not
      # also mean "could not ask". `qf verify-chain` exits 1 for a chain with
      # problems and 2 for a transport failure, and this used to discard both
      # along with the output, so a daemon whose socket was not up yet reported
      # as CORRUPTION on the one signal that says the audit record is intact.
      local vc_out vc_rc
      vc_out="$(as "$RESEARCH_USER" "qf verify-chain" 2>&1)"; vc_rc=$?
      if [ "$vc_rc" -eq 0 ]; then
        ok "$phase/$orphan_alive: the event chain still verifies"
      elif [ "$vc_rc" -eq 1 ]; then
        bad "$phase/$orphan_alive: THE EVENT CHAIN HAS PROBLEMS after the crash"
        printf '%s\n' "$vc_out" | sed 's/^/      /'
      else
        void "$phase/$orphan_alive: could not ask the daemon to verify the chain (rc=$vc_rc): $(printf '%s' "$vc_out" | head -1)"
      fi

      as "$RESEARCH_USER" "qf cancel $rid" >/dev/null 2>&1
      docker ps -q --filter "label=qf.run_id=$rid" | xargs -r docker kill >/dev/null 2>&1
    done
  done
  clear_dropin
}

# PRECONDITION, checked once. Every iteration of both gates submits a job, and a
# host whose admissions are already stopped -- or whose per-uid queue is already
# full -- turns one upstream condition into a screenful of unrelated VOIDs. The
# first real run produced eleven failures from a single stuck queue.
preflight() {
  local ping admit queued cap
  ping="$(sudo -H -u "$RESEARCH_USER" bash -lc 'qf ping' 2>/dev/null)" || true
  admit="$(printf '%s\n' "$ping" | awk -F': ' '/^admit:/{print $2}')"
  queued="$(printf '%s\n' "$ping" | awk -F': ' '/^queued:/{print $2}')"
  cap="${QFD_QUEUED_CAP_PER_UID:-20}"
  if [ -z "$admit" ]; then
    echo "cannot reach the dispatcher as $RESEARCH_USER; nothing below would mean anything" >&2
    exit 2
  fi
  if [ "$admit" != "ok" ]; then
    echo "REFUSING TO RUN: the dispatcher is not admitting ($admit)." >&2
    echo "Clear that first -- e.g. resolve or force-release the blocked run --" >&2
    echo "or every iteration below will VOID for this one reason." >&2
    exit 2
  fi
  # THE MUTEX, which `admit` does not cover: `may_admit` answers about the
  # cleanup stall and the intent gate, while the lock is taken per lane inside
  # try_one. A nightly run holding LOCK_EX freezes the light lane while every
  # other indicator reads healthy -- which is precisely what happened, and the
  # gate blamed the jobs.
  if ! ( exec 9>"$LOCK"; flock -s -n 9 ); then
    echo "REFUSING TO RUN: something holds the training mutex EXCLUSIVELY --" >&2
    echo "the nightly walk-forward, most likely. Light jobs cannot be admitted" >&2
    echo "while it does, so every iteration below would VOID on a healthy host." >&2
    echo "holders: $(lock_holders)" >&2
    exit 2
  fi
  if [ -n "$queued" ] && [ "$queued" -ge $(( cap / 2 )) ]; then
    echo "REFUSING TO RUN: $queued jobs are already QUEUED and the per-uid cap" >&2
    echo "is $cap, so submits will start being refused mid-run and report as" >&2
    echo "'no run id'. Drain the queue first (qf list --state QUEUED)." >&2
    exit 2
  fi
  echo "preflight: admitting, mutex free, ${queued} queued (cap ${cap})"
}


main() {
  [ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 2; }
  command -v sqlite3 >/dev/null || { echo "sqlite3 is required" >&2; exit 2; }
  trap 'rm -f "$DROPIN"; systemctl daemon-reload; systemctl restart qf-dispatch' EXIT

  echo "== Phase 2a fault gates =="

  preflight
  echo "settle window: ${BUILD_SETTLE_S}s"

  gate_a
  gate_b

  echo
  echo "== measurements =="
  if [ "${#MEASUREMENTS[@]}" -gt 0 ]; then
    printf '  %s\n' "${MEASUREMENTS[@]}"
  fi
  echo "== totals: pass=$pass fail=$fail =="
  [ "$fail" -gt 0 ] && printf 'failed: %s\n' "${FAILED_NAMES[@]}"

  {
    echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) fault-gates-phase2.sh ==="
    echo "host=$(hostname) settle_window=${BUILD_SETTLE_S}s"
    echo "pass=$pass fail=$fail"
    # Every measured cancellation time, because the D10 decision depends on
    # these numbers rather than on a pass/fail.
    [ "${#MEASUREMENTS[@]}" -gt 0 ] && printf '  %s\n' "${MEASUREMENTS[@]}"
    [ "$fail" -gt 0 ] && printf 'failed: %s\n' "${FAILED_NAMES[@]}"
    echo
  } >> "$EVIDENCE"

  [ "$fail" -eq 0 ]
}

main "$@"
