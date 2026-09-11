#!/usr/bin/env bash
# Contract v2 cutover: plan Task 12, steps 4-7, as one resumable script.
#
#   sudo ./host/cutover-v2.sh            # runs every stage in order
#   sudo ./host/cutover-v2.sh export     # or one stage: export | promote | activate | verify | probe
#
# Run it FROM THE DEPLOY CHECKOUT (the one with .env and trainer/data), on the
# host, after `git pull` + `phase2-setup.sh mirror-refresh` have landed
# 44b24cd888 (the exporter must write bl_*_level / bl_*_sample_size).
#
# Stages, each idempotent and each re-runnable on its own:
#   export    one baseline set for BOTH cohorts (NDJSON FROM..TO with the level
#             columns, plus a per-day JSON for every holdout day of the two
#             cohorts) into a FRESH staging dir under trainer/data, under the
#             host's heavy-work mutex so it cannot overlap a probe or the
#             nightly. Every file is written to a .partial name and renamed only
#             once complete and parseable; a provenance record (inputs + sha256
#             of every file) is published last and atomically; a rerun reuses
#             the set only if the inputs match AND every file still hashes.
#   promote   promote-baseline.sh on that set with the exclusions the provenance
#             records -> NEW_HASH (first publication wins; rerun is a no-op).
#   activate  instantiate-contract.sh wait_time.v2 against NEW_HASH --activate,
#             then COMMIT wait_time.v2.json + ACTIVE and nothing else. An
#             existing v2 file is accepted only if it is exactly what the
#             template instantiates to today; ACTIVE may differ from HEAD only
#             by the wait_time line.
#   verify    push, mirror-refresh, then `experiment.py plan` must show the v2
#             contract ACTIVE and pinned to NEW_HASH.
#   probe     requires the loop PAUSED and takes its tick.lock; `experiment.py
#             sync` (the research workspace does NOT follow the mirror by
#             itself); then the two reference runs, one per cohort, pinned with
#             --contract to v2. Each blocks up to ~2h45m. State per cohort is
#             recorded with what it certifies; a probe whose evaluation failed
#             is resumed with `qf evaluate`, never re-trained. Prints the four
#             candidate commands (qctx, qctx_d per cohort, --vs <reference>).
#
# Deadline: retention (60d) drops the 2026-07-25 rows around 2026-09-23, after
# which the export can no longer cover cohort B's training window.
set -Eeuo pipefail
export PYTHONDONTWRITEBYTECODE=1            # root imports of host/shared must not leave root-owned __pycache__

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QF_DIR="$(cd "$HERE/.." && pwd)"           # tools/queue-forecasting in the deploy checkout
cd "$QF_DIR"

# --- knobs (override by env) -----------------------------------------------
FROM_DATE="${FROM_DATE:-2026-07-25}"       # inclusive
TO_DATE="${TO_DATE:-2026-09-01}"           # EXCLUSIVE, as predictor.js --to is
# Cohort A: request_hash prefix of the loop's canonical Series A extract
# (AGENTS.md "The canonical inputs"; as_of 2026-08-27). Three extracts share
# that as_of, so a date does not name it. An as_of prefix is still accepted
# when it is unambiguous.
COHORT_A="${COHORT_A:-bd29b39ab625}"
# Cohort B: request_hash prefix of the second, non-overlapping extract.
COHORT_B="${COHORT_B:-975aea71d759}"
REFERENCE_CONFIG="${REFERENCE_CONFIG:-configs/wait_time_residual_throughput_filtered_baseline.yaml}"
CANDIDATES="${CANDIDATES:-configs/wait_time_residual_throughput_filtered_baseline_qctx.yaml configs/wait_qctx_d_priority_flow.yaml}"
TEMPLATE="${TEMPLATE:-host/contracts/wait_time.v2.json.in}"
STAGE_REL="${STAGE_REL:-data/baseline_v2_${FROM_DATE}_${TO_DATE}}"   # under trainer/, a child of data/
STORE="${QF_BASELINE_STORE:-/var/lib/qf-baselines}"
LOCK_FILE="${QFD_LOCK_FILE:-/var/lib/qf-locks/heavy-training.lock}"
INTENT_DIR="${QFD_INTENT_DIR:-/var/lib/qf-locks/intent.d}"
LOCK_WAIT_S="${LOCK_WAIT_S:-9000}"
TRUSTED_REF="${TRUSTED_REF:-origin/feat/queue-forecasting}"
GIT_REMOTE="${GIT_REMOTE:-lotas}"
RESEARCH="${RESEARCH_USER:-research}"
TRUSTED_HOST="${QF_TRUSTED_HOST:-/srv/queue-forecasting/tools/queue-forecasting/host}"

# Derived. STAGE_REL is contained by construction: one plain name under data/,
# never data/ itself and never a path that can climb out of it, because the
# export stage removes an incomplete staging dir and `rm -rf` on the wrong
# STAGE would be the trainer's whole data tree.
[[ "$STAGE_REL" =~ ^data/[A-Za-z0-9][A-Za-z0-9._-]*$ ]] \
  || { echo "FATAL: STAGE_REL must be data/<name> (one component, [A-Za-z0-9._-]), got '$STAGE_REL'" >&2; exit 2; }
