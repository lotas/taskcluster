#!/usr/bin/env bash
# Tests `phase2-setup.sh`'s stale-unit check.
#
# WHY THIS FILE EXISTS. `mirror-refresh` reset the checkout and restarted the
# daemon while /etc/systemd/system held a unit copied from an older commit, so
# the dispatcher ran NEW CODE UNDER OLD CONFIGURATION -- silently. 2b-1 added
# `Environment=PYTHONPATH=.../host/shared` to qf-dispatch.service, the refresh
# did not reinstall it, and every `qf extract` failed with
# `ModuleNotFoundError: No module named 'extract_spec'`: an error about a module,
# for a cause that was a missing directive.
#
# The check that closes it must ignore substituted placeholders and notice
# everything else. Its first implementation chained two `sed` expressions to
# derive the excluded keys, and the second fired on the first's output --
# reducing `Environment=QFD_ADMIN_UID=%%DEPLOY_UID%%` to the key `Environment`,
# which excluded EVERY environment line. It passed on identical files, which is
# what a check that has stopped checking looks like from outside.
#
#   ./tests/test_unit_drift.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETUP="$HERE/../phase2-setup.sh"
[ -f "$SETUP" ] || { echo "cannot find $SETUP" >&2; exit 2; }

# shellcheck disable=SC1090
source <(sed -n '/^unit_matches()/,/^}/p; /^_unit_key_filter()/,/^}/p;
                 /^env_matches()/,/^}/p; /^_env_qf_tokens()/,/^}/p;
                 /^profile_qf_matches()/,/^}/p; /^_profile_qf_assignments()/,/^}/p;
                 /^_profile_source_targets()/,/^}/p;
                 /^_unit_reads_login_profile()/,/^}/p' "$SETUP")
# THE GUARD IS THE POINT, not paranoia: a `sed` range that stops matching
# extracts NOTHING and every case below then tests an undefined function --
# which in `bash -uo pipefail` without `-e` prints "command not found" and
# carries on, i.e. a suite that has stopped testing while still printing `ok`.
# That is the same failure the header records one level up.
for fn in unit_matches _unit_key_filter env_matches _env_qf_tokens \
          profile_qf_matches _profile_qf_assignments _profile_source_targets \
          _unit_reads_login_profile; do
  declare -F "$fn" >/dev/null || { echo "extraction missed $fn" >&2; exit 2; }
done

pass=0; fail=0
ok()  { echo "ok    $1"; pass=$((pass + 1)); }
bad() { echo "FAIL  $1"; fail=$((fail + 1)); }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
cat > "$TMP/checkout" <<'UNIT'
[Service]
ExecStart=/usr/bin/python3 /srv/x/qfd.py
Environment=PYTHONPATH=/srv/x/host/shared
Environment=QFD_ADMIN_UID=%%DEPLOY_UID%%
Environment=QFD_LOG_CAP_MB=16
User=qfd
UNIT

variant() { sed "s/%%DEPLOY_UID%%/999/; ${1:-}" "$TMP/checkout" > "$TMP/installed"; }

expect() {
  if unit_matches "$TMP/checkout" "$TMP/installed"; then
    [ "$1" = match ] && ok "$2" || bad "$2 (matched, wanted drift)"
  else
    [ "$1" = drift ] && ok "$2" || bad "$2 (drifted, wanted match)"
  fi
}

variant
expect match "a substituted placeholder is not drift"

variant 's|^Environment=PYTHONPATH=.*|Environment=PYTHONPATH=/old|'
expect drift "a changed PYTHONPATH is drift"

variant '/^Environment=PYTHONPATH=/d'
expect drift "a REMOVED directive is drift"

variant 's|^ExecStart=.*|ExecStart=/old/python|'
expect drift "a changed ExecStart is drift"

variant 's/^Environment=QFD_LOG_CAP_MB=16/Environment=QFD_LOG_CAP_MB=99/'
expect drift "a changed non-placeholder env value is drift"

variant 's/^User=qfd/User=root/'
expect drift "a changed User is drift"

variant 's/999/1234/'
expect match "the substituted VALUE may differ"

# The regression that made the first implementation vacuous: one substituted
# environment line must not excuse the others.
variant 's/^Environment=QFD_ADMIN_UID=999/Environment=QFD_ADMIN_UID=1/; s|^Environment=PYTHONPATH=.*|Environment=PYTHONPATH=/old|'
expect drift "one substituted env line does not excuse the others"

