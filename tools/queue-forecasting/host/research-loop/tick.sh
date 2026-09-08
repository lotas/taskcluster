#!/usr/bin/env bash
# One turn of the research loop. Runs as `research`, from a timer, unattended.
#
# WHAT THIS IS AND IS NOT. It is not an orchestrator and it holds no research
# judgement. Everything mechanical was already built -- `experiment.py` resolves
# inputs and runs a config end to end, `qf` submits typed jobs, the evaluator
# scores them, `results.sh` reads them back. The only thing missing was that
# nothing ever INVOKED an agent. This is that, plus the three guards an
# unattended agent needs and a human did not: a lock, a budget, and a second
# opinion before a conclusion is recorded.
#
# ONE ACTION PER TICK. Not for tidiness -- because the leader blocks on
# `experiment.py run` for up to 90 minutes, and a turn that started a second
# thing would be reasoning about a result it had not seen yet.
#
# THE TRUST MODEL IS THE OPERATING SYSTEM, NOT THE CLI. This invokes the agent
# with its permission prompts disabled, which is only defensible because nothing
# here is what contains it: `research` cannot read the DB credential, cannot
# reach the network except through the uid-scoped proxy allowlist, cannot write
# the trusted mirror, and cannot reach the admin socket. Every job it submits is
# a closed-world typed spec validated by `qfd` and run in a sandbox with no
# egress. An interactive approval prompt in the middle of a 3am timer would not
# add a guarantee; it would hang the tick.
#
#   ./tick.sh              one turn
#   ./tick.sh --dry-run    build the context, print what the leader would see,
#                          invoke nothing
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="$(dirname "$HERE")"
QF_RESEARCH="${QF_RESEARCH:-$HOME/qf-research}"
TRUSTED="${QF_TRUSTED_HOST:-/srv/queue-forecasting/tools/queue-forecasting/host}"
QUEUE="${QF_QUEUE_FILE:-$(dirname "$TRUSTED")/experiment-queue.md}"
STATE="${QF_TICK_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/qf-tick}"
JOURNAL="$QF_RESEARCH/journal"
USAGE_LOG="${QF_TICK_USAGE_LOG:-$STATE/usage.log}"

# Budgets. Both are per calendar day, UTC, and both are PRE-gates: a tick that
# would exceed one does not start, rather than being stopped partway.
MAX_RUNS="${QF_TICK_MAX_RUNS:-4}"
MAX_TICKS="${QF_TICK_MAX_TICKS:-12}"
# Extracts are capped separately and much lower: one is a long read against the
# production database, and it is the only action here that touches it.
MAX_EXTRACTS="${QF_TICK_MAX_EXTRACTS:-1}"
NO_MORE_EXTRACTS=0
# How much of `experiment-queue.md` goes into the prompt. 24KiB fits the file as
# it stands (~17KiB) with room to grow; past that the leader is told it was cut
# rather than left to assume it saw everything.
MAX_QUEUE_BYTES="${QF_TICK_MAX_QUEUE_BYTES:-24576}"
# Consecutive verification failures before the loop stops itself. Not 1: two
# agents disagreeing once is the mechanism working. Repeatedly is a leader whose
# reasoning has drifted, and it must not keep pushing.
MAX_DISAGREE="${QF_TICK_MAX_DISAGREE:-3}"
# TWO BRAKES, BECAUSE THERE ARE TWO FAILURES (design §2). This one counts
# invocations that produced no USABLE VERDICT -- exit 0 AND a valid anchored
# final `VERDICT:` line, both halves required. It used to share the counter
# above, deliberately; that became unsafe the moment a repeated rejection of one
# bounded target stopped advancing drift, because a `codex` outage on such a
# target would then advance NOTHING and the loop would burn a leader turn an hour
# forever. Separating them also lets the PAUSE file say which of the two
# happened, which is the first thing a human needs to know.
MAX_VERIFIER_FAILS="${QF_TICK_MAX_VERIFIER_FAILS:-3}"
# How much of the previous tick's rejection is quoted back to the leader. Small
# on purpose: it is one verdict's reason, not a transcript, and it competes for
# the leader's attention with the numbers it is supposed to be reading.
MAX_FEEDBACK_BYTES="${QF_TICK_MAX_FEEDBACK_BYTES:-4096}"
# How many times the copilot may be INVOKED before the tick gives up on it, and
# the base of the linear backoff between attempts. Retries cover a crash, never
# a verdict -- see the loop for why re-asking an answered question is not a
# retry.
COPILOT_TRIES="${QF_TICK_COPILOT_TRIES:-3}"
COPILOT_BACKOFF="${QF_TICK_COPILOT_BACKOFF_S:-30}"

# Configurable behaviour flags live here. The structured-output flags remain at
# the two call sites because usage logging requires them and is not optional.
LEADER_FLAGS=(${QF_LEADER_FLAGS:---permission-mode bypassPermissions})
COPILOT_FLAGS=(${QF_COPILOT_FLAGS:---skip-git-repo-check})

# ENFORCED HERE, not only in the unit. `install.sh once` and a hand-run tick
# execute this script directly, so a default that lived only in
# `qf-tick.service` would leave the supervised first run -- the one an operator
# is most likely to trust -- silently exempt from the discipline the prompt
# tells the leader is in force.
export QF_REQUIRE_PREREG="${QF_REQUIRE_PREREG:-1}"

DRY_RUN=0
[ "${1:-}" != "--dry-run" ] || DRY_RUN=1

say() { printf '[tick %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { say "ABORT: $*" >&2; exit 1; }

# THE WORKING DIRECTORY, SET BEFORE ANYTHING RUNS -- not inherited.
#
# This used to be set only at publish time, hundreds of lines below, so both
# agents ran in whatever directory the tick was started from. Under systemd that
# is `/` (no `WorkingDirectory=`, so the default applies) and nothing broke.
# Under `install.sh once` it is the operator's shell cwd, because `sudo -H`
# sets HOME and deliberately does NOT change directory.
#
# This is HARDENING, not a fix for anything observed. It was written while
# chasing a 2026-09-02 tick in which the leader's every shell call returned exit
# 1 with empty output; an inherited-unreadable-cwd was the hypothesis and it was
# WRONG -- `sudo -H -u research bash -lc` from that same operator directory runs
# fine. That failure was a transient agent tool-layer fault and was gone the
# next tick; the copilot failure beside it was `codex` erroring on
# "cloud config bundle (workspace-managed policies)". Neither involved cwd.
#
# It stays because an inherited working directory is still an unpinned input to
# every command both agents run, and the two entry points disagreed about it:
# systemd gives `/` (no `WorkingDirectory=`), `install.sh once` gives the
# operator's shell. Do not cite this block as the cause of a tool failure.
#
# The workspace is also the RIGHT cwd, not merely a reachable one: it is the
# repository the leader writes its journal into, so a relative path in an agent
# command means what the prompt says it means.
cd "$QF_RESEARCH" || die "no workspace at $QF_RESEARCH (cwd would be inherited,
  and an unreadable one silently breaks every command both agents run)"

# EVERY NUMERIC KNOB IS VALIDATED, because each is used BOTH in an arithmetic
# test and as an argument to something else, and neither position fails safely
# under `set -uo pipefail` (there is no `errexit`).
#
# `QF_TICK_MAX_QUEUE_BYTES=bogus` made `[ ... -le bogus ]` and `head -c bogus`
# both fail while the surrounding `{ ... }` block still succeeded -- so the
# leader ran with NO queue excerpt and a notice claiming it had been given the
# "first bogus bytes". And on GNU `head`, `-c -1` means "all but the last byte",
# so a negative value silently REMOVED the cap it was setting.
for _knob in MAX_RUNS MAX_TICKS MAX_EXTRACTS MAX_DISAGREE MAX_VERIFIER_FAILS \
              MAX_QUEUE_BYTES MAX_FEEDBACK_BYTES COPILOT_TRIES COPILOT_BACKOFF; do
  _value="${!_knob}"
  case "$_value" in
    ''|*[!0-9]*)
      die "$_knob must be a non-negative integer, got '$_value'. Refusing to
  run: this value bounds what the loop may spend, and a malformed bound is not
  a smaller bound -- it is no bound." ;;
  esac
done
unset _knob _value
[ "$MAX_QUEUE_BYTES" -gt 0 ] \
  || die "MAX_QUEUE_BYTES must be greater than zero"
[ "$MAX_FEEDBACK_BYTES" -gt 0 ] \
  || die "MAX_FEEDBACK_BYTES must be greater than zero"
[ "$COPILOT_TRIES" -gt 0 ] \
  || die "COPILOT_TRIES must be at least 1: zero would skip verification
  entirely and publish whatever the leader wrote."

# --------------------------------------------------------------------------
# THE KNOBS THIS TICK IS ACTUALLY USING, AGAINST WHAT THE UNIT DECLARES.
#
# THE BLIND SPOT THIS CLOSES, AND IT IS AN ORDERING FACT. `ExecStart=/bin/bash
# -lc` is load-bearing (the proxy variables live in `~/.profile` and nftables
# refuses without them), so the login profile is read AFTER systemd has already
# set the unit's environment -- and a stale `export QF_TICK_MAX_DISAGREE=5`
# there therefore WINS at execution time. Every check that compares systemd's
# CONFIGURATION -- `unit_matches` on the unit file, `env_matches` on the
# effective unit environment -- reports clean while the loop runs on 5. A
# drop-in doing the same thing ran unreviewed for weeks and was found only while
# investigating the 2026-09-04 pause.
#
# THE TICK IS THE ONLY PLACE THE ACTUAL VALUES EXIST, and it needs no privilege
# to ask what they were supposed to be. The delta between systemd's configured
# value and this process's own environment IS the injected override, with no
# dot-file parsing at all -- so it also sees an override this box's login
# profile makes invisible to a textual scan (`eval`, a command substitution,
# `$BASH_ENV`, a file reachable only through a path a running shell resolves).
# The sibling scan in `phase2-setup.sh` runs at DEPLOY time and says so; this
# runs hourly, which is the interval `~research` is writable over.
#
# A REPORT, NOT A REFUSAL, exactly as `env_matches`: a forgotten override must
# not block the deploy that ships its fix, and a tick that refuses to run is a
# worse outcome than one that runs loudly with a knob it has told you about.
#
# VALUES FOR DECLARED KEYS, NAMES ONLY FOR THE REST -- the same policy, for the
# same reason. A key the unit declares is a reviewed threshold and printing both
# sides is the entire point ("3 vs 5"); an undeclared key's value is by
# definition unreviewed, could be anything including a credential, and this
# lands in a journal, a systemd log and a GitHub issue.
#
# QF_* ONLY, in and out. Nothing else is read, compared or printed -- notably
# not the dispatcher's QFD_* family, whose values include a database URL.
#
# UNKNOWN IS NOT CLEAN. No `systemctl`, a call that fails, a unit that is not
# loaded on this host: each is a question nobody answered, and reporting those
# as a match is the exact failure mode being fixed. `LoadState` is asked for in
# the SAME call because `Environment=` alone cannot tell "declares nothing" from
# "no such unit", and the second reads as "every knob is unexpected".
#
# ONE `systemctl` CALL PER TICK, parsed once. This is on the hourly path.
env_selfcheck() {  # env_selfcheck -- prints report lines; 1 = not clean/unknown
  # THE UNIT NAME IS A CONSTANT, not a knob: a `QF_*` variable naming the unit
  # would itself land in the comparison below, and an override of the thing
  # being compared against is not a check.
  local unit=qf-tick.service
  local out rc=0 line loadstate="" envline="" seen_env=0 tok key val drift=0
  local -a toks=()
  local -A want=() live=()
  if ! command -v systemctl >/dev/null 2>&1; then
    printf 'UNVERIFIED (no `systemctl` on PATH, so what %s declares is UNKNOWN, not clean)\n' \
      "$unit"
    return 1
  fi
  out="$(systemctl show -p LoadState -p Environment "$unit" 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    printf 'UNVERIFIED (`systemctl show` exited %s: %s -- what %s declares is UNKNOWN, not clean)\n' \
      "$rc" "$(printf '%s' "$out" | tr '\n' ' ' | head -c 160)" "$unit"
    return 1
  fi
  while IFS= read -r line; do
    case "$line" in
      LoadState=*)   loadstate="${line#LoadState=}" ;;
      Environment=*) envline="${line#Environment=}"; seen_env=1 ;;
    esac
  done <<<"$out"
  if [ "$loadstate" != loaded ]; then
    printf 'UNVERIFIED (%s is LoadState=%s on this host, so what it declares is UNKNOWN, not empty)\n' \
      "$unit" "${loadstate:-unreadable}"
    return 1
  fi
  if [ "$seen_env" = 0 ]; then
    printf 'UNVERIFIED (`systemctl show` returned no Environment property for %s, so its declaration is UNKNOWN, not empty)\n' \
      "$unit"
    return 1
  fi
  # `read -r -a`, NEVER `for tok in $envline`: an unquoted expansion also
  # PATHNAME-expands, so a value containing `*` would be replaced by whatever
  # files sit in the cwd -- a comparison whose answer depends on the directory,
  # printing filenames into a report that is supposed to print no unreviewed
  # values at all. (`env_matches` records the same trap.)
  read -r -a toks <<<"$envline"
  for tok in "${toks[@]}"; do
    # A DECLARED VALUE THAT IS QUOTED CONTINUES PAST THE SPLIT, so neither half
    # is comparable. Named as unparseable rather than compared: a report that
    # says `unit="3` for a value of `3 and a half` sends an operator looking for
    # a difference where there is none -- a true alarm with a false description.
    # No unit declares such a value today; `QF_LEADER_FLAGS`/`QF_COPILOT_FLAGS`
    # are the family that would.
    case "$tok" in
      '"'*|*'"'*)
        printf 'UNPARSEABLE %s (the unit declares a quoted, whitespace-bearing value; read it with `systemctl cat %s`)\n' \
          "${tok%%=*}" "$unit"
        drift=1
        continue ;;
    esac
    case "$tok" in QF_*'='*) ;; *) continue ;; esac
    want["${tok%%=*}"]="${tok#*=}"
  done
  # `compgen -e` GIVES NAMES, NOT LINES. Reading `printenv` would split a value
  # containing a newline across two lines and compare half of it -- the false
  # description again. Names come from the builtin and each value is read by
  # indirection, so no value is ever parsed.
  while IFS= read -r key; do
    case "$key" in QF_*) live["$key"]="${!key}" ;; esac
  done < <(compgen -e)
  for key in "${!want[@]}"; do
    if [ -z "${live[$key]+x}" ]; then
      # THE UNIT'S VALUE IS PRINTABLE (declared, therefore reviewed) and the
      # ABSENCE is the finding: a profile `unset`, or a tick that is not running
      # under the unit at all -- `install.sh once` and a hand-run reach here too,
      # and "your environment is not the unit's" is true and worth saying in
      # both cases.
      printf 'MISSING %s (the unit declares it as %s; this tick has no such variable)\n' \
        "$key" "${want[$key]}"
      drift=1
      continue
    fi
    if [ "${live[$key]}" != "${want[$key]}" ]; then
      printf 'DRIFT %s unit=%s effective=%s\n' \
        "$key" "${want[$key]}" "$(printf '%s' "${live[$key]}" | tr '\n' ' ')"
      drift=1
    fi
  done
  for key in "${!live[@]}"; do
    [ -z "${want[$key]+x}" ] || continue
    # THE NAME ONLY. See the header: unreviewed value, and this text reaches a
    # GitHub issue.
    printf 'UNEXPECTED %s (set in the environment this tick is running with, absent from %s)\n' \
      "$key" "$unit"
    drift=1
  done
  return "$drift"
}

