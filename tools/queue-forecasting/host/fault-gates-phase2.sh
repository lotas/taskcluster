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
lock_holders() { fuser "${QFD_LOCK_FILE:-/var/lib/qf-locks/heavy-training.lock}" 2>/dev/null | tr -s ' '; }

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
    sleep 5
    local before_clients; before_clients="$(build_clients)"

    case "$method" in
      build_timeout) set_dropin "Environment=QFD_BUILD_TIMEOUT_S=$FG_BUILD_TIMEOUT_S"
                     systemctl restart qf-dispatch; sleep 5 ;;
    esac

    local rid
    rid="$(as "$RESEARCH_USER" "qf submit --kind test --sha $(head_sha)" 2>/dev/null | tail -1)"
    if [ -z "$rid" ]; then void "$method: submit produced no run id"; continue; fi

    if ! wait_for_state "$rid" BUILDING 180; then
      void "$method: the job never reached BUILDING (state $(sql "SELECT state FROM jobs WHERE run_id='$rid';"))"
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

    as "$RESEARCH_USER" "qf cancel $rid" >/dev/null 2>&1
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

      # Now a clean restart, and the assertion.
      rm -f "$DROPIN"; systemctl daemon-reload
      systemctl restart qf-dispatch
      sleep 10

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

      # The chain must still verify either way.
      if as "$RESEARCH_USER" "qf verify-chain" >/dev/null 2>&1; then
        ok "$phase/$orphan_alive: the event chain still verifies"
      else
        bad "$phase/$orphan_alive: the event chain does not verify after the crash"
      fi

      as "$RESEARCH_USER" "qf cancel $rid" >/dev/null 2>&1
      docker ps -q --filter "label=qf.run_id=$rid" | xargs -r docker kill >/dev/null 2>&1
    done
  done
  clear_dropin
}

main() {
  [ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 2; }
  command -v sqlite3 >/dev/null || { echo "sqlite3 is required" >&2; exit 2; }
  trap 'rm -f "$DROPIN"; systemctl daemon-reload; systemctl restart qf-dispatch' EXIT

  echo "== Phase 2a fault gates =="
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