# --- THE EFFECTIVE ENVIRONMENT, NOT THE FILE -------------------------------
# WHY THIS HALF EXISTS. `unit_matches` above compares FILES, so a `systemctl
# edit` drop-in is invisible to it. The live loop ran QF_TICK_MAX_DISAGREE=5
# against a committed 3 for weeks and nobody knew: new code under hand-tuned
# configuration, found only while investigating the 2026-09-04 pause.
#
# The failure shape this file already records applies here too: a check that
# compares a subset it derived from its own input passes on identical inputs and
# sees nothing else. So these cases assert what is REPORTED, not just the exit.
ENV_UNIT="$TMP/envunit"
cat > "$ENV_UNIT" <<'UNIT'
[Service]
Environment=QF_TICK_MAX_DISAGREE=3
Environment=QF_PAUSE_ISSUE_REPO=lotas/qf-research
UNIT

env_out=""
# OUTPUT AND EXIT STATUS FROM ONE CALL. Calling twice -- once for the status,
# once for the text -- would let a function that is not deterministic pass both
# halves while agreeing with neither.
env_expect() {  # env_expect <match|drift> <name> <effective>
  local rc=0
  env_out="$(env_matches "$ENV_UNIT" "$3")" || rc=$?
  if [ "$rc" = 0 ]; then
    [ "$1" = match ] && ok "$2" || bad "$2 (passed, wanted drift)"
  else
    [ "$1" = drift ] && ok "$2" || bad "$2 (reported drift, wanted a pass)"
  fi
}

env_expect match "identical environments pass" \
  "QF_TICK_MAX_DISAGREE=3 QF_PAUSE_ISSUE_REPO=lotas/qf-research"
[ -z "$env_out" ] && ok "an identical environment reports nothing" \
                  || bad "an identical environment reports nothing ($env_out)"

# A DECLARED KEY WHOSE VALUE DRIFTED is reported with BOTH values: these are
# thresholds and budgets, and the whole point is that a human can see 3 vs 5.
env_expect drift "a drifted declared value is drift" \
  "QF_TICK_MAX_DISAGREE=5 QF_PAUSE_ISSUE_REPO=lotas/qf-research"
case "$env_out" in
  *QF_TICK_MAX_DISAGREE*3*5*) ok "a drifted value is reported with both values" ;;
  *) bad "a drifted value is reported with both values ($env_out)" ;;
esac

# A DECLARED KEY ABSENT LIVE. The unit says it and the running service does not
# have it: the same class of failure as a stale unit file, reached the other way.
env_expect drift "a declared key absent live is drift" \
  "QF_PAUSE_ISSUE_REPO=lotas/qf-research"
case "$env_out" in
  *QF_TICK_MAX_DISAGREE*) ok "a declared key absent live is named" ;;
  *) bad "a declared key absent live is named ($env_out)" ;;
esac

# A KEY PRESENT ONLY LIVE. Reported BY NAME, with no value: this is the case a
# drop-in is most likely to produce, and its value is by definition
# unreviewed -- it could be anything, including a credential.
env_expect drift "a key present only live is drift" \
  "QF_TICK_MAX_DISAGREE=3 QF_PAUSE_ISSUE_REPO=lotas/qf-research QF_SECRET_THING=hunter2"
case "$env_out" in
  *QF_SECRET_THING*hunter2*) bad "an unexpected key's VALUE must not be printed ($env_out)" ;;
  *QF_SECRET_THING*)         ok  "an unexpected key is named, not valued" ;;
  *)                         bad "an unexpected key is named at all ($env_out)" ;;
esac

# ONLY the exact key set of the repo unit would MISS the line above, so this
# asserts the comparison is not scoped to declared keys -- the mutation that
# makes this file vacuous.
env_expect drift "the comparison is not scoped to the keys the repo declares" \
  "QF_TICK_MAX_DISAGREE=3 QF_PAUSE_ISSUE_REPO=lotas/qf-research QF_NEW_KNOB=7"

# NOTHING OUTSIDE QF_* IS READ, COMPARED OR PRINTED. The credential-shaped value
# here is the assertion: this output reaches a deploy log.
env_expect match "non-QF keys are neither compared nor reported" \
  "QF_TICK_MAX_DISAGREE=3 QF_PAUSE_ISSUE_REPO=lotas/qf-research PATH=/x DATABASE_URL=postgres://u:pw@h/db"
case "$env_out" in
  *DATABASE_URL*|*postgres://*|*pw@h*|*PATH*)
    bad "a non-QF key or value LEAKED into the report ($env_out)" ;;
  *) ok "no non-QF name or value appears in the report" ;;
esac

