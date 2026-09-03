#!/usr/bin/env bash
# Prune run directories. Invoked by qf-runs-prune.service.
#
# WHY THIS REPLACED A ONE-LINE `find -mtime +90`. Age-only retention assumes a
# steady rate, and the rate is not steady: one day of fault-gate and NC-suite
# work put 5.6 GiB into /var/lib/qf-runs, on a filesystem where the dispatcher's
# own admission floor reserves the last 20 GiB and a co-tenant Postgres can
# transiently take 40 GiB. Ninety days of that is not a retention policy, it is
# an outage with a date on it.
#
# So: two tiers by age, and a SIZE CAP that works oldest-first when age alone has
# not kept up.
#
# THE OBJECTION THE OLD UNIT RAISED, AND THE ANSWER. Its comment said to prune
# whole directories and never parts of one, because "a half-pruned run reads as a
# run whose artifacts vanished". That is a real hazard and the fix is not to
# avoid partial pruning -- artifacts are kilobytes and logs are megabytes, so
# all-or-nothing throws away the cheap scientific record to save nothing. The fix
# is to make the partial state SELF-DESCRIBING: every tier-1 prune leaves a
# PRUNED file saying what was removed and when, so the state reads as "pruned",
# not as "lost".
#
# NO DB LOOKUP, DELIBERATELY. A live run must never be pruned, and the guarantee
# is arithmetic rather than a query: QFD_JOB_HOLD_DEADLINE_S is ~2.7h, so nothing
# older than MIN_AGE_HOURS=6 can still be running. That avoids opening the
# dispatcher's SQLite database from a second process under a hardened unit, where
# a read-only open of a WAL database still wants to create the -shm file.
set -euo pipefail

RUNS_DIR="${QFD_RUNS_DIR:-/var/lib/qf-runs}"
MIN_AGE_HOURS="${QF_PRUNE_MIN_AGE_HOURS:-6}"
LOG_RETENTION_DAYS="${QF_PRUNE_LOG_DAYS:-14}"
RUN_RETENTION_DAYS="${QF_PRUNE_RUN_DAYS:-90}"
MAX_TOTAL_MB="${QF_PRUNE_MAX_TOTAL_MB:-8192}"
DRY_RUN="${QF_PRUNE_DRY_RUN:-0}"

[ -d "$RUNS_DIR" ] || { echo "prune: $RUNS_DIR does not exist"; exit 0; }

say() { echo "prune: $*"; }
run() { if [ "$DRY_RUN" = 1 ]; then say "WOULD $*"; else "$@"; fi; }
# So a dry run never claims to have done something. The old wording printed
# "WOULD rm -rf ..." and then "removed ..." for the same directory, which is
# exactly the kind of output someone skims and believes.
verb() { if [ "$DRY_RUN" = 1 ]; then echo "would remove"; else echo "removed"; fi; }

total_mb() { du -sm "$RUNS_DIR" 2>/dev/null | cut -f1; }

# AGE FROM THE RUN ID, not from the directory's mtime.
#
# mtime is wrong here in a way that silently defeats the size cap: removing
# `out/` and `logs/` MODIFIES the directory, so a tier-1 prune resets its mtime
# to now and the entry becomes "too young to touch" for ever. The oldest runs
# then became permanently unprunable at exactly the moment the cap needed them.
#
# The run id already carries an authoritative UTC timestamp
# (`<kind>-<YYYYmmddTHHMMSSZ>-<sha>-<seq>`, see `make_run_id`), which no
# filesystem operation can move. Anything unparseable falls back to mtime rather
# than being skipped: an unknown age must not mean "immortal".
run_epoch() {  # run_epoch <dir> -> unix seconds
  local name ts d t
  name="$(basename "$1")"
  ts="$(printf '%s' "$name" | cut -d- -f2)"
  if [[ "$ts" =~ ^([0-9]{8})T([0-9]{6})Z$ ]]; then
    d="${BASH_REMATCH[1]}"; t="${BASH_REMATCH[2]}"
    if date -u -d "${d:0:4}-${d:4:2}-${d:6:2} ${t:0:2}:${t:2:2}:${t:4:2}" \
         +%s 2>/dev/null; then
      return 0
    fi
  fi
  stat -c %Y "$1" 2>/dev/null || echo 0
}

