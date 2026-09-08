#!/usr/bin/env bash
# The pause issue: the alarm, and the handle a human uses to release the brake.
#
# WHY THIS EXISTS. On 2026-09-04 the loop paused itself correctly and NOBODY
# FOUND OUT FOR TWO AND A HALF DAYS. `PAUSE` is a file on a box and the
# escalation is pushed to a git repo nobody was watching; the silence, not the
# pause, is what cost the time. This files a GitHub issue at the moment `PAUSE`
# is written, and reads it back on the next tick so closing it resumes the loop.
#
# WHAT THIS IS NOT: ENFORCEMENT. `research` can already `rm ~/qf-research/PAUSE`,
# and the design treats `qf-research` as untrusted narrative whose authority
# lives elsewhere -- a hash chain in the dispatcher and a root-owned evaluator
# (auto-research-loop-design.md:114, tick.sh:846). A token that can close an
# issue therefore grants nothing new, and unlike an `rm` a self-release leaves an
# attributable, timestamped GitHub event. The login allowlist below is
# normal-workflow policy, not a security boundary. Real enforcement would need
# root-owned pause state and a per-pause nonce (design §4.5); it is out of scope
# here, deliberately, and there is no enforcement theatre standing in for it.
#
#   pause-issue.sh open  <pause file> <brake> <n> <escalation path>
#   pause-issue.sh check <pause file>
#
# THE SEAM: this script PRODUCES `$QF_TICK_STATE/human-directive.md`. It does not
# assemble a prompt. Deciding that the directive is leader-only, injecting it
# beside the escalation feedback and deleting it after one use all live in
# `tick.sh`'s context assembly -- so the copilot's input is built in exactly one
# place and cannot acquire a new source from here.
#
# `check` EXITS ZERO ONLY WHEN THE LOOP MAY RUN. Every other answer -- no
# binding, a repo mismatch, an open issue, a stranger's close, an API error, an
# answer that does not parse -- is a non-zero exit and the brake stays on.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${QF_PAUSE_ISSUE_REPO:-}"
# `${HOME:-}`, NOT `$HOME`. Under `set -u` a bare `$HOME` in these defaults is a
# raw bash error at line 30 -- BEFORE `say` is even defined -- so an unset HOME
# produced `unbound variable` on stderr and the tick logged only "PAUSE exists;
# stopping". The two entry points disagree about HOME (`systemd` sets it from
# the unit's User=, `sudo -H` sets it, a bare `su` may not), so this is
# reachable. Refused with a message below rather than defaulted to `/`.
TOKEN_FILE="${QF_PAUSE_TOKEN_FILE:-${HOME:-}/.config/qf/pause-issue-token}"
ALLOWLIST="${QF_PAUSE_ALLOWLIST:-$HERE/pause-resume-allowlist.txt}"
STATE="${QF_TICK_STATE:-${XDG_STATE_HOME:-${HOME:-}/.local/state}/qf-tick}"
DIRECTIVE="$STATE/human-directive.md"
LABEL="qf-pause"

# THE THREE STATE FILENAMES ARE SHARED WITH `tick.sh`, not restated here. See
# state-names.sh for the rename failure that makes the read-back below
# self-confirming. A MISSING FILE IS A REFUSAL, not a set of defaults: guessing
# the names is precisely the failure the shared file exists to remove.
# shellcheck source=state-names.sh
if [ ! -r "$HERE/state-names.sh" ]; then
  printf '[pause-issue] %s\n' \
    "$HERE/state-names.sh is missing or unreadable, so the counter names this" \
    "  would zero cannot be trusted. Refusing to touch any state; if a PAUSE" \
    "  exists it stays. Reinstall the research-loop directory." >&2
  exit 1
fi
. "$HERE/state-names.sh"

# HOW MUCH OF A COMMENT THREAD REACHES THE LEADER. The same knob as `tick.sh`'s
# feedback cap on purpose: a comment thread is unbounded and the leader's
# context is not.
#
# ONE POLICY PER KNOB, AND IT IS TICK.SH'S. `tick.sh` DIES on a malformed
# QF_TICK_MAX_FEEDBACK_BYTES -- a malformed bound is not a smaller bound, it is
# no bound -- so by the time the real caller reaches here the value is already
# validated and this fallback is unreachable from it. It exists ONLY for a
# hand-invocation of this script (`pause-issue.sh check ~/qf-research/PAUSE` at
# a prompt), where there is no tick to have validated anything and refusing to
# read an issue over a formatting problem in an unrelated knob would be absurd.
# It is a hand-invocation convenience, NOT a second opinion about the policy:
# under the timer, tick.sh has already refused.
MAX_FEEDBACK_BYTES="${QF_TICK_MAX_FEEDBACK_BYTES:-4096}"
case "$MAX_FEEDBACK_BYTES" in
  ''|*[!0-9]*|0)
    say_pre="$MAX_FEEDBACK_BYTES"; MAX_FEEDBACK_BYTES=4096 ;;