# THE PLACEHOLDER RULE, CARRIED OVER. `unit_matches` excludes substituted
# directives because the checkout says `%%DEPLOY_UID%%` and the live copy says
# `999`; the effective environment is substituted too, so comparing the literal
# would cry drift on every single run -- and a check that always fires is a
# check that gets ignored, which is the same silence by a slower route.
cat > "$TMP/envunit_ph" <<'UNIT'
[Service]
Environment=QF_ADMIN_UID=%%DEPLOY_UID%%
UNIT
if env_matches "$TMP/envunit_ph" "QF_ADMIN_UID=999" >/dev/null; then
  ok "a substituted env value is not drift"
else
  bad "a substituted env value is not drift"
fi
# ...but the KEY still has to be there. A placeholder excuses the value, never
# the directive: that is how `Environment=PYTHONPATH=` went missing in 2b-1.
if env_matches "$TMP/envunit_ph" "" >/dev/null; then
  bad "a placeholder does not excuse the key being absent"
else
  ok "a placeholder does not excuse the key being absent"
fi

# NO GLOBBING. `for t in $effective` -- the obvious spelling -- also performs
# pathname expansion, so a value containing `*` is replaced by filenames from
# whatever directory the setup script happens to be in. The comparison would
# then depend on the contents of a directory, which is a check whose answer
# nobody can reproduce.
( cd "$TMP" && : > 'QF_TICK_MAX_DISAGREE=X' && : > 'star-bait' )
env_out="$(cd "$TMP" && env_matches "$ENV_UNIT" \
  "QF_TICK_MAX_DISAGREE=* QF_PAUSE_ISSUE_REPO=lotas/qf-research")"
# THE ASSERTION IS ON THE VALUE REPORTED, not merely that drift was reported:
# an expanded `*` drifts too (against a FILENAME), so "it said DRIFT" cannot
# tell the two apart. The report must carry the metacharacter itself.
case "$env_out" in
  *star-bait*) bad "a glob expansion put a FILENAME in the report ($env_out)" ;;
  *"live=*"*)  ok "a glob metacharacter in a value is compared literally" ;;
  *) bad "a glob metacharacter in a value is compared literally ($env_out)" ;;
esac

# A QUOTED VALUE IS SAID TO BE UNPARSEABLE RATHER THAN SILENTLY TRUNCATED.
# `systemctl show -p Environment` prints ONE line of space-separated
# assignments and double-quotes any value containing whitespace, so a split on
# whitespace cannot recover it. Comparing the first word would be the "stopped
# checking" shape again: it can only ever report equal by accident.
env_out="$(env_matches "$ENV_UNIT" \
  'QF_TICK_MAX_DISAGREE="3 and a half" QF_PAUSE_ISSUE_REPO=lotas/qf-research')"
# THE LITERAL WORD, not merely the key name: a TRUNCATED comparison also prints
# the name (`DRIFT QF_TICK_MAX_DISAGREE repo=3 live="3`), so deleting the whole
# UNPARSEABLE branch passed this case in its first form. The distinction is the
# entire content of the decision not to un-quote.
case "$env_out" in
  *"UNPARSEABLE QF_TICK_MAX_DISAGREE"*) ok "a quoted (whitespace-bearing) live value is UNPARSEABLE, not compared" ;;
  *) bad "a quoted live value is UNPARSEABLE rather than truncated ($env_out)" ;;
esac
# And the words of a quoted value are not mistaken for keys of their own.
case "$env_out" in
  *UNEXPECTED*) bad "the tail of a quoted value was read as a key ($env_out)" ;;
  *) ok "the tail of a quoted value is not read as a key" ;;
esac

# QUOTING ON THE DECLARED SIDE, WHICH IS WHERE IT WAS MISSING. Both spellings
# are legal, and they put the quote in different places:
#
#   declared:  Environment="QF_FLAGS=--a --b"   -> token  "QF_FLAGS=--a
#   live:      QF_FLAGS="--a --b"               -> token  QF_FLAGS="--a
#
# Only the live one was normalised, so the declared key was dropped from the
# comparison entirely -- and the two failures that produced are opposite and
# both bad, so both are asserted here.
cat > "$TMP/envunit_q" <<'UNIT'
[Service]
Environment="QF_FLAGS=--a --b"
UNIT
env_out="$(env_matches "$TMP/envunit_q" 'QF_FLAGS="--a --b"')"
case "$env_out" in
  *UNEXPECTED*) bad "a quoted DECLARATION must not be reported as absent from the unit: it is right there ($env_out)" ;;
  *"UNPARSEABLE QF_FLAGS"*) ok "a quoted declaration is UNPARSEABLE, not falsely UNEXPECTED" ;;
  *) bad "a quoted declaration is reported at all ($env_out)" ;;
esac
# THE WORSE HALF: the key vanishing live returned CLEAN, because a dropped
# declaration leaves nothing to miss it -- so the MISSING branch that exists for
# the 2b-1 `PYTHONPATH` disappearance did not fire for a quoted declaration.
env_out="$(env_matches "$TMP/envunit_q" "")"
if [ -z "$env_out" ]; then
  bad "a quoted declaration going missing live must NOT pass clean"
