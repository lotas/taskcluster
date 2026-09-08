#!/usr/bin/env bash
# `tick-failure-alarm.sh` -- the OnFailure= alarm -- against a stubbed `gh` and
# the REAL `pause-issue.sh`.
#
# WHY THIS FILE EXISTS. `tick.sh` has around twenty-five `die` sites and a `die`
# writes no `PAUSE` and files no issue, so a broken install left the loop
# retrying hourly forever with nothing raising an alarm: the 2026-09-04 silence
# by a different route. `qf-tick.service`'s `OnFailure=` closes it.
#
# THE REAL `pause-issue.sh`, NOT A STUB OF IT, on purpose. Everything that can
# go wrong here is a CONTRACT between two files: whether `pause_stamp` can parse
# the PAUSE this script writes, whether the stamp survives to make the issue
# idempotent, whether `check` can read the brake back out. A stub of the helper
# would agree with whatever this script did and prove none of it.
#
# WHAT THIS CANNOT PROVE, and a human must check on the host: that systemd runs
# the unit at all, and that `MONITOR_SERVICE_RESULT` is delivered (systemd v250+)
# -- the alarm treats its absence as `unknown` and still fires, so an older
# systemd loses a word of the title and nothing else.
#
#   ./tests/test_tick_failure_alarm.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/../research-loop"
[ -f "$SRC/tick-failure-alarm.sh" ] || { echo "cannot find the alarm" >&2; exit 2; }

pass=0; fail=0
ok()  { echo "ok    $1"; pass=$((pass + 1)); }
bad() { echo "FAIL  $1"; fail=$((fail + 1)); }

# A MISSING `jq` IS A FAILURE, NOT A SKIP -- the same reason as
# test_pause_issue.sh: the stub applies `--jq` for real, and a suite that cannot
# test what it exists to test must not exit 0.
if ! command -v jq >/dev/null 2>&1; then
  echo "FAIL  the whole suite -- jq is not installed" >&2
  echo; echo "tick-failure-alarm: pass=0 fail=1"; exit 1
fi

ROOT="$(mktemp -d)"; trap 'rm -rf "$ROOT"' EXIT
mkdir -p "$ROOT/bin"
export PATH="$ROOT/bin:$PATH"

# The `gh` stub, in the shape test_pause_issue.sh already uses: answers from
# $GH_DIR/<kind>.json, records argv in $GH_LOG, applies `--jq` for real.
cat >"$ROOT/bin/gh" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail
printf '%s\n' "$*" >>"$GH_LOG"
kind=other
case "$*" in
  *"issue list"*)   kind=search ;;
  *"issue create"*) kind=create ;;
  *"label list"*)   kind=labellist ;;
  *"label create"*) kind=label ;;
esac
f="$GH_DIR/$kind.json"
[ ! -f "$f.rc" ] || { echo "gh: stubbed failure ($kind)" >&2; exit "$(cat "$f.rc")"; }
filter=""; prev=""
for a in "$@"; do
  [ "$prev" != "--jq" ] || filter="$a"
  prev="$a"
done
[ -f "$f" ] || exit 0
if [ -n "$filter" ]; then jq -r "$filter" <"$f"; else cat "$f"; fi
STUB
chmod +x "$ROOT/bin/gh"

# EACH CASE GETS ITS OWN COPY OF research-loop/, because two of them turn on the
# alarm being unable to reach `pause-issue.sh`, and chmod-ing the checkout would
# be a test that edits the thing it is testing.
world() {  # world <case name>
  W="$ROOT/$1"
  mkdir -p "$W/gh" "$W/home"
  cp -r "$SRC" "$W/loop"
  ALARM="$W/loop/tick-failure-alarm.sh"
  chmod +x "$ALARM" "$W/loop/pause-issue.sh"
  export HOME="$W/home"
  export GH_DIR="$W/gh" GH_LOG="$W/gh.log"
  export QF_PAUSE_TOKEN_FILE="$W/token"
  export QF_PAUSE_ISSUE_REPO=lotas/qf-research
  export QF_TICK_STATE="$W/state"
  unset QF_RESEARCH
  echo dummy-token >"$W/token"
  : >"$GH_LOG"
  WS="$W/home/qf-research"
  mkdir -p "$WS"
  PAUSE="$WS/PAUSE"
  echo '[]' >"$GH_DIR/search.json"
  echo 'https://github.com/lotas/qf-research/issues/77' >"$GH_DIR/create.json"
  printf '[{"name":"qf-pause"}]\n' >"$GH_DIR/labellist.json"
}

