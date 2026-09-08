#!/usr/bin/env bash
# The alarm for a tick that DIED BEFORE IT COULD PAUSE ITSELF.
#
# WHY THIS EXISTS. `tick.sh` has around twenty-five `die` sites, and a `die`
# writes NO `PAUSE` and files NO issue -- it prints a reason to the journal and
# exits non-zero, and the timer tries again in an hour. An unreadable prompt
# file, a missing workspace, an unset HOME, a `results.sh` that will not run:
# every one of them leaves the loop retrying hourly forever with NOTHING raising
# an alarm. That is the 2026-09-04 failure (correct behaviour, two and a half
# days of silence) reached by a different route, and it was the last hole left
# after the pause issue closed the others.
#
# systemd's `OnFailure=` is what notices, because the service manager is the only
# party that sees the exit status of a unit nobody is watching. See
# `qf-tick.service`'s `OnFailure=qf-tick-failure.service`.
#
# IT WRITES THE SAME `PAUSE` AND FILES THE SAME ISSUE as `tick.sh`'s own
# `pause_now`, through the same `pause-issue.sh open`. There is deliberately no
# second notifier: the release a human already knows -- close the issue, an
# allowlisted close resumes the loop -- has to be the release for this too, or
# the alarm nobody recognises is as good as no alarm.
#
# WHAT KEEPS THIS TO ONE ISSUE IS THE STAMP, NOT THE EXIT CODE. `PAUSE` does
# quieten the loop -- the next tick reads it, refuses to run and exits ZERO --
# but that only holds for a tick that REACHES the PAUSE check (tick.sh:202), and
# `tick.sh` has `die` sites above it: workspace and HOME resolution, CTX
# creation. Those are exactly the broken-install faults this file exists for, so
# in the worst case this unit runs EVERY HOUR with `PAUSE` already on disk.
# It stays one issue anyway, because the stamp -- `pause-issue.sh`'s idempotency
# key, searched across `--state all` -- is written into `PAUSE` exactly once and
# NEVER rewritten below. A stamp taken from `date` on each attempt, or a `PAUSE`
# overwritten on each attempt, would file a twenty-fourth issue today and turn
# the alarm into the noise it exists to cut through.
#
# ONE MORE WAY IN, AND IT IS A HUMAN ONE: `systemctl stop qf-tick.service` over
# a tick inside a multi-hour probe. A clean stop ends `inactive (dead)` and
# alarms nothing, but if the cgroup does not die within qf-tick.service's
# `TimeoutStopSec=90` systemd SIGKILLs it, the result becomes `timeout` and the
# unit enters FAILED -- so a deliberate stop can file a pause issue. Recoverable
# (close the issue), arguably even correct, and it must not be a surprise:
# `install.sh` says so beside the instruction that suggests it. Stopping the
# TIMER cannot do this, because a timer cannot put the service into failed.
#
# NOT A DIAGNOSIS. The reason the tick died is in the journal; this says where.
#
#   tick-failure-alarm.sh          (systemd only; safe to run by hand to test)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
say() { printf '[tick-failure %s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }

# `${HOME:-}`, NOT `$HOME`, for the reason pause-issue.sh records: under `set -u`
# a bare `$HOME` here is a raw bash error before `say` exists, and an unset HOME
# is REACHABLE (systemd sets it from User=, a bare `su` may not) -- and an unset
# HOME is one of the very faults this alarm exists to report.
if [ -z "${QF_RESEARCH:-}" ] && [ -z "${HOME:-}" ]; then
  say "neither QF_RESEARCH nor HOME is set, so the workspace cannot be found"
  say "  and no PAUSE can be written. The tick's failure is in the journal:"
  say "  journalctl -u qf-tick.service -n 200"
  exit 1
fi
QF_RESEARCH="${QF_RESEARCH:-${HOME:-}/qf-research}"
PAUSE="$QF_RESEARCH/PAUSE"
# ONE LINE, deliberately: it is written as `see <text>` and read back with
# `sed -n 's/^see //p' | head -1`, so a second line would be a line of the PAUSE
# file that nothing owns and nothing reads.
SEE="journalctl -u qf-tick.service -n 200 (OnFailure= wrote this pause; the tick never reached an escalation file)"

# THE RESULT SYSTEMD SAW, when systemd tells us. `MONITOR_SERVICE_RESULT` is set
# for `OnFailure=` units since systemd v250 and is worth having in the title --
# `timeout` (a tick that hung past TimeoutStartSec=4h, abandoning a probe that
# holds the training mutex) is a different morning from `exit-code` (a `die`).
#
# ABSENT IS `unknown`, NEVER A REASON NOT TO ALARM: an older systemd would
# otherwise buy silence, which is the thing this file exists to prevent.
#
# THE CHARSET IS THE CONTRACT WITH THE READER, and the reader cannot enforce it.
# This value is interpolated into `auto-paused <stamp>: <brake> at <n>`, which
# `pause-issue.sh check` parses back with a GREEDY `s/^auto-paused [^:]*:
# \(.*\) at .*/\1/p` -- so the brake is everything before the LAST ` at `. A
# result containing ` at ` would therefore be absorbed into the brake and the
# issue would name a brake that does not exist. `[a-z-]` excludes the space, so
# it cannot happen; that is the only thing stopping it, which is why it is
# written here rather than left to the reader that cannot check it.
RESULT="${MONITOR_SERVICE_RESULT:-unknown}"
case "$RESULT" in
  ''|*[!a-z-]*) RESULT=unknown ;;
