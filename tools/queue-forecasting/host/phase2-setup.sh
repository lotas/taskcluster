#!/usr/bin/env bash
# Phase 2a host setup: the trusted dispatcher, its identity, its locks and its
# units (auto-research-phase2a-plan.md Task 7).
#
# Idempotent subcommands in the phase0-setup.sh idiom:
#   - run as root
#   - --check does a dry run: reports what WOULD change, touches nothing
#   - `discover` first, always; it FAILS rather than warns when a load-bearing
#     invariant is wrong, because every failure mode here is silent
#
# USAGE:
#   sudo ./phase2-setup.sh discover
#   sudo ./phase2-setup.sh dispatch-user [--check]
#   sudo ./phase2-setup.sh locks [--check]
#   sudo ./phase2-setup.sh cron-lock-path
#   sudo ./phase2-setup.sh builder-probe
#   sudo ./phase2-setup.sh runs-dir [--check]
#   sudo ./phase2-setup.sh pin-base
#   sudo ./phase2-setup.sh token /path/to/token [--check]
#   sudo ./phase2-setup.sh install [--check]
#   sudo ./phase2-setup.sh mirror-refresh
#   sudo ./phase2-setup.sh verify
#
# ORDER MATTERS. `locks` and `cron-lock-path` come before `install`, because the
# dispatcher refuses to start without the migration marker, and because an
# unmigrated nightly script plus a dispatcher holding LOCK_SH means skipped
# nightly runs.
#
# ENVIRONMENT:
#   TRUSTED       trusted checkout (default /srv/queue-forecasting)
#   DEPLOY_USER   the deploy account (default: owner of $TRUSTED)
#   RESEARCH_USER the untrusted agent account (default: research)

set -euo pipefail

TRUSTED="${TRUSTED:-/srv/queue-forecasting}"
DISPATCHER="$TRUSTED/tools/queue-forecasting/host/dispatcher"
RESEARCH_USER="${RESEARCH_USER:-research}"
CHECK=0

LOCK_DIR=/var/lib/qf-locks
LOCK_FILE="$LOCK_DIR/heavy-training.lock"
INTENT_DIR="$LOCK_DIR/intent.d"
RUNS_DIR=/var/lib/qf-runs
STATE_DIR=/var/lib/qf-platform
CONF_DIR=/etc/qf-dispatch
MIGRATED_MARKER="$CONF_DIR/lock-migrated"
TOKEN_FILE="$CONF_DIR/github-token"
BASE_IMAGE="ghcr.io/astral-sh/uv:python3.13-bookworm-slim"

# The figures the dispatcher runs with. `discover` checks the host against these
# rather than assuming them.
ADMITTED_MEM_BUDGET_MB=22528
DISK_FLOOR_GB=20
OUT_QUOTA_MB=2048
ARTIFACT_CAP_MB=2048
TIMEOUT_MAX_S=3600
BUILD_TIMEOUT_S=1800
BUILD_LOCK_WAIT_S=900
HANDOFF_TIMEOUT_S=120
SETUP_TEARDOWN_ALLOWANCE_S=600
JOB_HOLD_DEADLINE_S=7800
KILL_CONFIRM_S=300
LOCK_WAIT_S=9000
BUILD_SETTLE_S=30

