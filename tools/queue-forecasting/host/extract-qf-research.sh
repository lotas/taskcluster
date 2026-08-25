#!/usr/bin/env bash
# Extracts tools/queue-forecasting/trainer/ into a standalone history for
# qf-research, then verifies the result four ways.
# Spec: auto-research-phase1-design.md section 5.
#
#   ./extract-qf-research.sh            # clone from the remote and extract
#   SRC=/path/to/checkout ./extract...  # extract from a local checkout instead
#
# Run this on a machine with pypi access: check 4 runs `uv sync`.
#
# Every expectation below is DERIVED FROM THE SOURCE, not hardcoded. An earlier
# revision asserted "38 commits" and "68 files" as constants and broke the first
# time a commit touched trainer/ -- and a written-down number is a weaker claim
# than the property we actually want: that the rewrite preserved exactly the
# commits and blobs that touched the subtree, whatever their count.
set -uo pipefail

SRC=${SRC:-git@github.com:lotas/taskcluster.git}
SRC_BRANCH=${SRC_BRANCH:-feat/queue-forecasting}
WORK=${WORK:-/tmp/qf-extract}
SUBDIR=tools/queue-forecasting/trainer

die() { echo "extract: $*" >&2; exit 1; }
step() { echo; echo "== $*"; }
info() { echo "      $*"; }

command -v git-filter-repo >/dev/null 2>&1 || git filter-repo --version >/dev/null 2>&1 \
  || die "git-filter-repo is not installed (pipx install git-filter-repo)"
[ -e "$WORK" ] && die "$WORK already exists; remove it first (this script never overwrites)"

step "cloning $SRC ($SRC_BRANCH) into $WORK"
# A local path needs --no-local so the clone is a real copy, not hardlinks.
if [ -d "$SRC" ]; then
  git clone --quiet --no-local --single-branch --branch "$SRC_BRANCH" "$SRC" "$WORK" \
    || die "clone failed"
else
  git clone --quiet --single-branch --branch "$SRC_BRANCH" "$SRC" "$WORK" || die "clone failed"
fi

step "measuring the source"
# Taken from the CLONE, before any rewriting, so the comparisons below do not
# depend on what happens to be checked out anywhere else.
SRC_COMMITS=$(git -C "$WORK" rev-list --count "$SRC_BRANCH" -- "$SUBDIR")
[ "${SRC_COMMITS:-0}" -gt 0 ] || die "no commits touch $SUBDIR on $SRC_BRANCH - wrong path?"
git -C "$WORK" ls-files -s "$SUBDIR" \
  | awk '{sub("tools/queue-forecasting/","",$4); print $2, $4}' | sort > "$WORK/../qf-before.txt"
SRC_FILES=$(wc -l < "$WORK/../qf-before.txt")
[ "${SRC_FILES:-0}" -gt 0 ] || die "no tracked files under $SUBDIR - wrong path?"
info "source has $SRC_COMMITS commits touching $SUBDIR and $SRC_FILES tracked files"

step "rewriting history"
( cd "$WORK" && git filter-repo \
    --path "$SUBDIR/" \
    --path-rename "$SUBDIR/:trainer/" \
    --refs "$SRC_BRANCH" ) || die "filter-repo failed"

step "removing the source remote and its refs"
# --refs leaves `origin` and its remote-tracking refs in place, and those still
# point at UNREWRITTEN history: measured 194 MB of .git before this cleanup and
# 2.4 MB after. Skipping it ships the whole monorepo inside the new repo.
( cd "$WORK" \
  && (git remote remove origin 2>/dev/null || true) \
  && git for-each-ref --format='%(refname)' refs/remotes | xargs -r -n1 git update-ref -d \
  && git branch -m "$SRC_BRANCH" main \
  && git reflog expire --expire=now --all \
  && git gc --prune=now --quiet ) || die "cleanup failed"

refs=$(git -C "$WORK" for-each-ref --format='%(refname)')
[ "$refs" = "refs/heads/main" ] || die "expected only refs/heads/main, got: $refs"

step "check 1: the rewrite preserved every subtree commit"
n=$(git -C "$WORK" rev-list --count main)
[ "$n" -eq "$SRC_COMMITS" ] \
  || die "commit count changed: source had $SRC_COMMITS touching $SUBDIR, extract has $n"
info "ok    $n commits, matching the source"

step "check 2: every tracked blob is byte-identical (this is the fidelity check)"
git -C "$WORK" ls-files -s | awk '{print $2, $4}' | sort > "$WORK/../qf-after.txt"
if ! diff -u "$WORK/../qf-before.txt" "$WORK/../qf-after.txt"; then
  die "tracked object listings differ - the extraction is NOT faithful"
fi
info "ok    $SRC_FILES blobs identical, paths rooted at trainer/"

step "check 3: the production freeze notice did not come along"
# trainer/README.md describes the FROZEN PRODUCTION copy ("research happens
# elsewhere"). That text is actively wrong inside qf-research, so its presence
# means this was extracted from a commit that already had plan Task 5 applied.
if git -C "$WORK" ls-files --error-unmatch trainer/README.md >/dev/null 2>&1; then
  die "trainer/README.md is in the extract. It describes the production copy and
     is wrong inside qf-research. Either extract from a commit before plan
     Task 5, or 'git -C $WORK rm trainer/README.md' before pushing."
fi
info "ok    no production-only files carried over"

step "check 4: the test suite runs in the extracted tree"
out=$( cd "$WORK/trainer" && uv sync --locked >/dev/null 2>&1 \
       && uv run pytest -q 2>&1 | tail -1 )
printf '%s\n' "$out" | tee "$WORK/../qf-pytest.txt"
case "$out" in
  *failed*) die "tests FAILED in the extracted tree: $out" ;;
  *passed*) : ;;
  *)        die "could not read a pytest summary: $out" ;;
esac
# Exactly one skip is expected and load-bearing: the serving-parity guard needs
# src/repo-family.js from the service tree, which this repo does not contain.
# Zero skips would mean the guard silently vanished; more than one means
# something else stopped running and nobody noticed.
printf '%s\n' "$out" | grep -q '1 skipped' \
  || die "expected exactly '1 skipped' (the serving-parity guard); got: $out"
info "ok    no failures, exactly one expected skip"

echo
echo "extraction verified in $WORK -- push it with plan Task 8."
