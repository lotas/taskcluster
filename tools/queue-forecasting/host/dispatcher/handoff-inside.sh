#!/bin/sh
# TRUSTED artifact normalisation, run INSIDE a second container (design D9).
# Mounted read-only from the trusted checkout; never read from the research
# worktree (NC10).
#
# Why a second container at all: uid 10001 cannot write into a 0750
# qfd:qfclient directory, and anything it created there would be owned by
# 10001 -- the original problem one level down. So qfd PRE-CREATES each
# allowlisted destination in artifacts/ as qfd:qfclient mode 0660, and this
# container runs as 10001:10001 WITH --group-add <qfclient gid> and copies
# CONTENT into files that already exist.
#
# The same uid as the candidate is the point: a hostile candidate can leave
# predictions.parquet mode 0600, which qfd -- owner of the directory but not the
# file -- could neither read nor chmod. This container shares the candidate's
# uid, so it can always read what the candidate wrote.
#
# Exit codes are a contract; the runner maps them to error_class:
#   0  every requested artifact copied
#   2  a source is not a regular file        -> handoff_bad_type
#   3  a requested artifact is missing       -> handoff_missing_artifact
#   4  a source exceeds the per-file cap     -> handoff_oversize
#   5  a destination was not pre-created     -> handoff_missing_artifact
set -eu

SRC_DIR=/out
DST_DIR=/artifacts
CAP_BYTES="${QF_ARTIFACT_CAP_BYTES:-2147483648}"

# The allowlist arrives as arguments, from trusted code. It is never read from
# the output directory: a candidate that could name its own artifacts could name
# /etc/shadow.
if [ "$#" -eq 0 ]; then
  echo "handoff: no artifacts requested" >&2
  exit 3
fi

status=0
for name in "$@"; do
  src="$SRC_DIR/$name"
  dst="$DST_DIR/$name"

  if [ ! -e "$dst" ]; then
    # qfd pre-creates every destination. A missing one means trusted code and
    # this script disagree about the allowlist, which is not something to
    # improvise around.
    echo "handoff: destination not pre-created: $dst" >&2
    exit 5
  fi

  if [ ! -e "$src" ]; then
    echo "handoff: missing artifact: $src" >&2
    status=3
    continue
  fi

  # -h before -f: a symlink would read OUTSIDE the mount, and a FIFO would
  # block the copy forever -- which is also why this container carries its own
  # HANDOFF_TIMEOUT_S. `test -f` follows symlinks, so it alone would accept one.
  if [ -h "$src" ]; then
    echo "handoff: refusing symlink: $src" >&2
    exit 2
  fi
  if [ ! -f "$src" ]; then
    echo "handoff: refusing non-regular file: $src" >&2
    exit 2
  fi

  size=$(wc -c < "$src")
  if [ "$size" -gt "$CAP_BYTES" ]; then
    echo "handoff: $name is $size bytes, over the cap of $CAP_BYTES" >&2
    exit 4
  fi

  # Copy CONTENT into the pre-created file. `cat >` truncates and writes in
  # place; `cp` would try to replace the inode and lose qfd's ownership, which
  # is the whole reason this dance exists.
  if ! cat "$src" > "$dst"; then
    echo "handoff: could not write $dst" >&2
    exit 5
  fi
  echo "handoff: copied $name ($size bytes)"
done

exit "$status"
