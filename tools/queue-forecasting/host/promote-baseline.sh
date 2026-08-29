#!/usr/bin/env bash
# Promote a baseline set into the immutable store. Phase 2b-3 Task 13.
#
#   sudo ./promote-baseline.sh <source dir> [--exclude YYYY-MM-DD,...]
#
# THE SOURCE IS A STAGED DIRECTORY, not the pipeline's own cache. The store is
# closed-world (`baseline.describe`): the aggregate NDJSON and `<YYYY-MM-DD>.json`
# and NOTHING else. The trainer's cache legitimately holds more than that -- a
# `baseline_predictions.ndjson.meta.json` coverage sidecar, and per-day files
# from every earlier cohort of the same policy -- so promoting it directly is
# refused, and would be wrong if it were not: those other days would be recorded
# as part of THIS baseline's identity while the declared `exclude_dates` describe
# only the latest regeneration.
#
# e.g., after `run_training.sh` printed its holdout days:
#   S="$(mktemp -d)"
#   cp trainer/data/baseline_filtered/baseline_predictions.ndjson "$S"/
#   for d in 2026-08-22 2026-08-23 2026-08-24 2026-08-25 2026-08-26; do
#     cp "trainer/data/baseline_filtered/$d.json" "$S"/
#   done
#   sudo ./promote-baseline.sh "$S" --exclude 2026-07-04,2026-07-05
#
# WHY A SEPARATE, PRIVILEGED STEP.
#
# The baseline is produced by the Node predictor in the DEPLOYMENT domain, which
# legitimately holds both docker and the database credential -- that is what the
# nightly is. Promotion reuses that domain rather than inventing a fourth
# root-equivalent one.
#
# But the STORE sits outside that domain's write access, and that is the part
# that makes immutability more than a convention: if the deploy user owned
# /var/lib/qf-baselines, the domain that produces baselines could also rewrite
# published ones, and "immutable" would rest on nobody choosing to.
#
# So this is a MEDIATED write. It runs as root and will only ever write the shape
# `baseline.py` validates. **Being able to publish through a validating step is
# not the same as being able to write to the directory.**
set -Eeuo pipefail
trap 'echo "FATAL: ${BASH_SOURCE[0]}:${LINENO} failed (exit $?)" >&2; exit 1' ERR

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STORE="${QF_BASELINE_STORE:-/var/lib/qf-baselines}"
# The owner the store MUST have. A parameter rather than a literal `root` so the
# test suite can exercise the whole path without being root -- production leaves
# it at the default, and the check is what enforces the boundary either way.
STORE_OWNER="${QF_BASELINE_STORE_OWNER:-root}"
DRY_RUN="${DRY_RUN:-0}"

info() { echo "  $*"; }
warn() { echo "  warn  $*" >&2; }
ok()   { echo "  ok    $*"; }
die()  { echo "FATAL: $*" >&2; exit 2; }

SRC=""
EXCLUDE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --exclude) EXCLUDE="${2:-}"; shift 2 ;;
    --exclude=*) EXCLUDE="${1#*=}"; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    -*) die "unknown flag $1" ;;
    *) [ -z "$SRC" ] || die "only one source directory"; SRC="$1"; shift ;;
  esac
done
[ -n "$SRC" ] || die "usage: $0 <source dir> [--exclude YYYY-MM-DD,...]"
[ -d "$SRC" ] || die "$SRC is not a directory"