c_red=$'\033[31m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_dim=$'\033[2m'; c_off=$'\033[0m'

step() { printf '\n%s== %s ==%s\n' "$c_grn" "$1" "$c_off"; }
info() { printf '   %s\n' "$1"; }
skip() { printf '   %sskip%s %s\n' "$c_dim" "$c_off" "$1"; }
warn() { printf '   %swarn%s %s\n' "$c_ylw" "$c_off" "$1"; }
die()  { printf '\n%sSTOP%s %s\n\n' "$c_red" "$c_off" "$1" >&2; exit 1; }

would() {  # would <description> ; returns 0 if we should actually act
  if [ "$CHECK" = 1 ]; then
    printf '   %swould%s %s\n' "$c_ylw" "$c_off" "$1"
    return 1
  fi
  printf '   %s\n' "$1"
  return 0
}

deploy_user() {
  if [ -n "${DEPLOY_USER:-}" ]; then printf '%s' "$DEPLOY_USER"; return; fi
  stat -c '%U' "$TRUSTED" 2>/dev/null || echo deploy
}

# Drop to a user for ONE probe. Deliberately not `sg`: `sg` re-executes the
# target's LOGIN SHELL, and qfd's shell is /usr/sbin/nologin by design (see
# cmd_dispatch_user), so every probe below failed with "This account is
# currently not available." -- a false negative indistinguishable from a
# permission fault, on the one account these checks exist for. `runuser -u`
# execs the command directly and calls initgroups(), which reads /etc/group
# fresh, so a membership added a moment ago is already in effect and no `sg`
# context is needed at all.
as_user() {  # as_user <user> <shell string>
  local u="$1"; shift
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$u" -- /bin/sh -c "$*"
  else
    sudo -u "$u" /bin/sh -c "$*"
  fi
}

require_group() {
  getent group "$1" >/dev/null \
    || die "group $1 does not exist; run dispatch-user and locks first"
}

# --------------------------------------------------------------------------
cmd_discover() {
  local failures=0
  bad() { printf '   %sFAIL%s %s\n' "$c_red" "$c_off" "$1"; failures=$((failures + 1)); }

  step "versions"
  info "python3: $(python3 -V 2>&1)"
  info "docker:  $(docker --version 2>&1 || echo MISSING)"
  command -v docker >/dev/null || bad "docker is not installed"
  command -v flock  >/dev/null || bad "flock is not installed (the mutex depends on it)"
  command -v sqlite3 >/dev/null || warn "sqlite3 absent: the fault gates need it"

  step "cgroup v2"
  if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
    info "cgroup v2 unified hierarchy present"
    grep -q memory /sys/fs/cgroup/cgroup.controllers \
      || bad "the memory controller is not delegated; --memory caps would not hold"
  else
    bad "cgroup v2 not found; memory.current sampling and --memory caps are unreliable"
  fi

  step "trusted checkout"
  [ -d "$TRUSTED" ] || bad "$TRUSTED does not exist"
  info "trusted: $TRUSTED"
  # Phase 1 §4.1: the trusted mirror must not BE the deploy checkout, or a
  # deploy-side edit changes the code that enforces the boundary.
  if [ -f "$TRUSTED/tools/queue-forecasting/docker-compose.yml" ] \
     && docker compose --project-directory "$TRUSTED/tools/queue-forecasting" ps \
        >/dev/null 2>&1; then
    bad "\$TRUSTED looks like the RUNNING deploy checkout; they must be separate (phase1 §4.1)"
  else
    info "trusted checkout is not the running deploy stack"
  fi
  if [ -d "$STATE_DIR/mirror.git" ]; then
    info "mirror HEAD: $(git -C "$STATE_DIR/mirror.git" rev-parse --short \
      refs/remotes/origin/main 2>/dev/null || echo none)"
  else
    info "mirror: not yet created (the dispatcher makes it at first start)"
  fi

  step "lock inode and intent directory"
  if [ -e "$LOCK_FILE" ]; then
    info "lock: $(stat -c '%A %U:%G %d:%i' "$LOCK_FILE")"
    [ "$(stat -c '%a' "$LOCK_FILE")" = "660" ] || bad "$LOCK_FILE is not mode 0660"
    [ "$(stat -c '%G' "$LOCK_FILE")" = "qfheavy" ] || bad "$LOCK_FILE is not group qfheavy"
  else
    warn "$LOCK_FILE absent; run 'locks'"
  fi
  if [ -e "$INTENT_DIR" ]; then
    info "intent: $(stat -c '%A %U:%G' "$INTENT_DIR")"
    # The setgid bit is load-bearing, not decorative: without it a marker's mode
    # comes from the deploy user's umask and qfd could not read the declaration.
    [ "$(stat -c '%a' "$INTENT_DIR")" = "2770" ] \
      || bad "$INTENT_DIR is not mode 2770 (the setgid bit fixes marker groups)"
  else
    warn "$INTENT_DIR absent; run 'locks'"
  fi
  if getent group qfheavy >/dev/null && \
     getent group qfheavy | cut -d: -f4 | tr ',' '\n' | grep -qx "$RESEARCH_USER"; then
    bad "$RESEARCH_USER is in qfheavy; it could hold the mutex indefinitely"
  fi
  if getent group docker | cut -d: -f4 | tr ',' '\n' | grep -qx "$RESEARCH_USER"; then
    bad "$RESEARCH_USER is in the docker group; that is root-equivalent"
  fi

  step "memory budget"
  local ram_mb
  ram_mb=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo) / 1024 ))
  info "host RAM: ${ram_mb}MiB, ADMITTED_MEM_BUDGET=${ADMITTED_MEM_BUDGET_MB}MiB"
  if [ "$ADMITTED_MEM_BUDGET_MB" -ge "$ram_mb" ]; then
    bad "the admitted-memory budget is not below host RAM"
  else
    info "headroom: $(( ram_mb - ADMITTED_MEM_BUDGET_MB ))MiB for the live stack"
  fi

  step "runs filesystem"
  local fstype free_gb probe
  probe="$RUNS_DIR"; [ -d "$probe" ] || probe="$(dirname "$RUNS_DIR")"
  fstype="$(stat -f -c %T "$probe")"
  free_gb=$(( $(df -m --output=avail "$probe" | tail -1) / 1024 ))
  info "filesystem: $fstype, free: ${free_gb}GiB"
  # Design §4.5 measure 3: REPORT what is available rather than assuming it.
  case "$fstype" in
    xfs)
      if command -v xfs_quota >/dev/null; then
        info "xfs + xfs_quota: per-directory PROJECT QUOTAS are available (measure 3)"
      else
        warn "xfs but no xfs_quota; measure 3 unavailable, so the out/ bound is a SAMPLE"
      fi ;;
    ext2/ext3|ext4)
      info "ext4: project quotas possible only if the fs was made with -O project" ;;
    *)
      warn "$fstype: no per-directory quota mechanism known; the out/ bound is a SAMPLE, not a guarantee" ;;
  esac
  if [ "$free_gb" -lt "$(( DISK_FLOOR_GB + (OUT_QUOTA_MB + ARTIFACT_CAP_MB) / 1024 ))" ]; then
    bad "free space is already below the admission floor plus one job's allowance"
  fi
  # flock must actually exclude here, or every mutex clause is vacuous -- and a
  # vacuous mutex reads exactly like a working one.
  if [ -e "$LOCK_FILE" ]; then
    if ( exec 9>"$LOCK_FILE"; flock -n 9 \
         && ( exec 8>"$LOCK_FILE"; flock -n 8 ) && exit 1 || exit 0 ); then
      info "flock excludes correctly on $fstype"
    else
      bad "flock does NOT exclude on $fstype"
    fi
  fi

  step "deadline chain"
  # These numbers move together or not at all, so an inversion FAILS here.
  local chain=$(( TIMEOUT_MAX_S + BUILD_TIMEOUT_S + BUILD_LOCK_WAIT_S \
                  + HANDOFF_TIMEOUT_S + SETUP_TEARDOWN_ALLOWANCE_S ))
  info "phase budget:            ${chain}s"
  info "JOB_HOLD_DEADLINE_S:     ${JOB_HOLD_DEADLINE_S}s"
  info "deadline + kill confirm: $(( JOB_HOLD_DEADLINE_S + KILL_CONFIRM_S ))s"
  info "LOCK_WAIT_S:             ${LOCK_WAIT_S}s"
  [ "$chain" -lt "$JOB_HOLD_DEADLINE_S" ] \
    || bad "the phase budget does not fit inside JOB_HOLD_DEADLINE_S"
  [ "$(( JOB_HOLD_DEADLINE_S + KILL_CONFIRM_S ))" -lt "$LOCK_WAIT_S" ] \
    || bad "LOCK_WAIT_S must exceed JOB_HOLD_DEADLINE_S + KILL_CONFIRM_S, or the nightly run gives up while a kill is still being confirmed"

  step "egress for a new system user"
  # Phase 0's rules are uid-scoped on the research uid, so a new system user is
  # unrestricted -- but VERIFY rather than assume (facts #6).
  if id qfd >/dev/null 2>&1; then
    if sudo -u qfd curl -sS -m 15 -o /dev/null -w '%{http_code}' \
        https://github.com 2>/dev/null | grep -qE '^(200|301|302)$'; then
      info "qfd can reach github.com (no new nftables rule needed)"
    else
      bad "qfd cannot reach github.com; the mirror fetch would fail"
    fi
  else
    skip "user qfd does not exist yet; re-run discover after dispatch-user"
  fi
  info "nftables uid-scoped rules: $(nft list ruleset 2>/dev/null | grep -c skuid || echo 'nft unavailable')"

  echo
  if [ "$failures" -gt 0 ]; then
    die "$failures load-bearing check(s) failed. discover FAILS rather than warns, because every one of these is silent in production."
  fi
  printf '%sdiscover: all checks passed%s\n' "$c_grn" "$c_off"
}