STAGE="trainer/$STAGE_REL"
STAGE_IN_CONTAINER="/app/tools/queue-forecasting/$STAGE"
STAGE_MARK="$STAGE.cutover-stage"          # sibling files, OUTSIDE the set: the store is closed-world
PROVENANCE="$STAGE.provenance.json"
CONTRACT_OUT="${TEMPLATE%.in}"
RESEARCH_HOME="$( (getent passwd "$RESEARCH" || true) | cut -d: -f6)"
PAUSE_FILE="$RESEARCH_HOME/qf-research/PAUSE"
TICK_LOCK="${QF_TICK_STATE:-$RESEARCH_HOME/.local/state/qf-tick}/tick.lock"

info() { echo "  $*"; }
ok()   { echo "  ok    $*"; }
step() { echo; echo "== $*"; }
die()  { echo "FATAL: $*" >&2; exit 2; }
as_research() { sudo -H -u "$RESEARCH" bash -lc "$*"; }
qf_json() { sudo -H -u "$RESEARCH" qf "$@" --json; }
json_get() { python3 -c 'import json,sys; v=json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."): v=v[k]
print(v if not isinstance(v,(list,dict)) else json.dumps(v))' "$1" "$2"; }

# The checkout's owner, from the directory (`.git` is at the MONOREPO root, not
# here). Every git command runs as that user: root running git inside another
# user's checkout is refused as "dubious ownership", and a commit must carry
# the operator's identity, not root's.
OWNER="$(stat -c %U "$QF_DIR")"
g() { sudo -H -u "$OWNER" git -C "$QF_DIR" "$@"; }

# Everything this script creates inside the checkout goes back to the owner,
# on success AND on failure: a root-owned staging dir is one the operator
# cannot clean up, and a root-owned log is one they cannot rotate.
fix_ownership() {
  local p
  for p in "$STAGE" "$STAGE".* "$CONTRACT_OUT" host/contracts/ACTIVE; do
    [ -e "$p" ] && chown -R "$OWNER": "$p" 2>/dev/null || true
  done
}
INTENT_FILE=""
cleanup() {
  local rc=$?
  [ -n "$INTENT_FILE" ] && rm -f "$INTENT_FILE" "$INTENT_FILE.tmp"
  fix_ownership
  [ "$rc" -eq 0 ] || echo "FATAL: ${BASH_SOURCE[0]} stopped (exit $rc)" >&2
}
trap cleanup EXIT
trap 'echo "FATAL: ${BASH_SOURCE[0]}:${LINENO} failed" >&2; exit 1' ERR

# --- preflight, before ANY stage mutates anything --------------------------
[ -f docker-compose.yml ] || die "$QF_DIR has no docker-compose.yml: run this from the DEPLOY checkout's host/ dir"
[ -f .env ] || die "$QF_DIR/.env is missing: compose cannot render without DATABASE_URL (host/README.md)"
[ "$(id -u)" -eq 0 ] || die "run with sudo: promote-baseline.sh and mirror-refresh need root"
[ -n "$RESEARCH_HOME" ] || die "no such user '$RESEARCH' (set RESEARCH_USER)"
g rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "$QF_DIR is not inside a git checkout owned by $OWNER"
BRANCH="$(g rev-parse --abbrev-ref HEAD)"
PREFIX="$(g rev-parse --show-prefix)"      # e.g. tools/queue-forecasting/ -- porcelain paths are ROOT-relative
[ "$BRANCH" = "${TRUSTED_REF#origin/}" ] || die "checkout is on '$BRANCH' but the mirror tracks $TRUSTED_REF; check out that branch or set TRUSTED_REF"
grep -q "baselineExportRecord" src/predictor.js \
  || die "src/predictor.js does not carry baselineExportRecord: git pull 44b24cd888 first"
[[ "$FROM_DATE" < "$TO_DATE" ]] || die "FROM_DATE $FROM_DATE must be before TO_DATE $TO_DATE"

# `git status` output that is EMPTY because git FAILED must not read as clean.
# Captured in its own assignment so the exit status is checked, then inspected.
porcelain() {  # porcelain <pathspec...>  -> sets PORCELAIN (status lines); dies on git failure
  # A function, not a subshell: `die` inside `$(...)` would only end the
  # subshell and hand the caller an empty string that reads as "clean".
  PORCELAIN="$(g status --porcelain -- "$@")" || die "git status failed"
}

# ---------------------------------------------------------------------------
# The cohorts, from `qf extracts --json`. The per-day JSONs must cover
# `[as_of - holdout_days, as_of)` for EACH cohort (trainer/src/train.py:277
# refuses a missing day), and the NDJSON must span both training windows.
# ---------------------------------------------------------------------------
resolve_cohorts() {
  local extracts
  extracts="$(qf_json extracts)" || die "qf extracts failed (is qf-dispatch up?)"
  COHORTS="$(python3 - "$extracts" "$COHORT_A" "$COHORT_B" <<'PYEOF'
import json, sys
resp, a_prefix, b_prefix = json.loads(sys.argv[1]), sys.argv[2], sys.argv[3]
if not resp.get("ok"):
    sys.exit(f"qf extracts: {resp.get('error')}")
rows = resp.get("extracts") or []
def has_qctx(r):
    return "task_created" in ((r.get("columns") or {}).get("qctx_runs") or ())
def pick(prefix, label):
    named = [r for r in rows if r.get("target") == "wait_time"
             and (str(r.get("as_of_date", "")).startswith(prefix)
                  or str(r.get("request_hash", "")).startswith(prefix))]
    # The qctx candidates need `task_created` in qctx_runs (experiment.py
    # refuses otherwise), and the reference must run on the SAME extract, so
    # an extract without it cannot be a cohort here at all.
    hits = [r for r in named if has_qctx(r)]
    if len(hits) != 1:
        sys.exit(f"{label} '{prefix}': expected exactly one wait_time extract with task_created, found {len(hits)}"
                 f" (of {len(named)} named): " + ", ".join(
                     f"{(h.get('request_hash') or '?')[:12]}@{str(h.get('as_of_date'))[:10]}"
                     f" from {str(h.get('train_start'))[:10]} qctx={'yes' if has_qctx(h) else 'NO'}"
                     for h in named) + ". Name one by hash prefix: COHORT_A=<12 hex> / COHORT_B=<12 hex>.")
    return hits[0]
a, b = pick(a_prefix, "cohort A"), pick(b_prefix, "cohort B")
if a["request_hash"] == b["request_hash"]:
    sys.exit("cohort A and B resolved to the same extract")
for r in (a, b):
    print(r["request_hash"], str(r["as_of_date"])[:10], str(r["train_start"])[:10])
PYEOF
)" || die "could not resolve the two cohorts"
  read -r A_HASH A_ASOF A_START <<<"$(echo "$COHORTS" | sed -n 1p)"
  read -r B_HASH B_ASOF B_START <<<"$(echo "$COHORTS" | sed -n 2p)"
  info "cohort A: ${A_HASH:0:12} $A_START..$A_ASOF"
  info "cohort B: ${B_HASH:0:12} $B_START..$B_ASOF"
  # Coverage, both ends, both cohorts. Residual rows are pending_at < as_of, so
  # an exclusive TO_DATE equal to the later as_of is exactly enough.
  for s in "$A_START" "$B_START"; do
    [[ "$FROM_DATE" > "$s" ]] && die "a cohort trains from $s but the export starts $FROM_DATE; lower FROM_DATE"
  done
  for a in "$A_ASOF" "$B_ASOF"; do
    [[ "$TO_DATE" < "$a" ]] && die "a cohort's as_of is $a but the export ends (exclusive) at $TO_DATE; raise TO_DATE"
  done
  EARLIEST_START="$A_START"; [[ "$B_START" < "$A_START" ]] && EARLIEST_START="$B_START"
  local h
  h="$(grep -E '^holdout_days:' "trainer/$REFERENCE_CONFIG" | awk '{print $2}')"
  [[ "$h" =~ ^[0-9]+$ ]] || die "cannot read holdout_days from trainer/$REFERENCE_CONFIG"
  HOLDOUT_DAYS="$(python3 -c '
import sys, datetime
h, out = int(sys.argv[1]), []
for asof in sys.argv[2:]:
    d = datetime.date.fromisoformat(asof)
    out += [(d - datetime.timedelta(days=k)).isoformat() for k in range(h, 0, -1)]
print(" ".join(sorted(set(out))))' "$h" "$A_ASOF" "$B_ASOF")"
  LAST_HOLDOUT="${HOLDOUT_DAYS##* }"
  info "holdout days needing a per-day JSON: $HOLDOUT_DAYS"
}