esac

# EVERY LOG LINE GOES TO STDERR. Not cosmetic: `cmd_directive`'s STDOUT IS THE
# DIRECTIVE FILE and `cmd_open` captures `gh`'s stdout to learn an issue number,
# so a `say` on stdout would splice a log line into the leader's instructions
# or turn "no token" into an issue number. `tick.sh` captures both streams.
say() { printf '[pause-issue %s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }

# AN UNSET HOME IS SAID OUT LOUD. `${HOME:-}` above keeps `set -u` from killing
# this script before `say` exists, but a silently `/`-rooted token path is its
# own mystery -- so if HOME is unset and the caller did not pin the paths, say
# which paths are now wrong and how to fix it.
if [ -z "${HOME:-}" ]; then
  if [ -z "${QF_PAUSE_TOKEN_FILE:-}" ] || [ -z "${QF_TICK_STATE:-}" ]; then
    say "HOME is unset, so the default token and state paths are rooted at '/'"
    say "  and will not resolve. Set HOME, or pin QF_PAUSE_TOKEN_FILE and"
    say "  QF_TICK_STATE. Any PAUSE stays until then."
  fi
fi

[ -z "${say_pre:-}" ] || say "QF_TICK_MAX_FEEDBACK_BYTES='$say_pre' is not a
  positive integer; using 4096 for this hand-invocation. Under the timer
  tick.sh would have refused to run at all -- fix the value."

# PINNED IN TRUSTED CONFIGURATION, NEVER DERIVED FROM `git remote`: the remote
# lives in a research-writable checkout, so deriving the repository from it
# would let the identity being watched choose who gets told. Unset means do not
# attempt, and say so -- for `check` that is one more reason to stay paused.
if [ -z "$REPO" ]; then
  say "QF_PAUSE_ISSUE_REPO is unset (it belongs in the root-owned unit);"
  say "  filing and reading nothing. If a PAUSE exists it stays."
  exit 1
fi

gh_() {  # gh with ONLY the pause token in scope, so the interactive `gh` login
         # this box already has is neither used nor disturbed.
  local token=""
  if [ -r "$TOKEN_FILE" ]; then
    token="$(cat "$TOKEN_FILE" 2>/dev/null)"
  fi
  if [ -z "$token" ]; then
    # THE MOST LIKELY FIRST-RUN FAILURE, so it says what to DO. "No token at
    # <path>" told an operator what broke and left them to guess the rest.
    say "no pause token at $TOKEN_FILE (empty or unreadable). Create a"
    say "  fine-grained PAT on $REPO with Issues: read and write and NOTHING"
    say "  else -- notably not Contents -- then:"
    say "    install -m 0600 /dev/null '$TOKEN_FILE' && \\"
    say "      printf '%s' <token> > '$TOKEN_FILE'"
    say "  QF_PAUSE_TOKEN_FILE overrides the path. Until then the alarm cannot"
    say "  be filed and no pause can be released through the issue."
    return 1
  fi
  GH_TOKEN="$token" GITHUB_TOKEN="$token" GH_PROMPT_DISABLED=1 gh "$@"
}

# LOWERCASED ON BOTH SIDES. GitHub logins are case-insensitive, so an allowlist
# entry a human typed as `Lotas` must not silently authorise nobody -- an
# allowlist that quietly matches nothing is the pause that never releases.
lc() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

allowed() {  # allowed <login>
  local login="$1"
  [ -n "$login" ] || return 1
  if [ ! -r "$ALLOWLIST" ]; then
    say "no readable allowlist at $ALLOWLIST; nobody is authorised"
    return 1
  fi
  # Strip comments, blanks and surrounding whitespace, then match the WHOLE
  # line: a substring match would let `lotas-bot` release a pause for `lotas`.
  sed -e 's/#.*//' -e 's/[[:space:]]//g' "$ALLOWLIST" \
    | tr '[:upper:]' '[:lower:]' \
    | grep -qxF "$(lc "$login")"
}

# THE STAMP IS THE IDEMPOTENCY KEY, AND IT COMES OUT OF A RESEARCH-WRITABLE
# FILE, so its shape is checked before use rather than trusted. `tick.sh` writes
# `20260904T101112Z`.
#
# THE TWO SINKS THAT MAKE THE CHARSET LOAD-BEARING, named because a wrong `why`
# in this file is itself a defect:
#
#   1. `gh issue list --search "$stamp"` -- argv, so no shell parsing, but a
#      qualifier-shaped value (`is:`, `label:`) would silently redirect the
#      idempotency query and either duplicate the alarm or suppress it.
#   2. `case "$body" in *"$stamp"*)` -- the stamp-agreement test. This is the
#      important one. The pattern is quoted, so a `*` inside the stamp is a
#      literal here, but the charset ALSO excludes glob metacharacters, which
#      means no future unquoting of that pattern can turn the containment test
#      into a wildcard that makes every issue body "carry" the stamp. That
#      would restore the self-release the stamp check exists to close.
#
# It no longer reaches a jq program -- the search results are re-checked in
# shell -- and that clause used to say otherwise.
stamp_ok() {  # stamp_ok <stamp>
  case "$1" in
    ''|*[!0-9A-Za-z:._-]*) return 1 ;;
  esac
  return 0
}