esac
BRAKE=tick-unit-failure

if [ ! -d "$QF_RESEARCH" ]; then
  # THE ONE FAILURE THIS ALARM CANNOT ALARM ABOUT, said as loudly as it can be
  # said: `pause-issue.sh open` takes a pause FILE, so with no workspace there
  # is nothing to write and nothing to bind an issue to.
  say "CRITICAL no workspace at $QF_RESEARCH, so no PAUSE can be written and"
  say "  no issue can be filed. The loop will keep failing hourly, silently."
  say "  Fix the workspace, or disable the timer:"
  say "    sudo systemctl disable --now qf-tick.timer"
  exit 1
fi

if [ -e "$PAUSE" ]; then
  # NEVER OVERWRITTEN. An existing PAUSE carries the `issue:`/`stamp:` binding
  # that is the only documented release path; replacing it with a fresh stamp
  # would orphan the issue a human may already be reading and file another.
  say "PAUSE already exists at $PAUSE; leaving it alone and re-checking its
  alarm. Reason on file: $(head -c 200 "$PAUSE" 2>/dev/null)"
else
  STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
  # THE SAME FOUR LINES `pause_now` WRITES, in the same order, because
  # `pause-issue.sh check` parses the brake and the count back out of the first
  # one and `tick.sh` prints it as the reason. A private format here would be a
  # pause the release path does not understand.
  if ! printf 'auto-paused %s: %s at %s\nsee %s\nstamp: %s\n' \
       "$STAMP" "$BRAKE" "$RESULT" "$SEE" "$STAMP" >"$PAUSE"; then
    say "CRITICAL cannot write $PAUSE. The loop is NOT paused and will keep"
    say "  failing hourly. Disable the timer by hand:"
    say "    sudo systemctl disable --now qf-tick.timer"
    exit 1
  fi
  say "PAUSED: $BRAKE at $RESULT (qf-tick.service failed; see the journal)"
fi

# THE ALARM IS NOT THE BRAKE, and its failure must never undo the line above --
# the same order, and the same reason, as `pause_now`. Its stderr is not
# swallowed: whatever it says about why the alarm failed is the only diagnostic
# there will be.
if [ ! -x "$HERE/pause-issue.sh" ]; then
  say "WARNING $HERE/pause-issue.sh is missing or not executable, so NO issue"
  say "  was filed. The loop is paused and nobody has been told -- exactly the"
  say "  2026-09-04 shape. Fix with: chmod +x $HERE/pause-issue.sh"
  exit 1
fi
"$HERE/pause-issue.sh" open "$PAUSE" "$BRAKE" "$RESULT" "$SEE" \
  || { say "WARNING could not file the pause issue; the loop is still paused"
       exit 1; }