# Policy B exclusions. `load_anomalous_dates` has no date bound (data_loader.py):
# one call returns every flagged day, so one list serves the whole window.
resolve_exclusions() {
  EXCLUDED="$(docker compose run --rm --entrypoint uv trainer \
      run python -m scripts.resolve_excluded_dates --config "$REFERENCE_CONFIG" \
    | { grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' || true; } | sort -u | tr '\n' ',' | sed 's/,$//')"
  EXCLUDE_FLAG=()
  if [ -n "$EXCLUDED" ]; then
    EXCLUDE_FLAG=(--exclude-dates "$EXCLUDED")
    info "excluding anomalous days: $EXCLUDED"
  else
    info "no anomalous days flagged"
  fi
}

# What THIS run would produce, as one string; compared to the provenance
# record to decide whether an existing set may be reused.
provenance_key() {
  printf 'from=%s to=%s exclude=%s cohorts=%s,%s days=%s\n' \
    "$FROM_DATE" "$TO_DATE" "$EXCLUDED" "$A_HASH" "$B_HASH" "$HOLDOUT_DAYS"
}

# Every file the provenance lists must still exist and hash the same, and
# nothing else may be in the set. Prints nothing on success.
verify_provenance_files() {
  python3 - "$PROVENANCE" "$STAGE" <<'PYEOF'
import hashlib, json, os, sys
prov, stage = json.load(open(sys.argv[1])), sys.argv[2]
files = prov.get("files") or {}
if not files:
    sys.exit("provenance lists no files")
present = {n for n in os.listdir(stage) if os.path.isfile(os.path.join(stage, n))}
extra = present - files.keys()
if extra:
    sys.exit(f"{stage} holds files the provenance does not list: {sorted(extra)}")
for name, want in files.items():
    path = os.path.join(stage, name)
    if not os.path.isfile(path):
        sys.exit(f"{name} is missing from {stage}")
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != want:
        sys.exit(f"{name} has changed since the export completed")
PYEOF
}