# CAPTURED, NOT JUST LOGGED. `ENV_NOTE` is empty when the check is clean and is
# quoted into the escalation -- and therefore into the pause issue -- when it is
# not; see the escalation writer for why that fence is the only seam that
# reaches a human who is not reading the journal.
ENV_NOTE=""
if ! ENV_NOTE="$(env_selfcheck)"; then
  say "WARNING the knobs this tick is using were not confirmed against"
  say "  qf-tick.service. systemd sets the unit's environment and then"
  say "  \`ExecStart=/bin/bash -lc\` reads ~/.profile ON TOP of it, so a stale"
  say "  \`export QF_TICK_MAX_DISAGREE=5\` there wins here while every check of"
  say "  systemd's CONFIGURATION reports clean. What differs, or could not be"
  say "  asked:"
  while IFS= read -r _env_line; do
    [ -z "$_env_line" ] || say "    $_env_line"
  done <<<"$ENV_NOTE"
  unset _env_line
  say "  Reported, not enforced: a forgotten override must not block the deploy"
  say "  that ships its fix, and a tick that refuses to run is worse."
fi

# THE THREE STATE FILENAMES, SHARED WITH `pause-issue.sh`. Not restated in both
# files: `pause-issue.sh check` zeroes these three and reads them back before it
# removes PAUSE, so a rename on either side would make that read-back
# SELF-CONFIRMING -- it verifies the value it just wrote to a path nothing else
# reads, PAUSE goes away, and the real counter is still at its threshold. See
# state-names.sh. A missing file is a STOP: guessing these names is the failure
# the shared file exists to remove.
# shellcheck source=state-names.sh
[ -r "$HERE/state-names.sh" ] \
  || die "$HERE/state-names.sh is missing or unreadable. Refusing to run: it
  names the counter files that are this loop's brakes, and \`pause-issue.sh\`
  zeroes those same names on a release. Reinstall the research-loop directory."
. "$HERE/state-names.sh"
DISAGREE_FILE="$STATE/$QF_STATE_DISAGREE"
VERIFIER_FILE="$STATE/$QF_STATE_VERIFIER"
TARGET_FILE="$STATE/$QF_STATE_TARGET"

# THE AGENT CLIs AND THE PROXY, before anything looks for them. Sourced here --
# the single entry point -- so the timer, `install.sh once` and a hand-run tick
# all get the same environment. See the file for why `which claude` succeeding in
# an ssh session says nothing about whether this shell can find it.
# shellcheck disable=SC1091
[ ! -r "$HERE/agent-env.sh" ] || . "$HERE/agent-env.sh"

# --------------------------------------------------------------------------
# Guards. Every one of these is a way an unattended loop goes wrong.
# --------------------------------------------------------------------------
mkdir -p "$STATE" || die "cannot create $STATE"

# THE LOCK, taken on its own fd and never released explicitly: the kernel drops
# it when this process exits, including on a kill, which is the property a
# `trap`-based release does not have.
exec 9>"$STATE/tick.lock" || die "cannot open the tick lock"
if ! flock -n 9; then
  say "a tick is already running; nothing to do"
  exit 0
fi

# CTX IS CREATED HERE, ABOVE THE PAUSE CHECK, because a pause that a human just
# released hands the loop a directive and the directive needs somewhere to live
# (design §4.3). The `trap` already covers every early-exit path below, so
# moving it up costs nothing and leaks nothing.
CTX="$(mktemp -d "$STATE/ctx.XXXXXX")" || die "cannot make a context directory"
trap 'rm -rf "$CTX"' EXIT

# THE PAUSE CHECK IS NOW A QUESTION, NOT A FULL STOP -- because on 2026-09-04
# the loop paused itself correctly and nobody found out for two and a half days,
# and the only release was to shell into the box. `pause-issue.sh check` resumes
# ONLY when an allowlisted human closed the issue this PAUSE names, and leaves
# the brake in place for every other answer -- an open issue, a stranger's
# close, a repo mismatch, every API error, every answer that does not parse.
#
# ITS OUTPUT IS NOT SWALLOWED: whether it resumed or why it refused is the only
# diagnostic an operator gets, and the tick log is where they look.
if [ -e "$QF_RESEARCH/PAUSE" ]; then
  # A MISSING HELPER IS ITS OWN DIAGNOSIS, NOT JUST ANOTHER REASON TO STAY
  # PAUSED. Folding "the script is not there" into the else-branch reproduced
  # the 2026-09-04 failure in a new place: brake on, the documented release path
  # dead, and the log saying only "PAUSE exists; stopping". A forgotten
  # `chmod +x` or a half-finished deploy is exactly how that happens, and it is
  # indistinguishable from a pause nobody has got round to releasing.
  RESUMED=0
  if [ ! -x "$HERE/pause-issue.sh" ]; then
    say "WARNING $HERE/pause-issue.sh is missing or not executable, so this"
    say "  PAUSE cannot be released by closing its GitHub issue -- the only"
    say "  remaining release is 'rm $QF_RESEARCH/PAUSE' on the box."
    say "  Fix with: chmod +x $HERE/pause-issue.sh (or redeploy research-loop/)."
  elif "$HERE/pause-issue.sh" check "$QF_RESEARCH/PAUSE"; then
    RESUMED=1
    say "resumed by an allowlisted close; continuing this tick"
    # LEADER-ONLY AND CONSUMED ONCE. Copied into CTX now and deleted from the
    # state directory when it is injected, so a directive from a pause three
    # weeks ago is never read as an instruction about this tick.
    #
    # AND THE COPY IS CHECKED, because this is the one step of the resume that
    # runs with the brake ALREADY GONE. `pause-issue.sh` persists the directive,
    # zeroes and reads back all three state files and removes PAUSE last -- in
    # that order, verified. An unchecked `cp` on the far side of it had two
    # silent failures on a nearly full filesystem: a PARTIAL copy was injected
    # into the leader's prompt and the state copy was then deleted, or no copy
    # arrived at all and the state copy survived unread, because nothing looks
    # at it once PAUSE is gone. The resume stays safe either way; what is lost
    # is the release instruction a human deliberately left behind.
    #
    # `cmp`, NOT JUST THE EXIT STATUS: half a directive is worse than none --
    # a truncated sentence is still read as an instruction -- so the
    # destination has to be IDENTICAL, not merely written. `cmp` missing (it is
    # `diffutils`, essential on Debian) fails the same way a bad copy does: the
    # directive is dropped loudly, never injected half-read.
    #
    # THE PARTIAL DESTINATION IS REMOVED, which is what keeps the invariant the
    # injection site relies on: `$CTX/human-directive.md` exists only if it is a
    # verified-intact copy, so the `rm` of the state copy there still means
    # "this was consumed" and never "this was lost".
    if [ -s "$STATE/human-directive.md" ]; then
      if ! cp "$STATE/human-directive.md" "$CTX/human-directive.md" \
         || ! cmp -s "$STATE/human-directive.md" "$CTX/human-directive.md"; then
        rm -f "$CTX/human-directive.md"
        say "CRITICAL the human directive could not be copied intact into $CTX."
        say "  The leader is about to run WITHOUT an instruction a human left"
        say "  on the pause issue. It is kept at $STATE/human-directive.md,"
        say "  but nothing reads that once PAUSE is gone -- read it by hand and"
        say "  check the state directory's filesystem for space."
      fi
    fi
  fi
  # THE STOP IS ONE BRANCH, REACHED BY BOTH REFUSALS. A missing helper diagnoses
  # itself above and then stops here like any other unhappy answer -- warning
  # and continuing would be the one outcome worse than either.
  if [ "$RESUMED" = 0 ]; then
    say "PAUSE exists; stopping"
    say "  reason: $(head -c 200 "$QF_RESEARCH/PAUSE" 2>/dev/null)"
    exit 0
  fi
fi

# THE PROMPT FILES ARE THE MECHANISM, so an unreadable one is a STOP rather than
# a quieter tick. Every one of them fails OPEN when `cat`ed: the surrounding
# group succeeds, the tick reports nothing unusual, and an agent runs with its
# instructions missing. The three failures are not equally bad but they are all
# silent:
#
#   tick-prompt.md   -- the leader gets context with no task, no six actions and
#                       no output template.
#   verify-prompt.md -- the copilot is handed an entry and the evidence with no
#                       instruction to check one against the other, and its
#                       verdict line is what publishes a finding.
#   retry-prompt.md  -- the worst of the three, because the retry block's OTHER
#                       half still emits: the leader would be steered onto
#                       action 1 on one named row with the shrink instruction,
#                       the closed action list and all four bans absent. That is
#                       the 2026-09-04 rewrite-and-resample with the brake off,
#                       which is strictly worse than not retrying at all.
#
# Checked here, once, before any budget is spent or any agent is paid.
for _prompt in tick-prompt.md verify-prompt.md retry-prompt.md; do
  [ -r "$HERE/$_prompt" ] && [ -s "$HERE/$_prompt" ] \
    || die "$HERE/$_prompt is missing, empty or unreadable. Refusing to run:
  every prompt file fails OPEN when it is cat'ed, so this would have run an
  agent with its instructions silently absent."
done
unset _prompt