else
  case "$env_out" in
    *QF_FLAGS*) ok "a quoted declaration absent live is still reported" ;;
    *) bad "a quoted declaration absent live is reported ($env_out)" ;;
  esac
fi
# AND A FULLY QUOTED SINGLE-WORD DECLARATION IS NOT A PROBLEM. `Environment=
# "QF_A=1"` has to compare equal to a live `1`: firing over punctuation is the
# always-fires shape, which is the failure this file keeps circling.
cat > "$TMP/envunit_q1" <<'UNIT'
[Service]
Environment="QF_A=1"
UNIT
env_matches "$TMP/envunit_q1" 'QF_A=1' >/dev/null \
  && ok "a fully quoted single-word declaration compares as its value" \
  || bad "a fully quoted single-word declaration compares as its value"
env_matches "$TMP/envunit_q1" 'QF_A="1"' >/dev/null \
  && ok "a fully quoted single-word LIVE value compares as its value" \
  || bad "a fully quoted single-word LIVE value compares as its value"

# A `QF_`-SHAPED TOKEN WITH NO `=`. The branch that keeps it has a comment
# saying "a tokeniser that silently discards what it does not understand is how
# a check narrows itself to nothing" -- and replacing that branch with
# `continue` passed every other case in this file. The quoted-value cases cannot
# reach it, because the tail words of a quoted value are not QF_-shaped.
env_out="$(env_matches "$ENV_UNIT" \
  "QF_TICK_MAX_DISAGREE=3 QF_PAUSE_ISSUE_REPO=lotas/qf-research QF_STRAY")"
case "$env_out" in
  *"UNEXPECTED QF_STRAY"*) ok "a QF_-shaped token with no '=' is reported, not discarded" ;;
  *) bad "a QF_-shaped token with no '=' is reported, not discarded ($env_out)" ;;
esac

# --- THE CHECK IS ACTUALLY CALLED ------------------------------------------
# WHY THIS IS A TEST. Everything above sources `env_matches` and calls it
# directly, so DELETING THE CALL SITE from `assert_units_current` left this
# suite entirely green: a function nothing invokes is a check that does not run,
# which is this file's own subject one level up. The sibling suite
# (test_tick_failure_alarm.sh) asserts the token preflight is wired for the same
# reason.
CALLER="$(sed -n '/^assert_units_current()/,/^}/p' "$SETUP")"
printf '%s' "$CALLER" | grep -q 'env_matches "\$src"' \
  && ok "assert_units_current calls env_matches on each unit" \
  || bad "assert_units_current never calls env_matches: the check does not run"
printf '%s' "$CALLER" | grep -q 'systemctl show -p Environment' \
  && ok "it reads the EFFECTIVE environment, which is the whole point" \
  || bad "it does not read systemctl show -p Environment"
# AND THE ALL-CLEAR IS GATED. `info "installed units match the checkout"` used
# to print unconditionally, so the last line after a reported drop-in was an
# all-clear -- re-burying exactly what this feature surfaces.
printf '%s' "$CALLER" | grep -q 'unit FILES match the checkout' \
  && ok "the summary claims only that the FILES match when env drift was found" \
  || bad "the summary line is an unconditional all-clear over the warnings"

# --- EVERY unit is in the drift list --------------------------------------
# WHY THIS IS A TEST AND NOT A READING. `assert_units_current` is what stops
# `mirror-refresh` restarting the daemon into configuration from an older commit,
# and it works off a HAND-WRITTEN list. A phase that adds a unit and forgets the
# list gets exactly the failure the function exists to prevent -- silently, and
# one phase later, which is what happened: the evaluator's two units were absent
# from it until this test was written.
LIST="$(sed -n '/^assert_units_current()/,/^}/p' "$SETUP")"
missing=0
while IFS= read -r unit; do
  rel="${unit#"$HERE"/../}"
  if printf '%s' "$LIST" | grep -q "$rel"; then
    ok "$rel is in the drift list"
  else
    bad "$rel is NOT in assert_units_current's list: mirror-refresh would
     restart into a stale copy of it without saying so"
    missing=$((missing + 1))
  fi
done < <(find "$HERE/.." -mindepth 2 -maxdepth 2 \
              \( -name '*.service' -o -name '*.socket' -o -name '*.timer' \) \
         | sort)
[ "$missing" -eq 0 ] || echo "  (remedy: add them, with the setup script that installs each)"

