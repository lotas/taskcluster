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

# The CLI flags live here, in ONE place, because the exact spelling varies by
# CLI version and a wrong flag should be a one-line fix rather than a hunt
# through a 200-line script at 3am.
LEADER_FLAGS=(${QF_LEADER_FLAGS:---output-format text --permission-mode bypassPermissions})
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

# EVERY NUMERIC KNOB IS VALIDATED, because each is used BOTH in an arithmetic
# test and as an argument to something else, and neither position fails safely
# under `set -uo pipefail` (there is no `errexit`).
#
# `QF_TICK_MAX_QUEUE_BYTES=bogus` made `[ ... -le bogus ]` and `head -c bogus`
# both fail while the surrounding `{ ... }` block still succeeded -- so the
# leader ran with NO queue excerpt and a notice claiming it had been given the
# "first bogus bytes". And on GNU `head`, `-c -1` means "all but the last byte",
# so a negative value silently REMOVED the cap it was setting.
for _knob in MAX_RUNS MAX_TICKS MAX_EXTRACTS MAX_DISAGREE MAX_QUEUE_BYTES; do
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

if [ -e "$QF_RESEARCH/PAUSE" ]; then
  say "PAUSE exists; stopping"
  say "  reason: $(head -c 200 "$QF_RESEARCH/PAUSE" 2>/dev/null)"
  exit 0