# STATE THAT CANNOT BE READ OR WRITTEN IS A STOP, NOT A ZERO. Both counters used
# `cat ... || echo 0`, so an unreadable or unwritable state directory silently
# reset them: the tick budget never accumulated, and -- much worse -- the
# consecutive-disagreement counter never reached its threshold, so the automatic
# PAUSE could never fire. A loop whose brakes depend on a file must refuse to run
# when it cannot trust that file.
counter() {  # counter <path> -- prints the value, or fails
  local path="$1" value
  [ -e "$path" ] || { echo 0; return 0; }
  value="$(cat "$path" 2>/dev/null)" || return 1
  case "$value" in
    ''|*[!0-9]*) return 1 ;;
  esac
  echo "$value"
}
set_counter() {  # set_counter <path> <value> -- fails if it did not persist
  printf '%s\n' "$2" >"$1" 2>/dev/null || return 1
  [ "$(cat "$1" 2>/dev/null)" = "$2" ] || return 1
}

# THE REJECTED TARGET CANNOT USE `counter`. That helper refuses any value
# containing a non-digit -- correctly, a counter it cannot trust is a stop -- so
# it fails on every run id. Two shapes are legal here and nothing else: the
# literal `none`, or ONE canonical EVALUATION id. A probe id is refused because
# `frontier.py`'s index maps an id to a LIST of rows (one probe can be scored
# under two contracts), so a probe id does not identify a row, and suppressing a
# streak on an id retirement cannot bound is unbounded by construction.
#
# ANYTHING ELSE IS A *CHANGED* TARGET, not an error that stops the tick: empty,
# multi-line, malformed and unreadable all fail here, the caller then treats the
# rejection as a new episode, and the streak advances. Failing towards the pause
# is the safe direction; failing towards suppression is the 2026-09-04 livelock.
target() {  # target <path> -- prints `none` or one canonical evaluation id
  local path="$1" value
  # NEVER-WRITTEN IS `none`, not a failure: a fresh state directory has no
  # stored target and that is the normal first tick, not a fault.
  [ -e "$path" ] || { echo none; return 0; }
  value="$(cat "$path" 2>/dev/null)" || return 1
  case "$value" in
    none) echo none; return 0 ;;
    evaluate-*) ;;
    *) return 1 ;;
  esac
  # `grep -qx` ON THE WHOLE LINE, and via a pipe so a multi-line file cannot
  # pass by having one good line: `-x` anchors each line, but the surrounding
  # `case` has already rejected anything whose FIRST line is not `evaluate-*`,
  # and a second line makes this a two-line stream that `$(...)` would have
  # collapsed on output. So the count is checked too.
  printf '%s' "$value" | grep -qxE 'evaluate-[0-9A-Za-z]+-[0-9a-f]+-[0-9]+' \
    || return 1
  [ "$(printf '%s' "$value" | wc -l)" = 0 ] || return 1
  echo "$value"
}

set_target() {  # set_target <path> <value> -- persists, or fails
  # WRITTEN THEN VERIFIED, the same contract as `set_counter`: a target that
  # silently did not persist would make the next rejection of the same run look
  # like a new episode, which merely advances the streak -- but the reverse (a
  # stale value surviving a failed write) would suppress one it must not.
  # BRACED, because `>"$1.tmp" 2>/dev/null` does NOT silence a failure to OPEN
  # `$1.tmp`: redirections are applied left to right, so the shell has already
  # printed `Permission denied` by the time stderr is pointed away.
  { printf '%s\n' "$2" >"$1.tmp"; } 2>/dev/null || return 1
  mv "$1.tmp" "$1" 2>/dev/null || { rm -f "$1.tmp" 2>/dev/null; return 1; }
  [ "$(cat "$1" 2>/dev/null)" = "$2" ] || return 1
}

# THE ENTRY'S DECLARED TARGET, resolved by `frontier.py` ITSELF.
#
# NOT A LOCAL `sed`, and this is a correctness requirement rather than reuse for
# its own sake. `frontier.py:target_of` collects EVERY `**Target run:**` match
# and returns nothing when they disagree, because an escalation quoting a
# previous entry's target line inside an `Evidence:` fence has two matches and
# resolving by position is exactly the failure the declared field exists to
# prevent. A local first-match `sed` would disagree with it in that case: the
# frontier would count the rejection against NO run (so retirement never fires)
# while this script suppressed the drift streak for it -- suppression with
# nothing bounding it, which is the 2026-09-04 livelock with the brake off. The
# two must never disagree, so there is one implementation.
#
# LINE ENDINGS ARE NORMALIZED first, as `frontier.py:_committed_blobs` does for
# committed files: a `\r\n`-bodied entry otherwise captures `<id>\r` and matches
# nothing, silently reading as `none`.
#
# ANY FAILURE IS `none`, which increments. A leader whose entry cannot be parsed
# gets no suppression.
entry_target() {  # entry_target <entry file> -- `none` or one evaluation id
  local out
  out="$(python3 - "$HERE" "$1" <<'PYTGT'
import sys
try:
    sys.path.insert(0, sys.argv[1])
    import frontier
    with open(sys.argv[2], encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    print(frontier.target_of(text.replace("\r\n", "\n").replace("\r", "\n"))
          or "none")
except Exception as exc:
    # `none` IS THE SAFE ANSWER BUT A SILENT ONE. If `frontier.py`'s import
    # chain breaks -- it reaches `prereg` and `experiment` under `host/` -- every
    # rejection would read as `none`, suppression would be off, and three ticks
    # later the loop would pause reporting `consecutive-disagreements at 3`:
    # the 2026-09-04 misdiagnosis reproduced exactly. Stderr is not captured at
    # the call site, so this lands in the tick log.
    print(f"WARNING entry_target could not resolve a target: {exc!r}",
          file=sys.stderr)
    print("none")
PYTGT
)" || out=none
  case "$out" in
    evaluate-*) printf '%s\n' "$out" ;;
    *) echo none ;;
  esac
}

# WHAT THE FRONTIER SAYS ABOUT ONE ID: recorded | unrecorded | retired |
# ambiguous | absent. The last two exist as their own answers rather than being
# folded into `unrecorded`, because a target that does not resolve to EXACTLY
# ONE row is one retirement cannot reach -- see `suppressible`.
handling_of() {  # handling_of <frontier.json> <run id>
  python3 - "$1" "$2" <<'PYHDL'
import json, sys
try:
    with open(sys.argv[1]) as fh:
        report = json.load(fh)
except Exception as exc:
    # UNREADABLE RESOLVES NOTHING. `absent` is not suppressible, so a reporting
    # glitch advances the streak instead of silently suppressing it -- but it is
    # NOT the same fact as "that id is off the scoreboard", and the log line the
    # caller prints cannot tell them apart. So the difference is said here.
    # The predicate answer stays `absent`: failing towards the pause is right.
    print(f"WARNING handling_of could not read {sys.argv[1]}: {exc!r}",
          file=sys.stderr)
    print("absent")
    raise SystemExit(0)
rid = sys.argv[2]
hits = [r for s in (report.get("series") or []) for r in (s.get("rows") or [])
        if r.get("evaluation") == rid]
print("absent" if not hits else
      "ambiguous" if len(hits) > 1 else
      hits[0].get("handling", "unrecorded"))
PYHDL
}

# THE LOAD-BEARING PREDICATE (design §2.1). Suppressing the drift streak for a
# repeated target is safe ONLY because RETIREMENT bounds it, and retirement only
# counts escalation episodes against a resolved, UNRECORDED row. Suppressing on
# anything else -- ambiguous, absent (stale), already recorded, already retired
# -- is unbounded, and rebuilds the exact 2026-09-04 livelock with the drift
# brake switched off. So the whole predicate is one comparison, and every other
# state behaves exactly like `none`: it increments and stores nothing.
#
# IT TAKES THE HANDLING, NOT THE FRONTIER. An earlier signature accepted the
# JSON path and never read it, which made every call site read as though it
# resolved the id here -- it does not, `handling_of` does, exactly once.
suppressible() {  # suppressible <handling>
  [ "$1" = unrecorded ]
}

# THE RETRY DECISION, and it is made HERE -- from the stored target and the
# frontier THIS tick built -- rather than from the entry, which does not exist
# yet. Design §3.1: the shrink instruction is worthless unless the leader reads
# it before it plans the entry, so the retry cannot be derived from PENDING.md.
#
# THE SAME PREDICATE THE STREAK USED (design §2.1), deliberately. If the two
# ever disagreed the tick could suppress a drift streak for a target it then
# refuses to instruct the leader to retry: the brake would be off and the leader
# would get no steer either, which is 2026-09-04 with nothing left to stop it.
#
# A STORED TARGET THAT IS NO LONGER SUPPRESSIBLE IS CLEARED HERE -- recorded by
# another route, retired, gone ambiguous, or off the scoreboard entirely -- so
# the next rejection is counted as a NEW episode rather than suppressed against
# a target nobody was ever told to retry.
# THE NAME SAYS `take`, because this function MUTATES: on two of its three
# paths it writes `none` into the target file. An earlier name (`retry_target`)
# read as a pure query at a call site that is a command substitution inside the
# context assembly, which is the last place a reader expects state to change.
take_retry_target() {  # take_retry_target <frontier.json> <target file> -- prints id, or fails
  local json="$1" file="$2" stored state
  # A MALFORMED STORED VALUE IS CLEARED TOO, not just refused: leaving it there
  # would make every later tick re-derive the same failure, and `target` already
  # treats unreadable/empty/multi-line as "not a target".
  #
  # AND IT SAYS SO. This was the one silent branch: an operator whose target
  # file had been corrupted saw the retry simply not happen, while the sibling
  # path below named the state that disqualified it.
  if ! stored="$(target "$file")"; then
    # `|| true` HERE FOR THE SAME REASON AS BELOW, and with the same owner: the
    # verdict path rewrites this file on every branch except a verifier outage
    # and warns when it cannot. THE MESSAGE SAYS `cleared` UNCONDITIONALLY --
    # both here and below -- so a `set_target` that failed prints a claim that
    # is false for the length of one tick; the verdict path's own
    # "could not clear the last rejected target" warning is the correction, and
    # it is the line an operator should believe.
    set_target "$file" none || true
    say "the stored retry target is unreadable or malformed; cleared" >&2
    return 1
  fi
  [ "$stored" != none ] || return 1
  state="$(handling_of "$json" "$stored")"
  # AN EMPTY ANSWER IS `absent`, which is not suppressible. `handling_of` shells
  # out to `python3`; if that vanished, `$state` would be empty, the refusal
  # line would render "is ; cleared", and the log would name no state at all.
  [ -n "$state" ] || state=absent
  # `suppressible <handling>` TAKES THE RESOLVED STATE, not the path.
  # `handling_of` is the one thing that resolves an id, and it did so above --
  # exactly once, so the two answers cannot drift apart.
  if suppressible "$state"; then
    # STDERR, NOT STDOUT, FOR EVERY DIAGNOSTIC IN THIS FUNCTION. The call site
    # is a command substitution -- it reads the id off stdout -- and it sits
    # inside the group redirected into `context.md`. A `say` on stdout would
    # therefore be appended to the id on success, injected into the leader's
    # context, and absent from the tick log. All three at once.
    say "this tick is a RETRY of $stored (still unrecorded)" >&2
    printf '%s\n' "$stored"
    return 0
  fi
  # `|| true`, AND THE OWNER OF A FAILED CLEAR IS THE VERDICT PATH. A clear that
  # did not persist leaves a stale target that the streak block then compares
  # against this tick's entry -- and that block rewrites the file on every
  # branch except a verifier outage, warning when it cannot ("could not clear
  # the last rejected target"). So a failure here costs at most one tick's retry
  # instruction, is re-attempted within the same tick, and is warned about
  # there; dying here would instead lose a leader turn to a state-directory
  # glitch that the tick can still recover from.
  set_target "$file" none || true
  say "the stored retry target $stored is $state; cleared" >&2
  return 1
}

