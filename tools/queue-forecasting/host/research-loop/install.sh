#!/usr/bin/env bash
# Install the tick's timer. Run as root, from the TRUSTED checkout.
#
# A SEPARATE SCRIPT and not a section in `phase2-setup.sh`, for one reason: this
# is the only step in the whole system that turns autonomy ON, and it must be as
# easy to reverse as to apply. `./install.sh off` stops the loop and leaves
# everything else standing.
#
#   sudo ./install.sh on       install the units and start the timer
#   sudo ./install.sh off      stop and disable the timer; units stay installed
#   sudo ./install.sh status   what is installed and when it next fires
#   sudo ./install.sh once     run one tick now, in the foreground, and watch it
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TRUSTED="/srv/queue-forecasting/tools/queue-forecasting/host/research-loop"
UNITS=/etc/systemd/system

die() { echo "error: $*" >&2; exit 1; }

# THE UNITS NAME AN ABSOLUTE /srv PATH, so installing them from anywhere else
# would enable a timer that executes a script this checkout does not control.
# The same trap `experiment.sh` guards: what another privilege domain executes is
# deployed code, not whatever is saved in an editor.
if [ "$HERE" != "$TRUSTED" ]; then
  echo "note: this is $HERE, and the units execute $TRUSTED/tick.sh." >&2
  echo "      Deploy first, or the timer will run code you are not editing:" >&2
  echo "        sudo TRUSTED_REF=<your branch> \\" >&2
  echo "          $(dirname "$HERE")/phase2-setup.sh mirror-refresh" >&2
  [ "${QF_ALLOW_UNDEPLOYED:-0}" = 1 ] || die "refusing to install from a non-trusted path"
fi

case "${1:-status}" in
on)
  [ "$(id -u)" = 0 ] || die "run as root"
  id research >/dev/null 2>&1 || die "no research user (run phase0-setup.sh)"
  [ -x "$TRUSTED/tick.sh" ] || die "no $TRUSTED/tick.sh (run mirror-refresh)"
  # BOTH CLIs CHECKED AS THE IDENTITY THAT WILL RUN THEM, not as root. A CLI on
  # root's PATH and not on the research user's is exactly the failure this whole
  # block exists for, and `command -v` here would report it as fine.
  # CHECKED THROUGH `agent-env.sh`, exactly as the tick will resolve them. A bare
  # `bash -lc "command -v claude"` reported a missing CLI for a CLI that was
  # installed: nvm's init lives in ~/.bashrc, which returns early in a
  # non-interactive shell, so this check has to load the same environment the
  # tick loads or it tests the wrong shell.
  for cli in claude codex; do
    sudo -H -u research bash -lc \
      ". '$TRUSTED/agent-env.sh'; command -v $cli" >/dev/null 2>&1 \
      || die "the research user cannot reach \`$cli\` even with
  $TRUSTED/agent-env.sh loaded. Install it (phase0-setup.sh agents) or check
  that $TRUSTED/agent-env.sh is readable by the research user."
  done
  # EVERY STEP CHECKED. There is no `set -e` here, so an unchecked `systemctl`
  # let a D-Bus failure end with a cheerful "The loop is live" -- or, in `off`,
  # with "timer disabled" over a timer that was still armed. A control that
  # reports success it did not achieve is worse than no control.
  install -m 0644 "$HERE/qf-tick.service" "$UNITS/qf-tick.service" \
    || die "cannot install qf-tick.service"
  install -m 0644 "$HERE/qf-tick.timer" "$UNITS/qf-tick.timer" \
    || die "cannot install qf-tick.timer"
  systemctl daemon-reload || die "daemon-reload failed"
  systemctl enable --now qf-tick.timer || die "cannot enable qf-tick.timer"
  # VERIFIED, not assumed: `enable --now` can succeed and the timer still not be
  # active if the unit was masked.
  systemctl is-active --quiet qf-tick.timer \
    || die "qf-tick.timer is installed and enabled but NOT active"
  systemctl list-timers qf-tick.timer --no-pager
  echo
  echo "The loop is live. To stop it:"
  echo "  sudo $HERE/install.sh off        # stops the schedule"
  echo "  touch ~research/qf-research/PAUSE  # stops the next tick, keeps the timer"
  ;;
off)
  [ "$(id -u)" = 0 ] || die "run as root"
  systemctl disable --now qf-tick.timer 2>/dev/null \
    || die "cannot disable qf-tick.timer; the loop may still be armed."
  # The RUNNING tick is left alone deliberately: killing it mid-experiment would
  # abandon a probe holding the training mutex. `PAUSE` is the way to stop the
  # next one; this stops the schedule.
  if systemctl is-active --quiet qf-tick.service; then
    echo "note: a tick is RUNNING and was not killed. It holds the training"
    echo "      mutex; let it finish, or: sudo systemctl stop qf-tick.service"
  fi
  # ASSERTED, so this line can only print over a timer that is really down.
  if systemctl is-active --quiet qf-tick.timer; then
    die "qf-tick.timer is STILL ACTIVE after disable; do not trust the loop to be stopped"
  fi
  echo "timer disabled."
  ;;
once)
  # `cd /` so this path and the timer agree on cwd: `sudo -H` sets HOME and
  # deliberately leaves the working directory alone, while the systemd unit sets
  # no `WorkingDirectory=` and so gets `/`. `tick.sh` moves to the workspace
  # itself. This is consistency, not a fix -- the 2026-09-02 tool failures were
  # a transient agent fault and a codex config error, not a cwd problem.
  cd / || die "cannot cd /"
  [ "$(id -un)" = research ] \
    && exec "$TRUSTED/tick.sh" \
    || exec sudo -H -u research bash -lc "exec '$TRUSTED/tick.sh'"
  ;;
status)
  systemctl status qf-tick.timer --no-pager 2>/dev/null | head -5
  systemctl list-timers qf-tick.timer --no-pager 2>/dev/null
  echo
  for f in ~research/qf-research/PAUSE; do
    [ -e "$f" ] && { echo "PAUSED: $(head -c 200 "$f")"; } || echo "not paused"
  done
  echo
  echo "journal:"
  ls -1t ~research/qf-research/journal/*.md 2>/dev/null | head -5 || echo "  (none)"
  echo "escalations:"
  ls -1t ~research/qf-research/journal/escalations/*.md 2>/dev/null | head -5 \
    || echo "  (none)"
  ;;
*)
  sed -n '2,12p' "$0"
  exit 2
  ;;
esac
