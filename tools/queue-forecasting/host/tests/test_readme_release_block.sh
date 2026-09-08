#!/usr/bin/env bash
# The README's HAND-RELEASE block, asserted as code.
#
# WHY THIS FILE EXISTS. That block is the only mechanism in this change with no
# automated brake: deleting its read-back gate leaves every other suite green,
# and the failure it produces is `PAUSE` removed with the drift counter still at
# its threshold -- the 2026-09-04 livelock, reached by an operator following the
# documentation. Documentation an operator pastes into a root shell on a paused
# box is executable, so it is tested like code.
#
# IT EXECUTES NOTHING. The block writes three state files and removes `PAUSE`;
# running it here would either do that to a developer's home or need a fake one
# convincing enough to prove nothing. Every assertion below is text.
#
# THE PROSE IS DELIBERATELY NOT ASSERTED. A test that pins sentences fires on
# every edit and gets ignored, which is this document's own failure mode.
#
# ITS OWN FILE, not a section of test_unit_drift.sh: the subject is a DOCUMENT,
# the extraction anchors on a heading rather than on a shell function, and a
# separate line in the suite list means "the block moved" shows up as a named
# suite failing rather than as one line inside a forty-assertion run.
#
#   ./tests/test_readme_release_block.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
README="$HERE/../research-loop/README.md"
NAMES="$HERE/../research-loop/state-names.sh"
[ -f "$README" ] || { echo "cannot find $README" >&2; exit 2; }

pass=0; fail=0
ok()  { echo "ok    $1"; pass=$((pass + 1)); }
bad() { echo "FAIL  $1"; fail=$((fail + 1)); }

# THE THREE BASENAMES COME FROM `state-names.sh`, NOT FROM THIS FILE. This is
# the assertion that pays for the whole suite: the README's copies are the last
# reader of those names that the shared file does not reach, and a rename done
# by halves is the silent, self-confirming failure `state-names.sh` exists to
# prevent. Hardcoding them here would reproduce exactly that.
# shellcheck source=../research-loop/state-names.sh
[ -r "$NAMES" ] || { echo "cannot read $NAMES" >&2; exit 2; }
. "$NAMES"
for v in QF_STATE_DISAGREE QF_STATE_VERIFIER QF_STATE_TARGET; do
  [ -n "${!v:-}" ] || { echo "$NAMES does not define $v" >&2; exit 2; }
done

# --------------------------------------------------------------------------
# Extraction. A FAILURE, NEVER A SKIP: "the block moved or was renamed" is
# exactly the change this suite must not sleep through, and a skip that exits 0
# is how a check stops checking without anybody noticing.
# --------------------------------------------------------------------------
HEADING='## Releasing the pause the box is in now'
RM_MARK='rm -f ~/qf-research/PAUSE'

if grep -qxF "$HEADING" "$README"; then
  ok "the release section is still called '$HEADING'"
else
  bad "the release heading is gone or renamed, so nothing below is anchored"
  echo; echo "readme-release-block: pass=$pass fail=$fail"; exit 1
fi

# EXACTLY ONE such block in the whole file: a second copy elsewhere would be
# unchecked, and two release recipes that disagree is worse than one that is
# wrong, because the reader cannot tell which is current.
n_blocks="$(awk '
  /^```/ { infence = !infence; if (infence) { body = "" } else if (body ~ /rm -f ~\/qf-research\/PAUSE/) n++ ; next }
  infence { body = body "\n" $0 }
  END { print n + 0 }
' "$README")"
[ "$n_blocks" = 1 ] \
  && ok "exactly one fenced block removes PAUSE" \
  || bad "$n_blocks fenced blocks remove PAUSE: a second copy would go unchecked"

# The first fenced block AFTER the heading that contains the `rm`.
BLOCK="$(awk -v h="$HEADING" '
  $0 == h { seen = 1; next }
  !seen { next }
  /^```/ {
    infence = !infence
    if (!infence) {
      if (body ~ /rm -f ~\/qf-research\/PAUSE/) { printf "%s", body; exit }
      body = ""
    }
    next
  }
  infence { body = body $0 "\n" }
' "$README")"
if [ -n "$BLOCK" ]; then
  ok "the hand-release block was extracted from under its heading"
else
  bad "no fenced block containing '$RM_MARK' follows the heading: extraction
     failed, which is a FAILURE and not a skip -- the recipe may have been
     moved, renamed or quietly deleted"
  echo; echo "readme-release-block: pass=$pass fail=$fail"; exit 1
fi

has() { printf '%s' "$BLOCK" | grep -q -e "$1"; }

# --- 1. set -e, OR every command &&-chained ---------------------------------
# ONE OF THE TWO, so a legitimate rewrite into an &&-chain is not a false
# failure. What must not be true is NEITHER: without one of them the three
# writes can all fail and `rm -f` still runs, which is the whole livelock.
if has '^[[:space:]]*set -e'; then
  ok "the block fails fast (set -e)"