# THE TARGET ROW AND ITS COMPARATOR, PASTED -- because the leader NEVER
# RECEIVES `frontier.json`.
#
# Its prompt is `tick-prompt.md` + `context.md`, and `context.md` embeds the
# MARKDOWN render; the JSON goes to the copilot alone as its evidence. So a
# template telling the leader to take `claim`, `bar`, `direction`, `tol`, `vs`
# and `config_digest` "from the frontier JSON" mandated fields it could not
# fill: the markdown's row table carries none of those, and its only figure is
# a `%.4g` cell for the WINNING config. Both ways out were rejectable -- a blank
# mandated field, or a number recited from the previous entry, which is the
# "number you remember is a number you invented" rejection the retry exists to
# stop.
#
# TWO ROWS, BECAUSE A SIGNED DELTA HAS TWO OPERANDS. Ban 1 demands both operands
# and the delta between them, and the second operand is the PRE-REGISTERED `vs`
# ROW'S metric -- which the target row carries only as an id, not as a figure.
# Pasting the target alone reproduced the same defect one layer down: the header
# says every field is sourced from the paste, and ban 1 then asked for a number
# that was not in it.
#
# SAME SERIES ONLY, which is `judge_claim`'s rule and not a convenience: a
# reference from another extract, baseline or contract is a different
# population, and a delta across that boundary is the cross-series comparison
# `frontier.py` exists to refuse.
#
# A MISSING COMPARATOR IS A LEGITIMATE STATE, not a failure of the retry: a
# reference run has no `vs` by declaration, and a `vs` may name a run this
# series has not scored. So the block says WHICH, and stops there -- in that
# case `retry-prompt.md` requires NO DELTA AT ALL rather than a second operand
# fetched from somewhere, so the note must not send the leader looking for one.
# See the note itself for why an instruction inside data is the wrong place for
# one.
#
# ONE ROW EACH, NOT THE REPORT, and at FULL PRECISION: ban 1 asks for a signed
# delta at four significant figures (0.0817 vs 0.0818 was not "unchanged"),
# which no `%.4g` cell can support.
#
# WITH THE SERIES' `holdout` AT THE TOP LEVEL. `holdout` is a SERIES field, not
# a row field, so pasting rows alone would have left the mandated `Confidence`
# field unsatisfiable in exactly the same way -- and a row carries no `extract`,
# so the leader could not even find its own series heading in the markdown to
# read the window off. It sits above both rows because it is the one window both
# were scored on.
#
# ONE FIELD HERE IS NOT IMMUTABLE: `claim`. It is derived from the comparator's
# metrics, so a comparator scored during the leader's turn can flip it
# (`kept` -> `broken`) between this pre-leader snapshot and the refreshed JSON
# the copilot is judged against. Low probability -- the comparator is by
# definition an earlier run -- and not worth engineering around, but the
# template says to cite `claim` verbatim, so the skew is recorded here.
#
# PASTING RATHER THAN WIDENING THE MARKDOWN: every ordinary tick would pay for a
# retry-only need. And rather than naming a command: that adds a tool call the
# leader can get wrong for data the tick already holds. Pasting is also the only
# option where the copilot can check every figure, because the same values reach
# it in the JSON it receives.
retry_row() {  # retry_row <frontier.json> <run id> -- pretty JSON, or fails
  python3 - "$1" "$2" <<'PYROW'
import json, sys
try:
    with open(sys.argv[1]) as fh:
        report = json.load(fh)
except Exception as exc:
    print(f"WARNING retry_row could not read {sys.argv[1]}: {exc!r}",
          file=sys.stderr)
    raise SystemExit(1)
# A NON-DICT TOP LEVEL WARNS INSTEAD OF TRACEBACKING. Unreachable while
# `frontier.py --json` is the only writer, and the same shape `handling_of`
# assumes -- but a traceback here is indistinguishable in the log from the
# read failure above, and it costs one line to say which happened.
if not isinstance(report, dict):
    print(f"WARNING retry_row got {type(report).__name__}, not an object",
          file=sys.stderr)
    raise SystemExit(1)
rid = sys.argv[2]
hits = []
for series in report.get("series") or []:
    rows = series.get("rows") or []
    for row in rows:
        if row.get("evaluation") == rid:
            hits.append((series, row, rows))
# EXACTLY ONE, checked again here even though `suppressible` already rejected
# `ambiguous`: this function is what produces the numbers a claim rests on, and
# silently pasting the first of two rows would put a figure from the wrong
# contract into a mandated field.
if len(hits) != 1:
    print(f"WARNING retry_row resolved {len(hits)} rows for {rid}",
          file=sys.stderr)
    raise SystemExit(1)
series, row, rows = hits[0]
# THE COMPARATOR IS MATCHED ON EITHER ID, because `vs` is a RUN id and
# `judge_claim` resolves it through an index keyed by BOTH the evaluation and
# the probe -- a `vs` naming the probe must resolve to the same row here as it
# does there, or the pasted delta would disagree with the pasted `claim`.
vs = row.get("vs") or ""
peers = [r for r in rows if r is not row
         and vs in (r.get("evaluation"), r.get("probe"))]
out = {"holdout": series.get("holdout"), "target": row, "comparator": None}
# THE NOTE STATES A FACT AND PRESCRIBES NOTHING. Both notes used to end
# "obtain <it> with a command and paste it under `Evidence:`", which is the
# exact opposite of what `retry-prompt.md` requires: a null comparator means NO
# DELTA, because a reference row has no `vs` by construction and a `vs` that
# resolves to zero rows in THIS series may well be scored in another one -- so
# fetching its figure builds the cross-series comparison `verify-prompt.md`
# calls the single most consequential error possible here. The leader was
# handed both instructions in one prompt, and the template only won because it
# names the note's clause and overrides it verbatim -- which breaks the moment
# either side is reworded. This is DATA about the paste; the prompt owns what
# the leader should do about it.
#
# EACH REASON IS LOAD-BEARING AND UNCHANGED. `retry-prompt.md` quotes all three
# (no `vs`; `vs` resolves to 0 rows; a count above 1) to explain why no delta
# was ever pre-registered, so the reason text is not the part to shorten.
if not vs:
    out["comparator_note"] = (
        "this row pre-registered no `vs`, so there is no comparator row and no"
        " delta is possible")
elif len(peers) != 1:
    out["comparator_note"] = (
        f"`vs` is {vs}, which resolves to {len(peers)} rows in this series, so"
        " no comparator row is pasted")
else:
    out["comparator"] = peers[0]
# `sort_keys=True` ORDERS THE KEYS OF EACH OBJECT, which makes the paste stable
# across ticks; the REPORT is not reordered and neither row is mutated -- the
# rows are the ones the copilot holds, wrapped, not rewritten.
print(json.dumps(out, indent=2, sort_keys=True))
PYROW
}

# A BRAKE THAT COULD NOT BE WRITTEN MAKES THE TICK A FAILURE. Set by either
# `pause_now` caller, read by `finish` far below. Both callers used to IGNORE
# `pause_now`'s status and carry on through publish to a ZERO exit, so
# `OnFailure=qf-tick-failure.service` -- the one thing that can tell a human
# about a `die` or a brake that did not take (design §4b) -- never fired. The
# tick logged CRITICAL, published normally, exited 0, and repeated hourly with
# no brake and no issue: the 2026-09-04 silence exactly, by the route the alarm
# was built to cover.
PAUSE_UNWRITABLE=0

# THE ONLY ORDINARY WAY OUT BELOW THE ESCALATION, so the flag above cannot be
# lost on one exit path and honoured on another.
#
# THE PUBLISH RUNS FIRST AND THIS IS NOT A `die`. By the time a brake fails the
# escalation file is already written, and `publish` is what commits and pushes
# it; aborting before that would lose the record AS WELL as the brake, which is
# two harms where there was one. So the tick finishes its work and only then
# reports the failure through its exit status.
finish() {  # finish -- exit 0, or non-zero if a brake could not be written
  [ "$PAUSE_UNWRITABLE" = 1 ] || exit 0
  say "EXITING NON-ZERO: the brake could not be written, so this tick is a"
  say "  FAILURE and not a normal stop. Nothing local is holding the loop back"
  say "  and the next timer tick will run as though nothing happened, so"
  say "  OnFailure=qf-tick-failure.service is the only thing left that can"
  say "  tell a human. The escalation above was published first, deliberately."
  exit 1
}

# WRITING THE BRAKE, AND RAISING THE ALARM. Two callers, one behaviour, and the
# brake name reaches the PAUSE file so a human can tell an outage from drift
# without opening anything. The `stamp:` line is machine-read: §4 binds a pause
# to the issue that authorizes releasing it.
pause_now() {  # pause_now <brake> <n> <escalation path> <stamp>
  local brake="$1" n="$2" recorded="$3" stamp="$4"
  # THE BRAKE NAME IS VERBATIM AND THE COUNT IS BESIDE IT. Both matter: a human
  # greps for `consecutive-verifier-failures` to tell an outage from drift, and
  # §4.1 gives the issue title the same `<brake> at <n>` shape so the two agree.
  # Not "<n> consecutive <brake>", which read "3 consecutive
  # consecutive-disagreements".
  if ! printf 'auto-paused %s: %s at %s\nsee %s\nstamp: %s\n' \
       "$stamp" "$brake" "$n" "$recorded" "$stamp" >"$QF_RESEARCH/PAUSE"; then
    # The PAUSE file IS the brake. If it cannot be written, say so as loudly as
    # possible rather than reporting a pause that did not happen.
    #
    # AND THE `return 1` IS LOAD-BEARING, not decoration: both callers set
    # `PAUSE_UNWRITABLE` from it and `finish` turns that into a non-zero exit.
    # A log line is not an alarm -- for two and a half days in 2026-09-04
    # nothing read the log.
    say "CRITICAL cannot write $QF_RESEARCH/PAUSE. The loop is NOT paused."
    say "  Disable the timer by hand: sudo systemctl disable --now qf-tick.timer"
    return 1
  fi
  say "PAUSED: $brake at $n"
  # THE ALARM IS NOT THE BRAKE, and a failure here must never undo the line
  # above. It depends on a token, a network and GitHub -- three things that fail
  # independently of the reason this loop is pausing, and one of which
  # (`QF_PAUSE_ISSUE_REPO` unset) is the normal state of a box that has not been
  # configured for it. Losing the pause because the notification failed would be
  # strictly worse than a silent pause.
  # ITS STDERR IS NOT SWALLOWED: whatever it says about why the alarm failed is
  # the only diagnostic there will be, and the tick's own log is where an
  # operator looks.
  "$HERE/pause-issue.sh" open "$QF_RESEARCH/PAUSE" "$brake" "$n" "$recorded" \
    || say "WARNING could not file the pause issue; the loop is still paused"
}

TODAY="$(date -u +%Y-%m-%d)"
TICKS_FILE="$STATE/ticks-$TODAY"
TICKS="$(counter "$TICKS_FILE")" \
  || die "the tick counter at $TICKS_FILE is unreadable or not a number, so the
  daily budget cannot be enforced. Fix or remove it."
if [ "$TICKS" -ge "$MAX_TICKS" ]; then
  say "already $TICKS ticks today (max $MAX_TICKS); stopping"
  exit 0
fi
# WRITTEN AND VERIFIED BEFORE ANY WORK, so a state directory that cannot be
# written stops the tick instead of letting it run unbudgeted forever.
set_counter "$TICKS_FILE" "$((TICKS + 1))" \
  || die "cannot persist the tick counter at $TICKS_FILE; refusing to run"

# --------------------------------------------------------------------------
# Context. Bounded on purpose: a leader handed the whole history re-derives
# conclusions instead of acting on them. (CTX itself is created much earlier --
# see the PAUSE check, which needs somewhere to put a released pause's
# directive.)
# --------------------------------------------------------------------------
say "reading scored history"
if ! "$TRUSTED/results.sh" --json >"$CTX/results.json" 2>"$CTX/results.err"; then
  die "results.sh failed: $(head -c 300 "$CTX/results.err")"
fi

# THE BUDGET, counted from SUBMITTED JOBS and not from scored results. Counting
# `results.sh` rows would have counted only successes: an OOM, a refusal, a
# crashed probe and a probe still awaiting evaluation all cost real host time and
# would each have scored zero against the cap, so twelve ticks could have
# launched twelve expensive failures under a four-run budget.
#
# Counted from the dispatcher rather than a local counter, because a counter
# drifts the first time a human runs an experiment by hand, and the question is
# what the HOST spent today.
JOBS_JSON="$CTX/jobs.json"
if ! qf list --limit 500 --json >"$JOBS_JSON" 2>"$CTX/jobs.err"; then
  # FAIL CLOSED. An unreadable job list means the budget cannot be enforced, and
  # an unenforced budget on an unattended loop is the whole risk.
  die "cannot read \`qf list\`, so the budget cannot be enforced: $(head -c 300 "$CTX/jobs.err")"