# --- THE PROFILE, WHICH RUNS AFTER SYSTEMD HAS ALREADY SPOKEN --------------
# WHY THIS THIRD HALF EXISTS, and it is the blind spot the half above still
# had. `qf-tick.service` starts a LOGIN shell -- `/bin/bash -lc` -- and does so
# deliberately: the research user's egress is nftables uid-scoped through
# tinyproxy and the proxy variables live in `~/.profile`, so a bare `ExecStart`
# makes git fail as "Failed to connect to github.com port 443 after 5 ms".
#
# The consequence is an ORDERING one. systemd sets the unit's `Environment=`
# FIRST and the profile runs AFTERWARDS, inside the process, so a stale
# `export QF_TICK_MAX_DISAGREE=5` in `~research/.profile` beats the reviewed 3
# at execution time -- while `env_matches` above compares systemd's
# CONFIGURATION and reports a clean match. The check whose entire purpose is
# making the loop's configuration visible was blind in the one direction the
# loop actually reads its configuration from, and the same route can override
# `QF_PAUSE_ISSUE_REPO` and `QF_PAUSE_TOKEN_FILE`, which would make a pause
# file nothing at all.
#
# A REPORT, NOT A REFUSAL, for the same reason `env_matches` warns: a forgotten
# override must not block the deploy that ships its fix.
PHOME="$TMP/phome"
PUNIT="$TMP/punit"
cat > "$PUNIT" <<'UNIT'
[Service]
User=research
ExecStart=/bin/bash -lc '/srv/x/research-loop/tick.sh'
Environment=QF_TICK_MAX_DISAGREE=3
Environment=QF_PAUSE_ISSUE_REPO=lotas/qf-research
UNIT

# A FRESH HOME PER CASE. A leftover `.profile` from the previous case is the
# shape that makes a suite pass for the wrong reason.
phome_reset() { rm -rf "$PHOME"; mkdir -p "$PHOME"; }

prof_out=""
# OUTPUT AND EXIT STATUS FROM ONE CALL, as with `env_expect` above.
prof_expect() {  # prof_expect <clean|reported> <name>
  local rc=0
  prof_out="$(profile_qf_matches "$PUNIT" "$PHOME")" || rc=$?
  if [ "$rc" = 0 ]; then
    [ "$1" = clean ] && ok "$2" || bad "$2 (passed, wanted a report)"
  else
    [ "$1" = reported ] && ok "$2" || bad "$2 (reported, wanted a clean pass)"
  fi
}

# NO PROFILE AT ALL IS NOT AN ERROR. A login shell with no login file reads no
# login file; refusing or warning here would fire on every host that has none
# and teach an operator to ignore the whole block.
phome_reset
prof_expect clean "no profile at all is not an error"
[ -z "$prof_out" ] && ok "no profile reports nothing" \
                   || bad "no profile reports nothing ($prof_out)"

# THE CORE CASE: the exact stale export that motivated this, reported with the
# FILE, because "a QF_ key is overridden somewhere" is not actionable.
phome_reset
printf 'export QF_TICK_MAX_DISAGREE=5\n' > "$PHOME/.profile"
prof_expect reported "a QF_* export in the login profile is reported"
case "$prof_out" in
  *"QF_TICK_MAX_DISAGREE"*"$PHOME/.profile"*)
    ok "the report names the key AND the file it was found in" ;;
  *) bad "the report names the key AND the file it was found in ($prof_out)" ;;
esac
# BOTH VALUES, following `env_matches`'s policy exactly: a key the unit declares
# is a reviewed threshold, and the entire point is that a human reads "3 vs 5".
case "$prof_out" in
  *OVERRIDE*QF_TICK_MAX_DISAGREE*3*5*)
    ok "a declared key exported with a DIFFERENT value is an OVERRIDE naming both values" ;;
  *) bad "a declared key exported with a different value is an OVERRIDE naming both values ($prof_out)" ;;
esac

# THE SAME VALUE IS STILL WORTH SAYING, at lower volume: it is a second source
# of truth for a reviewed number, and it will drift silently the moment either
# side changes -- which is how the live 5 survived a committed 3 for weeks.
phome_reset
printf 'export QF_TICK_MAX_DISAGREE=3\n' > "$PHOME/.profile"
prof_expect reported "a declared key exported with the SAME value is still reported"
case "$prof_out" in
  *DUPLICATE*QF_TICK_MAX_DISAGREE*) ok "a matching assignment is reported as DUPLICATION, not as an override" ;;
  *) bad "a matching assignment is reported as DUPLICATION, not as an override ($prof_out)" ;;
esac
case "$prof_out" in
  *OVERRIDE*) bad "a matching assignment must NOT be called an override ($prof_out)" ;;
  *) ok "a matching assignment is not called an override" ;;
esac