# The host's heavy-work mutex (qfd admits nothing heavy while it is held; the
# nightly waits on it). The exporter is a 6g container whose memory budget in
# docker-compose.yml assumes training is NOT running beside it. Held on fd 7
# for the export stage only; probes need it themselves.
acquire_heavy_lock() {
  [ -w "$LOCK_FILE" ] || die "lock file $LOCK_FILE is missing or not writable. Provision it: sudo ./host/phase2-setup.sh locks"
  { [ -d "$INTENT_DIR" ] && [ -w "$INTENT_DIR" ]; } || die "intent dir $INTENT_DIR is missing. Provision it: sudo ./host/phase2-setup.sh locks"
  # The marker qfd's admission gate understands (`nightly.<pid>.<ts>.intent`,
  # body pid=/deadline=): "heavy work is waiting, admit nothing new". Same
  # protocol as scripts/daily_walk_forward.sh; the name is the gate's vocabulary.
  INTENT_FILE="$INTENT_DIR/nightly.$$.$(date +%s).intent"
  ( umask 027; printf 'pid=%d\ndeadline=%d\n' "$$" "$(( $(date +%s) + LOCK_WAIT_S ))" > "$INTENT_FILE.tmp" )
  chmod 0640 "$INTENT_FILE.tmp" && mv -f "$INTENT_FILE.tmp" "$INTENT_FILE"
  exec 7>>"$LOCK_FILE"
  info "waiting for the heavy-work lock (up to ${LOCK_WAIT_S}s; a running probe or nightly holds it)"
  flock -w "$LOCK_WAIT_S" 7 || die "heavy-work lock not acquired within ${LOCK_WAIT_S}s ($LOCK_FILE)"
  ok "heavy-work lock held"
}
release_heavy_lock() {
  exec 7>&- 2>/dev/null || true
  [ -n "$INTENT_FILE" ] && rm -f "$INTENT_FILE"; INTENT_FILE=""
}