run() {  # run [MONITOR_SERVICE_RESULT]
  local rc=0
  if [ "$#" -gt 0 ]; then
    OUT="$(MONITOR_SERVICE_RESULT="$1" "$ALARM" 2>&1)" || rc=$?
  else
    OUT="$(env -u MONITOR_SERVICE_RESULT "$ALARM" 2>&1)" || rc=$?
  fi
  RC=$rc
}

# A NEGATIVE ASSERTION ON A MISSING FILE PROVES NOTHING (test_pause_issue.sh's
# `absent_from`, and the same reason): `grep -q X missing` "succeeds" as an
# absence, so an assertion about a file the script never wrote asserts nothing.
pause_has() {  # pause_has <pattern> <label>
  if [ ! -s "$PAUSE" ]; then bad "$2 -- PAUSE is missing or empty"
  elif grep -q -e "$1" "$PAUSE"; then ok "$2"
  else bad "$2 -- no '$1' in $(tr '\n' '|' <"$PAUSE")"; fi
}
gh_did() {  # gh_did <pattern> <label>
  if grep -q -e "$1" "$GH_LOG"; then ok "$2"
  else bad "$2 -- '$1' not in the gh log: $(tr '\n' '|' <"$GH_LOG")"; fi
}
gh_did_not() {  # gh_did_not <pattern> <label>
  if [ ! -f "$GH_LOG" ]; then bad "$2 -- there is no gh log, so this proves nothing"
  elif grep -q -e "$1" "$GH_LOG"; then bad "$2 -- found '$1' in the gh log"
  else ok "$2"; fi
}

# --------------------------------------------------------------------------
# The normal case: a tick died, nothing paused it, nobody was told.
# --------------------------------------------------------------------------
world normal
run exit-code
[ "$RC" = 0 ] && ok "a failing tick alarms and exits 0" \
              || bad "a failing tick alarms and exits 0 (rc=$RC: $OUT)"
pause_has '^auto-paused [0-9TZ]*: tick-unit-failure at exit-code$' \
  "PAUSE names the brake and the systemd result, in pause_now's format"
pause_has '^stamp: [0-9]\{8\}T[0-9]\{6\}Z$' "PAUSE carries a well-formed stamp"
pause_has '^see journalctl -u qf-tick.service' \
  "the reason points at the journal, which is where the die message is"
gh_did 'issue create' "the issue is filed"
gh_did 'PAUSED .*tick-unit-failure at exit-code' \
  "the issue title names the brake, so it reads like every other pause"
pause_has '^issue: lotas/qf-research#77$' \
  "PAUSE is bound to the issue, so closing it releases the loop"

# ONE LINE PER FIELD. `see` is read back with `sed -n 's/^see //p' | head -1`,
# so a multi-line reason would leave a line of PAUSE that nothing owns.
n_lines="$(wc -l <"$PAUSE")"
[ "$n_lines" = 4 ] && ok "PAUSE is exactly four lines: brake, reason, issue, stamp" \
  || bad "PAUSE has $n_lines lines: $(tr '\n' '|' <"$PAUSE")"

# THE RELEASE PATH UNDERSTANDS IT. This is the contract the whole file is for:
# `check` must be able to read this PAUSE back, and refuse while the issue is
# open. A private format here would be a pause nobody can release.
printf '{"state":"open","body":"x"}\n' >"$GH_DIR/issue.json"
if "$W/loop/pause-issue.sh" check "$PAUSE" >/dev/null 2>&1; then
  bad "pause-issue.sh check stays paused while the issue is open"
else
  ok "pause-issue.sh check stays paused while the issue is open"
fi