# --------------------------------------------------------------------------
cmd_dispatch_user() {
  step "groups and the qfd identity"
  # gid 10001 is FIXED: it is the in-container group the trusted image creates,
  # which is how out/ is handed over by group (design §4.4).
  getent group qfrun >/dev/null \
    || { would "groupadd -g 10001 qfrun" && groupadd -g 10001 qfrun; }
  getent group qfclient >/dev/null \
    || { would "groupadd qfclient" && groupadd qfclient; }
  if getent group qfrun >/dev/null \
     && [ "$(getent group qfrun | cut -d: -f3)" != "10001" ]; then
    die "group qfrun exists with gid $(getent group qfrun | cut -d: -f3), not 10001; the image bakes 10001 in"
  fi

  if id qfd >/dev/null 2>&1; then
    skip "user qfd exists"
  else
    would "useradd -r qfd" \
      && useradd -r -d "$STATE_DIR" -s /usr/sbin/nologin qfd
  fi
  would "add qfd to docker,qfrun,qfclient" \
    && usermod -aG docker,qfrun,qfclient qfd

  local du; du="$(deploy_user)"
  would "add $RESEARCH_USER and $du to qfclient" \
    && usermod -aG qfclient "$RESEARCH_USER" && usermod -aG qfclient "$du"

  # RE-ASSERT the Phase 0 invariant. Checked here as well as in phase0 because
  # adding groups is exactly the moment someone reaches for `docker`.
  if getent group docker | cut -d: -f4 | tr ',' '\n' | grep -qx "$RESEARCH_USER"; then
    die "$RESEARCH_USER is in the docker group. That is root-equivalent and defeats the entire boundary."
  fi
  info "$RESEARCH_USER is not in the docker group"
}