# THE STAMP, RECOVERED THREE WAYS, because the pause files already on the box
# predate §4 and have no `stamp:` line -- and refusing to alarm for them would
# leave exactly the pause nobody heard about un-alarmed.
#
# THE FALLBACK IS THE FILE'S MTIME, NEVER `date`. A stamp that changed on every
# retry would defeat the idempotency search and file a fresh duplicate issue
# every hour, which turns the alarm into the noise it was meant to cut through.
pause_stamp() {  # pause_stamp <pause file>
  local pause="$1" s
  s="$(sed -n 's/^stamp: //p' "$pause" 2>/dev/null | head -1)"
  if [ -z "$s" ]; then
    s="$(sed -n 's/^auto-paused \([^:]*\):.*/\1/p' "$pause" 2>/dev/null | head -1)"
  fi
  if [ -z "$s" ]; then
    s="$(date -u -r "$pause" +%Y%m%dT%H%M%SZ 2>/dev/null)"
  fi
  stamp_ok "$s" || return 1
  printf '%s' "$s"
}

# --------------------------------------------------------------------------
# open
# --------------------------------------------------------------------------
cmd_open() {
  local pause="$1" brake="${2:-unknown}" n="${3:-?}" recorded="${4:-}"
  [ -n "$pause" ] || { say "open needs a pause file"; return 2; }
  [ -e "$pause" ] || { say "no pause file at $pause"; return 1; }
  local stamp
  stamp="$(pause_stamp "$pause")" || {
    say "no usable stamp in $pause; refusing to interpolate one"; return 1; }

  # THE LABEL IS PREFLIGHTED AS A READ FIRST, THEN CREATED ONLY IF ABSENT.
  #
  # THE FAILURE THIS FIXES: `gh label create` exits NON-ZERO when the label
  # already exists, which is the state on every run after the first. Keying
  # `--label` off "did create succeed" therefore filed essentially every pause
  # issue WITHOUT the label -- the exact opposite of §4.1's intent. "Continue if
  # creating it fails" exists so a LABEL problem cannot cost us the ALARM; it
  # was never meant to make the label unreachable.
  #
  # AND THE EXISTENCE TEST IS A READ, NOT `create`'s ERROR PROSE. Parsing "already
  # exists" out of stderr is a string contract with a tool that changes under us,
  # and it fails silently in the safe-looking direction.
  #
  # FAIL-OPEN IS UNCHANGED: if the label's state cannot be determined at all, the
  # issue is filed WITHOUT the label and that is said out loud. An unlabelled
  # alarm is worth incomparably more than no alarm.
  local label_ok=0 labels
  if labels="$(gh_ label list --repo "$REPO" --search "$LABEL" --limit 50 \
                 --json name --jq '.[].name')"; then
    # `--search` IS FUZZY, so the name is matched exactly here rather than
    # trusting the hit: `qf-paused` is not `qf-pause`.
    if printf '%s\n' "$labels" | grep -qxF "$LABEL"; then
      label_ok=1
    elif gh_ label create "$LABEL" --repo "$REPO" \
           --description "auto-paused research loop" --color B60205 \
           >/dev/null 2>&1; then
      label_ok=1
    else
      say "could not create the $LABEL label; filing the issue without it"
    fi
  else
    say "could not read the labels of $REPO, so the $LABEL label's state is"
    say "  unknown; filing the issue without it rather than losing the alarm"
  fi

  # IDEMPOTENT ON THE STAMP, ACROSS OPEN *AND CLOSED*, AND NOT FILTERED BY
  # LABEL. Both halves are load-bearing: if creation succeeded and this process
  # died before rewriting PAUSE, the retry runs after a human may already have
  # CLOSED the issue, and an open-only search would file a duplicate against a
  # pause that was already handled. Label creation is allowed to fail above, so
  # the label cannot be part of the key either. The key is the exact stamp
  # string in the body -- GitHub search is fuzzy, so the hit is re-checked here.
  local hits found=""
  hits="$(gh_ issue list --repo "$REPO" --state all --search "$stamp" \
            --limit 50 --json number,body \
            --jq '.[] | [(.number|tostring), (.body // "" | gsub("[\r\n]"; " "))] | @tsv')" || {
    # AN ERRORED SEARCH IS NOT "NO SUCH ISSUE". Reading it as one would file a
    # duplicate on every tick for as long as the API is unhappy.
    say "the idempotency search failed; not creating an issue this time"
    return 1
  }
  local num body
  while IFS=$'\t' read -r num body; do
    [ -n "$num" ] || continue
    case "$body" in *"$stamp"*) found="$num"; break ;; esac
  done <<<"$hits"

  if [ -n "$found" ]; then
    say "issue $REPO#$found already carries $stamp; not filing a second"
  else
    local body_text
    body_text="$(issue_body "$brake" "$n" "$recorded" "$stamp")"
    local args=(--repo "$REPO" --title "PAUSED $stamp: $brake at $n"
                --body "$body_text")
    [ "$label_ok" = 1 ] && args+=(--label "$LABEL")
    local url
    url="$(gh_ issue create "${args[@]}")" || {
      # NOTHING IS BOUND ON A FAILED CREATE. A binding to an issue that does not
      # exist would turn every later `check` into an API error against a phantom
      # -- a pause that can never be released through the documented path.
      say "could not create the issue; PAUSE is left unbound and the loop"
      say "  stays paused. Retried on the next tick."
      return 1
    }
    found="${url##*/}"
    case "$found" in
      ''|*[!0-9]*) say "gh returned no issue number ('$url'); binding nothing"
                   return 1 ;;
    esac
    say "filed $REPO#$found"
  fi

  # BIND THE AUTHORIZATION TO ONE ISSUE: repo, number and stamp all have to
  # agree in `check`, so a bare mutable number is not by itself a release.
  #
  # REWRITTEN, NOT APPENDED. `check` retries `open` on every tick until it
  # lands, so an append-only rewrite would leave several `issue:` lines and hide
  # the live number behind whichever one `head -1` found first.
  # THE NARROW LOSS, STATED: if `grep -v` cannot READ the pause file (it was
  # readable moments ago -- `pause_stamp` parsed it), the rewrite still succeeds
  # and produces a PAUSE carrying only `issue:` and `stamp:`. The BRAKE and the
  # AUTHORIZATION both survive, which is what has to; the human-readable
  # `auto-paused`/`see` reason lines are what would be lost, and the reason is
  # also in the issue this function just filed. A test asserts they survive the
  # normal path.
  local tmp="$pause.new.$$"
  {
    grep -v -e '^issue: ' -e '^stamp: ' "$pause" 2>/dev/null
    printf 'issue: %s#%s\n' "$REPO" "$found"
    printf 'stamp: %s\n' "$stamp"
  } >"$tmp" 2>/dev/null || { rm -f "$tmp"; say "cannot rewrite $pause"; return 1; }
  mv -f "$tmp" "$pause" 2>/dev/null || {
    rm -f "$tmp"; say "cannot replace $pause"; return 1; }
  return 0
}