elif ! printf '%s' "$BLOCK" | grep -v '^[[:space:]]*#' | grep -v '^[[:space:]]*$' \
       | grep -qv '&&[[:space:]]*$\|^[[:space:]]*[A-Za-z_]*=\|^sudo\|^  *echo'; then
  ok "the block is fully &&-chained, which is the other acceptable shape"
else
  bad "the block has neither 'set -e' nor an unbroken &&-chain: every write can
     fail and the 'rm -f' still runs, which is the livelock this recipe exists
     to avoid"
fi

# --- 2. mkdir -p, on the VARIABLE ------------------------------------------
has 'mkdir -p' && ok "the state directory is created before it is written" \
               || bad "no 'mkdir -p': the three writes fail on a fresh box"
has 'mkdir -p "\$S"\|mkdir -p "\${S' \
  && ok "mkdir -p applies to the derived state directory, not a literal path" \
  || bad "mkdir -p is not applied to the state-directory variable"

# --- 3. the three basenames, taken from state-names.sh ---------------------
for v in QF_STATE_DISAGREE QF_STATE_VERIFIER QF_STATE_TARGET; do
  if has "${!v}"; then
    ok "the block names $v's value (${!v}) as state-names.sh defines it"
  else
    bad "the block does not mention ${!v} (from \$$v): a rename done by halves
     leaves this recipe zeroing a path nobody reads, and its own read-back
     still passes because it reads back what it just wrote"
  fi
done

# --- 4/5. a gate on ALL THREE, LEXICALLY BEFORE the rm ---------------------
# ORDERING ASSERTED AS ORDERING. Presence alone passes a block that removes
# `PAUSE` first and verifies afterwards -- the same failure, inverted -- and
# two-of-three verified is the same livelock at two thirds the odds.
rm_off="$(printf '%s' "$BLOCK" | grep -bo -e "$RM_MARK" | head -1 | cut -d: -f1)"
if [ -n "$rm_off" ]; then
  ok "the removal of PAUSE was located in the block"
else
  bad "the block no longer contains '$RM_MARK'"
  rm_off=0
fi
for v in QF_STATE_DISAGREE QF_STATE_VERIFIER QF_STATE_TARGET; do
  gate_off="$(printf '%s' "$BLOCK" \
    | grep -bo -e "test .*${!v}" -e "\[ .*${!v}" -e "&&.*${!v}" \
    | head -1 | cut -d: -f1)"
  if [ -z "$gate_off" ]; then
    bad "${!v} is written but never GATED: nothing verifies it before PAUSE is
     removed, so the loop resumes with the counter still at its threshold"
  elif [ "$gate_off" -lt "$rm_off" ]; then
    ok "${!v} is gated BEFORE the rm, which is the ordering that matters"
  else
    bad "${!v}'s gate appears AFTER the rm (offset $gate_off vs $rm_off): the
     verification cannot prevent the removal it comes after"
  fi
done

# --- 6. the state directory is derived, not hardcoded ----------------------
# Neither unit pins QF_TICK_STATE or XDG_STATE_HOME, so an XDG_STATE_HOME in
# ~research/.profile moves the real directory -- and a hardcoded path would
# write three decoys beside a live streak.
has 'XDG_STATE_HOME' \
  && ok "the state directory is derived through XDG_STATE_HOME" \
  || bad "the block hardcodes the state directory: an XDG_STATE_HOME in
     ~research/.profile moves the real one and this would write decoys"
if printf '%s' "$BLOCK" | grep -v '^[[:space:]]*#' | grep -q '\.local/state/qf-tick'; then
  bad "a literal .local/state/qf-tick appears outside a comment"
else
  ok "no literal .local/state/qf-tick outside a comment"
fi

# --- 7. every git commit/push in the README runs as `research` -------------
# CHEAP IN THE SAME PASS, and it guards the other defect class this document
# has already produced twice: a root-run `git` in the research workspace leaves
# root-owned objects the loop then cannot write.
bad_git=0
while IFS= read -r blk; do
  case "$blk" in
    *"git commit"*|*"git push"*) ;;
    *) continue ;;
  esac
  case "$blk" in
    *"sudo -H -u research"*) ;;
    *) bad_git=$((bad_git + 1))
       echo "      offending block: $(printf '%s' "$blk" | head -c 120)" >&2 ;;
  esac
done < <(awk '
  /^```/ { infence = !infence; if (!infence) { gsub(/\n/, " ", body); print body; body = "" } next }
  infence { body = body $0 "\n" }
' "$README")
[ "$bad_git" = 0 ] \
  && ok "every README block running git commit/push wraps it in sudo -H -u research" \
  || bad "$bad_git README block(s) run git commit/push outside sudo -H -u research"

echo
echo "readme-release-block: pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