# --- the store must not be writable by the domain that produces baselines ---
if [ -d "$STORE" ]; then
  owner="$(stat -c %U "$STORE")"
  mode="$(stat -c %a "$STORE")"
  [ "$owner" = "$STORE_OWNER" ] \
    || die "$STORE is owned by '$owner', not '$STORE_OWNER'. The store must sit
  outside the deployment domain's write access, or a published baseline can be
  rewritten by the same domain that produced it and 'immutable' means nobody has
  chosen to."
  # A BITWISE test, not a glob. The first version was `case "$mode" in *[2367])`,
  # which matches only the LAST digit -- so it caught other-writable and missed
  # group-writable entirely, and `0775` sailed through. Same shape as a socket
  # mode test earlier in this phase: a pattern that looks like it checks a
  # property and checks a different one.
  if (( (8#$mode) & 8#022 )); then
    die "$STORE is mode $mode: group- or other-writable, so the boundary above
  is not in force -- the domain that produces baselines could rewrite published
  ones."
  fi
  ok "$STORE is $mode $owner"
else
  [ "$DRY_RUN" = 1 ] || mkdir -p "$STORE"
  [ "$DRY_RUN" = 1 ] || chmod 0755 "$STORE"
  info "created $STORE"
fi

# --- validate and identify, before anything is written ---------------------
# `baseline.py` is stdlib-only, so the system python runs it: no venv, no
# dependency on the extractor's environment.
MANIFEST_JSON="$(PYTHONPATH="$HERE/shared" python3 - "$SRC" "$EXCLUDE" <<'PY'
import json, sys
import baseline
source, raw_exclude = sys.argv[1], sys.argv[2]
exclude = [d for d in raw_exclude.split(",") if d]
try:
    manifest = baseline.describe(source, exclude_dates=exclude)
except baseline.BaselineError as e:
    sys.exit(f"REFUSED: {e}")
manifest["baseline_hash"] = baseline.baseline_hash(manifest)
print(json.dumps(manifest, sort_keys=True, indent=2))
PY
)" || {
  # THE LIKELIEST CAUSE, NAMED. The refusal above says which file is wrong; this
  # says why it is there and what to do, because the file that triggers it is one
  # the pipeline writes on purpose. Computed after the failure, so a working
  # promotion pays nothing for it.
  extra="$(find "$SRC" -maxdepth 1 -type f -printf '%f\n' 2>/dev/null \
    | grep -vE '^baseline_predictions\.ndjson$|^[0-9]{4}-[0-9]{2}-[0-9]{2}\.json$' \
    | head -5 | tr '\n' ' ')"
  if [ -n "$extra" ]; then
    warn "$SRC also holds: $extra"
    warn "The store is closed-world -- the aggregate NDJSON and <YYYY-MM-DD>.json"
    warn "and nothing else -- so stage exactly the window you are promoting:"
    warn "  S=\$(mktemp -d)"
    warn "  cp $SRC/baseline_predictions.ndjson \$S/"
    warn "  cp $SRC/<each holdout day>.json \$S/"
    warn "  sudo $0 \$S [--exclude ...]"
  fi
  die "the source directory is not a promotable baseline set (see above)"
}

HASH="$(printf '%s' "$MANIFEST_JSON" | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["baseline_hash"])')"
DAYS="$(printf '%s' "$MANIFEST_JSON" | python3 -c \
  'import json,sys; print(len(json.load(sys.stdin)["days"]))')"
ROWS="$(printf '%s' "$MANIFEST_JSON" | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["ndjson_rows"])')"
ok "validated: $DAYS per-day files, $ROWS NDJSON rows, hash ${HASH:0:12}"

TARGET="$STORE/$HASH"

# --- FIRST PUBLICATION WINS -----------------------------------------------
if [ -d "$TARGET" ]; then
  # A no-op, not a rewrite. The hash is a CONTENT key, so an existing directory
  # under it holds these same bytes -- there is nothing to update and rewriting
  # would only create a window in which a mounted baseline was incomplete.
  ok "already published as ${HASH:0:12}; nothing to do"
  info "$TARGET"
  exit 0
fi

if [ "$DRY_RUN" = 1 ]; then
  info "WOULD publish ${HASH:0:12} to $TARGET"
  exit 0
fi

STAGING="$STORE/.staging.$HASH.$$"
trap 'rm -rf "$STAGING"' EXIT
mkdir -p "$STAGING"
# Only the files the manifest names. Copying the directory wholesale would
# publish whatever else was in it, and the validation above exists precisely to
# decide what belongs.
printf '%s' "$MANIFEST_JSON" | python3 -c 'import json,sys
for name in sorted(json.load(sys.stdin)["files"]):
    print(name)' | while IFS= read -r name; do
  cp -p "$SRC/$name" "$STAGING/$name"
done
printf '%s\n' "$MANIFEST_JSON" > "$STAGING/MANIFEST.json"
# OUTSIDE the manifest, deliberately. The manifest IS the identity, so a
# promotion time inside it would make every promotion of the same bytes a
# different artifact -- exactly what a content key exists to prevent. It is still
# worth recording: "which of these is newest" is the first question anyone asks a
# listing, and a directory mtime answers it with something that survives a
# filesystem copy as a confident wrong answer.
date -u +%Y-%m-%dT%H:%M:%SZ > "$STAGING/PROMOTED_AT"
# Readable by the sandbox uid, which is a different user entirely -- the same
# reasoning as the extract store's 0755.
chmod 0755 "$STAGING"
chmod 0644 "$STAGING"/*
chown -R "$STORE_OWNER" "$STAGING" 2>/dev/null || true

# ONE atomic act, the same discipline as D20: the artifact and its
# discoverability are the same rename, so there is no window in which a mounted
# baseline is missing files.
mv -T "$STAGING" "$TARGET"
trap - EXIT
ok "published ${HASH:0:12}"
info "$TARGET"
info ""
info "Use it in a probe:"
info "    qf probe --sha <sha> --path research/experiments/<script>.py \\"
info "        --extract <hash> --baseline ${HASH:0:12}"