# --------------------------------------------------------------------------
# FIRING TWICE MUST NOT FILE TWICE. A tick that died of a broken install dies
# again every hour; 24 issues a day is the alarm becoming the noise it exists
# to cut through.
# --------------------------------------------------------------------------
world idempotent
run exit-code
stamp1="$(sed -n 's/^stamp: //p' "$PAUSE")"
# The issue now exists and carries the stamp, which is what the real GitHub
# would answer on the second call.
printf '[{"number":77,"body":"stamp %s here"}]\n' "$stamp1" >"$GH_DIR/search.json"
: >"$GH_LOG"
sleep 1   # so a `date`-derived stamp would demonstrably differ
run exit-code
gh_did_not 'issue create' "a second failure files no second issue"
stamp2="$(sed -n 's/^stamp: //p' "$PAUSE")"
[ "$stamp1" = "$stamp2" ] && ok "the stamp is written once and never rewritten" \
  || bad "the stamp changed ($stamp1 -> $stamp2), which would duplicate the alarm"
case "$OUT" in *"already exists"*) ok "it says it left the existing PAUSE alone" ;;
  *) bad "it says it left the existing PAUSE alone ($OUT)" ;; esac

# --------------------------------------------------------------------------
# AN EXISTING PAUSE FROM tick.sh IS NOT TOUCHED. Overwriting it would orphan the
# issue a human may be reading and lose the binding that releases it.
# --------------------------------------------------------------------------
world existing
printf 'auto-paused 20260904T101112Z: consecutive-disagreements at 3\nsee /esc/x.md\nissue: lotas/qf-research#12\nstamp: 20260904T101112Z\n' >"$PAUSE"
# The issue that pause carries, findable by its stamp -- which is what GitHub
# would answer. (If the search finds NOTHING, `pause-issue.sh open` files a
# fresh issue and rebinds; that is its own long-standing behaviour, reached
# identically from `tick.sh`, and not this alarm's to change.)
printf '[{"number":12,"body":"stamp 20260904T101112Z here"}]\n' >"$GH_DIR/search.json"
run exit-code
pause_has 'consecutive-disagreements at 3' "an existing brake survives"
pause_has 'lotas/qf-research#12' "an existing issue binding survives"
gh_did_not 'issue create' "no second issue is filed over an existing pause"

# --------------------------------------------------------------------------
# WHAT SYSTEMD SAID, AND WHAT IT DID NOT.
# --------------------------------------------------------------------------
world timeout
run timeout
pause_has 'tick-unit-failure at timeout$' \
  "a 4h TimeoutStartSec kill is distinguishable from a die"

world nomonitor
run
pause_has 'tick-unit-failure at unknown$' \
  "an absent MONITOR_SERVICE_RESULT alarms anyway, as 'unknown'"

# THE `auto-paused` LINE IS PARSED BACK OUT, so a result carrying whitespace or
# punctuation would corrupt the field `check` reads. Constrained, not trusted.
world junkresult
run 'exit code; rm -rf /'
pause_has 'tick-unit-failure at unknown$' \
  "a malformed systemd result is reduced to 'unknown', not interpolated"
absent=1
grep -q 'rm -rf' "$PAUSE" && absent=0
[ "$absent" = 1 ] && ok "a malformed result is not written into PAUSE" \
  || bad "a malformed result reached PAUSE"

# ` at ` IS THE ONE SUBSTRING THAT WOULD CORRUPT THE READER. `pause-issue.sh
# check` parses the brake with a GREEDY `\(.*\) at .*`, so a result containing
# ` at ` is absorbed into the brake and the issue names a brake that does not
# exist. The existing junk case does not test this -- it is caught by its
# semicolons and slashes, not by its space.
world atresult
run 'exit at code'
pause_has 'tick-unit-failure at unknown$' \
  "a result containing ' at ' is reduced to 'unknown': the reader parses greedily"
brake="$(sed -n 's/^auto-paused [^:]*: \(.*\) at .*/\1/p' "$PAUSE")"
[ "$brake" = tick-unit-failure ] \
  && ok "the reader's own greedy parse recovers the brake unchanged" \
  || bad "the reader's greedy parse recovers '$brake', not tick-unit-failure"

# --------------------------------------------------------------------------
# THE TWO FAILURES THE ALARM CANNOT ALARM ABOUT. Both must be LOUD in the
# journal and must not exit 0: an alarm that reports success it did not achieve
# is worse than no alarm.
# --------------------------------------------------------------------------
world noworkspace
rm -rf "$WS"
run exit-code
[ "$RC" != 0 ] && ok "a missing workspace is a non-zero exit" \
              || bad "a missing workspace is a non-zero exit"