# --------------------------------------------------------------------------
cmd_locks() {
  step "the shared lock inode and the intent directory"
  local du; du="$(deploy_user)"

  getent group qfheavy >/dev/null || { would "groupadd qfheavy" && groupadd qfheavy; }
  # qfheavy contains qfd and the deploy user, NEVER research: qfclient does
  # contain research, which would let the agent hold the mutex indefinitely.
  would "add qfd and $du to qfheavy" \
    && usermod -aG qfheavy qfd && usermod -aG qfheavy "$du"
  if getent group qfheavy >/dev/null \
     && getent group qfheavy | cut -d: -f4 | tr ',' '\n' | grep -qx "$RESEARCH_USER"; then
    die "$RESEARCH_USER is a member of qfheavy; it could stop nightly training at will"
  fi
  info "$RESEARCH_USER is not in qfheavy"

  if would "install qf-locks.conf and run systemd-tmpfiles"; then
    install -m 0644 "$DISPATCHER/qf-locks.conf" /etc/tmpfiles.d/qf-locks.conf
    systemd-tmpfiles --create /etc/tmpfiles.d/qf-locks.conf
  fi
  [ "$CHECK" = 1 ] && return 0

  [ -e "$LOCK_FILE" ] || die "$LOCK_FILE was not created"
  [ -d "$INTENT_DIR" ] || die "$INTENT_DIR was not created"
  info "lock:   $(stat -c '%A %U:%G' "$LOCK_FILE")"
  info "intent: $(stat -c '%A %U:%G' "$INTENT_DIR")"

  step "verify as BOTH users"
  local u
  for u in qfd "$du"; do
    as_user "$u" "exec 9>$LOCK_FILE" \
      || die "$u cannot open the lock for WRITE; the nightly script's exec 9> would fail fatally"
    info "$u can open the lock for write"
  done
  # A flock held by one side must be SEEN by the other.
  as_user qfd "flock -n $LOCK_FILE -c 'sleep 5'" &
  local holder=$!
  sleep 1
  if as_user "$du" "flock -n $LOCK_FILE -c true" 2>/dev/null; then
    kill "$holder" 2>/dev/null || true
    die "a lock held by qfd was NOT seen by $du; this is not a mutex"
  fi
  info "a lock held by qfd blocks $du"
  wait "$holder" 2>/dev/null || true

  # The deploy user creates markers; qfd unlinks stale ones; research does neither.
  local probe="$INTENT_DIR/nightly.$$.$(date +%s).intent"
  as_user "$du" "printf 'pid=%d\ndeadline=1\n' $$ > $probe" \
    || die "$du cannot create an intent marker"
  info "$du can create a marker"
  as_user qfd "cat $probe > /dev/null" || die "qfd cannot read the marker"
  as_user qfd "rm -f $probe" || die "qfd cannot unlink a stale marker"
  info "qfd can read and unlink a marker"

  # POSITIVE CANARY FIRST. The two refusals below are the point of this step,
  # and a refusal proves nothing unless the same invocation can succeed at
  # something: a nologin shell, a missing user or a broken `sudo` rule would
  # make both of them "pass" while proving the opposite of what they claim.
  # This is the same trap as the `sg` false negative above, in the direction
  # that fails OPEN instead of closed.
  local canary; canary="$(mktemp -d)"
  chmod 0777 "$canary"
  as_user "$RESEARCH_USER" "exec 9>$canary/probe" \
    || die "cannot run anything as $RESEARCH_USER, so the two refusals below would prove nothing"
  rm -rf "$canary"
  info "$RESEARCH_USER probes actually execute (canary)"

  if as_user "$RESEARCH_USER" "touch $INTENT_DIR/nightly.1.1.intent" 2>/dev/null; then
    rm -f "$INTENT_DIR/nightly.1.1.intent"
    die "$RESEARCH_USER can create an intent marker; it could stop the dispatcher indefinitely"
  fi
  info "$RESEARCH_USER can neither create nor delete a marker"
  if as_user "$RESEARCH_USER" "exec 9>$LOCK_FILE" 2>/dev/null; then
    die "$RESEARCH_USER can open the lock for write"
  fi
  info "$RESEARCH_USER cannot open the lock"
}