# Oldest first.
list_by_age() {
  local dir
  for dir in "$RUNS_DIR"/*; do
    [ -d "$dir" ] || continue
    printf '%s\t%s\n' "$(run_epoch "$dir")" "$dir"
  done | sort -n | cut -f2
}

older_than_hours() {  # older_than_hours <dir> <hours>
  local epoch now
  epoch="$(run_epoch "$1")"
  now="$(date -u +%s)"
  [ "$(( now - epoch ))" -gt "$(( $2 * 3600 ))" ]
}

# Tier 1: the bulk. `out/` is a STAGING directory whose allowlisted contents the
# handoff already copied into artifacts/, so for a terminal run it is duplication;
# `logs/` is capped at QFD_LOG_CAP_MB per stream and is only useful while someone
# is still triaging. `artifacts/` and the directory itself stay.
prune_tier1() {  # prune_tier1 <dir> <why>
  local dir="$1" why="$2" freed
  [ -d "$dir/out" ] || [ -d "$dir/logs" ] || return 1
  freed="$(du -sm "$dir" 2>/dev/null | cut -f1)"
  run rm -rf "$dir/out" "$dir/logs" "$dir/src"
  if [ "$DRY_RUN" != 1 ]; then
    printf 'pruned_at=%s\nremoved=out,logs,src\nreason=%s\nsize_before_mb=%s\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$why" "${freed:-unknown}" \
      > "$dir/PRUNED"
  fi
  say "tier1 $why: $(verb) out/logs/src from $(basename "$dir") (was ${freed:-?}MiB; artifacts kept)"
  return 0
}

removed_dirs=0 tier1=0

# --- age tier 2: whole directories ----------------------------------------
while IFS= read -r dir; do
  [ -n "$dir" ] || continue
  if older_than_hours "$dir" $(( RUN_RETENTION_DAYS * 24 )); then
    run rm -rf "$dir"
    removed_dirs=$((removed_dirs + 1))
    say "tier2 age: $(verb) $(basename "$dir")"
  fi
done < <(list_by_age)

# --- age tier 1 -----------------------------------------------------------
while IFS= read -r dir; do
  [ -n "$dir" ] || continue
  [ -e "$dir/PRUNED" ] && continue
  if older_than_hours "$dir" $(( LOG_RETENTION_DAYS * 24 )); then
    prune_tier1 "$dir" "age>${LOG_RETENTION_DAYS}d" && tier1=$((tier1 + 1))
  fi
done < <(list_by_age)

# --- size cap, oldest first ----------------------------------------------
#
# TWO PASSES, not one, and that is the whole subtlety. Each directory offers two
# escalating actions -- drop out/logs/src, then drop the whole thing -- and a
# single sweep over the list can only take the first from each. A run that has
# just been tier-1'd is still the oldest thing there, so if the cap is still
# breached it has to be revisited, not skipped until tomorrow. The first version
# did exactly one pass and reported STILL OVER THE CAP without ever trying the
# escalation it had available.
#
# `acted` bounds it: at most one tier-1 and one tier-2 per directory per run, so
# this terminates even under --dry-run, where no action changes what the next
# pass would see.
declare -A acted=()
now_mb="$(total_mb)"
if [ -n "$now_mb" ] && [ "$now_mb" -gt "$MAX_TOTAL_MB" ]; then
  say "over the cap: ${now_mb}MiB > ${MAX_TOTAL_MB}MiB; pruning oldest first"
  progress=1
  while [ "$progress" = 1 ]; do
    progress=0
    while IFS= read -r dir; do
      [ -n "$dir" ] || continue
      now_mb="$(total_mb)"
      [ "$now_mb" -le "$MAX_TOTAL_MB" ] && break
      # The floor that keeps a LIVE run safe. See the header: the hold deadline
      # is ~2.2h, so six hours is proof rather than a guess.
      older_than_hours "$dir" "$MIN_AGE_HOURS" || continue
      if [ ! -e "$dir/PRUNED" ] && [ -z "${acted[$dir]:-}" ]; then
        if prune_tier1 "$dir" "size cap"; then
          acted[$dir]=tier1; tier1=$((tier1 + 1)); progress=1
          continue
        fi
      fi
      [ "${acted[$dir]:-}" = tier2 ] && continue
      # Already tier-1'd and still over: the artifacts go too, oldest first.
      run rm -rf "$dir"
      acted[$dir]=tier2
      removed_dirs=$((removed_dirs + 1))
      say "tier2 size cap: $(verb) $(basename "$dir")"
      progress=1
    done < <(list_by_age)
  done
fi

final_mb="$(total_mb)"
if [ "$DRY_RUN" = 1 ]; then
  say "done (DRY RUN, nothing changed): ${tier1} tier1 and ${removed_dirs} directory removals were planned; still ${final_mb:-?}MiB"
else
  say "done: ${tier1} tier1, ${removed_dirs} directories removed, now ${final_mb:-?}MiB"
fi

# NO SILENT CAPS. If the policy could not get under its own budget, that is the
# single most important line of this run's output: it means everything prunable
# is either too young to touch or already pruned, and the next thing to give way
# is the dispatcher's admission floor.
if [ -n "$final_mb" ] && [ "$final_mb" -gt "$MAX_TOTAL_MB" ]; then
  echo "prune: STILL OVER THE CAP: ${final_mb}MiB > ${MAX_TOTAL_MB}MiB after" \
       "pruning everything older than ${MIN_AGE_HOURS}h. Raise" \
       "QF_PRUNE_MAX_TOTAL_MB deliberately, or find what is generating runs" \
       "this fast -- do not leave this unread." >&2
  exit 1
fi