fi
# JOBS STILL IN FLIGHT. The tick's header used to claim the leader "blocks on
# `experiment.py run` for up to 90 minutes"; it does not. The leader is an agent
# with its own tool timeouts, so it can submit a probe, return, and leave the
# tick to exit while the dispatcher trains for half an hour. Two consecutive
# ticks then found nothing to do and recorded nothing.
#
# So the leader is TOLD what is already running. Without it, action 4 looks
# available while an experiment is mid-flight, and the only thing stopping a
# second submission is the daily budget.
read -r PROBES_TODAY EXTRACTS_TODAY IN_FLIGHT <<EOF
$(python3 - "$JOBS_JSON" "$TODAY" <<'PY2'
import json, sys
jobs = (json.load(open(sys.argv[1])) or {}).get("jobs") or []
today = sys.argv[2]
def n(kind):
    return sum(1 for j in jobs if j.get("kind") == kind
               and (j.get("submitted_at") or "").startswith(today))
# `store.py:27`. Anything NOT terminal is still the dispatcher's business --
# including BUILDING, whose omission from a state set has caused three silent
# bugs in this project already.
TERMINAL = {"SUCCEEDED", "FAILED", "TIMEOUT", "CANCELLED", "REFUSED"}
live = sum(1 for j in jobs
           if j.get("kind") in ("probe", "extract")
           and j.get("state") not in TERMINAL)
print(n("probe"), n("extract"), live)
PY2
)
EOF
if [ "${PROBES_TODAY:-999}" -ge "$MAX_RUNS" ]; then
  say "$PROBES_TODAY probe(s) submitted today (max $MAX_RUNS); stopping"
  exit 0
fi
if [ "${EXTRACTS_TODAY:-999}" -ge "$MAX_EXTRACTS" ]; then
  say "$EXTRACTS_TODAY extract(s) submitted today (max $MAX_EXTRACTS);"
  say "  the loop may still write up results, but not build another cohort"
  NO_MORE_EXTRACTS=1
fi

# THE EXTRACT CAP IS MECHANICAL, NOT ADVISORY. Telling the leader in its prompt
# that the budget is spent is a request, and the whole premise here is that the
# leader is untrusted -- an agent that ignores the sentence submits a second long
# read against the production database. So when the budget is spent, `qf` is
# SHADOWED for the leader by a shim that forwards everything except an extract
# submission.
#
# A shim rather than stopping the tick, because the other five actions are still
# valuable: writing up a finished result costs nothing and is what the loop is
# for. Stopping would trade a real cap for a lost day.
SHIM="$CTX/shim"
mkdir -p "$SHIM" || die "cannot create the shim directory"
REAL_QF="$(command -v qf)" || die "no \`qf\` on PATH"
# CHECKS ONLY $1, and that is exact rather than lazy: `qf extract` is its own
# subcommand (`qf` line 624), and `qf submit --kind` accepts only `test` and
# `selftest`, so an extraction cannot be requested any other way. Matching the
# WORD "extract" anywhere in argv would have been wrong and dangerous -- every
# probe carries `--extract <hash>`, so a broad match would have blocked the one
# action the loop most needs to keep working.
cat >"$SHIM/qf" <<SHIMEOF
#!/usr/bin/env bash
# Generated per tick by tick.sh. Refuses ONE subcommand; forwards everything
# else untouched, including \`probe --extract <hash>\`.
if [ "\${1:-}" = "extract" ]; then
  echo "qf: refused: today's extract budget ($MAX_EXTRACTS) is spent." >&2
  echo "  Enforced by the tick, not by policy. Pick another action." >&2
  exit 3
fi
exec "$REAL_QF" "\$@"
SHIMEOF
chmod +x "$SHIM/qf" || die "cannot make the shim executable"
if [ "$NO_MORE_EXTRACTS" = 1 ]; then
  LEADER_PATH="$SHIM:$PATH"
  say "extract submission is shimmed off for this tick"
else
  LEADER_PATH="$PATH"
fi

# THE JOURNAL IS AN INPUT, not just an output. Without it the frontier cannot
# tell a new result from one the loop wrote up an hour ago, so the leader's first
# action ("a finished run is unrecorded") would match forever and every tick
# would re-narrate the same row instead of advancing.
mkdir -p "$JOURNAL/escalations" || die "cannot write $JOURNAL"

say "building the frontier"
frontier() {  # frontier <results.json> <out-prefix>
  python3 "$HERE/frontier.py" --journal "$JOURNAL" <"$1" >"$2.md" \
    2>"$2.err" || return 1
  # BOTH HALVES MUST SUCCEED, and the JSON half is the one that matters: it is
  # the copilot's entire evidence. `|| : >"$2.json"` used to swallow a failure
  # here and leave an EMPTY file behind, which the refresh path then treated as
  # a successful fresh snapshot -- so the copilot would have been handed `{}`
  # and told it was current, and every cited figure would have looked fabricated.
  python3 "$HERE/frontier.py" --journal "$JOURNAL" --json <"$1" >"$2.json" \
    2>>"$2.err" || return 1
  # A well-formed but EMPTY payload is also a failure of this function: an
  # evidence file with no rows cannot verify anything.
  [ -s "$2.json" ] || return 1
}
frontier "$CTX/results.json" "$CTX/frontier" \
  || die "frontier.py failed: $(head -c 300 "$CTX/frontier.err")"

# IS THERE ANYTHING TO DO? Asked BEFORE the leader is invoked, because invoking
# it is the expensive part. Measured on 2026-09-01: two consecutive ticks whose
# only possible conclusion was "the probe is still training" cost $1.48 and
# $1.26 -- 2.4M tokens, 94% of it cache reads -- and recorded nothing. Paying
# an agent turn to be told to wait is the one cost here with no upside.
#
# The condition is deliberately narrow: a job is in flight (so actions 4 and 5
# are unavailable), AND no scored run is unwritten (actions 1 and 2), AND nothing
# is PROMISING (action 3). Then only action 6 remains, and its content is already
# known -- so it is written here rather than bought.
NOTHING_TO_DO=0
if [ "${IN_FLIGHT:-0}" -gt 0 ] && [ "${QF_TICK_ALWAYS_LEAD:-0}" != 1 ]; then
  read -r UNRECORDED PROMISING <<EOF
$(python3 - "$CTX/frontier.json" <<'PYIDLE'
import json, sys
try:
    with open(sys.argv[1]) as fh:
        report = json.load(fh)
except (OSError, ValueError):
    # UNREADABLE MEANS RUN THE LEADER. Skipping a tick on a parse failure would
    # turn a reporting glitch into a silently stalled loop.
    print("1 1")
else:
    configs = (report.get("configs") or {}).values()
    print((report.get("health") or {}).get("unrecorded_runs", 1),
          sum(1 for c in configs if c.get("status") == "PROMISING"))
PYIDLE
)
EOF
  if [ "${UNRECORDED:-1}" = 0 ] && [ "${PROMISING:-1}" = 0 ]; then
    NOTHING_TO_DO=1
  fi
fi

if [ "$NOTHING_TO_DO" = 1 ]; then
  say "$IN_FLIGHT job(s) in flight, nothing unrecorded, nothing PROMISING:"
  say "  the only available action is 'wait', so the leader is not invoked."
  say "  (override with QF_TICK_ALWAYS_LEAD=1)"
  set_counter "$TICKS_FILE" "$TICKS"     || say "note: could not roll the tick counter back"
  exit 0
fi

say "checking the host"
if ! "$TRUSTED/experiment.py" doctor >"$CTX/doctor.txt" 2>&1; then
  # NOT fatal by itself. `doctor` reports notes as well as blockers, and its exit
  # status is one bit; the leader is given the text and told to stop if it names
  # a blocker. Aborting here would make a cosmetic note stop the loop.
  say "doctor exited non-zero; passing its output to the leader"
fi

PENDING="$JOURNAL/PENDING.md"
: >"$PENDING"

# THE FACTS THIS TICK ESTABLISHED, in one file given to BOTH agents.
#
# The leader used to be told "Probes submitted today: 2 of 4" and the copilot
# was not, so a leader that faithfully repeated the number was rejected for
# citing a figure with no source -- and it was right, and so was the copilot.
# The tick computed that number; withholding it from the verifier made a true,
# tick-authored fact unciteable, and the leader had no way to win.
#
# This is deliberately NOT "give the copilot the leader's context". The queue,
# the frontier prose, the doctor output and the command list are instructions to
# the leader, and handing them to the verifier would invite it to check the
# entry against the leader's own briefing rather than against the numbers. Only
# figures the TICK ITSELF measured belong here.
{
  echo "## Facts established by this tick (authored by tick.sh, not by an agent)"
  echo
  echo "These counts are measured by the tick before either agent runs. They are"
  echo "a valid source: an entry may cite them without pasting a command."
  echo
  echo "- Probes submitted today: $PROBES_TODAY of $MAX_RUNS."
  echo "- Extracts submitted today: $EXTRACTS_TODAY of $MAX_EXTRACTS."
  # `TICKS` IS THE COUNT BEFORE THIS ONE. The counter is persisted as TICKS + 1
  # above, before any work, so by the time this file is written the slot is
  # already spent -- printing $TICKS said "0 of 12" during the first tick of the
  # day. The probe and extract counts above are NOT adjusted the same way: they
  # come from `qf list` and count jobs already submitted, so they correctly
  # exclude anything this tick is about to do.
  echo "- This is tick $((TICKS + 1)) of $MAX_TICKS today."
  echo "- Jobs in flight at the start of this tick: ${IN_FLIGHT:-0}."
} >"$CTX/tick-facts.md"

# THE PREVIOUS TICK'S REJECTION, HANDED BACK TO THE LEADER -- and to the leader
# only.
#
# Until this existed every tick was a blind attempt. The copilot's reason went
# into an escalation file the leader never reads, so the same entry could be
# rewritten three ticks running against an objection it had never seen, and the
# streak counter treated that as a drifting leader and paused the loop. This
# closes that one loop and nothing else: no extra invocation, no revise round,
# no change to the gate or to what counts as a disagreement.
#
# NOT TO THE COPILOT, deliberately. Showing a verifier its own previous verdict
# anchors it: the question is whether THIS entry is supported by THIS tick's
# numbers, and a copy of what it said last time is an argument, not a number.
#
# NOT EVIDENCE, and said so in-band. This is another agent's prose about a run
# that is over. A figure quoted here has no more standing than a remembered one,
# and the copilot -- which cannot see this block -- will reject any number whose
# only source is it.
: >"$CTX/prev-escalation.md"
PREV_ESC="$(ls -1 "$JOURNAL/escalations"/*.md 2>/dev/null | LC_ALL=C sort | tail -1)"
if [ -n "$PREV_ESC" ]; then
  # The fenced block this script itself writes after the NOT RECORDED heading,
  # which is the copilot's reason verbatim. Anchored on that heading rather than
  # on the first fence in the file, because the rejected entry above it usually
  # contains fences of its own -- an `Evidence:` block is pasted command output.
  awk '
    /^## NOT RECORDED/         { seen = 1; next }
    seen && !inblock && /^```/ { inblock = 1; next }
    inblock && /^```/          { exit }
    inblock                    { print }
  ' "$PREV_ESC" >"$CTX/prev-reason.txt" 2>/dev/null || : >"$CTX/prev-reason.txt"
  if [ -s "$CTX/prev-reason.txt" ]; then
    {
      echo "## Your previous entry was NOT recorded (feedback, NOT evidence)"
      echo
      echo "The copilot returned VERDICT: DISAGREE on the last entry"
      echo "($(basename "$PREV_ESC")). Its reason, verbatim:"
      echo
      echo '```'
      head -c "$MAX_FEEDBACK_BYTES" "$CTX/prev-reason.txt"
      # A NEWLINE OF OUR OWN before the closing fence: `head -c` cuts mid-line.
      echo
      echo '```'
      if [ "$(wc -c <"$CTX/prev-reason.txt")" -gt "$MAX_FEEDBACK_BYTES" ]; then
        echo
        echo "**[TRUNCATED at $MAX_FEEDBACK_BYTES bytes. The whole text is in"
        echo "$PREV_ESC.]**"
      fi
      echo
      echo "Read that as feedback on how to write THIS tick's entry, and not as"
      echo "a source. No figure appearing in it may be cited: to use one, obtain"
      echo "it again from the JSON, from the tick facts above, or from a command"
      echo "you paste into \`Evidence:\`. The copilot has NOT been shown this"
      echo "block and will judge your new entry on its own."
      echo
      echo "The rejected entry itself is at $PREV_ESC. It is NOT a finding and"
      echo "must not be cited as one -- if its claim still holds, re-establish it"
      echo "here from the numbers rather than referring back to it."
    } >"$CTX/prev-escalation.md"
  fi