# --------------------------------------------------------------------------
cmd_cron_lock_path() {
  step "the deploy user's crontab lines"
  local du; du="$(deploy_user)"
  echo
  echo "   Put these INLINE on the daily_walk_forward.sh cron entry for $du:"
  echo
  echo "     0 1 * * * LOCK_FILE=$LOCK_FILE \\"
  echo "               INTENT_DIR=$INTENT_DIR \\"
  echo "               LOCK_WAIT_S=$LOCK_WAIT_S \\"
  echo "               <path>/scripts/daily_walk_forward.sh >> <log> 2>&1"
  echo
  echo "   INLINE, not as standalone crontab variable assignments. A bare"
  echo "   'LOCK_FILE=' line in a crontab applies to EVERY entry below it, and"
  echo "   scripts/backup.sh sits in the same crontab. It used to read the same"
  echo "   unnamespaced LOCK_FILE, so a crontab-wide assignment made backups"
  echo "   flock the HEAVY-TRAINING mutex and exit 1 whenever a training job"
  echo "   held it: backups died silently and became a contender on a mutex they"
  echo "   have no business in. backup.sh now reads BACKUP_LOCK_FILE, so this is"
  echo "   belt and braces -- but the habit is the hazard, not just the name."
  echo
  echo "   This prints and CHECKS. It does not edit another user's crontab."
  echo

  local cron cron_lock cron_intent
  cron="$(crontab -l -u "$du" 2>/dev/null || true)"
  [ -n "$cron" ] || die "$du has no crontab; add the entry, then re-run"
  # Word-anchored: a bare 'LOCK_FILE=' pattern also matches inside
  # 'BACKUP_LOCK_FILE=/tmp/queue-forecasting-backup.lock', and `head -1` would
  # then compare the BACKUP lock's inode and pass or fail for the wrong reason.
  cron_lock="$(printf '%s' "$cron" | grep -oE '(^|[[:space:]])LOCK_FILE=[^[:space:]]*' | head -1 | cut -d= -f2 || true)"
  cron_intent="$(printf '%s' "$cron" | grep -oE '(^|[[:space:]])INTENT_DIR=[^[:space:]]*' | head -1 | cut -d= -f2 || true)"
  [ -n "$cron_lock" ] || die "no LOCK_FILE= in $du's crontab"
  [ -n "$cron_intent" ] || die "no INTENT_DIR= in $du's crontab"

  # BOTH sides' lock paths AND both sides' intent directories must stat to the
  # same device and inode. Revision 6's marker attested only the lock, so a
  # divergent intent path would have silently restored starvation.
  local a b
  a="$(stat -c '%d:%i' "$LOCK_FILE")"
  b="$(stat -c '%d:%i' "$cron_lock" 2>/dev/null || echo none)"
  [ "$a" = "$b" ] \
    || die "lock inode mismatch: dispatcher $a vs cron $b. Two provisioned paths are two mutexes."
  info "lock inode agrees: $a"
  a="$(stat -c '%d:%i' "$INTENT_DIR")"
  b="$(stat -c '%d:%i' "$cron_intent" 2>/dev/null || echo none)"
  [ "$a" = "$b" ] || die "intent dir inode mismatch: dispatcher $a vs cron $b"
  info "intent dir inode agrees: $a"

  if would "write the migration marker $MIGRATED_MARKER"; then
    install -d -m 0755 "$CONF_DIR"
    printf 'migrated %s\nlock=%s\nintent=%s\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$LOCK_FILE" "$INTENT_DIR" > "$MIGRATED_MARKER"
    chmod 0444 "$MIGRATED_MARKER"
    info "marker written; qfd will now start"
  fi
}

