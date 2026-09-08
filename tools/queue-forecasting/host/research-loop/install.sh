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

# THE CREDENTIAL THAT MAKES THE ALARM POSSIBLE, checked at the one moment
# somebody is looking. A pause that files no issue is the 2026-09-04 failure
# again -- correct, and invisible -- and the token is the single thing that
# turns the brake into a message. `pause-issue.sh` says the same thing at the
# moment of failure; by then nobody is reading.
#
# A WARNING, NOT A DIE. The token is the NOTIFICATION path, not the loop, and
# refusing to install the loop over it would trade a loop that alarms nobody for
# no loop at all. It is the last thing this script prints before "the loop is
# live", so it is not lost in the scroll.
#
# THE MODE IS CHECKED TOO, and this half is easy to drop. A readable token is
# not a SAFE one: 0644 leaves a repo-write credential legible to every uid on
# the box, and nothing else in the system would ever notice. `-r` alone would
# report that file as fine.
# WHO CAN RELEASE A PAUSE, checked for the same reason as the token and with a
# worse consequence if it is wrong. `allowed()` returns false for EVERYBODY when
# this file is missing or has no usable line, so the loop pauses, files a
# perfectly good alarm, and CANNOT BE RELEASED THROUGH THE DOCUMENTED PATH AT
# ALL: the issue body renders "(none readable -- no login can release this)" and
# the only way back is to work out from source that `rm PAUSE` plus zeroing two
# counters is it. A token failure at least fails loudly; this one produces an
# alarm nobody can act on.
#
# COUNTED WITH `allowed()`'s OWN FILTER, not with `[ -s ]` or a line count. A
# file holding one line of `   ` or one `# comment` is non-empty and authorises
# NOBODY, so any cheaper test here would pass exactly the files that break it.
# The two `sed` expressions are copied from pause-issue.sh:allowed() on purpose:
# if they ever diverge, this check is measuring a different file than the one
# doing the authorising.
allowlist_preflight() {  # allowlist_preflight <allowlist file>
  local f="${1:-}" n=0
  if [ ! -r "$f" ]; then
    echo "warn: no readable pause-release allowlist at $f. The loop will pause," >&2
    echo "      file its issue, and authorise NOBODY to release it -- closing" >&2
    echo "      the issue will change nothing. Add one GitHub login per line." >&2
    return 1
  fi
  n="$(sed -e 's/#.*//' -e 's/[[:space:]]//g' "$f" | grep -c .)"
  if [ "${n:-0}" -lt 1 ]; then
    echo "warn: $f has no usable login (only comments or blanks), so no login" >&2
    echo "      can release a pause by closing its issue. The file EXISTING is" >&2
    echo "      not the property that matters. Add one login per line." >&2
    return 1
  fi
  return 0
}