fi

{
  echo "# Tick context — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo
  cat "$CTX/tick-facts.md"
  echo
  if [ "${IN_FLIGHT:-0}" -gt 0 ]; then
    echo "**${IN_FLIGHT} job(s) are STILL RUNNING from an earlier tick.**"
    echo "Actions 4 and 5 are unavailable: do not submit another experiment"
    echo "while one is in flight. If nothing else applies, that is action 6 --"
    echo "write the paragraph to your journal entry as usual."
    qf list --limit 20 2>/dev/null | awk '$2 !~ /SUCCEEDED|FAILED|TIMEOUT|CANCELLED|REFUSED/ {print "  " $0}' \
      || true
  fi
  [ "$NO_MORE_EXTRACTS" = 0 ] \
    || echo "**The extract budget is spent: action 5 is unavailable this tick.**"
  echo
  # Leader-only, and placed before the briefing so it is read before the entry
  # is planned rather than after it is written.
  if [ -s "$CTX/prev-escalation.md" ]; then
    cat "$CTX/prev-escalation.md"
    echo
  fi
  # THE HUMAN'S RELEASE COMMENTS (design §4.3), leader-only and labelled
  # non-evidence on exactly the same contract as the escalation feedback above:
  # a figure typed into a GitHub comment is still not a source, and the copilot
  # never sees this block, so it cannot be cited past the gate.
  #
  # CONSUMED ONCE. Deleted from the state directory as it is injected -- a
  # directive from a pause three weeks ago is not an instruction about this
  # tick, and a `check` that resumed nothing must not re-serve the last one.
  #
  # NOT RE-CAPPED HERE. `pause-issue.sh` already caps the COMMENT TEXT at
  # QF_TICK_MAX_FEEDBACK_BYTES, and it is the only layer that knows which bytes
  # are the untrusted prose and which are the rules about it. A second blind
  # `head -c` at this size cut the tail off the whole file instead -- removing
  # the citation ban precisely when the most human prose had arrived. One cap,
  # in the layer that can aim it.
  if [ -s "$CTX/human-directive.md" ]; then
    cat "$CTX/human-directive.md"
    echo
    rm -f "$STATE/human-directive.md"
  fi
  # RETRY MODE, AND THE ACTION IS NOT OPEN IN IT (design §3). A retry must
  # SHRINK the entry, not rewrite it: on 2026-09-04 round 2 fixed the defect it
  # was told about and introduced a new one, because rewriting resamples the
  # defect surface and every added hedge is another sentence that can be wrong.
  #
  # PLACED AFTER THE REJECTION FEEDBACK, which is the objection this shrink
  # answers, and BEFORE the briefing, so both are read before the entry is
  # planned. And the tick NAMES the target: deriving the retry from whatever
  # the leader happens to pick cannot work when the instruction has to be read
  # before the choice is made.
  #
  # AND IT MUTATES STATE: `take_retry_target` clears a stored target that is no
  # longer suppressible, here, during context assembly. That is the point -- the
  # decision cannot be derived from an entry that does not exist yet -- but it
  # is why the name says `take`.
  #
  # FAIL CLOSED, ALL OR NOTHING. Half a retry block is worse than none: the
  # target line alone steers the leader onto action 1 on one named row with the
  # shrink instruction and the four bans absent, and the template without the
  # pasted row mandates fields whose figures the leader cannot see. Either way
  # it rewrites rather than shrinks, which is the 2026-09-04 failure. So the row
  # is extracted BEFORE anything is emitted, and a failure degrades to an
  # ORDINARY tick -- the stored target stays, so the streak stays suppressed and
  # retirement still bounds it; one tick's instruction is lost, not the brake.
  if RETRY_ID="$(take_retry_target "$CTX/frontier.json" "$TARGET_FILE")" \
     && retry_row "$CTX/frontier.json" "$RETRY_ID" >"$CTX/retry-row.json"; then
    cat "$HERE/retry-prompt.md"
    echo
    echo "The target of this retry is \`$RETRY_ID\`."
    echo
    echo "Its row from the frontier JSON at full precision, under \`target\`,"
    echo "with the pre-registered comparator row under \`comparator\` and the"
    echo "series' \`holdout\` window above both. This is the source for every"
    echo "field of the template above; you have not been given the JSON it came"
    echo "from. A null \`comparator\` carries a \`comparator_note\` saying why."
    echo
    echo '```json'
    cat "$CTX/retry-row.json"
    echo '```'
    echo
  elif [ -n "${RETRY_ID:-}" ]; then
    # SAID LOUDLY, because the tick has just decided a retry was due and then
    # not asked for one: an operator seeing repeated rejections of one run needs
    # to know the steer never reached the leader.
    say "WARNING could not paste the row for retry target $RETRY_ID; this tick" >&2
    say "  runs as an ORDINARY tick rather than emitting half a retry block" >&2
  fi
  # ABSOLUTE PATHS, SUPPLIED. The leader runs as `research`, whose PATH does not
  # carry the trusted host directory, and a leader that guesses `./experiment.py`
  # spends its one action discovering that. The workspace is named for the same
  # reason: `journal/PENDING.md` is relative to a directory it must not have to
  # find.
  echo "## Commands (use these exact paths)"
  echo
  echo '```'
  echo "$TRUSTED/experiment.py plan|run <config>   # QF_REQUIRE_PREREG is set"
  echo "qf probe|evaluate|extract|list|status --help  # typed job kinds"
  echo "$TRUSTED/results.sh [--json]               # every scored run"
  echo "workspace (yours, writable):  $QF_RESEARCH"
  echo "journal entry to write:       $PENDING"
  echo '```'
  echo
  echo "## Host"
  echo '```'
  cat "$CTX/doctor.txt"
  echo '```'
  echo
  cat "$CTX/frontier.md"
  echo
  echo "## The queue (read-only; you cannot write this file)"
  echo
  # BOUNDED BY BYTES, NOT ONLY BY LINES. `sed -n '1,400p'` bounds nothing useful:
  # a 400-line markdown table is small and 400 lines of prose is not, and this
  # file only ever grows. Stdin removed the hard argv cliff, so an overlong
  # prompt no longer CRASHES the tick -- it quietly degrades the leader instead,
  # which is harder to notice.
  #
  # TRIMMED FROM THE END and SAID SO. The queue's ranked list is at the end, so
  # losing it silently would remove the one thing the leader is asked to act on.
  if [ ! -r "$QUEUE" ]; then
    echo "(unreadable: $QUEUE)"
  else
    QUEUE_BYTES="$(wc -c <"$QUEUE")"
    if [ "$QUEUE_BYTES" -le "$MAX_QUEUE_BYTES" ]; then
      cat "$QUEUE"
    else
      head -c "$MAX_QUEUE_BYTES" "$QUEUE"
      echo
      echo "**[TRUNCATED: this excerpt is the first $MAX_QUEUE_BYTES of"
      echo "$QUEUE_BYTES bytes. The ranked list of entries is at the END of that"
      echo "file, so it may be missing here -- read it by absolute path before"
      echo "concluding that an entry does not exist.]**"
    fi
  fi
} >"$CTX/context.md"

if [ "$DRY_RUN" = 1 ]; then
  say "--dry-run: the leader would see"
  cat "$CTX/context.md"
  exit 0
fi

# --------------------------------------------------------------------------
# The leader.
# --------------------------------------------------------------------------
if ! command -v claude >/dev/null 2>&1; then
  die "no \`claude\` on PATH.
  This is almost never a missing install -- check the NON-INTERACTIVE path,
  which is not the one an ssh session shows you:
      sudo -H -u research bash -lc 'command -v claude'   # what the tick sees
      sudo -H -u research bash -ic 'command -v claude'   # what you see
  If the second finds it and the first does not, nvm's init is in ~/.bashrc,
  which returns early for non-interactive shells. $HERE/agent-env.sh exists to
  fix exactly that; check it is readable by the research user."
fi
say "leader starting"
LEADER_LOG="$CTX/leader.log"
# ON STDIN, NOT IN ARGV, and this is not cosmetic:
#
#   1. A single argv entry is capped at MAX_ARG_STRLEN = 131072 bytes on Linux
#      (32 pages, not tunable). The assembled prompt is ~26KB today, but the
#      frontier grows with every scored run -- roughly 240 bytes per run across
#      its two tables -- so a few hundred experiments would have hit E2BIG and
#      the loop would have started failing for a reason nothing here explains.
#   2. Argv is world-readable in `ps`. The whole prompt, including the queue
#      excerpt, was visible to every account on the host.
#
# Both CLIs support it: `claude -p` reads the prompt from stdin when no
# positional is given, and `codex exec -` is documented to do the same.
cat "$HERE/tick-prompt.md" >"$CTX/leader-input.md"
printf '\n\n' >>"$CTX/leader-input.md"
cat "$CTX/context.md" >>"$CTX/leader-input.md"
LEADER_BYTES="$(wc -c <"$CTX/leader-input.md")"
say "leader input: $LEADER_BYTES bytes"
# REPORTED AND WARNED, never truncated wholesale: the frontier is the part that
# grows with history, and dropping rows from it is a research decision rather
# than a plumbing one. If this fires, bound the frontier deliberately.
[ "$LEADER_BYTES" -lt 100000 ] \
  || say "WARNING the leader prompt is $LEADER_BYTES bytes and growing with
  scored history. Nothing is truncated, but consider bounding the frontier."

# PATH="$LEADER_PATH": when the extract budget is spent this puts the refusing
# shim ahead of the real `qf` for the leader and everything it spawns, including
# `experiment.py`, whose own `qf` calls (probe, evaluate, extracts, contracts)
# pass through untouched.
LEADER_RAW="$CTX/leader.json"
LEADER_ERR="$CTX/leader.err"
LEADER_RC=0
PATH="$LEADER_PATH" claude -p "${LEADER_FLAGS[@]}" --output-format json \
  <"$CTX/leader-input.md" >"$LEADER_RAW" 2>"$LEADER_ERR" || LEADER_RC=$?
python3 "$HERE/usage.py" claude "$LEADER_RAW" "$USAGE_LOG" \
  "$LEADER_RC" >"$LEADER_LOG" \
  || say "WARNING: could not append Claude usage to $USAGE_LOG"
if [ -s "$LEADER_ERR" ]; then
  cat "$LEADER_ERR" >>"$LEADER_LOG"
fi
if [ "$LEADER_RC" -ne 0 ]; then
  say "leader exited non-zero; its output:"
  tail -c 2000 "$LEADER_LOG"
  # Not an escalation: a crashed leader made no claim, so there is nothing to
  # verify and nothing to record. REMOVED rather than left for the next tick,
  # because a half-written entry that survived a crash is the one thing that
  # could reach the journal without ever having been verified.
  rm -f "$PENDING"
  exit 1
fi
tail -c 2000 "$LEADER_LOG"

if [ ! -s "$PENDING" ]; then
  say "the leader wrote no journal entry; treating this tick as a NOOP"
  rm -f "$PENDING"
  exit 0
fi

# --------------------------------------------------------------------------
# The copilot. Verifies the CLAIM, not the arithmetic.
#
# DELIBERATELY NARROWER THAN THE DESIGN'S "independent derivation". The metrics
# are computed by the root-owned evaluator and already reproduce bit-identically
# across re-evaluations; a second LLM recomputing them would not be a second
# source of truth, it would be a worse one. What has actually gone wrong in this
# project is never the arithmetic -- it is the sentence about the arithmetic
# ("capacity actively dilutes the model", carried for weeks from a confounded
# run). So the copilot is pointed at exactly that.
# --------------------------------------------------------------------------
# REFRESHED, and this is the difference between verifying and rubber-stamping.
# The snapshot above was taken BEFORE the leader ran. If the leader's one action
# was to run an experiment, its result does not appear in that snapshot at all --
# so the copilot would be asked to check figures against numbers that predate
# them, and would have to reject every genuinely new result or accept it blind.
say "refreshing the evidence for verification"
if "$TRUSTED/results.sh" --json >"$CTX/results2.json" 2>"$CTX/results2.err" \
   && frontier "$CTX/results2.json" "$CTX/frontier2"; then
  EVIDENCE="$CTX/frontier2.json"
else
  # FALLS BACK LOUDLY, and the copilot is told. Stale evidence that is labelled
  # stale can still catch a cross-series comparison or an overreach; stale
  # evidence presented as current cannot be reasoned about at all.
  say "could not refresh; the copilot gets the pre-leader snapshot, labelled"
  EVIDENCE="$CTX/frontier.json"
  STALE=" (NOTE: this snapshot predates the leader's action and may not contain