fi

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
# conclusions instead of acting on them.
# --------------------------------------------------------------------------
CTX="$(mktemp -d "$STATE/ctx.XXXXXX")" || die "cannot make a context directory"
trap 'rm -rf "$CTX"' EXIT

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
read -r PROBES_TODAY EXTRACTS_TODAY <<EOF
$(python3 - "$JOBS_JSON" "$TODAY" <<'PY2'
import json, sys
jobs = (json.load(open(sys.argv[1])) or {}).get("jobs") or []
today = sys.argv[2]
def n(kind):
    return sum(1 for j in jobs if j.get("kind") == kind
               and (j.get("submitted_at") or "").startswith(today))
print(n("probe"), n("extract"))
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

say "checking the host"
if ! "$TRUSTED/experiment.py" doctor >"$CTX/doctor.txt" 2>&1; then
  # NOT fatal by itself. `doctor` reports notes as well as blockers, and its exit
  # status is one bit; the leader is given the text and told to stop if it names
  # a blocker. Aborting here would make a cosmetic note stop the loop.
  say "doctor exited non-zero; passing its output to the leader"
fi

PENDING="$JOURNAL/PENDING.md"
: >"$PENDING"

{
  echo "# Tick context — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo
  echo "Probes submitted today: $PROBES_TODAY of $MAX_RUNS."
  echo "Extracts submitted today: $EXTRACTS_TODAY of $MAX_EXTRACTS."
  echo "Ticks today: $TICKS of $MAX_TICKS."
  [ "$NO_MORE_EXTRACTS" = 0 ] \
    || echo "**The extract budget is spent: action 5 is unavailable this tick.**"
  echo
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
if ! PATH="$LEADER_PATH" claude -p "${LEADER_FLAGS[@]}" \
      <"$CTX/leader-input.md" >"$LEADER_LOG" 2>&1; then
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
  REASON="codex is not installed, so the claim could not be verified"
else
  say "copilot verifying"
  # ASSEMBLED TO A FILE for the same two reasons as the leader's, and the
  # evidence JSON is the part that grows -- it carries every scored row.
  {
    cat "$HERE/verify-prompt.md"
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
  CODEX_OK=1
  # `-` IS EXPLICIT: `codex exec` reads stdin when the prompt is `-` or absent,
  # but "if stdin is piped AND a prompt is also provided, stdin is appended as a
  # <stdin> block" -- so passing both would silently reshape the prompt.
  CHECK="$(codex exec "${COPILOT_FLAGS[@]}" - <"$CTX/verify-input.md" 2>&1)" \
    || CODEX_OK=0
  printf '%s\n' "$CHECK" >"$CTX/verify.log"
  REASON="$(printf '%s' "$CHECK" | tail -c 1200)"
  if [ "$CODEX_OK" = 0 ]; then
    # A FAILED COMMAND'S OUTPUT IS NOT PARSED AT ALL. Prepending a DISAGREE line
    # to it and re-parsing was wrong twice over: the tail-wins rule then picked a
    # trailing `VERDICT: AGREE` out of the partial output, so a copilot that
    # printed AGREE and then crashed published the entry as verified.
    VERDICT="DISAGREE"
    REASON="codex exited non-zero; its output is NOT trusted as a verdict:
$REASON"
  else
    # ANCHORED to its own line, and the LAST such line wins: a model that reasons
    # out loud may name both words before committing to one, so the first match
    # would read the wrong one -- but an unanchored match would also accept
    # `VERDICT: AGREE` quoted inside a sentence arguing against it.
    VERDICT="$(printf '%s\n' "$CHECK" \
               | grep -oE '^[[:space:]]*VERDICT:[[:space:]]*(AGREE|DISAGREE)[[:space:]]*$' \
               | tail -1 | grep -oE '(AGREE|DISAGREE)')"
  fi
  # NO VERDICT IS A DISAGREEMENT. A copilot that returned prose without a verdict
  # verified nothing, and defaulting the other way would make the whole step
  # decorative the first time its output format drifted.
  [ -n "$VERDICT" ] || { VERDICT="DISAGREE"; REASON="no VERDICT line in the copilot's reply
$REASON"; }
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DISAGREE_FILE="$STATE/consecutive-disagreements"
if [ "$VERDICT" = "AGREE" ]; then
  mv "$PENDING" "$JOURNAL/$STAMP.md"
  RECORDED="$JOURNAL/$STAMP.md"
  # A FAILURE HERE IS NOT FATAL, unlike the increment: an un-reset streak is
  # conservative (it pauses sooner), while an un-incremented one is not.
  set_counter "$DISAGREE_FILE" 0 \
    || say "note: could not reset the disagreement counter"
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
    echo "This entry is an escalation, not a finding. The claim above was not"
    echo "accepted and must not be cited as a result."
    echo
    echo '```'
    printf '%s\n' "$REASON"
    echo '```'
  } >"$RECORDED"
  rm -f "$PENDING"
  # IF THE STREAK CANNOT BE COUNTED, PAUSE NOW. An unreadable or unwritable
  # counter meant every disagreement recorded "1" and the threshold was never
  # reached, so the one automatic brake on a drifting leader silently did not
  # exist. Not being able to count to three is a reason to stop, not to continue.
  if PREV="$(counter "$DISAGREE_FILE")" && set_counter "$DISAGREE_FILE" \
       "$((PREV + 1))"; then
    N=$((PREV + 1))
  else
    N="$MAX_DISAGREE"
    say "WARNING the disagreement counter at $DISAGREE_FILE cannot be"
    say "  persisted, so the streak cannot be tracked. Pausing now."
  fi
  say "NOT verified ($N consecutive); escalated to escalations/$STAMP.md"
  if [ "$N" -ge "$MAX_DISAGREE" ]; then
    if ! printf 'auto-paused %s: %s consecutive unverified claims\nsee %s\n' \
         "$STAMP" "$N" "$RECORDED" >"$QF_RESEARCH/PAUSE"; then
      # The PAUSE file IS the brake. If it cannot be written, say so as loudly
      # as possible rather than reporting a pause that did not happen.
      say "CRITICAL cannot write $QF_RESEARCH/PAUSE. The loop is NOT paused."
      say "  Disable the timer by hand: sudo systemctl disable --now qf-tick.timer"
    else
      say "PAUSED: $N consecutive unverified claims"
    fi
  fi
fi

# --------------------------------------------------------------------------
# Publish. The journal is the agent's own repository, so this is a push to
# `qf-research` and nothing else -- the loop has no credential on the monorepo,
# which is why `experiment-queue.md` arrives read-only.
# --------------------------------------------------------------------------
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
  exit 0
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