# ---------------------------------------------------------------------------
stage_export() {
  step "export: one baseline set for both cohorts -> $STAGE"
  resolve_cohorts
  resolve_exclusions
  local ndjson="$STAGE/baseline_predictions.ndjson"

  if [ -f "$PROVENANCE" ]; then
    [ "$(json_get "$PROVENANCE" key)" = "$(provenance_key)" ] || die "$STAGE was exported with DIFFERENT inputs:
    recorded: $(json_get "$PROVENANCE" key)
    now:      $(provenance_key)
  Set STAGE_REL to a fresh name; a set is never patched in place."
    verify_provenance_files || die "the completed set in $STAGE no longer matches its provenance (above); set STAGE_REL to a fresh name and re-export"
    ok "complete set already exported with identical inputs and intact files; reusing $STAGE"
    return 0
  fi
  if [ -d "$STAGE" ] && [ -n "$(ls -A "$STAGE")" ]; then
    # Files but no provenance = an interrupted export. Nothing in it is trusted,
    # because the predictor writes its final pathname directly and a truncated
    # NDJSON ends on a record boundary just like a finished one. Removed ONLY if
    # this script created the directory (the marker says so); anything else
    # under trainer/data is somebody else's and is refused.
    [ -f "$STAGE_MARK" ] || die "$STAGE exists, is not empty, and was not created by this script (no $STAGE_MARK). Refusing to remove it; set STAGE_REL to a fresh name."
    info "$STAGE holds an incomplete export; starting over"
    rm -rf "$STAGE"
  fi
  mkdir -p "$STAGE"
  date -u +%FT%TZ > "$STAGE_MARK"

  acquire_heavy_lock
  info "exporting $FROM_DATE..$TO_DATE (this takes a while; predictor has 6g)"
  docker compose run --rm predictor node src/predictor.js \
    --export-baseline-predictions --from "$FROM_DATE" --to "$TO_DATE" \
    --output "$STAGE_IN_CONTAINER/baseline_predictions.ndjson.partial" "${EXCLUDE_FLAG[@]}"
  [ -s "$ndjson.partial" ] || die "export wrote nothing"
  # Every row parses and carries the level columns, and the rows actually span
  # what the cohorts need: earliest training day .. last holdout day.
  python3 - "$ndjson.partial" "$EARLIEST_START" "$LAST_HOLDOUT" <<'PYEOF' || die "NDJSON did not validate"
import json, sys
path, need_lo, need_hi = sys.argv[1:4]
need = {"task_id", "run_id", "pending_at", "bl_wait_p50", "bl_wait_p90",
        "bl_wait_level", "bl_wait_sample_size", "bl_duration_level", "bl_duration_sample_size"}
n, lo, hi = 0, None, None
with open(path) as fh:
    for line in fh:
        row = json.loads(line)          # a truncated last line fails here
        missing = need - row.keys()
        if missing:
            sys.exit(f"row {n} lacks {sorted(missing)}: the exporter change is not deployed")
        day = str(row["pending_at"])[:10]
        lo = day if lo is None or day < lo else lo
        hi = day if hi is None or day > hi else hi
        n += 1
if n == 0:
    sys.exit("zero rows")
if lo > need_lo:
    sys.exit(f"rows start {lo} but the earliest cohort trains from {need_lo}")
if hi < need_hi:
    sys.exit(f"rows end {hi} but the last holdout day is {need_hi}")
print(f"  ok    NDJSON rows: {n}, pending_at {lo}..{hi}, level columns present")
PYEOF
  mv -T "$ndjson.partial" "$ndjson"

  for d in $HOLDOUT_DAYS; do
    local out="$STAGE/$d.json"
    info "per-day generate: $d"
    docker compose run --rm predictor node src/predictor.js \
      --pending-eval-date "$d" --output-json "$STAGE_IN_CONTAINER/$d.json.partial" "${EXCLUDE_FLAG[@]}"
    python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$out.partial" \
      || die "per-day JSON for $d is not parseable"
    mv -T "$out.partial" "$out"
  done
  release_heavy_lock

  # Closed-world: promote refuses anything but the NDJSON and <day>.json files.
  local extra
  extra="$(find "$STAGE" -maxdepth 1 -regextype posix-extended -type f \
           ! -name baseline_predictions.ndjson ! -regex '.*/[0-9]{4}-[0-9]{2}-[0-9]{2}\.json' -printf '%f ' || true)"
  [ -z "$extra" ] || die "$STAGE holds non-baseline files: $extra"

  # Published LAST and atomically: its presence means "every file above is
  # complete", and it carries each file's digest so that claim can be re-checked.
  python3 - "$PROVENANCE" "$STAGE" "$(provenance_key)" "$FROM_DATE" "$TO_DATE" "$EXCLUDED" "$A_HASH" "$B_HASH" "$HOLDOUT_DAYS" <<'PYEOF'
import hashlib, json, os, sys, datetime
path, stage, key, frm, to, excl, a, b, days = sys.argv[1:10]
files = {}
for name in sorted(os.listdir(stage)):
    p = os.path.join(stage, name)
    if os.path.isfile(p):
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        files[name] = h.hexdigest()
rec = {"key": key, "from_date": frm, "to_date_exclusive": to,
       "exclude_dates": [d for d in excl.split(",") if d],
       "cohorts": {"A": a, "B": b}, "holdout_days": days.split(), "files": files,
       "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
tmp = path + ".partial"
with open(tmp, "w") as fh:
    json.dump(rec, fh, indent=2, sort_keys=True); fh.write("\n")
os.replace(tmp, path)
PYEOF
  fix_ownership
  ok "staged: $(ls "$STAGE" | wc -l) files, provenance in $PROVENANCE"
}

stage_promote() {
  step "promote: $STAGE -> $STORE/<hash>"
  [ -f "$PROVENANCE" ] || die "no $PROVENANCE: the export did not complete (an interrupted export leaves no provenance). Run the export stage."
  verify_provenance_files || die "the staged set does not match its provenance (above); re-export into a fresh STAGE_REL"
  local excluded args=()
  excluded="$(json_get "$PROVENANCE" exclude_dates | python3 -c 'import json,sys; print(",".join(json.load(sys.stdin)))')"
  [ -n "$excluded" ] && args=(--exclude "$excluded")
  info "exclusions from provenance: ${excluded:-none}"
  # The content hash, computed exactly as promote-baseline.sh computes it, so
  # nothing here depends on parsing that script's wording.
  NEW_HASH="$(PYTHONPATH="$HERE/shared" python3 - "$STAGE" "$excluded" <<'PYEOF'
import sys, baseline
src, exclude = sys.argv[1], [d for d in sys.argv[2].split(",") if d]
try:
    manifest = baseline.describe(src, exclude_dates=exclude)
except baseline.BaselineError as e:
    sys.exit(f"REFUSED: {e}")
print(baseline.baseline_hash(manifest))
PYEOF
)" || die "the staged set does not describe as a baseline"
  "$HERE/promote-baseline.sh" "$STAGE" "${args[@]}" | tee "$STAGE.promote.log"
  [ -f "$STORE/$NEW_HASH/MANIFEST.json" ] || die "promote finished but $STORE/$NEW_HASH is not there"
  echo "$NEW_HASH" > "$STAGE.baseline_hash"
  fix_ownership
  ok "baseline: $NEW_HASH"
}