# A KEY THE UNIT DOES NOT DECLARE: NAME ONLY. Its value is by definition
# unreviewed -- it could be anything, including a credential -- and this output
# goes to a deploy log. Same rule as `env_matches`'s UNEXPECTED.
phome_reset
printf 'export QF_SECRET_THING=hunter2\n' > "$PHOME/.profile"
prof_expect reported "an unexpected QF_ key in a profile is reported"
case "$prof_out" in
  *hunter2*)          bad "an unexpected key's VALUE must not be printed ($prof_out)" ;;
  *QF_SECRET_THING*)  ok  "an unexpected profile key is named, not valued" ;;
  *)                  bad "an unexpected profile key is named at all ($prof_out)" ;;
esac

# NOTHING OUTSIDE QF_* IS READ, COMPARED OR PRINTED. This profile is where the
# proxy variables and the agent CLIs' credentials live, so the credential-shaped
# export here is the assertion, not decoration.
phome_reset
cat > "$PHOME/.profile" <<'PROF'
export GH_TOKEN=ghp_averyrealsecret999
export https_proxy=http://127.0.0.1:8888
export DATABASE_URL=postgres://u:pw@h/db
PROF
prof_expect clean "a profile with no QF_* assignment is clean"
case "$prof_out" in
  *ghp_averyrealsecret999*|*GH_TOKEN*|*DATABASE_URL*|*postgres://*|*https_proxy*|*8888*)
    bad "a non-QF name or value LEAKED into the profile report ($prof_out)" ;;
  *) ok "no non-QF name or value appears in the profile report" ;;
esac
# AND NOT EVEN WHEN THERE IS A QF_ LINE TO REPORT ALONGSIDE IT: the leak that
# matters is the one in a report that is being printed anyway.
printf 'export QF_TICK_MAX_DISAGREE=5\n' >> "$PHOME/.profile"
prof_expect reported "a QF_ line beside a credential line is reported"
case "$prof_out" in
  *ghp_averyrealsecret999*|*GH_TOKEN*|*DATABASE_URL*|*pw@h*)
    bad "a credential leaked into a report that had a QF_ finding to print ($prof_out)" ;;
  *) ok "the credential beside the finding is still neither compared nor printed" ;;
esac

# AN UNREADABLE PROFILE IS UNKNOWN, NOT CLEAN. Reading nothing and reporting a
# pass is the "check that has stopped checking" shape this file exists for: the
# question was never asked, so the answer is not "no".
phome_reset
printf 'export QF_TICK_MAX_DISAGREE=5\n' > "$PHOME/.profile"
chmod 000 "$PHOME/.profile"
if [ -r "$PHOME/.profile" ]; then
  # Running as root, where mode 000 is still readable: assert the same class
  # (exists, cannot be read AS A SHELL FILE) by a route privilege cannot undo.
  rm -f "$PHOME/.profile"; mkdir -p "$PHOME/.profile"
fi
prof_expect reported "an unreadable profile does not pass clean"
case "$prof_out" in
  *UNREADABLE*"$PHOME/.profile"*) ok "an unreadable profile is reported as UNKNOWN, naming the file" ;;
  *) bad "an unreadable profile is reported as UNKNOWN, naming the file ($prof_out)" ;;
esac
chmod 700 "$PHOME" 2>/dev/null; rm -rf "$PHOME/.profile" 2>/dev/null

# PRECEDENCE, WHICH IS NOT A GLOB. bash reads the FIRST of ~/.bash_profile,
# ~/.bash_login, ~/.profile that exists and is readable, and the others are then
# dead text. Reporting a dead file's export as live would send an operator to
# edit a file the tick never reads; NOT reporting it at all would hide an
# override that goes live the moment the shadowing file is deleted.
phome_reset
printf 'export QF_TICK_MAX_DISAGREE=9\n' > "$PHOME/.bash_profile"
printf 'export QF_TICK_MAX_DISAGREE=5\n' > "$PHOME/.profile"
prof_expect reported "both candidate login files are reported"
case "$prof_out" in
  *"$PHOME/.bash_profile"*) ok ".bash_profile, which bash actually reads, is reported" ;;
  *) bad ".bash_profile, which bash actually reads, is reported ($prof_out)" ;;
esac
case "$prof_out" in
  *"$PHOME/.profile"*shadowed*|*shadowed*"$PHOME/.profile"*)
    ok "the shadowed .profile is reported AS shadowed, not as live" ;;
  *) bad "the shadowed .profile is reported as shadowed ($prof_out)" ;;
esac
# AND THE PRECEDENCE FOLLOWS BASH'S OWN RULE FOR AN UNREADABLE FIRST CANDIDATE:
# bash skips one it cannot read and uses the next, so `.profile` is then LIVE
# and calling it shadowed would be a false description.
chmod 000 "$PHOME/.bash_profile"
if [ -r "$PHOME/.bash_profile" ]; then
  rm -f "$PHOME/.bash_profile"; mkdir -p "$PHOME/.bash_profile"