# --------------------------------------------------------------------------
cmd_builder_probe() {
  step "measure the classic builder rather than trusting it"
  local tmp; tmp="$(mktemp -d)"

  # (1) available and working on a two-line fixture.
  printf 'FROM busybox\nRUN true\n' > "$tmp/Dockerfile"
  if DOCKER_BUILDKIT=0 docker build -q -t qf-probe-basic "$tmp" >/dev/null 2>&1; then
    info "(1) DOCKER_BUILDKIT=0 docker build works"
  else
    rm -rf "$tmp"; die "(1) the classic builder is unavailable; design D10 assumes it"
  fi

  # (2) --memory is HONOURED. Classic honours the build-time resource flags
  #     BuildKit ignores, which is the whole reason for choosing it.
  printf 'FROM busybox\nRUN dd if=/dev/zero of=/tmp/big bs=1M count=200\n' > "$tmp/Dockerfile"
  if DOCKER_BUILDKIT=0 docker build --memory 64m -q -t qf-probe-mem "$tmp" \
      >/dev/null 2>&1; then
    # FAILS, not warns. This probe is the whole reason design D10 chose the
    # classic builder over buildx -- classic honours the build-time resource
    # flags BuildKit ignores. If the cap is not enforced, every `RUN` step is
    # uncapped and the image build sits outside the memory budget that the
    # admission arithmetic assumes covers it. Warning and continuing would let
    # `builder-probe` "pass" on a host where the premise is false.
    docker image rm -f qf-probe-mem qf-probe-basic >/dev/null 2>&1 || true
    rm -rf "$tmp"
    die "(2) a step allocating 200MiB SUCCEEDED under --memory 64m, so the build-time memory cap is NOT enforced on this host. Design D10 chose the classic builder precisely for this flag; without it the image build is outside the admission budget."
  else
    info "(2) --memory 64m is honoured (an over-allocating step failed)"
  fi

  # (3) --force-rm leaves no intermediates after a FAILING build as well.
  local before after
  before="$(docker ps -aq | wc -l)"
  printf 'FROM busybox\nRUN true\nRUN false\n' > "$tmp/Dockerfile"
  DOCKER_BUILDKIT=0 docker build --force-rm -t qf-probe-fail "$tmp" >/dev/null 2>&1 || true
  after="$(docker ps -aq | wc -l)"
  if [ "$after" -le "$before" ]; then
    info "(3) --force-rm left no intermediate containers after a failing build"
  else
    rm -rf "$tmp"; die "(3) a failing build left $(( after - before )) intermediate container(s)"
  fi

  # (4) CANCELLATION TIMING, five times. The D10 decision depends on these
  #     numbers, so each is printed.
  step "cancellation timing (5 runs)"
  local i worst=0
  for i in 1 2 3 4 5; do
    printf 'FROM busybox\nRUN sleep 120\n' > "$tmp/Dockerfile"
    DOCKER_BUILDKIT=0 docker build --force-rm -t "qf-probe-slow-$i" "$tmp" \
      >/dev/null 2>&1 &
    local client=$!
    sleep 8
    kill -9 "$client" 2>/dev/null || true
    local t0 elapsed=0
    t0=$(date +%s)
    while [ "$elapsed" -lt $(( BUILD_SETTLE_S * 3 )) ]; do
      # Daemon-side work is gone when nothing is running our slow step.
      docker ps --format '{{.Command}}' | grep -q 'sleep 120' || break
      sleep 1; elapsed=$(( $(date +%s) - t0 ))
    done
    info "run $i: daemon-side work stopped after ${elapsed}s"
    [ "$elapsed" -gt "$worst" ] && worst="$elapsed"
  done
  info "worst: ${worst}s; QFD_BUILD_SETTLE_S is ${BUILD_SETTLE_S}s"

  docker image rm -f qf-probe-basic qf-probe-mem qf-probe-fail >/dev/null 2>&1 || true
  for i in 1 2 3 4 5; do
    docker image rm -f "qf-probe-slow-$i" >/dev/null 2>&1 || true
  done
  rm -rf "$tmp"

  if [ "$worst" -gt "$BUILD_SETTLE_S" ]; then
    # The documented response is to move building OUT of qfd, not to raise the
    # window (design D10).
    die "cancellation took ${worst}s > QFD_BUILD_SETTLE_S=${BUILD_SETTLE_S}s. The documented response is to move building OUT of qfd, not to raise the window."
  fi
  info "every measured cancellation fits inside the settle window"
}