issue_body() {  # issue_body <brake> <n> <escalation path> <stamp>
  local brake="$1" n="$2" recorded="$3" stamp="$4"
  # THE ALLOWLIST IS QUOTED INLINE, AND THE HEADLINE SAYS *AUTHORISED*.
  #
  # THE FAILURE THIS FIXES: the headline read "will run nothing until this issue
  # is closed", which is false in the case that matters. A colleague reads it,
  # closes the issue, and now the issue is CLOSED AND THE BRAKE IS ON, with the
  # refusal visible only in a journal nobody watches -- strictly worse for
  # discoverability than no issue at all. A closer has to know before clicking,
  # so the logins are listed here and not merely referenced.
  local logins
  logins="$(sed -e 's/#.*//' -e 's/[[:space:]]//g' "$ALLOWLIST" 2>/dev/null \
            | grep -v '^$' | sed 's/^/@/' | paste -sd' ' -)"
  [ -n "$logins" ] || logins="(none readable -- no login can release this)"
  printf '%s\n' \
    "The research loop auto-paused at \`$stamp\` and will run nothing until an" \
    "**authorised** login closes this issue (see below -- closing it as anyone" \
    "else leaves the brake on)." \
    "" \
    "- brake: \`$brake\` reached $n" \
    "- escalation: \`${recorded:-(none recorded)}\`" \
    "" \
    "## To release it" \
    "" \
    "Authorised logins right now: $logins" \
    "" \
    "**Comment first if you want to steer the next tick**, then **close this" \
    "issue as one of those logins**. The next tick reads the *closing actor*" \
    "from the issue events and resumes only if it is on that list; a close by" \
    "anyone else is logged and the loop stays paused. The list lives in" \
    "\`tools/queue-forecasting/host/research-loop/pause-resume-allowlist.txt\`;" \
    "add a login there (and deploy it) if it should be able to release a pause." \
    "" \
    "Comments from authorised logins are handed to the leader as a directive --" \
    "labelled an instruction, not evidence, and never shown to the verifier, so" \
    "a figure typed here still cannot be cited." \
    "" \
    "Closing without commenting is a plain resume: both streak counters are" \
    "zeroed and the rejected-target memory is cleared." \
    "" \
    "This is visibility and convenience, not enforcement -- see design §4." \
    "" \
    "## The last three rejections, verbatim" \
    ""
  # THE REASONS ARE THE POINT OF THE ALARM. A human who has to clone a repo to
  # find out why the loop stopped is a human who finds out on Monday.
  local d=""
  [ -z "$recorded" ] || d="$(dirname "$recorded")"
  if [ -n "$d" ] && [ -d "$d" ]; then
    local f
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      printf '### %s\n\n```\n%s\n```\n\n' "$(basename "$f")" \
        "$(awk '
            /^## NOT RECORDED/         { seen = 1; next }
            seen && !inblock && /^```/ { inblock = 1; next }
            inblock && /^```/          { exit }
            inblock                    { print }
          ' "$f" 2>/dev/null | head -40)"
    done < <(find "$d" -maxdepth 1 -name '*.md' -type f 2>/dev/null \
             | LC_ALL=C sort | tail -3)
  else
    printf '%s\n' "(no escalation directory at \`$d\`.)"
  fi
}