# Instantiate the template against a hash WITHOUT touching the checkout, and
# print the canonical body. Used to prove an existing output is what the
# template says, not merely a file that pins the same baseline.
expected_contract_body() {
  PYTHONPATH="$HERE/shared" python3 - "$TEMPLATE" "$1" <<'PYEOF'
import json, sys, contract
template, digest = sys.argv[1], sys.argv[2]
raw = open(template).read().replace("@BASELINE_HASH@", digest)
body = contract.validate(json.loads(raw))
body["contract_hash"] = contract.contract_hash(body)
print(json.dumps(body, indent=2, sort_keys=True))
PYEOF
}
canonical_json() { python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])), indent=2, sort_keys=True))' "$1"; }

# ACTIVE content on stdin -> ACTIVE content with exactly `<target> <hash>` for
# the target (the same edit instantiate --activate makes). Pure; used both to
# write the file and to compute what HEAD's file SHOULD become.
active_with() {  # active_with <target> <hash>
  python3 - "$1" "$2" <<'PYEOF'
import sys
target, digest = sys.argv[1], sys.argv[2]
lines = sys.stdin.read().splitlines()
out, replaced = [], False
for line in lines:
    fields = line.split("#", 1)[0].split()
    if len(fields) == 2 and fields[0] == target:
        if not replaced:
            out.append(f"{target} {digest}"); replaced = True
        continue
    out.append(line)
if not replaced:
    if not out:
        out.append("# Which published contract each target is judged by. See README.md.")
    out.append(f"{target} {digest}")
sys.stdout.write("\n".join(out) + "\n")
PYEOF
}

stage_activate() {
  step "activate: instantiate $TEMPLATE against the new baseline, then commit"
  [ -s "$STAGE.baseline_hash" ] || die "no $STAGE.baseline_hash; run the promote stage first"
  NEW_HASH="$(cat "$STAGE.baseline_hash")"
  [ -d "$STORE/$NEW_HASH" ] || die "$NEW_HASH is not in $STORE"
  local out="$CONTRACT_OUT" active=host/contracts/ACTIVE

  # Preflight the checkout BEFORE anything is written. The commit below must
  # contain exactly two files. Staged changes are tolerated ONLY if they are
  # those two files (a previous activation whose commit failed); anything else
  # staged, or any other change under host/contracts, is refused.
  local staged unrelated
  staged="$(g diff --cached --name-only)" || die "git diff --cached failed"
  if [ -n "$staged" ]; then
    unrelated="$(printf '%s\n' "$staged" | grep -vx "${PREFIX}${out}" | grep -vx "${PREFIX}${active}" || true)"
    [ -z "$unrelated" ] || die "the index has unrelated staged changes; commit or unstage them first:
$unrelated"
    info "resuming: $out / ACTIVE were staged by an earlier attempt"
  fi
  porcelain host/contracts ":(exclude)$out" ":(exclude)$active"
  [ -z "$PORCELAIN" ] || die "host/contracts has unrelated uncommitted changes:
$PORCELAIN"

  local expected; expected="$(expected_contract_body "$NEW_HASH")" || die "the template does not instantiate against $NEW_HASH"
  V2_HASH="$(echo "$expected" | python3 -c 'import json,sys; print(json.load(sys.stdin)["contract_hash"])')"

  if [ -f "$out" ]; then
    # Accepted ONLY if it is what the template instantiates to today. A file
    # that merely pins the same baseline could be edited, corrupted, or a
    # different rule with a self-consistent hash.
    diff -u <(echo "$expected") <(canonical_json "$out") \
      || die "$out exists and is NOT what $TEMPLATE instantiates to against $NEW_HASH (diff above). A contract is versioned, not repointed: bump to v3, or remove the stray file deliberately."
    ok "$out already matches the template at $NEW_HASH (contract ${V2_HASH:0:12})"
    # Re-do the ACTIVE edit idempotently: instantiate-contract.sh refuses an
    # existing output, so this is the resume path when the .json landed and
    # the activation did not.
    local tmp; tmp="$(mktemp "$active.XXXXXX")"
    { [ -f "$active" ] && cat "$active" || true; } | active_with wait_time "$V2_HASH" > "$tmp"
    chmod 0644 "$tmp"; mv -T "$tmp" "$active"
  else
    "$HERE/instantiate-contract.sh" "$TEMPLATE" "$NEW_HASH" --activate
    diff -q <(echo "$expected") <(canonical_json "$out") \
      || die "instantiate-contract.sh wrote something other than the expected body"
  fi
  grep -qx "wait_time $V2_HASH" "$active" || die "ACTIVE does not name 'wait_time $V2_HASH'"

  # ACTIVE may differ from HEAD by the wait_time line and NOTHING else: the
  # whole file is committed, so an operator's stray edit to another target
  # would otherwise ride along under this commit's message.
  local head_active
  head_active="$(g show "HEAD:${PREFIX}${active}" 2>/dev/null || true)"
  diff -u <(printf '%s' "$head_active" | active_with wait_time "$V2_HASH") "$active" \
    || die "ACTIVE carries changes beyond the wait_time cutover (diff above: expected vs working). Commit or revert those separately first."

  chmod 0644 "$out" "$active"
  chown "$OWNER": "$out" "$active"
  echo "$V2_HASH" > "$STAGE.contract_hash"

  porcelain "$out" "$active"
  if [ -n "$PORCELAIN" ]; then
    local excluded; excluded="$(json_get "$PROVENANCE" exclude_dates 2>/dev/null | python3 -c 'import json,sys; print(",".join(json.load(sys.stdin)) or "none")' 2>/dev/null || echo unknown)"
    g add -- "$out" "$active"
    # --only with explicit paths: nothing else that might be in the index rides along.
    g -c commit.gpgsign=false commit -q --only -F - -- "$out" "$active" <<EOF
queue-forecasting: cut the wait_time contract over to v2 (${V2_HASH:0:12})

Pinned to baseline ${NEW_HASH:0:12} (${FROM_DATE}..${TO_DATE} exclusive, level
columns present, Policy B exclusions: ${excluded}). ACTIVE names it, so the
resolver judges every wait_time run by v2 from the next mirror-refresh.
v1 stays published for the frontier's history.
EOF
    ok "committed: $(g log --oneline -1)"
  else
    ok "already committed"
  fi
  fix_ownership
  info "contract v2: $V2_HASH"
}