its result. If a cited figure is absent for that reason, say so explicitly
rather than treating it as fabricated.)"
fi
STALE="${STALE:-}"

if ! command -v codex >/dev/null 2>&1; then
  say "no \`codex\` on PATH: recording nothing, because an unverified claim is"
  say "  what this step exists to prevent. Escalating instead."
  say "  (If \`bash -ic 'command -v codex'\` finds it, see agent-env.sh.)"
  VERDICT="DISAGREE"
  # AN OUTAGE, NOT A REJECTION, and the distinction is not cosmetic in two
  # places. A copilot that is not installed judged nothing, so counting it as
  # drift accuses the leader of a claim nobody read -- and the escalation's
  # `Escalation kind:` line is machine-read by `frontier.py`'s retirement
  # counting, so labelling this a rejection would retire the run after two
  # `codex` outages, which is precisely the harm retirement exists to prevent.
  VERIFIER_FAILED=1
  REASON="codex is not installed, so the claim could not be verified"
else
  say "copilot verifying"
  # ASSEMBLED TO A FILE for the same two reasons as the leader's, and the
  # evidence JSON is the part that grows -- it carries every scored row.
  {
    cat "$HERE/verify-prompt.md"
    echo
    # THE SAME FILE THE LEADER GOT, so a count the tick measured is citeable by
    # the entry and checkable by the verifier. Before this, only the leader saw
    # it and "this spends probe 3 of 4" read as a figure with no source.
    cat "$CTX/tick-facts.md"
    echo
    echo "## The claim to check"
    echo
    cat "$PENDING"
    echo
    echo "## The numbers it must be consistent with$STALE"
    echo
    echo '```json'
    cat "$EVIDENCE"
    echo '```'
  } >"$CTX/verify-input.md"
  say "copilot input: $(wc -c <"$CTX/verify-input.md") bytes"
  CODEX_RAW="$CTX/codex.jsonl"
  CODEX_ERR="$CTX/codex.err"
  # RETRIED, because a copilot that could not START is the single most common
  # way this loop has failed: `codex` has died on `Failed to load cloud config
  # bundle` twice -- once the tinyproxy allowlist, once with no proxy entry at
  # all, so upstream -- and each time an entry that might have been recordable
  # escalated instead.
  #
  # ONLY ON A NON-ZERO EXIT. A copilot that ran and returned DISAGREE, or that
  # returned prose with no verdict, has ANSWERED; re-asking it until it says
  # something else is not a retry, it is shopping for a verdict.
  #
  # USAGE IS LOGGED PER ATTEMPT, not once at the end: a failed attempt can still
  # have spent tokens, and the cost log is the only place that is visible.
  ATTEMPT=1
  while :; do
    CODEX_RC=0
    # `-` IS EXPLICIT: `codex exec` reads stdin when the prompt is `-` or
    # absent, but "if stdin is piped AND a prompt is also provided, stdin is
    # appended as a <stdin> block" -- so passing both would silently reshape
    # the prompt.
    codex exec "${COPILOT_FLAGS[@]}" --json - <"$CTX/verify-input.md" \
      >"$CODEX_RAW" 2>"$CODEX_ERR" || CODEX_RC=$?
    python3 "$HERE/usage.py" codex "$CODEX_RAW" "$USAGE_LOG" \
      "$CODEX_RC" >"$CTX/verify.log" \
      || say "WARNING: could not append Codex usage to $USAGE_LOG"
    [ "$CODEX_RC" -ne 0 ] || break
    [ "$ATTEMPT" -lt "$COPILOT_TRIES" ] || break
    # AND ONLY WHEN IT NEVER ANSWERED. A non-zero exit AFTER a verdict was
    # produced is not a startup failure: the copilot ran, judged, and then died,
    # and its verdict is still not trusted (see below) -- but re-asking it is
    # verdict shopping and costs a full invocation to do it.
    # THE RAW STREAM, not the parsed log: `usage.py` cannot always render the
    # output of a run that died, and an empty parsed log would read as "never
    # answered" for a copilot that plainly did. The verdict text survives in the
    # JSONL either way, escaped inside a string or bare.
    if grep -q "VERDICT:" "$CODEX_RAW" 2>/dev/null; then
      say "copilot exited $CODEX_RC but had already produced a verdict;"
      say "  not retrying -- that would be re-asking an answered question"
      break
    fi
    say "copilot exited $CODEX_RC on attempt $ATTEMPT of $COPILOT_TRIES:"
    say "  $(tail -c 200 "$CODEX_ERR" | tr '\n' ' ')"
    # LINEAR BACKOFF, not exponential: the failures seen so far are either
    # instant (misconfiguration, which no wait fixes) or a provider outage
    # measured in minutes, and the tick has hours of TimeoutStartSec to spend.
    say "  retrying in $((COPILOT_BACKOFF * ATTEMPT))s"
    sleep "$((COPILOT_BACKOFF * ATTEMPT))"
    ATTEMPT=$((ATTEMPT + 1))
  done
  [ "$CODEX_RC" -eq 0 ] || say "copilot failed $ATTEMPT time(s); giving up"
  CHECK="$(cat "$CTX/verify.log")"
  if [ "$CODEX_RC" -ne 0 ] && [ -s "$CODEX_ERR" ]; then
    CHECK="$CHECK
$(cat "$CODEX_ERR")"
  fi
  REASON="$(printf '%s' "$CHECK" | tail -c 1200)"
  if [ "$CODEX_RC" -ne 0 ]; then
    # A FAILED COMMAND'S OUTPUT IS NOT PARSED AT ALL. Prepending a DISAGREE line
    # to it and re-parsing was wrong twice over: the tail-wins rule then picked a
    # trailing `VERDICT: AGREE` out of the partial output, so a copilot that
    # printed AGREE and then crashed published the entry as verified.
    #
    # AND THIS IS THE OTHER HALF OF THE USABLE-VERDICT DEFINITION: verdict text
    # from a process that exited non-zero is not a verdict. So this advances the
    # verifier-failure counter and -- the direction that matters -- must not
    # RESET it, or a copilot crashing after printing a verdict would clear the
    # infrastructure brake every tick and the outage would never reach the
    # threshold.
    VERDICT="DISAGREE"
    # The DRIFT streak is not advanced for this (the verifier-failure counter
    # is, see six lines above); see the block after the escalation
    # is written for why a verifier that never ran is not a leader that drifted.
    VERIFIER_FAILED=1
    REASON="codex exited non-zero; its output is NOT trusted as a verdict:
$REASON"
  else
    # ANCHORED to its own line, and it must be the LAST NONBLANK LINE of the
    # reply -- not merely the last line that happens to match.
    #
    # THE ANCHORING WAS ALREADY HERE; THE FINALITY WAS NOT. `tail -1` over the
    # MATCHES read
    #
    #     VERDICT: AGREE
    #     Correction: the central figure is absent; this must not be recorded.
    #
    # as a usable AGREE: the entry was PUBLISHED and both counters were reset by
    # a copilot that retracted itself in the very next sentence. The contract in
    # design §2.2 is "exit 0 AND a valid anchored FINAL `VERDICT:` line", and
    # only two of those three words were implemented.
    #
    # THE OLD PROTECTION IS PRESERVED, deliberately: a model that reasons out
    # loud may name BOTH words before committing to one, so the first match
    # would read the wrong one. The last NONBLANK line still gives that reply
    # the right answer, while refusing the retracted one above -- and an
    # unanchored match would in either case accept `VERDICT: AGREE` quoted
    # inside a sentence arguing against it.
    #
    # NONBLANK, NOT LAST: `codex` output routinely ends in blank lines, and
    # "the last line" would fail every real reply -- three ticks of that pauses
    # the loop on the verifier brake over a formatting artefact.
    #
    # AND THIS IS A REAL BEHAVIOUR CHANGE, in the conservative direction: a
    # copilot that appends a trailing log line after its verdict now escalates
    # (as no usable verdict, so the verifier counter -- not the drift streak)
    # instead of recording. `verify-prompt.md` already instructs it to end with
    # exactly one line, and publishing an entry whose verifier kept talking is
    # the failure that is not recoverable.
    VERDICT="$(printf '%s\n' "$CHECK" \
               | grep -v '^[[:space:]]*$' | tail -1 \
               | grep -oE '^[[:space:]]*VERDICT:[[:space:]]*(AGREE|DISAGREE)[[:space:]]*$' \
               | grep -oE '(AGREE|DISAGREE)')"
  fi
  # NO VERDICT IS A DISAGREEMENT -- it still escalates, because nothing may be
  # recorded unverified and defaulting the other way would make the whole step
  # decorative the first time the output format drifted.
  #
  # BUT IT IS AN INFRASTRUCTURE FAILURE, NOT DRIFT, and this line used to leave
  # `VERIFIER_FAILED` at 0. A usable verdict is `exit 0 AND a valid anchored
  # final VERDICT line`; a copilot that returned prose without one verified
  # nothing, exactly like one that could not start, so it advances the verifier
  # counter rather than accusing the leader of drifting -- and its escalation is
  # labelled an outage, so it retires nothing.
  if [ -z "$VERDICT" ]; then
    VERDICT="DISAGREE"
    VERIFIER_FAILED=1
    REASON="no VERDICT line in the copilot's reply
$REASON"
  fi
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
# DISAGREE_FILE / VERIFIER_FILE / TARGET_FILE are set once near the top, from
# state-names.sh, which `pause-issue.sh` sources too.
VERIFIER_FAILED="${VERIFIER_FAILED:-0}"

# THE ENTRY'S DECLARED TARGET, and its state in the frontier THIS tick built.
#
# THE SAME SNAPSHOT THE COPILOT WAS JUDGED AGAINST, and for the same reason it
# was refreshed: `frontier2.json` is built AFTER the leader acted, so a run this
# tick submitted and scored appears there and not in the pre-leader snapshot.
# Judging the target against the stale one would read every fresh submission as
# `absent` -- not suppressible -- so the loop would advance the drift brake on
# exactly the ticks that did the most work.
#
# `$EVIDENCE`, NOT A SECOND DERIVATION FROM `[ -s frontier2.json ]`. That test
# is weaker than the one that produced `$EVIDENCE`: `frontier()` requires BOTH
# renders to succeed AND the JSON to be non-empty, so a `--json` pass that wrote
# complete output and then exited non-zero leaves a non-empty file the tick has
# already declared unusable. Re-deriving would then judge suppression against a
# snapshot the copilot was deliberately not shown -- two answers to one question.
FRONTIER_NOW="$EVIDENCE"
ENTRY_TARGET="$(entry_target "$PENDING")"
case "$ENTRY_TARGET" in
  evaluate-*) ENTRY_HANDLING="$(handling_of "$FRONTIER_NOW" "$ENTRY_TARGET")" ;;
  # `none` AND `absent` ARE THE SAME THING TO EVERY CONSUMER BELOW: neither is
  # suppressible, both increment, and both store `none`.
  *) ENTRY_TARGET=none; ENTRY_HANDLING=absent ;;
esac

if [ "$VERDICT" = "AGREE" ]; then
  mv "$PENDING" "$JOURNAL/$STAMP.md"
  RECORDED="$JOURNAL/$STAMP.md"
  # A FAILURE HERE IS NOT FATAL, unlike the increment: an un-reset streak is
  # conservative (it pauses sooner), while an un-incremented one is not.
  set_counter "$DISAGREE_FILE" 0 \
    || say "note: could not reset the disagreement counter"
  # A USABLE VERDICT MEANS THE VERIFIER IS UP, whichever way it went.
  set_counter "$VERIFIER_FILE" 0 \
    || say "note: could not reset the verifier-failure counter"
  # CLEARED ON AGREE, which is the part that was missing: resetting only the
  # NUMBER left a stored target behind, so the next rejection of a DIFFERENT run
  # could read as a retry of this one and be suppressed.
  set_target "$TARGET_FILE" none \
    || say "note: could not clear the last rejected target"
  say "verified; recording $STAMP.md"