# --------------------------------------------------------------------------
# check -- design §4.2. EVERY unhappy answer stays paused.
# --------------------------------------------------------------------------
cmd_check() {
  local pause="${1:-}"
  [ -n "$pause" ] || { say "check needs a pause file"; return 2; }
  [ -e "$pause" ] || { say "not paused"; return 0; }

  local bind stamp number
  bind="$(sed -n 's/^issue: //p' "$pause" 2>/dev/null | head -1)"
  stamp="$(sed -n 's/^stamp: //p' "$pause" 2>/dev/null | head -1)"
  if [ -z "$bind" ] || [ -z "$stamp" ]; then
    # COVERS A PAUSE WHOSE ISSUE CREATION FAILED, and every pause written before
    # §4 existed. Staying paused is not enough on its own -- the failure of
    # 2026-09-04 was the silence, so the retry is the whole point of this row.
    say "PAUSE carries no issue/stamp binding; staying paused and filing one"
    cmd_open "$pause" \
      "$(sed -n 's/^auto-paused [^:]*: \(.*\) at .*/\1/p' "$pause" 2>/dev/null | head -1)" \
      "$(sed -n 's/^auto-paused [^:]*: .* at \(.*\)/\1/p' "$pause" 2>/dev/null | head -1)" \
      "$(sed -n 's/^see //p' "$pause" 2>/dev/null | head -1)" \
      || say "the alarm could not be filed; the loop is still paused"
    return 1
  fi
  if [ "${bind%%#*}" != "$REPO" ]; then
    say "PAUSE names '${bind%%#*}' but QF_PAUSE_ISSUE_REPO is '$REPO':"
    say "  repo mismatch, staying paused. The pinned repo wins -- PAUSE is"
    say "  research-writable, so the repo it names is not evidence of anything."
    return 1
  fi
  number="${bind##*#}"
  case "$number" in
    ''|*[!0-9]*) say "PAUSE binds a non-numeric issue number '$number';"
                 say "  staying paused (it would be pasted into an API path)"
                 return 1 ;;
  esac
  stamp_ok "$stamp" || { say "PAUSE carries a malformed stamp; staying paused"
                         return 1; }

  local answer state body
  answer="$(gh_ api "repos/$REPO/issues/$number" \
              --jq '[.state, (.body // "" | gsub("[\r\n]"; " "))] | @tsv')" || {
    say "cannot read $bind; staying paused"; return 1; }
  IFS=$'\t' read -r state body <<<"$answer"
  case "${state:-}" in
    open)   say "$bind is open; staying paused"; return 1 ;;
    closed) ;;
    *)      say "$bind returned an unparseable state ('${state:-}');"
            say "  staying paused"; return 1 ;;
  esac

  # REPO, NUMBER *AND* STAMP HAVE TO AGREE. Without the stamp a bare mutable
  # number in a research-writable file would be an authorisation on its own:
  # rewrite `issue:` to point at any long-closed issue in the repo and the loop
  # releases itself. The stamp ties the release to THIS pause.
  case "$body" in
    *"$stamp"*) ;;
    *) say "$bind does not carry the stamp $stamp: this is not this pause's"
       say "  issue, staying paused"; return 1 ;;
  esac

  # THE ACTOR COMES FROM THE EVENTS API. `gh issue view --json` exposes
  # `closedAt` and `stateReason` but NO `closedBy`, so it cannot answer this
  # question at all.
  #
  # LATEST `closed` EVENT WINS: an issue can be closed, reopened and closed
  # again, and only the last close authorised anything.
  #
  # `awk`, NOT `tail -1`, AND ONE LINE PER EVENT: with `--paginate` and `--jq`,
  # `gh` applies the filter per PAGE, so a page-spanning `| last` would emit one
  # answer per page and the multi-line result would authorise nobody. Filtering
  # per element and taking the last non-empty line is correct across pages.
  local actor
  actor="$(gh_ api "repos/$REPO/issues/$number/events" --paginate \
             --jq '.[] | select(.event == "closed") | (.actor.login // "")' \
           | awk 'NF { last = $0 } END { if (last != "") print last }')" || {
    say "cannot read the events of $bind; staying paused"; return 1; }
  case "${actor:-}" in
    ''|null)
      say "$bind is closed but has no readable 'closed' event, so who closed"
      say "  it is unknown. That is an answer that does not parse: staying paused."
      return 1 ;;
  esac
  if ! allowed "$actor"; then
    say "$bind was closed by '$actor', who is not on $ALLOWLIST:"
    say "  staying paused. Add the login there if that was meant to release it."
    return 1
  fi

  # ORDERED, NOT ATOMIC (design §4.3). Four filesystem objects cannot be changed
  # together, and the invariant is NEVER UNPAUSED WITH STALE COUNTERS -- not
  # "all four or none". So: persist the directive, zero the three state files,
  # READ ALL THREE BACK, and remove PAUSE LAST and only then.
  #
  # A crash between the read-back and the `rm` leaves zeroed counters under a
  # live PAUSE, which is harmless -- the next check re-resumes. THE REVERSE
  # ORDER IS THE ONE THAT MUST NEVER HAPPEN: PAUSE gone with a streak still at
  # its threshold pauses again on the first rejection, which is the livelock.
  mkdir -p "$STATE" 2>/dev/null
  local dtmp="$DIRECTIVE.new.$$"
  # `gh`'s STDERR IS NOT SWALLOWED. Stdout is already captured by $dtmp, so
  # letting stderr through is free -- and this is the LAST step before a resume
  # and the single highest-value log line in the file. Discarding it left an
  # operator with "could not fetch the directive" and no way to tell an HTTP 403
  # from a rate limit from a revoked token. (It is also why every `say` here
  # goes to stderr: so a diagnostic can never land in $dtmp.)
  if ! directive_for "$number" >"$dtmp"; then
    rm -f "$dtmp"
    say "could not fetch or persist the human directive (reason above);"
    say "  staying paused. Nothing has been zeroed and PAUSE is untouched."
    return 1
  fi
  # REPLACED, NOT MERGED: a directive from a pause three weeks ago is not an
  # instruction about this tick, so a close with no comments must CLEAR the
  # previous one rather than inherit it.
  # MEASURED BEFORE THE MOVE, so the read-back after it has something to compare
  # against. An EMPTY directive is legitimate -- a close with no comments is a
  # plain resume -- so "non-empty" is not the property; "the bytes that were
  # produced are the bytes now on disk" is.
  local want_sz got_sz
  want_sz="$(wc -c <"$dtmp" 2>/dev/null)" || want_sz=""
  mv -f "$dtmp" "$DIRECTIVE" 2>/dev/null || {
    rm -f "$dtmp"; say "cannot install $DIRECTIVE; staying paused"; return 1; }
  # READ BACK, LIKE EVERY OTHER WRITE IN THIS SEQUENCE. This was the one write
  # in a read-back-verified ordering with no read-back: an existing DIRECTORY at
  # that path makes `mv` succeed by moving the file INSIDE it, and the loop then
  # resumed with a silently absent instruction -- in the case where a human had
  # taken the trouble to write one.
  got_sz="$(wc -c <"$DIRECTIVE" 2>/dev/null)" || got_sz=""
  if [ ! -f "$DIRECTIVE" ] || [ -z "$want_sz" ] || [ "$got_sz" != "$want_sz" ]; then
    say "$DIRECTIVE did not persist (wanted $want_sz bytes, read back"
    say "  '${got_sz:-unreadable}'); staying paused. Is it a directory or a"
    say "  symlink? Nothing has been zeroed."
    return 1
  fi

  # ORDERED WRITES AND THE READ-BACK SHARE ONE TABLE, so the pairing is visible
  # rather than re-derived, and a failure NAMES THE FILE. The old message,
  # `state did not persist (got '0'/'0'/'')`, could not tell an operator which
  # of three files failed or what to do about it.
  #
  # THE NAMES COME FROM state-names.sh, shared with `tick.sh`. If they did not,
  # a rename on either side would make this read-back SELF-CONFIRMING: it would
  # verify the value it had just written to a path nobody else reads, remove
  # PAUSE, and leave the real counter at its threshold.
  local pairs=("$QF_STATE_DISAGREE=0" "$QF_STATE_VERIFIER=0"
               "$QF_STATE_TARGET=none")
  local pair name want got
  for pair in "${pairs[@]}"; do
    name="${pair%%=*}"; want="${pair#*=}"
    printf '%s\n' "$want" >"$STATE/$name" 2>/dev/null || {
      say "cannot write $STATE/$name; staying paused with the counters it had."
      say "  Check the permissions and free space on $STATE."
      return 1; }
  done
  for pair in "${pairs[@]}"; do
    name="${pair%%=*}"; want="${pair#*=}"
    got="$(cat "$STATE/$name" 2>/dev/null)"
    # A WRITE THAT SUCCEEDED IS NOT A WRITE THAT PERSISTED -- a `>` onto a
    # symlink to /dev/null returns 0 and stores nothing. The read-back is what
    # makes the invariant true, so PAUSE stays.
    if [ "$got" != "$want" ]; then
      say "$STATE/$name did not persist: wanted '$want', read back '$got'."
      say "  Staying paused -- removing PAUSE now would resume the loop with"
      say "  that streak still at its threshold, which re-pauses on the first"
      say "  rejection. Check whether it is a symlink, a directory, or on a"
      say "  full or read-only filesystem, then close the issue again."
      return 1
    fi
  done

  rm -f "$pause" 2>/dev/null
  if [ -e "$pause" ]; then
    say "cannot remove $pause; the loop stays paused with zeroed counters"
    return 1
  fi
  say "RESUMED: $bind closed by $actor (stamp $stamp); counters zeroed"
  return 0
}