stage_verify() {
  step "verify: push, mirror-refresh, plan"
  [ -s "$STAGE.contract_hash" ] || die "no $STAGE.contract_hash; run the activate stage first"
  [ -s "$STAGE.baseline_hash" ] || die "no $STAGE.baseline_hash"
  V2_HASH="$(cat "$STAGE.contract_hash")"; NEW_HASH="$(cat "$STAGE.baseline_hash")"
  porcelain host/contracts
  [ -z "$PORCELAIN" ] || die "host/contracts is not fully committed"
  g push "$GIT_REMOTE" "HEAD:$BRANCH"
  ok "pushed $BRANCH to $GIT_REMOTE"
  TRUSTED_REF="$TRUSTED_REF" "$HERE/phase2-setup.sh" mirror-refresh
  grep -qx "wait_time $V2_HASH" "$TRUSTED_HOST/contracts/ACTIVE" 2>/dev/null \
    || die "$TRUSTED_HOST/contracts/ACTIVE does not name v2 after mirror-refresh (is $TRUSTED_REF what you pushed?)"
  local plan
  plan="$(as_research "python3 $TRUSTED_HOST/experiment.py plan $REFERENCE_CONFIG")" || { echo "$plan"; die "experiment.py plan refused"; }
  echo "$plan" | sed 's/^/    /'
  echo "$plan" | grep -q "no ACTIVE contract" && die "plan says no ACTIVE contract"
  echo "$plan" | grep -q "${V2_HASH:0:12}" || die "plan does not mention contract ${V2_HASH:0:12}"
  echo "$plan" | grep -q "${NEW_HASH:0:12}" || die "plan does not mention baseline ${NEW_HASH:0:12}"
  sudo -H -u "$RESEARCH" qf contracts | grep -q "^${V2_HASH:0:12}.*ACTIVE for wait_time" || die "qf contracts does not show ${V2_HASH:0:12} ACTIVE for wait_time"
  ok "v2 is ACTIVE and resolves to the new baseline"
}

# Per-cohort reference state: a JSON record of WHAT the run certifies, so a
# rerun with a different config/contract/extract cannot inherit it, and a
# probe whose evaluation failed is finished with `qf evaluate`, not re-trained.
ref_state_path() { echo "$STAGE.reference-${1:0:12}.json"; }
write_ref_state() {  # write_ref_state <cohort> <rid> <evaluated true|false>
  python3 - "$(ref_state_path "$1")" "$1" "$2" "$3" "$V2_HASH" "$REFERENCE_CONFIG" <<'PYEOF'
import json, os, sys
path, extract, rid, evaluated, contract, config = sys.argv[1:7]
tmp = path + ".partial"
with open(tmp, "w") as fh:
    json.dump({"extract": extract, "run_id": rid, "evaluated": evaluated == "true",
               "contract": contract, "config": config}, fh, indent=2, sort_keys=True)
    fh.write("\n")
os.replace(tmp, path)
PYEOF
}