case "$OUT" in *CRITICAL*disable*) ok "it says how to stop the loop by hand" ;;
  *) bad "it says how to stop the loop by hand ($OUT)" ;; esac
# THE DIAGNOSIS NAMES THE MISSING WORKSPACE, and not just "cannot write PAUSE":
# `mkdir` and `chown` are different remedies, and the whole value of an alarm
# that cannot alarm is the sentence it leaves in the journal.
case "$OUT" in *"no workspace at"*) ok "the missing workspace is named as the cause" ;;
  *) bad "the missing workspace is named as the cause ($OUT)" ;; esac
[ ! -e "$PAUSE" ] && ok "no PAUSE is invented outside the workspace" \
                  || bad "no PAUSE is invented outside the workspace"

world nohelper
chmod -x "$W/loop/pause-issue.sh"
run exit-code
[ "$RC" != 0 ] && ok "an unreachable pause-issue.sh is a non-zero exit" \
              || bad "an unreachable pause-issue.sh is a non-zero exit"
pause_has 'tick-unit-failure' "the brake is still written when the alarm cannot be filed"
case "$OUT" in *"nobody has been told"*) ok "the silence is named, not implied" ;;
  *) bad "the silence is named, not implied ($OUT)" ;; esac

world nohome
unset HOME
run exit-code
[ "$RC" != 0 ] && ok "no HOME and no QF_RESEARCH is a non-zero exit" \
              || bad "no HOME and no QF_RESEARCH is a non-zero exit"
case "$OUT" in *journalctl*) ok "with no workspace at all it still says where to look" ;;
  *) bad "with no workspace at all it still says where to look ($OUT)" ;; esac

# --------------------------------------------------------------------------
# THE UNIT AND THE SCRIPT AGREE, and the tick names the unit. An `OnFailure=`
# pointing at a unit nothing installs is a hole that LOOKS closed.
# --------------------------------------------------------------------------
grep -q '^OnFailure=qf-tick-failure.service$' "$SRC/qf-tick.service" \
  && ok "qf-tick.service names the alarm in OnFailure=" \
  || bad "qf-tick.service names the alarm in OnFailure="
grep -q 'tick-failure-alarm.sh' "$SRC/qf-tick-failure.service" \
  && ok "the alarm unit executes tick-failure-alarm.sh" \
  || bad "the alarm unit executes tick-failure-alarm.sh"
grep -q 'qf-tick-failure.service' "$SRC/install.sh" \
  && ok "install.sh installs the alarm unit" \
  || bad "install.sh installs the alarm unit -- OnFailure= would find nothing"
# NO `OnFailure=` OF ITS OWN. The boot-loop hazard is tested above; this is the
# self-referential one -- an alarm that alarms about its own failure either
# recurses or, worse, looks like it is covered when the thing that files issues
# is the thing that broke.
grep -q '^OnFailure=' "$SRC/qf-tick-failure.service" \
  && bad "the alarm unit must have no OnFailure= of its own" \
  || ok "the alarm unit has no OnFailure= of its own"
grep -q '^\[Install\]' "$SRC/qf-tick-failure.service" \
  && bad "the alarm unit must NOT be enablable: it would fire on every boot" \
  || ok "the alarm unit has no [Install] section"

# --------------------------------------------------------------------------
# THE TOKEN PREFLIGHT. `install.sh on` is the one moment somebody is watching,
# and a pause that files no issue is the 2026-09-04 failure again -- correct,
# and invisible. Extracted and called as a function, in the idiom
# test_unit_drift.sh uses on phase2-setup.sh, with a guard that fails if the
# extraction misses it: a check sourced out of a file by pattern can silently
# stop being sourced at all.
# --------------------------------------------------------------------------
# shellcheck disable=SC1090
source <(sed -n '/^token_preflight()/,/^}/p; /^allowlist_preflight()/,/^}/p;
                 /^repo_preflight()/,/^}/p' "$SRC/install.sh")
for fn in token_preflight allowlist_preflight repo_preflight; do
  declare -F "$fn" >/dev/null || { echo "extraction missed $fn" >&2; exit 2; }