# --------------------------------------------------------------------------
# The human directive: stdout is the file, so nothing here may log to stdout.
# --------------------------------------------------------------------------
directive_for() {  # directive_for <issue number>
  local number="$1"
  # ONLY ALLOWLISTED AUTHORS ARE PROMOTED. The research token can author
  # comments on this issue too, so an unfiltered fetch would let the loop write
  # its own instructions and hand them to itself as a human directive -- the
  # gate answering its own question.
  local json
  json="$(gh_ api "repos/$REPO/issues/$number/comments" --paginate \
            --jq '.[] | [(.user.login // ""), (.created_at // ""), (.body // "" | gsub("[\r\n]"; "  "))] | @tsv')" \
    || return 1

  local promoted=0 skipped=0 out="" login created cbody
  while IFS=$'\t' read -r login created cbody; do
    [ -n "$login" ] || continue
    if allowed "$login" 2>/dev/null; then
      out="$out### $login at $created

$cbody

"
      promoted=$((promoted + 1))
    else
      skipped=$((skipped + 1))
    fi
  done <<<"$json"

  # NOTHING TO SAY IS SAID BY SAYING NOTHING. An empty file, not a heading: a
  # heading with no content under it still reads to a leader as an instruction
  # it failed to understand.
  if [ "$promoted" = 0 ] && [ "$skipped" = 0 ]; then
    return 0
  fi

  # THE RULES COME BEFORE THE PROSE THEY GOVERN, AND TRUNCATION CAN ONLY EAT THE
  # PROSE.
  #
  # THE FAILURE THIS FIXES: the citation ban used to TRAIL the comments. The
  # comment text was capped at MAX_FEEDBACK_BYTES and ~360 bytes of fixed
  # boilerplate was appended after it, so the file overshot the cap and
  # `tick.sh`'s second `head -c` cut the tail -- taking the entire "no figure
  # here may be cited, re-obtain it from the frontier" rule with it, exactly in
  # the case where the MOST human prose had reached the leader. Ordering fixes
  # that for good; budgeting the cap against the boilerplate's length would only
  # work until somebody edited the boilerplate.
  #
  # It is also the right shape on its own terms: an instruction about how to
  # treat untrusted content belongs ABOVE the untrusted content, so a leader
  # reading top-down knows the rules before it reads the prose they apply to.
  printf '%s\n' \
    "## A human released the pause (INSTRUCTION, NOT EVIDENCE)" \
    "" \
    "Read the block below as an instruction about THIS tick and nothing else." \
    "No figure appearing in it may be cited: to use one, obtain it again from" \
    "the frontier JSON, from the tick facts, or from a command you paste into" \
    "\`Evidence:\`. The copilot has NOT been shown this block and will judge" \
    "your entry on its own." \
    ""
  # THE NON-PROMOTED COUNT IS METADATA, NOT PROSE, so it goes above the cut too:
  # "some comments were withheld" must not be the thing truncation removes.
  [ "$skipped" = 0 ] || printf '%s\n' \
    "($skipped comment(s) from non-allowlisted authors were NOT promoted." \
    "They are on the issue; they are not instructions.)" ""

  if [ "$promoted" = 0 ]; then
    printf '%s\n' "(The pause was released with no comment from an allowlisted" \
      "author.)"
    return 0
  fi

  # THE ONLY CAP IN THE PIPELINE (see `tick.sh`, which now `cat`s this file
  # rather than re-cutting it). Applied HERE because only this layer knows which
  # bytes are the untrusted comment text and which are the rules about it; a
  # blunt `head -c` downstream cannot tell them apart, which is how the defect
  # above arose from two layers truncating to the same limit.
  local truncated=0
  [ "$(printf '%s' "$out" | wc -c)" -le "$MAX_FEEDBACK_BYTES" ] || truncated=1
  printf '%s' "$out" | head -c "$MAX_FEEDBACK_BYTES"
  # A NEWLINE OF OUR OWN: `head -c` cuts mid-line.
  echo
  # REACHABLE BY CONSTRUCTION: the notice is appended AFTER the cut, by the same
  # layer that made it, so it cannot itself be truncated away.
  [ "$truncated" = 0 ] || printf '%s\n' \
    "" "**[TRUNCATED at $MAX_FEEDBACK_BYTES bytes. The whole thread is on the" \
    "issue. The rules above still apply to every word of it.]**"
  return 0
}

case "${1:-}" in
  open)  shift; cmd_open "$@" ;;
  check) shift; cmd_check "$@" ;;
  *) say "usage: pause-issue.sh open <pause> <brake> <n> <escalation>"
     say "       pause-issue.sh check <pause>"
     exit 2 ;;
esac