stage_probe() {
  step "probe: pause + tick lock, sync the research workspace, then two reference runs under v2"
  [ -s "$STAGE.contract_hash" ] || die "no $STAGE.contract_hash; run activate + verify first"
  V2_HASH="$(cat "$STAGE.contract_hash")"
  resolve_cohorts

  # The loop and this script both commit into the research workspace
  # (`experiment.py run` commits every non-journal change it finds there), so
  # they must not interleave: the loop must be PAUSED (no new tick starts) AND
  # its tick.lock held here (a tick already past its PAUSE check finishes first).
  [ -f "$PAUSE_FILE" ] || die "the research loop is not paused. Pause it first:
    sudo -H -u $RESEARCH touch $PAUSE_FILE
  then wait for 'sudo -H -u $RESEARCH qf list' to show no live probe, and rerun this stage."
  if [ -f "$TICK_LOCK" ]; then
    exec 8>>"$TICK_LOCK"
    flock -n 8 || die "a tick holds $TICK_LOCK right now; let it finish (watch 'sudo -H -u $RESEARCH qf list'), then rerun this stage"
    ok "paused, and holding the tick lock"
  else
    info "no $TICK_LOCK: the loop is not installed here, nothing to exclude"
  fi

  # The research workspace is provisioned from the mirror by `experiment.py
  # sync`, not by `run` (research-loop/README.md ~508). Without this the v2
  # probes train the pre-v2 trainer.
  local dry; dry="$(as_research "python3 $TRUSTED_HOST/experiment.py sync --dry-run")" || { echo "$dry"; die "sync --dry-run refused"; }
  if echo "$dry" | grep -q "every file already matches the mirror"; then
    ok "research workspace matches the mirror"
  else
    echo "$dry" | sed 's/^/    /'
    as_research "python3 $TRUSTED_HOST/experiment.py sync --note 'sync trainer for the contract v2 series'"
    ok "workspace synced to the mirror"
  fi

  local rids=()
  for cohort in "$A_HASH" "$B_HASH"; do
    local state; state="$(ref_state_path "$cohort")"
    local log="$STAGE.probe-${cohort:0:12}.log" rid="" evaluated=false
    if [ -s "$state" ]; then
      { [ "$(json_get "$state" extract)" = "$cohort" ] && [ "$(json_get "$state" contract)" = "$V2_HASH" ] \
        && [ "$(json_get "$state" config)" = "$REFERENCE_CONFIG" ]; } \
        || die "$state certifies a different extract/contract/config than this run asks for; remove it deliberately to redo"
      rid="$(json_get "$state" run_id)"; evaluated="$(json_get "$state" evaluated)"
    fi
    local note='v2 reference: percentile baseline + residual, first run of the v2 series on this cohort'
    if [ -z "$rid" ]; then
      info "reference on ${cohort:0:12} ... (blocks until probe AND evaluation finish)"
      set +e
      as_research "python3 $TRUSTED_HOST/experiment.py run $REFERENCE_CONFIG --extract $cohort --contract $V2_HASH \
          --bar pinball_p90_guarded --dir improve --reference-run --note '$note'" | tee "$log"
      local rc=${PIPESTATUS[0]}
      set -e
      rid="$(grep -oE '\bprobe-[0-9A-Za-z-]+\b' "$log" | tail -1 || true)"
      if [ "$rc" -ne 0 ]; then
        if [ -n "$rid" ]; then
          # The probe exists; only what came after failed. Record it so the
          # rerun evaluates THIS run instead of training another.
          write_ref_state "$cohort" "$rid" false
          die "probe $rid was submitted but the run did not complete (see $log). Rerun this stage: it will finish with 'qf evaluate', not a new probe."
        fi
        die "reference run on ${cohort:0:12} failed before a probe id was printed (see $log)"
      fi
      [ -n "$rid" ] || die "no run id in $log"
      write_ref_state "$cohort" "$rid" true
      ok "reference run: $rid"
    elif [ "$evaluated" != "True" ]; then
      info "finishing ${cohort:0:12}: evaluating recorded probe $rid under v2"
      as_research "qf evaluate --run $rid --contract $V2_HASH --note '$note' --wait" | tee -a "$log"
      write_ref_state "$cohort" "$rid" true
      ok "reference run evaluated: $rid"
    else
      ok "reference on ${cohort:0:12} already recorded: $rid (remove $state to redo)"
    fi
    rids+=("$cohort $rid")
  done
  exec 8>&- 2>/dev/null || true

  echo
  echo "Reference runs are scored. The four candidate probes, judged inside each cohort:"
  for pair in "${rids[@]}"; do
    local cohort rid; cohort="${pair% *}"; rid="${pair#* }"
    for cfg in $CANDIDATES; do
      echo "  sudo -H -u $RESEARCH bash -lc \"python3 $TRUSTED_HOST/experiment.py run $cfg --extract $cohort --contract $V2_HASH --bar pinball_p90_guarded --dir improve --vs $rid --note 'v2: does $(basename "$cfg" .yaml) beat the reference on the served p90'\""
    done
  done
  echo "Then: sudo -H -u $RESEARCH bash -lc \"$TRUSTED_HOST/results.sh --json | python3 $TRUSTED_HOST/research-loop/frontier.py --journal $RESEARCH_HOME/qf-research/journal\""
  echo "Or release the loop (rm $PAUSE_FILE, or close the pause issue) and let action 3/4 run them under the ACTIVE v2 contract."
  fix_ownership
}

# ---------------------------------------------------------------------------
run_stage() {
  case "$1" in
    export)   stage_export ;;
    promote)  stage_promote ;;
    activate) stage_activate ;;
    verify)   stage_verify ;;
    probe)    stage_probe ;;
    *) die "unknown stage '$1' (export | promote | activate | verify | probe)" ;;
  esac
}

if [ "$#" -eq 0 ]; then
  for s in export promote activate verify probe; do run_stage "$s"; done
else
  for s in "$@"; do run_stage "$s"; done
fi
echo; ok "done"