fi
prof_expect reported "an unreadable first candidate still reports"
case "$prof_out" in
  *"$PHOME/.profile"*shadowed*|*shadowed*"$PHOME/.profile"*)
    bad "an unreadable .bash_profile does not shadow .profile: bash skips it ($prof_out)" ;;
  *) ok "an unreadable first candidate does not shadow the file bash falls through to" ;;
esac
chmod 700 "$PHOME" 2>/dev/null; rm -rf "$PHOME/.bash_profile" 2>/dev/null

# WHAT THE PROFILE SOURCES IS PART OF THE PROFILE. phase0-setup.sh puts the
# proxy variables in `~/.profile.d-proxy` and appends `. /home/research/...` to
# `~/.profile`, so a scan of one file only would miss the very file this box
# actually keeps its exported environment in.
phome_reset
printf '. %s/.profile.d-proxy\n' "$PHOME" > "$PHOME/.profile"
printf 'export QF_PAUSE_ISSUE_REPO=someone/else\n' > "$PHOME/.profile.d-proxy"
prof_expect reported "a QF_ export in a SOURCED file is reported"
case "$prof_out" in
  *"$PHOME/.profile.d-proxy"*)
    ok "a sourced file is named as the file the assignment is in" ;;
  *) bad "a sourced file is named as the file the assignment is in ($prof_out)" ;;
esac
case "$prof_out" in
  *OVERRIDE*QF_PAUSE_ISSUE_REPO*lotas/qf-research*someone/else*)
    ok "an override of the alarm's repo is reported with both values" ;;
  *) bad "an override of the alarm's repo is reported with both values ($prof_out)" ;;
esac
# The Debian spelling, with $HOME and quotes and a test in front of it.
phome_reset
printf '[ -r "$HOME/.profile.d-proxy" ] && . "$HOME/.profile.d-proxy"\n' > "$PHOME/.profile"
printf 'export QF_FRONTIER_RETIRE_AFTER=0\n' > "$PHOME/.profile.d-proxy"
prof_expect reported 'a $HOME-relative, quoted, guarded source is followed'
case "$prof_out" in
  *QF_FRONTIER_RETIRE_AFTER*) ok "the assignment behind a guarded source is found" ;;
  *) bad "the assignment behind a guarded source is found ($prof_out)" ;;
esac

# A SOURCE PATH THIS SCAN CANNOT RESOLVE IS SAID SO, ONCE. `\. "$NVM_DIR/nvm.sh"`
# is in this box's real dot-files and no textual scan can resolve it. Claiming
# coverage of a file that was never opened is worse than saying it was not.
phome_reset
cat > "$PHOME/.profile" <<'PROF'
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
PROF
prof_expect reported "an unresolvable source path is reported as UNSCANNED"
case "$prof_out" in
  *UNSCANNED*) ok "an unresolvable source is UNSCANNED, not silently claimed" ;;
  *) bad "an unresolvable source is UNSCANNED, not silently claimed ($prof_out)" ;;
esac
case "$prof_out" in
  *NVM_DIR*) bad "a non-QF variable NAME leaked via the UNSCANNED line ($prof_out)" ;;
  *) ok "the UNSCANNED line leaks no non-QF name" ;;
esac

# THE DEPTH LIMIT IS STATED, NOT SILENT. A chain deeper than the scan follows
# must be reported as unscanned; stopping quietly is a check narrowing itself.
phome_reset
printf '. %s/a\n' "$PHOME" > "$PHOME/.profile"
printf '. %s/b\n' "$PHOME" > "$PHOME/a"
printf '. %s/c\n' "$PHOME" > "$PHOME/b"
printf 'export QF_TOO_DEEP=1\n' > "$PHOME/c"
prof_expect reported "a chain past the depth limit is reported"
case "$prof_out" in
  *UNSCANNED*"$PHOME/c"*) ok "the file past the depth limit is named as UNSCANNED" ;;
  *) bad "the file past the depth limit is named as UNSCANNED ($prof_out)" ;;
esac
case "$prof_out" in
  *"UNEXPECTED QF_TOO_DEEP"*) bad "the depth limit is not being applied ($prof_out)" ;;
  *) ok "the scan does not claim to have read past its own depth limit" ;;