# --------------------------------------------------------------------------
cmd_runs_dir() {
  step "runs directory ownership"
  # StateDirectory= creates it qfd:qfd, so clients could not otherwise traverse
  # to the run directories they are meant to read.
  require_group qfclient
  install -d "$RUNS_DIR"
  if would "chgrp qfclient $RUNS_DIR && chmod 0750"; then
    chgrp qfclient "$RUNS_DIR"
    chmod 0750 "$RUNS_DIR"
    info "$(stat -c '%A %U:%G %n' "$RUNS_DIR")"
  fi
}

# --------------------------------------------------------------------------
cmd_pin_base() {
  step "pin the base image by digest"
  docker pull "$BASE_IMAGE" >/dev/null || die "cannot pull $BASE_IMAGE"
  local digest
  digest="$(docker image inspect --format '{{index .RepoDigests 0}}' "$BASE_IMAGE" \
    | cut -d@ -f2)"
  [ -n "$digest" ] || die "could not read a RepoDigest for $BASE_IMAGE"
  echo
  echo "   Paste this FROM line into trainer-env.Dockerfile, commit it, push,"
  echo "   then run mirror-refresh:"
  echo
  echo "     FROM $BASE_IMAGE@$digest"
  echo
  echo "   This PRINTS. It does not edit a file in the trusted checkout."
}

# --------------------------------------------------------------------------
cmd_token() {
  local src="${1:-}"
  [ -n "$src" ] || die "usage: phase2-setup.sh token /path/to/token-file"
  [ -r "$src" ] || die "$src is not readable"
  step "install the dispatcher's read-only token"

  if would "install $TOKEN_FILE as 0400 qfd:qfd"; then
    install -d -m 0755 "$CONF_DIR"
    install -m 0400 -o qfd -g qfd "$src" "$TOKEN_FILE"
    info "$(stat -c '%A %U:%G %n' "$TOKEN_FILE")"
  fi
  [ "$CHECK" = 1 ] && return 0

  step "verify: read works, write does NOT (NC14)"
  local scratch; scratch="$(mktemp -d)"; chmod 700 "$scratch"
  # The token goes into a header FILE, never into argv and never into a URL.
  printf 'Authorization: Bearer %s\n' "$(cat "$TOKEN_FILE")" > "$scratch/hdr"
  chmod 400 "$scratch/hdr"
  local repo="${QFD_REPO:-lotas/qf-research}" code sha
  code="$(curl -sS -o /dev/null -w '%{http_code}' -H @"$scratch/hdr" \
    "https://api.github.com/repos/$repo")"
  if [ "$code" != "200" ]; then
    rm -rf "$scratch"
    die "authenticated GET returned $code; the token does not work, so a write refusal would prove nothing"
  fi
  info "canary: authenticated GET returns 200"

  sha="$(git -C "$STATE_DIR/mirror.git" rev-parse refs/remotes/origin/main 2>/dev/null \
    || printf '%040d' 0)"
  code="$(curl -sS -o /dev/null -w '%{http_code}' -X POST -H @"$scratch/hdr" \
    -H 'Accept: application/vnd.github+json' \
    -d "{\"ref\":\"refs/heads/setup-probe-$(date +%s)\",\"sha\":\"$sha\"}" \
    "https://api.github.com/repos/$repo/git/refs")"
  rm -rf "$scratch"
  case "$code" in
    403|404) info "write is refused ($code): the token is Contents: read only" ;;
    2*)      die "the token CAN WRITE ($code). Re-issue it with Contents: read only." ;;
    *)       die "inconclusive write probe ($code); a 422 or 5xx must not be read as containment" ;;
  esac
}