else
  RECORDED="$JOURNAL/escalations/$STAMP.md"
  {
    cat "$PENDING"
    echo
    echo "---"
    echo
    echo "## NOT RECORDED — the copilot did not agree"
    echo
    # MACHINE-READ BY `frontier.py:escalation_targets`, which counts `rejection`
    # only. `verifier-failure` must never count toward retirement: two `codex`
    # outages would otherwise retire a write-up nobody ever judged, which is the
    # precise harm retirement exists to prevent.
    if [ "$VERIFIER_FAILED" = 1 ]; then
      echo "Escalation kind: verifier-failure"
    else
      echo "Escalation kind: rejection"
    fi
    echo
    echo "This entry is an escalation, not a finding. The claim above was not"
    echo "accepted and must not be cited as a result."
    echo
    echo '```'
    printf '%s\n' "$REASON"
    # THE KNOB DELTA GOES INSIDE THIS FENCE, and that is not stylistic: this
    # fence -- the first one after `## NOT RECORDED` -- is exactly what
    # `pause-issue.sh:issue_body` lifts for its "last three rejections,
    # verbatim" section, so a line placed OUTSIDE it reaches the journal and
    # nothing else. "Only in a journal nobody was reading" is the 2026-09-04
    # failure this whole change exists to end: if the loop is about to pause
    # with MAX_DISAGREE effectively 5 while the unit says 3, that belongs in
    # the issue a human opens.
    #
    # ATTRIBUTED IN-BAND, because the same block is quoted back to the NEXT
    # leader as the copilot's reason (see the feedback assembly above). The tick
    # already authors `REASON` itself on every outage path -- "codex is not
    # installed", "no VERDICT line in the copilot's reply" -- so tick-authored
    # text in here is the existing contract, and the marker is what keeps the
    # provenance honest.
    #
    # ONLY WHEN THERE IS SOMETHING TO SAY. Empty on a clean check, which is
    # every production tick that is not overridden, so this adds nothing to the
    # ordinary escalation.
    [ -z "$ENV_NOTE" ] || printf '%s\n' "" \
      "-- the lines below are tick.sh, not the copilot: this tick's QF_* knobs" \
      "   were not confirmed against qf-tick.service --" "$ENV_NOTE"
    echo '```'
  } >"$RECORDED"
  rm -f "$PENDING"

  # IF A STREAK CANNOT BE COUNTED, PAUSE NOW. Both counters used `cat || echo 0`
  # once, so an unreadable or unwritable state directory silently reset them:
  # every failure recorded "1" and no threshold was ever reached, so the one
  # automatic brake on a loop that cannot make progress did not exist. Not being
  # able to count to three is a reason to stop, not to continue.
  if [ "$VERIFIER_FAILED" = 1 ]; then
    # INFRASTRUCTURE. The retry loop above absorbs the transient case, which is
    # the one that kept costing recordable entries; what is left after
    # COPILOT_TRIES attempts is a copilot that is DOWN, and a loop that cannot
    # verify anything must not keep spending a leader turn an hour -- twelve a
    # day, recording nothing -- because the reason it cannot verify is the
    # network rather than the research.
    #
    # NOTHING IS STORED AS A TARGET, and the stored one is left exactly as it
    # was. The entry was never judged, so this tick is no evidence at all about
    # which run the leader is stuck on: storing one would suppress a future
    # rejection nobody has made yet, and clearing one would forget a genuine
    # repeat that an outage merely interrupted.
    if PREV="$(counter "$VERIFIER_FILE")" \
       && set_counter "$VERIFIER_FILE" "$((PREV + 1))"; then
      VN=$((PREV + 1))
    else
      VN="$MAX_VERIFIER_FAILS"
      say "WARNING the verifier-failure counter at $VERIFIER_FILE cannot be"
      say "  persisted, so the streak cannot be tracked. Pausing now."
    fi
    say "no usable verdict ($VN consecutive); escalated to escalations/$STAMP.md"
    say "  the copilot did not answer: this streak is infrastructure, not drift"
    if [ "$VN" -ge "$MAX_VERIFIER_FAILS" ]; then
      # THE STATUS IS ACTED ON, NOT LOGGED. See `finish`: a brake that did not
      # take must reach `OnFailure=`, or the CRITICAL line is the only trace and
      # nothing reads it.
      pause_now "consecutive-verifier-failures" "$VN" "$RECORDED" "$STAMP" \
        || PAUSE_UNWRITABLE=1
    fi
  else
    # RESEARCH DRIFT, counted once per rejected target EPISODE. Three ticks on
    # 2026-09-04 each rewrote the SAME finished run and were each rejected on
    # incidental prose; the third hit the threshold and paused the loop for 2.5
    # days. That is one episode, not three, and what must bound it is retirement
    # (the run stops being offered after two rejections), not the brake.
    #
    # SUPPRESSION IS THEREFORE CONDITIONAL ON THE TARGET BEING ONE RETIREMENT
    # CAN REACH -- see `suppressible`. Everything else advances.
    STORED="$(target "$TARGET_FILE")" || STORED=""
    # THE ORDER OF THESE TWO IS LOAD-BEARING. `none` equals `none`, so the
    # string comparison ALONE would suppress a rejection of "no target at all"
    # against the last tick's "no target at all" -- an unbounded episode, since
    # `none` retires nothing. `suppressible`'s veto is what stops that, and
    # `driftnone` is the case that catches its removal.
    if suppressible "$ENTRY_HANDLING" && [ "$STORED" = "$ENTRY_TARGET" ]; then
      # FAIL CLOSED HERE TOO, *AND SAY SO*. An unreadable counter in this branch
      # used to become `MAX_DISAGREE` silently, so the PAUSE file reported a
      # drift streak at its threshold when the real cause was a counter that
      # could not be read -- the same misdiagnosis the other two branches warn
      # about. The direction is right; only the diagnostic was missing.
      if ! N="$(counter "$DISAGREE_FILE")"; then
        N="$MAX_DISAGREE"
        say "WARNING the disagreement counter at $DISAGREE_FILE cannot be"
        say "  read, so the streak cannot be tracked. Pausing now."
      fi
      # `last-reject-target` IS DELIBERATELY NOT WRITTEN HERE. It already holds
      # this exact id -- that is what `STORED = ENTRY_TARGET` just established --
      # so a write would be a no-op whose only effect could be to FAIL, and turn
      # a bounded repeat into a fresh episode on the next tick.
      say "NOT verified; same target as last tick ($ENTRY_TARGET)"
      say "  the drift streak stays at $N -- retirement bounds this, not the brake"
      say "  escalated to escalations/$STAMP.md"
    else
      if PREV="$(counter "$DISAGREE_FILE")" \
         && set_counter "$DISAGREE_FILE" "$((PREV + 1))"; then
        N=$((PREV + 1))
      else
        N="$MAX_DISAGREE"
        say "WARNING the disagreement counter at $DISAGREE_FILE cannot be"
        say "  persisted, so the streak cannot be tracked. Pausing now."
      fi
      # A TARGET IS STORED ONLY IF SUPPRESSION COULD LEGITIMATELY APPLY TO IT
      # NEXT TIME. Storing an ambiguous, absent, recorded or retired id would
      # suppress a streak retirement cannot bound -- the 2026-09-04 livelock
      # with the drift brake switched off.
      if suppressible "$ENTRY_HANDLING"; then
        set_target "$TARGET_FILE" "$ENTRY_TARGET" \
          || { say "WARNING cannot persist the rejected target at $TARGET_FILE;"
               say "  the next rejection of $ENTRY_TARGET will count as a new one"; }
      else
        # SET, NOT LEFT ALONE. A stale stored target must not survive a
        # rejection of something else, or the tick after it could read as a
        # repeat of a run this leader has stopped working on.
        set_target "$TARGET_FILE" none \
          || say "note: could not clear the last rejected target"
        say "  target $ENTRY_TARGET is $ENTRY_HANDLING: not suppressible"
      fi
      say "NOT verified ($N consecutive); escalated to escalations/$STAMP.md"
    fi
    # A VERIFIER THAT ANSWERED IS UP, and whether it agreed is not an
    # infrastructure fact. Without this a copilot that failed twice and then
    # returned a real DISAGREE would keep accumulating towards an outage pause
    # across ticks it actually verified.
    set_counter "$VERIFIER_FILE" 0 \
      || say "note: could not reset the verifier-failure counter"
    if [ "$N" -ge "$MAX_DISAGREE" ]; then
      # SAME CONTRACT AS THE VERIFIER BRAKE ABOVE, and it must stay the same:
      # the two brakes differ in what they mean, never in whether a failed write
      # is a failed tick.
      pause_now "consecutive-disagreements" "$N" "$RECORDED" "$STAMP" \
        || PAUSE_UNWRITABLE=1
    fi
  fi
fi

# --------------------------------------------------------------------------
# Publish. The journal is the agent's own repository, so this is a push to
# `qf-research` and nothing else -- the loop has no credential on the monorepo,
# which is why `experiment-queue.md` arrives read-only.
# --------------------------------------------------------------------------
# Re-asserted rather than assumed: cwd was set at startup, and this step is a
# `git` push that means the wrong thing from the wrong directory.
cd "$QF_RESEARCH" || die "no workspace at $QF_RESEARCH"

# ONLY THE FILE THAT WAS JUST VERIFIED IS STAGED. `git add -A journal` staged
# everything, and the leader shares this uid -- so it could have edited an older
# entry and had that change committed without the copilot ever seeing it. The
# copilot checks one PENDING file; only that file's successor may be published.
#
# THIS IS NOT AN ENFORCEMENT BOUNDARY and must not be read as one. The leader
# runs as `research`, as does this script, so it can also touch PAUSE, the tick
# counters and the disagreement streak. The authoritative record is elsewhere by
# design: the pre-registration is hash-chained in the dispatcher's store, the
# metrics come from the root-owned evaluator, and the job history is SQLite the
# research identity cannot write. The journal is a narrative projection, and its
# authority is exactly that (design §4.2). What this buys is that a rewrite of
# past findings is VISIBLE rather than silent.
# THE INDEX IS EMPTIED FIRST. Checking only `git diff` missed a STAGED edit --
# the leader shares this uid, so it can `git add journal/old.md` itself, and
# `git commit` commits the whole index regardless of what this script staged. So
# the index is reset, then exactly one path is added.
git reset -q -- journal 2>/dev/null || true

# NUL-DELIMITED, because a tracked entry named `journal/old result.md` was split
# into two arguments by word-splitting and therefore never restored -- leaving
# precisely the unauthorised edit this check exists to undo.
RESTORED=()
PREFIX="$(git rev-parse --show-prefix 2>/dev/null)"
while IFS= read -r -d '' path; do
  case "$path" in
    "${PREFIX}journal/escalations/"*) continue ;;
  esac
  RESTORED+=("$path")
done < <(git diff -z --name-only -- journal 2>/dev/null)

if [ "${#RESTORED[@]}" -gt 0 ]; then
  say "WARNING the leader modified journal entries it was not asked to touch:"
  printf '  %s\n' "${RESTORED[@]}"
  say "  restoring them; the recorded history is not the leader's to revise"
  git checkout -- "${RESTORED[@]}" 2>/dev/null || say "  (could not restore some)"
fi

git add -- "$RECORDED" >/dev/null 2>&1 || die "cannot stage $RECORDED"
# BELT AND BRACES: assert the index holds nothing but the one verified file, so a
# path staged by some route not anticipated here cannot ride along.
STAGED="$(git diff --cached --name-only | wc -l | tr -d ' ')"
if [ "$STAGED" != 1 ]; then
  say "refusing to publish: $STAGED path(s) staged, expected only $RECORDED"
  git diff --cached --name-only | sed 's/^/  /'
  die "the index holds more than the verified entry"
fi
if git diff --cached --quiet; then
  say "nothing to publish"
  # `finish`, NOT `exit 0`: a tick that reaches here with a brake it could not
  # write is still a failed tick, and this is one of exactly two ways out.
  finish
fi
git -c "user.name=${QF_GIT_NAME:-qf-research agent}" \
    -c "user.email=${QF_GIT_EMAIL:-research@queue-forecasting.invalid}" \
    commit -q -m "journal: $STAMP ($VERDICT)" || die "cannot commit the journal"
# ONE rebase-and-retry, then give up. A rejected push means something landed
# concurrently; force-pushing a narrative whose authority lives in SQLite would
# destroy readable history to fix nothing.
if ! git push -q 2>/dev/null; then
  say "push rejected; rebasing once"
  git pull --rebase -q || die "cannot rebase onto the remote"
  git push -q || die "cannot push the journal"
fi
say "published $(basename "$RECORDED")"
# THE LAST LINE OF THE SCRIPT IS NOT AN EXIT STATUS. Without this the tick's
# status would be `say`'s, which is 0 whatever happened to the brake.
finish