esac
# A CYCLE TERMINATES. `.profile` sourcing a file that sources `.profile` back is
# legal shell and would hang a naive walk.
phome_reset
printf '. %s/loop\nexport QF_TICK_MAX_DISAGREE=5\n' "$PHOME" > "$PHOME/.profile"
printf '. %s/.profile\n' "$PHOME" > "$PHOME/loop"
prof_expect reported "a source cycle terminates and still reports"
# AND REPORTS IT ONCE. Deleting the `seen` guard TERMINATED anyway -- the depth
# limit stops the walk -- so "it did not hang" proved nothing about the guard.
# What the guard actually buys is that a file reached twice is not REPORTED
# twice, and a report that says the same override twice is a report an operator
# starts skimming.
cyc="$(printf '%s\n' "$prof_out" | grep -c 'PROFILE-OVERRIDE QF_TICK_MAX_DISAGREE')"
[ "$cyc" = 1 ] && ok "a file reached twice through a cycle is reported once" \
               || bad "a file reached twice is reported $cyc times, not once ($prof_out)"

# COMMENTS ARE NOT ASSIGNMENTS. The stale export an operator has already
# commented out must not keep firing forever: a check that always fires is one
# that gets ignored, which is this file's own failure mode.
phome_reset
printf '# export QF_TICK_MAX_DISAGREE=5\n   #export QF_PAUSE_ISSUE_REPO=x/y\n' > "$PHOME/.profile"
prof_expect clean "a commented-out QF_ assignment is not reported"
[ -z "$prof_out" ] && ok "a commented-out assignment reports nothing" \
                   || bad "a commented-out assignment reports nothing ($prof_out)"

# ONLY UNITS THAT ACTUALLY READ A PROFILE. `qf-dispatch.service` runs python
# directly, so scanning qfd's dot-files would be a report about a file nothing
# reads -- noise in the one block that must stay worth reading.
_unit_reads_login_profile "$HERE/../research-loop/qf-tick.service" \
  && ok "the tick unit is recognised as reading a login profile" \
  || bad "the tick unit is recognised as reading a login profile"
_unit_reads_login_profile "$HERE/../research-loop/qf-tick-failure.service" \
  && ok "the failure-alarm unit is recognised too (same -lc, same exposure)" \
  || bad "the failure-alarm unit is recognised too"
_unit_reads_login_profile "$HERE/../dispatcher/qf-dispatch.service" \
  && bad "a unit with no login shell must NOT be scanned for profiles" \
  || ok "a unit with no login shell is not scanned for profiles"

# --- THE PROFILE CHECK IS ACTUALLY CALLED ----------------------------------
# Same reason as the sibling block above: deleting the call site leaves every
# case in this section green, because they all call the function directly.
printf '%s' "$CALLER" | grep -q 'profile_qf_matches "\$src"' \
  && ok "assert_units_current calls profile_qf_matches on each login-shell unit" \
  || bad "assert_units_current never calls profile_qf_matches: the check does not run"
# THE GUARD LINE IS PINNED WHOLE, and that is not pedantry: prefixing it with
# `if false &&` left every case in this file green, because they all call the
# function directly and the call site still contained the name a substring grep
# was looking for. A call that cannot be reached is a check that does not run --
# the same defect as no call at all, wearing the text of one.
printf '%s' "$CALLER" | grep -qF 'if _unit_reads_login_profile "$src"; then' \
  && ok "the profile scan is gated by the login-shell predicate and nothing else" \
  || bad "the guard on the profile scan is not the bare predicate: it may be unreachable"
# AND IT RESOLVES THE OWNER'S HOME RATHER THAN ASSUMING /home/<user>. A guessed
# home that does not exist finds no profile and reports CLEAN -- a false
# all-clear produced by looking in the wrong place.
printf '%s' "$CALLER" | grep -q 'getent passwd' \
  && ok "the caller resolves the profile owner's home from passwd" \
  || bad "the caller guesses the home directory instead of resolving it"
# THE PROFILE-SPECIFIC WORDING, not just the phrase: `UNKNOWN, not absent` is
# already in the `systemctl show` branch above, so grepping for it alone passed
# while this branch did not exist at all -- a green assertion over absent code,
# which is this file's subject in miniature.
printf '%s' "$CALLER" | grep -q 'login profile is UNKNOWN, not absent' \
  && ok "an unresolvable home is reported as UNKNOWN, not treated as no profile" \
  || bad "an unresolvable home is not reported as UNKNOWN"
# AND THE ALL-CLEAR IS GATED ON THIS TOO. `installed units match the checkout`
# after a reported profile override would re-bury exactly what was surfaced --
# the same defect that gating on `env_drifted` alone was added to fix.
printf '%s' "$CALLER" | grep -q 'prof_drifted' \
  && ok "the summary accounts for profile findings as well as drop-in drift" \
  || bad "the summary ignores profile findings, so the last line can contradict them"

echo
echo "unit-drift: pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