# --------------------------------------------------------------------------
cmd_install() {
  step "install units and client symlinks"
  [ -f "$MIGRATED_MARKER" ] \
    || die "$MIGRATED_MARKER is absent. Run cron-lock-path FIRST: an unmigrated cron entry locks a different inode, which is no mutex at all."
  require_group qfheavy
  require_group qfclient
  id qfd >/dev/null 2>&1 || die "user qfd does not exist; run dispatch-user first"

  local du duid
  du="$(deploy_user)"; duid="$(id -u "$du")"

  if would "install /usr/local/bin/qf and /usr/local/sbin/qfadmin"; then
    ln -sfn "$DISPATCHER/qf" /usr/local/bin/qf
    # qfadmin is the SAME script under another name; the split is the
    # access-control boundary, and the socket's group enforces it.
    ln -sfn "$DISPATCHER/qf" /usr/local/sbin/qfadmin
    chmod 0755 "$DISPATCHER/qf"
  fi

  if would "install qf-dispatch.service with QFD_ADMIN_UID=$duid"; then
    sed "s/%%DEPLOY_UID%%/$duid/" "$DISPATCHER/qf-dispatch.service" \
      > /etc/systemd/system/qf-dispatch.service
    chmod 0644 /etc/systemd/system/qf-dispatch.service
    if grep -q '%%DEPLOY_UID%%' /etc/systemd/system/qf-dispatch.service; then
      die "the DEPLOY_UID placeholder was not substituted; the admin socket would authorise nobody"
    fi
    install -m 0644 "$DISPATCHER/qf-runs-prune.service" /etc/systemd/system/
    install -m 0644 "$DISPATCHER/qf-runs-prune.timer" /etc/systemd/system/
  fi

  if would "daemon-reload, enable and start"; then
    systemctl daemon-reload
    systemctl enable --now qf-dispatch.service
    systemctl enable --now qf-runs-prune.timer
    sleep 5
    systemctl is-active --quiet qf-dispatch \
      || { journalctl -u qf-dispatch -n 40 --no-pager; die "qf-dispatch did not start"; }
    info "running commit: $(sudo -u "$RESEARCH_USER" qf ping 2>/dev/null \
      | grep -i '^commit' || echo unknown)"
  fi
}

# --------------------------------------------------------------------------
cmd_mirror_refresh() {
  step "refresh the trusted checkout and restart"
  # The dispatcher EXECUTES from this checkout (design §7 risk 4), so the code
  # and the running process must move together.
  local du; du="$(deploy_user)"
  if would "fetch and hard-reset $TRUSTED"; then
    sudo -u "$du" git -C "$TRUSTED" fetch --prune origin
    sudo -u "$du" git -C "$TRUSTED" reset --hard origin/main
    info "now at $(git -C "$TRUSTED" rev-parse --short HEAD)"
  fi
  if would "restart qf-dispatch"; then
    systemctl restart qf-dispatch
    sleep 5
    systemctl is-active --quiet qf-dispatch || die "qf-dispatch did not come back"
    info "restarted"
  fi
}

# --------------------------------------------------------------------------
cmd_verify() {
  step "run the negative-control suite and refresh the evidence"
  local here; here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  "$here/nc-suite-phase2.sh"
  info "evidence appended to $here/nc-evidence-phase2a.txt"
}

# --------------------------------------------------------------------------
usage() { sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "${1:-1}"; }

main() {
  local cmd="${1:-}"; shift || true
  case "$cmd" in -h|--help|'') usage 0 ;; esac
  local positional=()
  for arg in "$@"; do
    case "$arg" in
      --check) CHECK=1 ;;
      -h|--help) usage 0 ;;
      -*) die "unknown argument: $arg" ;;
      *) positional+=("$arg") ;;
    esac
  done
  [ "$(id -u)" -eq 0 ] || die "run as root"

  case "$cmd" in
    discover)       cmd_discover ;;
    dispatch-user)  cmd_dispatch_user ;;
    locks)          cmd_locks ;;
    cron-lock-path) cmd_cron_lock_path ;;
    builder-probe)  cmd_builder_probe ;;
    runs-dir)       cmd_runs_dir ;;
    pin-base)       cmd_pin_base ;;
    token)          cmd_token "${positional[0]:-}" ;;
    install)        cmd_install ;;
    mirror-refresh) cmd_mirror_refresh ;;
    verify)         cmd_verify ;;
    *) die "unknown subcommand: $cmd" ;;
  esac
}

main "$@"