done
# AND IT IS ACTUALLY CALLED. A function nothing invokes is a check that does not
# run, and this file could otherwise pass on a preflight that was never wired.
grep -q 'token_preflight "\$(sed' "$SRC/install.sh" \
  && ok "install.sh calls token_preflight with the path pinned in the unit" \
  || bad "install.sh defines token_preflight but never calls it on the unit's path"

TOK="$ROOT/tok"
pf() {  # pf <arg...> -- captures output and rc
  PF_RC=0
  PF_OUT="$(token_preflight "$@" 2>&1)" || PF_RC=$?
}

install -m 0600 /dev/null "$TOK"
pf "$TOK"
[ "$PF_RC" = 0 ] && ok "a 0600 token passes the preflight" \
                 || bad "a 0600 token passes the preflight ($PF_OUT)"

# HALF ONE: MISSING.
pf "$ROOT/no-such-token"
[ "$PF_RC" != 0 ] && ok "a MISSING token warns" \
                  || bad "a MISSING token warns"
case "$PF_OUT" in *"TELL NOBODY"*) ok "the missing-token warning names the consequence" ;;
  *) bad "the missing-token warning names the consequence ($PF_OUT)" ;; esac
case "$PF_OUT" in *"Issues: read and write"*) ok "it says which scopes to grant" ;;
  *) bad "it says which scopes to grant ($PF_OUT)" ;; esac

# HALF TWO: PRESENT BUT NOT 0600. This is the half a `[ -r "$f" ]` check reports
# as fine -- a repo-write credential legible to every uid on the box.
chmod 0644 "$TOK"
pf "$TOK"
[ "$PF_RC" != 0 ] && ok "a token that is readable but NOT 0600 warns" \
                  || bad "a token that is readable but NOT 0600 warns"
case "$PF_OUT" in *644*chmod*) ok "the mode warning names the mode and the fix" ;;
  *) bad "the mode warning names the mode and the fix ($PF_OUT)" ;; esac
chmod 0600 "$TOK"

# A 0400 TOKEN IS NOT 0600 EITHER, and this is deliberate rather than an
# oversight: `pause-issue.sh` only reads it, but the documented, checkable
# state is one state. Two acceptable modes is a check nobody can keep in step.
chmod 0400 "$TOK"
pf "$TOK"
[ "$PF_RC" != 0 ] && ok "0400 is reported too: one documented mode, not a range" \
                  || bad "0400 is reported too"
chmod 0600 "$TOK"

# NO `stat` ON PATH: the mode is UNKNOWN, and unknown is not fine. This branch
# looks unreachable (a path that exists always stats) until the box has no GNU
# `stat` -- busybox, a minimal image -- and then an empty answer read as consent
# would silently downgrade the whole check to `[ -e ]`. A mutation that deleted
# this branch survived every other case in this file.
PF_RC=0
PF_OUT="$(PATH=/nonexistent-for-this-case token_preflight "$TOK" 2>&1)" || PF_RC=$?
[ "$PF_RC" != 0 ] && ok "with no \`stat\` the mode is UNKNOWN, not fine" \
                  || bad "with no \`stat\` the mode is UNKNOWN, not fine ($PF_OUT)"
case "$PF_OUT" in *UNKNOWN*) ok "the unknown-mode warning says so in those words" ;;
  *) bad "the unknown-mode warning says so in those words ($PF_OUT)" ;; esac

# AN EMPTY PATH IS THE UNIT NOT PINNING ONE, which is its own failure: the
# default is invisible, which is why the unit pins it.
pf ""
[ "$PF_RC" != 0 ] && ok "an unpinned QF_PAUSE_TOKEN_FILE warns" \
                  || bad "an unpinned QF_PAUSE_TOKEN_FILE warns"
case "$PF_OUT" in *QF_PAUSE_TOKEN_FILE*) ok "it names the directive that is missing" ;;
  *) bad "it names the directive that is missing ($PF_OUT)" ;; esac