# THE REPOSITORY THE ALARM IS FILED IN. `pause-issue.sh` exits 1 when
# QF_PAUSE_ISSUE_REPO is unset, so `pause_now` AND the OnFailure alarm both file
# nothing while the loop sits paused -- the 2026-09-04 silence, from a directive
# nobody noticed was absent.
#
# THE UNIT IS WHAT IS CHECKED, NOT THIS SHELL'S ENVIRONMENT. It is pinned in
# root-owned configuration precisely so it is never derived from the
# research-writable checkout, and an ambient value in the installing shell is
# not something the timer will ever see.
repo_preflight() {  # repo_preflight <unit file>
  local unit="${1:-}" repo=""
  repo="$(sed -n 's/^Environment=QF_PAUSE_ISSUE_REPO=//p' "$unit" 2>/dev/null | head -1)"
  case "$repo" in
    */*) return 0 ;;
    '') echo "warn: $unit declares no QF_PAUSE_ISSUE_REPO, so pause-issue.sh" >&2
        echo "      will file nothing and every pause will be silent. Pin it in" >&2
        echo "      the unit (owner/repo) -- never derive it from git remote." >&2
        return 1 ;;
    *)  echo "warn: $unit declares QF_PAUSE_ISSUE_REPO='$repo', which is not" >&2
        echo "      owner/repo shaped; pause-issue.sh will fail against it." >&2
        return 1 ;;
  esac
}

token_preflight() {  # token_preflight <token file>
  local f="${1:-}" mode=""
  if [ -z "$f" ]; then
    echo "warn: qf-tick.service pins no QF_PAUSE_TOKEN_FILE, so the token path" >&2
    echo "      is whatever pause-issue.sh defaults to -- an invisible default" >&2
    echo "      that fails at the one moment the alarm is needed." >&2
    return 1
  fi
  if [ ! -e "$f" ]; then
    echo "warn: no pause-issue token at $f. The loop will pause correctly and" >&2
    echo "      TELL NOBODY, which is the 2026-09-04 failure. Create a" >&2
    # THE PHRASE IS NOT WRAPPED MID-SCOPE. It is the one line an operator
    # greps for, and "Issues: read and" / "write" on two lines is not greppable
    # -- a test caught exactly that.
    echo "      fine-grained PAT on lotas/qf-research with Issues: read and write" >&2
    echo "      and NOTHING else (notably not Contents), then:" >&2
    echo "        install -m 0600 -o research -g research /dev/null '$f'" >&2
    echo "        printf '%s' <token> > '$f'" >&2
    return 1
  fi
  # `stat -c` is GNU; the fallback is BSD's spelling, so an operator running
  # this on a mac gets the check rather than an empty answer read as consent.
  mode="$(stat -c '%a' "$f" 2>/dev/null)" || mode=""
  [ -n "$mode" ] || mode="$(stat -f '%Lp' "$f" 2>/dev/null)" || mode=""
  if [ -z "$mode" ]; then
    echo "warn: cannot read the mode of $f, so whether it is 0600 is UNKNOWN," >&2
    echo "      not fine. Check it by hand: ls -l '$f'" >&2
    return 1
  fi
  if [ "$mode" != 600 ]; then
    echo "warn: $f is mode $mode, not 0600: a repo-write credential readable by" >&2
    echo "      other uids on this box. Fix with: chmod 0600 '$f'" >&2
    return 1
  fi
  return 0
}

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
  # THE EXEC BITS, WITH THE OTHER PREFLIGHTS AND BEFORE ANYTHING IS INSTALLED.
  # These are pure local checks that depend on nothing installed, and `on` has
  # to be all-or-nothing: dying BETWEEN the `install` calls left three fresh
  # unit files in /etc/systemd/system un-reloaded, so a later, unrelated
  # `daemon-reload` would activate units whose exec bit was never fixed.
  #
  # A DIE, not a warning, unlike the credential checks at the end: this is not a
  # box configured wrong, it is a deploy that is broken, and the remedy is one
  # chmod.
  [ -x "$HERE/tick-failure-alarm.sh" ] \
    || die "$HERE/tick-failure-alarm.sh is missing or not executable, so
  qf-tick-failure.service would fail and a failing tick would alarm nobody.
  Fix with: chmod +x $HERE/tick-failure-alarm.sh (or redeploy research-loop/)."
  # AND THE SCRIPT BOTH OF THEM CALL. Without the exec bit `tick.sh` warns and
  # STOPS (it names the missing helper, then falls into the shared stop -- see
  # tick.sh:209-232; warning and continuing would be the one outcome worse than
  # either) and the alarm exits non-zero. So the loop installs cleanly, pauses
  # correctly, and has NO RELEASE HANDLE: no issue filed, nothing to close.
  [ -x "$HERE/pause-issue.sh" ] \
    || die "$HERE/pause-issue.sh is missing or not executable, so a pause would
  file no issue and could not be released by closing one.
  Fix with: chmod +x $HERE/pause-issue.sh (or redeploy research-loop/)."
  # EVERY STEP CHECKED. There is no `set -e` here, so an unchecked `systemctl`
  # let a D-Bus failure end with a cheerful "The loop is live" -- or, in `off`,
  # with "timer disabled" over a timer that was still armed. A control that
  # reports success it did not achieve is worse than no control.
  install -m 0644 "$HERE/qf-tick.service" "$UNITS/qf-tick.service" \
    || die "cannot install qf-tick.service"
  install -m 0644 "$HERE/qf-tick.timer" "$UNITS/qf-tick.timer" \
    || die "cannot install qf-tick.timer"
  # THE FAILURE ALARM, AND IT IS NOT OPTIONAL. qf-tick.service names it in
  # `OnFailure=`; an `OnFailure=` pointing at a unit nothing installs is a hole
  # that LOOKS closed -- systemd logs "Failed to enqueue OnFailure job" into the
  # same journal nobody was reading, and the tick keeps dying hourly in silence.
  # Installed before the timer is enabled below, so it cannot be missing for the
  # first tick.
  install -m 0644 "$HERE/qf-tick-failure.service" "$UNITS/qf-tick-failure.service" \
    || die "cannot install qf-tick-failure.service"
  systemctl daemon-reload || die "daemon-reload failed"
  systemctl enable --now qf-tick.timer || die "cannot enable qf-tick.timer"
  # VERIFIED, not assumed: `enable --now` can succeed and the timer still not be
  # active if the unit was masked.
  systemctl is-active --quiet qf-tick.timer \
    || die "qf-tick.timer is installed and enabled but NOT active"
  systemctl list-timers qf-tick.timer --no-pager
  echo
  # THE PATH COMES OUT OF THE UNIT, not restated here: the unit is the authority
  # (qf-tick.service pins QF_PAUSE_TOKEN_FILE precisely because the default is
  # invisible), and a second copy of the path in this script would be a second
  # thing to keep in step -- which is how the stale-unit failure worked.
  token_preflight "$(sed -n 's/^Environment=QF_PAUSE_TOKEN_FILE=//p' \
                       "$HERE/qf-tick.service" | head -1)" \
    || echo "      (the loop is installed; this is the alarm, not the brake)" >&2
  repo_preflight "$HERE/qf-tick.service" \
    || echo "      (the loop is installed; this is the alarm, not the brake)" >&2
  # AND THE ALARM UNIT'S OWN COPIES, because it HAS its own: qf-tick-failure.
  # service restates both directives deliberately (an alarm that inherits its
  # configuration from the unit that just failed has the same failure modes),
  # and restating means they can DIVERGE. Drop or mistype either line there and
  # every OnFailure alarm exits 1 in silence while every check here prints
  # green: a tick dies, nothing is filed, nothing is paused, and the hourly
  # retry says nothing -- the exact harm this whole change exists to prevent,
  # arriving down the path most likely to carry it. `test_unit_drift.sh` cannot
  # see this: it compares the checkout against what is installed, and both
  # copies would be missing it.
  repo_preflight "$HERE/qf-tick-failure.service" \
    || echo "      (that is the OnFailure alarm: a failing TICK would then be silent)" >&2
  token_preflight "$(sed -n 's/^Environment=QF_PAUSE_TOKEN_FILE=//p' \
                       "$HERE/qf-tick-failure.service" | head -1)" \
    || echo "      (that is the OnFailure alarm: a failing TICK would then be silent)" >&2
  # THE ALLOWLIST IS NOT PINNED IN THE UNIT and is not restated here either:
  # `pause-issue.sh` defaults it to a file beside itself
  # (`QF_PAUSE_ALLOWLIST` overrides), so that is the path that is checked.
  allowlist_preflight "$HERE/pause-resume-allowlist.txt" \
    || echo "      (the loop is installed; this is the RELEASE path, not the brake)" >&2
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
    # AND THAT STOP CAN FILE A PAUSE ISSUE, so say it here rather than let it be
    # discovered. A clean stop ends `inactive (dead)` and alarms nothing, but a
    # cgroup that does not die within TimeoutStopSec=90 is SIGKILLed, the result
    # becomes `timeout`, the unit enters FAILED and OnFailure= runs the alarm.
    # A tick inside a multi-hour probe is exactly the case that misses 90s.
    # This `off` path itself is safe for a stronger reason: it stops the TIMER,
    # and stopping a timer cannot put the service into failed at all.
    echo "      If that stop takes longer than TimeoutStopSec=90 the unit ends"
    echo "      FAILED and OnFailure= files a pause issue; close it to release."
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
  # WHAT IS INSTALLED, NOT WHAT WOULD BE. `status` used to read the timer,
  # `PAUSE` and two journal listings and nothing else, while every preflight
  # lived in `on)` -- so a box whose checkout was upgraded but never re-`on`'d
  # showed GREEN and filed NOTHING, forever. "Is it armed" was being read as
  # "is it working", which is the 2026-09-04 shape: correct, and invisible.
  #
  # EVERY PATH BELOW IS THE INSTALLED OR DEPLOYED ONE, and that distinction is
  # the entire value of this block: `$HERE` tells you what would be installed
  # if you ran `on`, and a paused box needs to know what IS. The units come
  # from $UNITS, and the allowlist from $TRUSTED -- because the installed units
  # execute $TRUSTED/tick.sh, so that is the copy the timer will read.
  #
  # READ-ONLY, AND IT NEVER FAILS ON DRIFT. It reports; `on` is what refuses.
  # A non-zero exit here would make a report look like breakage to anything
  # wrapping it, and the branch ends with an explicit `exit 0` for that reason.
  echo "installed units:"
  for u in qf-tick.service qf-tick-failure.service; do
    if [ ! -f "$UNITS/$u" ]; then
      echo "  $u: NOT INSTALLED -- OnFailure and the pinned directives are absent."
      echo "    Remedy: sudo $HERE/install.sh on"
      continue
    fi
    # PLACEHOLDERS WOULD MAKE `cmp` LIE, so they are detected rather than
    # assumed away: neither research-loop unit is substituted at install time
    # (they are `install`ed verbatim), but a future one might be, and a
    # comparison that reports drift for a substitution the install performed
    # correctly is a check that gets ignored. phase2-setup.sh's `unit_matches`
    # is the one that knows how to exclude them; this says so rather than
    # duplicating it.
    if grep -q '%%' "$HERE/$u" 2>/dev/null; then
      echo "  $u: has install-time placeholders; compare it with"
      echo "    sudo $(dirname "$HERE")/phase2-setup.sh mirror-refresh --check"
    elif cmp -s "$HERE/$u" "$UNITS/$u"; then
      echo "  $u: matches this checkout"
    else
      echo "  $u: DIFFERS from this checkout -- the timer is running older"
      echo "    configuration than the code beside it. Remedy: sudo $HERE/install.sh on"
    fi
  done
  echo
  echo "alarm and release path (as INSTALLED):"
  for u in qf-tick.service qf-tick-failure.service; do
    [ -f "$UNITS/$u" ] || continue
    repo_preflight "$UNITS/$u" || echo "      (^ in the installed $u)" >&2
    token_preflight "$(sed -n 's/^Environment=QF_PAUSE_TOKEN_FILE=//p' \
                         "$UNITS/$u" | head -1)" \
      || echo "      (^ in the installed $u)" >&2
  done
  allowlist_preflight "$TRUSTED/pause-resume-allowlist.txt" \
    || echo "      (^ the DEPLOYED allowlist, which is the one the timer reads)" >&2
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
  # EXPLICITLY ZERO. Without this the branch's exit status is whatever the last
  # preflight or `ls` returned, so a REPORT about a missing token would exit 1
  # and read as "status failed" to anything wrapping it. `status` reports.
  exit 0
  ;;
*)
  sed -n '2,12p' "$0"
  exit 2
  ;;
esac