# AND THE PATH THE PREFLIGHT WILL BE GIVEN IS REALLY IN THE UNIT: the call site
# greps it out of qf-tick.service, so an empty answer would silently reduce the
# preflight to the "unpinned" branch on every install.
pinned="$(sed -n 's/^Environment=QF_PAUSE_TOKEN_FILE=//p' "$SRC/qf-tick.service" | head -1)"
case "$pinned" in
  /*) ok "qf-tick.service pins an absolute token path ($pinned)" ;;
  *)  bad "qf-tick.service pins no absolute QF_PAUSE_TOKEN_FILE ('$pinned')" ;;
esac

# --------------------------------------------------------------------------
# THE RELEASE PATH: the allowlist, and the repository the alarm is filed in.
# A missing token means no alarm, which is loud in its own way. An unusable
# allowlist is worse: the alarm is filed, it looks right, and NOBODY is
# authorised to release the pause by closing it.
# --------------------------------------------------------------------------
grep -q 'allowlist_preflight "\$HERE/pause-resume-allowlist.txt"' "$SRC/install.sh" \
  && ok "install.sh calls allowlist_preflight on the path pause-issue.sh uses" \
  || bad "install.sh never calls allowlist_preflight: the check does not run"
grep -q 'repo_preflight "\$HERE/qf-tick.service"' "$SRC/install.sh" \
  && ok "install.sh calls repo_preflight on the UNIT, not the ambient env" \
  || bad "install.sh never calls repo_preflight on the unit"
# THE ALARM UNIT HAS ITS OWN COPIES OF BOTH DIRECTIVES, restated deliberately --
# so they can diverge. If they do, every OnFailure alarm exits 1 in silence
# while every other preflight prints green, which is this whole change's failure
# arriving down its own alarm channel.
grep -q 'repo_preflight "\$HERE/qf-tick-failure.service"' "$SRC/install.sh" \
  && ok "install.sh preflights the ALARM unit's QF_PAUSE_ISSUE_REPO too" \
  || bad "install.sh never preflights qf-tick-failure.service's repo: a failing tick would alarm nobody"
grep -q 'qf-tick-failure.service" | head -1)"' "$SRC/install.sh" \
  && ok "install.sh preflights the ALARM unit's own token path" \
  || bad "install.sh never preflights qf-tick-failure.service's token path"

# AND BOTH UNITS ACTUALLY DECLARE THEM, read the way the preflight reads them.
rp_alarm() { PF_RC=0; PF_OUT="$(repo_preflight "$SRC/qf-tick-failure.service" 2>&1)" || PF_RC=$?; }
rp_alarm
[ "$PF_RC" = 0 ] && ok "the shipped alarm unit declares an owner/repo QF_PAUSE_ISSUE_REPO" \
                 || bad "the shipped alarm unit declares QF_PAUSE_ISSUE_REPO ($PF_OUT)"
alarm_tok="$(sed -n 's/^Environment=QF_PAUSE_TOKEN_FILE=//p' "$SRC/qf-tick-failure.service" | head -1)"
case "$alarm_tok" in
  /*) ok "the shipped alarm unit pins an absolute token path ($alarm_tok)" ;;
  *)  bad "the shipped alarm unit pins no absolute QF_PAUSE_TOKEN_FILE ('$alarm_tok')" ;;
esac
# THE TWO UNITS AGREE TODAY, and nothing but this line would notice if they
# stopped: `env_matches` compares each unit against its own installed copy, so
# two units disagreeing with EACH OTHER is invisible to it.
tick_tok="$(sed -n 's/^Environment=QF_PAUSE_TOKEN_FILE=//p' "$SRC/qf-tick.service" | head -1)"
[ "$alarm_tok" = "$tick_tok" ] \
  && ok "the tick and the alarm pin the SAME token path" \
  || bad "the tick pins '$tick_tok' and the alarm pins '$alarm_tok': the alarm would read a token that may not be there"
tick_repo="$(sed -n 's/^Environment=QF_PAUSE_ISSUE_REPO=//p' "$SRC/qf-tick.service" | head -1)"
alarm_repo="$(sed -n 's/^Environment=QF_PAUSE_ISSUE_REPO=//p' "$SRC/qf-tick-failure.service" | head -1)"
[ "$tick_repo" = "$alarm_repo" ] \
  && ok "the tick and the alarm file into the SAME repository" \
  || bad "the tick files into '$tick_repo' and the alarm into '$alarm_repo': a pause and its alarm would land in different repos"

# --------------------------------------------------------------------------
# `status` ANSWERS THE QUESTION A PAUSED BOX RAISES. It used to read the timer,
# PAUSE and two journal listings while every preflight lived in `on)`, so a box
# whose checkout was upgraded but never re-`on`'d showed green and filed nothing
# forever -- "is it armed" being read as "is it working".
#
# ASSERTED AS TEXT, because running `status` needs systemd. What is checkable
# here is the property that made the old version useless: WHICH PATHS it reads.
# --------------------------------------------------------------------------
STATUS_BLOCK="$(sed -n '/^status)/,/^  ;;/p' "$SRC/install.sh")"
[ -n "$STATUS_BLOCK" ] || { echo "cannot extract install.sh's status branch" >&2; exit 2; }
sb() {  # sb <pattern> <label>
  printf '%s' "$STATUS_BLOCK" | grep -q -e "$1" \
    && ok "$2" || bad "$2 -- no '$1' in the status branch"
}
sb 'repo_preflight "\$UNITS/' "status runs repo_preflight against the INSTALLED unit"
sb 'token_preflight "\$(sed' "status runs token_preflight from the installed unit's own pin"
sb 'allowlist_preflight "\$TRUSTED/' \
  "status checks the DEPLOYED allowlist, which is the copy the timer reads"
sb 'cmp -s "\$HERE/\$u" "\$UNITS/\$u"' \
  "status compares the installed unit files against the checkout"
sb 'qf-tick-failure.service' \
  "status covers the alarm unit, which restates both directives"
sb 'exit 0' "status ends in an explicit exit 0: it reports, it does not fail"
# AND IT READS THE INSTALLED COPY, NOT `$HERE`'s. This is the distinction the
# whole block exists for, so it is asserted as an ABSENCE too: a preflight on
# `$HERE` in this branch would answer "what would be installed if you ran on".
case "$STATUS_BLOCK" in
  *'repo_preflight "$HERE'*|*'allowlist_preflight "$HERE'*)
    bad "status preflights \$HERE's copies, which is the question it must not answer" ;;
  *) ok "status preflights nothing out of \$HERE" ;;
esac
# NO `die` IN THE STATUS BRANCH: it must stay read-only and must not fail on
# drift. (`on` is the branch that refuses.)
case "$STATUS_BLOCK" in
  *"die \""*) bad "status can die: a read-only report must not fail on drift" ;;
  *) ok "status contains no die" ;;
esac

# THE EXEC BITS ARE CHECKED BEFORE ANYTHING IS INSTALLED, so `on` is
# all-or-nothing: a die between the `install` calls leaves fresh unit files in
# /etc/systemd/system un-reloaded, and a later unrelated `daemon-reload` then
# activates units whose exec bit was never fixed.
first_install="$(grep -n 'install -m 0644' "$SRC/install.sh" | head -1 | cut -d: -f1)"
last_execbit="$(grep -n 'x "\$HERE/pause-issue.sh"\|x "\$HERE/tick-failure-alarm.sh"' \
                  "$SRC/install.sh" | tail -1 | cut -d: -f1)"
if [ -n "$first_install" ] && [ -n "$last_execbit" ] && [ "$last_execbit" -lt "$first_install" ]; then
  ok "both exec-bit checks run before the first install call"
else
  bad "an exec-bit check ($last_execbit) runs at or after the first install ($first_install): \`on\` is not all-or-nothing"
fi

AL="$ROOT/allowlist"
al() {  # al <arg...>
  PF_RC=0
  PF_OUT="$(allowlist_preflight "$@" 2>&1)" || PF_RC=$?
}

printf '# a comment\nlotas\n' >"$AL"
al "$AL"
[ "$PF_RC" = 0 ] && ok "an allowlist with one login passes" \
                 || bad "an allowlist with one login passes ($PF_OUT)"

al "$ROOT/no-such-allowlist"
[ "$PF_RC" != 0 ] && ok "a MISSING allowlist warns" \
                  || bad "a MISSING allowlist warns"
case "$PF_OUT" in *"authorise NOBODY"*) ok "the missing-allowlist warning names the consequence" ;;
  *) bad "the missing-allowlist warning names the consequence ($PF_OUT)" ;; esac

# EXISTING IS NOT THE PROPERTY THAT MATTERS. `[ -s ]` or a line count would pass
# both of these, and `allowed()` authorises nobody for either.
: >"$AL"
al "$AL"
[ "$PF_RC" != 0 ] && ok "an EMPTY allowlist warns" \
                  || bad "an EMPTY allowlist warns"

printf '# only comments\n#lotas\n\n   \n' >"$AL"
al "$AL"
[ "$PF_RC" != 0 ] && ok "a comments-and-blanks-only allowlist warns" \
                  || bad "a comments-and-blanks-only allowlist warns ($PF_OUT)"
case "$PF_OUT" in *"not the property that matters"*) ok "it says the file existing is not enough" ;;
  *) bad "it says the file existing is not enough ($PF_OUT)" ;; esac

# A LOGIN WITH A TRAILING COMMENT COUNTS, because `allowed()` strips the comment
# and matches what is left. A preflight that disagreed with the authoriser about
# what a usable line is would be measuring a different file.
printf 'lotas  # the human\n' >"$AL"
al "$AL"
[ "$PF_RC" = 0 ] && ok "a login with a trailing comment counts, as allowed() counts it" \
                 || bad "a login with a trailing comment counts ($PF_OUT)"

# AND THE SHIPPED FILE PASSES ITS OWN CHECK, read through the real filter: this
# is the file the deployed loop will use.
al "$SRC/pause-resume-allowlist.txt"
[ "$PF_RC" = 0 ] && ok "the shipped allowlist has at least one usable login" \
                 || bad "the shipped allowlist authorises nobody ($PF_OUT)"

rp() { PF_RC=0; PF_OUT="$(repo_preflight "$@" 2>&1)" || PF_RC=$?; }

rp "$SRC/qf-tick.service"
[ "$PF_RC" = 0 ] && ok "the shipped unit declares an owner/repo QF_PAUSE_ISSUE_REPO" \
                 || bad "the shipped unit declares QF_PAUSE_ISSUE_REPO ($PF_OUT)"

printf '[Service]\nEnvironment=QF_TICK_MAX_RUNS=4\n' >"$ROOT/unit_norepo"
rp "$ROOT/unit_norepo"
[ "$PF_RC" != 0 ] && ok "a unit declaring no QF_PAUSE_ISSUE_REPO warns" \
                  || bad "a unit declaring no QF_PAUSE_ISSUE_REPO warns"
case "$PF_OUT" in *"every pause will be silent"*) ok "the no-repo warning names the consequence" ;;
  *) bad "the no-repo warning names the consequence ($PF_OUT)" ;; esac

# AMBIENT ENVIRONMENT IS NOT AN ANSWER. It is pinned in the unit precisely so it
# is never inherited from wherever the installer happens to be standing.
PF_RC=0
PF_OUT="$(QF_PAUSE_ISSUE_REPO=lotas/qf-research repo_preflight "$ROOT/unit_norepo" 2>&1)" || PF_RC=$?
[ "$PF_RC" != 0 ] && ok "an ambient QF_PAUSE_ISSUE_REPO does not satisfy the unit check" \
                  || bad "an ambient QF_PAUSE_ISSUE_REPO satisfied a unit that declares none"

printf '[Service]\nEnvironment=QF_PAUSE_ISSUE_REPO=qf-research\n' >"$ROOT/unit_badrepo"
rp "$ROOT/unit_badrepo"
[ "$PF_RC" != 0 ] && ok "a repo that is not owner/repo shaped warns" \
                  || bad "a repo that is not owner/repo shaped warns"

# THE EXEC BIT ON THE SCRIPT BOTH CALLERS USE. Without it the loop installs
# cleanly, pauses correctly, and has no release handle at all.
grep -q 'x "\$HERE/pause-issue.sh"' "$SRC/install.sh" \
  && ok "install.sh refuses to install with pause-issue.sh non-executable" \
  || bad "install.sh does not check pause-issue.sh's exec bit"
[ -x "$SRC/pause-issue.sh" ] && ok "the shipped pause-issue.sh is executable" \
                             || bad "the shipped pause-issue.sh is NOT executable"

echo
echo "tick-failure-alarm: pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
