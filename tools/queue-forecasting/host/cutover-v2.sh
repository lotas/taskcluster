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
#             cohorts) into a FRESH staging dir under trainer/data. Every file is
#             written to a .partial name and renamed only once complete and
#             parseable; a provenance record (dates, exclusions, cohorts, days)
#             is written last, and a rerun reuses the set only on an exact match.
#   promote   promote-baseline.sh on that set with the exclusions the provenance
#             records -> NEW_HASH (first publication wins; rerun is a no-op).
#   activate  instantiate-contract.sh wait_time.v2 against NEW_HASH --activate,
#             then COMMIT wait_time.v2.json + ACTIVE (those two paths only).
#             An existing v2 file is accepted only if it is byte-for-byte what
#             the template instantiates to today.
#   verify    push, mirror-refresh, then `experiment.py plan` must show the v2
#             contract ACTIVE and pinned to NEW_HASH.
#   probe     `experiment.py sync` (the research workspace does NOT follow the
#             mirror by itself), then the two reference runs, one per cohort,
#             pinned with --contract to v2. Each blocks up to ~2h45m. A cohort
#             whose reference is recorded is skipped on rerun. Prints the four
#             candidate commands (qctx, qctx_d per cohort, --vs <reference>).
#
# Deadline: retention (60d) drops the 2026-07-25 rows around 2026-09-23, after
# which the export can no longer cover cohort B's training window.
set -Eeuo pipefail
trap 'echo "FATAL: ${BASH_SOURCE[0]}:${LINENO} failed (exit $?)" >&2; exit 1' ERR

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QF_DIR="$(cd "$HERE/.." && pwd)"           # tools/queue-forecasting in the deploy checkout
cd "$QF_DIR"

# --- knobs (override by env) -----------------------------------------------
FROM_DATE="${FROM_DATE:-2026-07-25}"       # inclusive
TO_DATE="${TO_DATE:-2026-09-01}"           # EXCLUSIVE, as predictor.js --to is
# Cohort A: the extract whose as_of_date (or request_hash) starts with this.
COHORT_A="${COHORT_A:-2026-08-27}"
# Cohort B: request_hash prefix of the second, non-overlapping extract.
COHORT_B="${COHORT_B:-975aea71d759}"
REFERENCE_CONFIG="${REFERENCE_CONFIG:-configs/wait_time_residual_throughput_filtered_baseline.yaml}"
CANDIDATES="${CANDIDATES:-configs/wait_time_residual_throughput_filtered_baseline_qctx.yaml configs/wait_qctx_d_priority_flow.yaml}"
TEMPLATE="${TEMPLATE:-host/contracts/wait_time.v2.json.in}"
STAGE_REL="${STAGE_REL:-data/baseline_v2_${FROM_DATE}_${TO_DATE}}"   # under trainer/
STAGE="trainer/$STAGE_REL"
STAGE_IN_CONTAINER="/app/tools/queue-forecasting/$STAGE"
PROVENANCE="$STAGE.provenance.json"       # OUTSIDE the set: the store is closed-world
STORE="${QF_BASELINE_STORE:-/var/lib/qf-baselines}"
TRUSTED_REF="${TRUSTED_REF:-origin/feat/queue-forecasting}"
GIT_REMOTE="${GIT_REMOTE:-lotas}"
RESEARCH="${RESEARCH_USER:-research}"
TRUSTED_HOST="${QF_TRUSTED_HOST:-/srv/queue-forecasting/tools/queue-forecasting/host}"
PAUSE_FILE="$( (getent passwd "$RESEARCH" || true) | cut -d: -f6)/qf-research/PAUSE"

info() { echo "  $*"; }
ok()   { echo "  ok    $*"; }
step() { echo; echo "== $*"; }
die()  { echo "FATAL: $*" >&2; exit 2; }
as_research() { sudo -H -u "$RESEARCH" bash -lc "$*"; }
qf_json() { sudo -H -u "$RESEARCH" qf "$@" --json; }
json_get() { python3 -c 'import json,sys; v=json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."): v=v[k]
print(v if not isinstance(v,(list,dict)) else json.dumps(v))' "$1" "$2"; }
OWNER="$(stat -c %U .git)"
as_owner() { sudo -H -u "$OWNER" "$@"; }

# --- preflight, before ANY stage mutates anything --------------------------
[ -f docker-compose.yml ] || die "$QF_DIR has no docker-compose.yml: run this from the DEPLOY checkout's host/ dir"
[ -f .env ] || die "$QF_DIR/.env is missing: compose cannot render without DATABASE_URL (host/README.md)"
[ "$(id -u)" -eq 0 ] || die "run with sudo: promote-baseline.sh and mirror-refresh need root"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "$QF_DIR is not a git checkout"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$BRANCH" = "${TRUSTED_REF#origin/}" ] || die "checkout is on '$BRANCH' but the mirror tracks $TRUSTED_REF; check out that branch or set TRUSTED_REF"
grep -q "baselineExportRecord" src/predictor.js \
  || die "src/predictor.js does not carry baselineExportRecord: git pull 44b24cd888 first"
[[ "$FROM_DATE" < "$TO_DATE" ]] || die "FROM_DATE $FROM_DATE must be before TO_DATE $TO_DATE"

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
def pick(prefix, label):
    hits = [r for r in rows if r.get("target") == "wait_time"
            and (str(r.get("as_of_date", "")).startswith(prefix)
                 or str(r.get("request_hash", "")).startswith(prefix))]
    if len(hits) != 1:
        sys.exit(f"{label} '{prefix}': expected exactly one wait_time extract, found {len(hits)}: "
                 + ", ".join((h.get('request_hash') or '?')[:12] + '@' + str(h.get('as_of_date'))[:10] for h in hits))
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
  # Coverage, both ends, both cohorts. --to is exclusive, so TO_DATE must be
  # AFTER the later as_of (a pending_at on the as_of day itself is not needed,
  # but a TO_DATE equal to it would drop the last holdout day's residual rows).
  for s in "$A_START" "$B_START"; do
    [[ "$FROM_DATE" > "$s" ]] && die "a cohort trains from $s but the export starts $FROM_DATE; lower FROM_DATE"
  done
  for a in "$A_ASOF" "$B_ASOF"; do
    [[ "$TO_DATE" < "$a" ]] && die "a cohort's as_of is $a but the export ends (exclusive) at $TO_DATE; raise TO_DATE"
  done
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

# ---------------------------------------------------------------------------
stage_export() {
  step "export: one baseline set for both cohorts -> $STAGE"
  resolve_cohorts
  resolve_exclusions
  local ndjson="$STAGE/baseline_predictions.ndjson"

  if [ -f "$PROVENANCE" ]; then
    if [ "$(json_get "$PROVENANCE" key)" = "$(provenance_key)" ]; then
      ok "complete set already exported with identical inputs; reusing $STAGE"
      return 0
    fi
    die "$STAGE was exported with DIFFERENT inputs:
    recorded: $(json_get "$PROVENANCE" key)
    now:      $(provenance_key)
  Move it aside or set STAGE_REL to a fresh name; a set is never patched in place."
  fi
  if [ -d "$STAGE" ] && [ -n "$(ls -A "$STAGE")" ]; then
    # Files but no provenance = an interrupted export. Nothing in it is trusted,
    # because the predictor writes its final pathname directly and a truncated
    # NDJSON ends on a record boundary just like a finished one.
    info "$STAGE holds an incomplete export; starting over"
    rm -rf "$STAGE"
  fi
  mkdir -p "$STAGE"

  info "exporting $FROM_DATE..$TO_DATE (this takes a while; predictor has 6g)"
  docker compose run --rm predictor node src/predictor.js \
    --export-baseline-predictions --from "$FROM_DATE" --to "$TO_DATE" \
    --output "$STAGE_IN_CONTAINER/baseline_predictions.ndjson.partial" "${EXCLUDE_FLAG[@]}"
  [ -s "$ndjson.partial" ] || die "export wrote nothing"
  python3 - "$ndjson.partial" <<'PYEOF' || die "NDJSON did not validate"
import json, sys
need = {"task_id", "run_id", "pending_at", "bl_wait_p50", "bl_wait_p90",
        "bl_wait_level", "bl_wait_sample_size", "bl_duration_level", "bl_duration_sample_size"}
n = 0
with open(sys.argv[1]) as fh:
    for line in fh:
        row = json.loads(line)          # a truncated last line fails here
        missing = need - row.keys()
        if missing:
            sys.exit(f"row {n} lacks {sorted(missing)}: the exporter change is not deployed")
        n += 1
if n == 0:
    sys.exit("zero rows")
print(f"  ok    NDJSON rows: {n}, level columns present")
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

  # Closed-world: promote refuses anything but the NDJSON and <day>.json files.
  local extra
  extra="$(find "$STAGE" -maxdepth 1 -regextype posix-extended -type f \
           ! -name baseline_predictions.ndjson ! -regex '.*/[0-9]{4}-[0-9]{2}-[0-9]{2}\.json' -printf '%f ' || true)"
  [ -z "$extra" ] || die "$STAGE holds non-baseline files: $extra"

  # Written LAST, so its presence means "every file above is complete".
  python3 - "$PROVENANCE" "$(provenance_key)" "$FROM_DATE" "$TO_DATE" "$EXCLUDED" "$A_HASH" "$B_HASH" "$HOLDOUT_DAYS" <<'PYEOF'
import json, sys, datetime
path, key, frm, to, excl, a, b, days = sys.argv[1:9]
json.dump({"key": key, "from_date": frm, "to_date_exclusive": to,
           "exclude_dates": [d for d in excl.split(",") if d],
           "cohorts": {"A": a, "B": b}, "holdout_days": days.split(),
           "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat()},
          open(path, "w"), indent=2, sort_keys=True)
PYEOF
  ok "staged: $(ls "$STAGE" | wc -l) files, provenance in $PROVENANCE"
}

stage_promote() {
  step "promote: $STAGE -> $STORE/<hash>"
  [ -f "$PROVENANCE" ] || die "no $PROVENANCE: the export did not complete (an interrupted export leaves no provenance). Run the export stage."
  [ -s "$STAGE/baseline_predictions.ndjson" ] || die "$STAGE has no NDJSON although provenance exists; remove both and re-export"
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

# Make ACTIVE say exactly `wait_time <hash>` (the same edit instantiate
# --activate makes), for the resume case where the .json exists but the
# activation did not land. Idempotent.
ensure_active_line() {
  local target="$1" digest="$2" file=host/contracts/ACTIVE
  python3 - "$file" "$target" "$digest" <<'PYEOF'
import os, sys
path, target, digest = sys.argv[1:4]
lines = open(path).read().splitlines() if os.path.exists(path) else []
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
tmp = path + ".tmp"
open(tmp, "w").write("\n".join(out) + "\n")
os.chmod(tmp, 0o644)
os.replace(tmp, path)
PYEOF
}

stage_activate() {
  step "activate: instantiate $TEMPLATE against the new baseline, then commit"
  [ -s "$STAGE.baseline_hash" ] || die "no $STAGE.baseline_hash; run the promote stage first"
  NEW_HASH="$(cat "$STAGE.baseline_hash")"
  [ -d "$STORE/$NEW_HASH" ] || die "$NEW_HASH is not in $STORE"
  local out="${TEMPLATE%.in}"

  # Preflight the checkout BEFORE anything is written: the commit below must
  # contain exactly two files, and an index with other staged changes would
  # either pollute it or be left half-committed.
  git diff --cached --quiet || die "the index has staged changes; commit or unstage them first (the cutover must be its own commit)"
  [ -z "$(git status --porcelain -- host/contracts | grep -v " $out$\| host/contracts/ACTIVE$")" ] \
    || die "host/contracts has unrelated uncommitted changes"

  local expected; expected="$(expected_contract_body "$NEW_HASH")" || die "the template does not instantiate against $NEW_HASH"
  V2_HASH="$(echo "$expected" | python3 -c 'import json,sys; print(json.load(sys.stdin)["contract_hash"])')"

  if [ -f "$out" ]; then
    # Accepted ONLY if it is what the template instantiates to today. A file
    # that merely pins the same baseline could be edited, corrupted, or a
    # different rule with a self-consistent hash.
    if ! diff -u <(echo "$expected") <(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])), indent=2, sort_keys=True))' "$out"); then
      die "$out exists and is NOT what $TEMPLATE instantiates to against $NEW_HASH (diff above). A contract is versioned, not repointed: bump to v3, or remove the stray file deliberately."
    fi
    ok "$out already matches the template at $NEW_HASH (contract ${V2_HASH:0:12})"
    ensure_active_line wait_time "$V2_HASH"
  else
    "$HERE/instantiate-contract.sh" "$TEMPLATE" "$NEW_HASH" --activate
    diff -q <(echo "$expected") <(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])), indent=2, sort_keys=True))' "$out") \
      || die "instantiate-contract.sh wrote something other than the expected body"
  fi
  grep -qx "wait_time $V2_HASH" host/contracts/ACTIVE || die "ACTIVE does not name 'wait_time $V2_HASH'"
  chmod 0644 "$out" host/contracts/ACTIVE
  chown "$OWNER": "$out" host/contracts/ACTIVE
  echo "$V2_HASH" > "$STAGE.contract_hash"

  if [ -n "$(git status --porcelain -- "$out" host/contracts/ACTIVE)" ]; then
    local excluded; excluded="$(json_get "$PROVENANCE" exclude_dates 2>/dev/null | python3 -c 'import json,sys; print(",".join(json.load(sys.stdin)) or "none")' 2>/dev/null || echo unknown)"
    as_owner git add -- "$out" host/contracts/ACTIVE
    # --only with explicit paths: nothing else that might be in the index rides along.
    as_owner git -c commit.gpgsign=false commit -q --only -F - -- "$out" host/contracts/ACTIVE <<EOF
queue-forecasting: cut the wait_time contract over to v2 (${V2_HASH:0:12})

Pinned to baseline ${NEW_HASH:0:12} (${FROM_DATE}..${TO_DATE} exclusive, level
columns present, Policy B exclusions: ${excluded}). ACTIVE names it, so the
resolver judges every wait_time run by v2 from the next mirror-refresh.
v1 stays published for the frontier's history.
EOF
    ok "committed: $(git log --oneline -1)"
  else
    ok "already committed"
  fi
  info "contract v2: $V2_HASH"
}

stage_verify() {
  step "verify: push, mirror-refresh, plan"
  [ -s "$STAGE.contract_hash" ] || die "no $STAGE.contract_hash; run the activate stage first"
  [ -s "$STAGE.baseline_hash" ] || die "no $STAGE.baseline_hash"
  V2_HASH="$(cat "$STAGE.contract_hash")"; NEW_HASH="$(cat "$STAGE.baseline_hash")"
  [ -z "$(git status --porcelain -- host/contracts)" ] || die "host/contracts is not fully committed"
  as_owner git push "$GIT_REMOTE" "HEAD:$BRANCH"
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

stage_probe() {
  step "probe: sync the research workspace, then two reference runs under v2"
  [ -s "$STAGE.contract_hash" ] || die "no $STAGE.contract_hash; run activate + verify first"
  V2_HASH="$(cat "$STAGE.contract_hash")"
  resolve_cohorts

  # The research workspace is provisioned from the mirror by `experiment.py
  # sync`, not by `run` (research-loop/README.md ~508). Without this the v2
  # probes train the pre-v2 trainer. `sync` commits and pushes the workspace,
  # so the loop must be paused first or a tick may be mid-edit.
  local dry; dry="$(as_research "python3 $TRUSTED_HOST/experiment.py sync --dry-run")" || { echo "$dry"; die "sync --dry-run refused"; }
  if echo "$dry" | grep -q "every file already matches the mirror"; then
    ok "research workspace matches the mirror"
  else
    echo "$dry" | sed 's/^/    /'
    [ -f "$PAUSE_FILE" ] || die "the workspace differs from the mirror and the loop is not paused. Pause it first: sudo -H -u $RESEARCH touch $PAUSE_FILE  (and wait for 'qf list' to show no live probe)"
    as_research "python3 $TRUSTED_HOST/experiment.py sync --note 'sync trainer for the contract v2 series'"
    ok "workspace synced to the mirror"
  fi

  local rids=()
  for cohort in "$A_HASH" "$B_HASH"; do
    local ridfile="$STAGE.reference-${cohort:0:12}.rid" log="$STAGE.probe-${cohort:0:12}.log" rid
    if [ -s "$ridfile" ]; then
      rid="$(cat "$ridfile")"
      ok "reference on ${cohort:0:12} already recorded: $rid (delete $ridfile to redo)"
    else
      info "reference on ${cohort:0:12} ... (blocks until probe AND evaluation finish)"
      if ! as_research "python3 $TRUSTED_HOST/experiment.py run $REFERENCE_CONFIG --extract $cohort --contract $V2_HASH \
          --bar pinball_p90_guarded --dir improve --reference-run \
          --note 'v2 reference: percentile baseline + residual, first run of the v2 series on this cohort'" \
          | tee "$log"; then
        die "reference run on ${cohort:0:12} failed (see $log); rerun this stage to retry it alone"
      fi
      rid="$(grep -oE '\bprobe-[0-9A-Za-z-]+\b' "$log" | tail -1)"
      [ -n "$rid" ] || die "no run id in $log"
      echo "$rid" > "$ridfile"
      ok "reference run: $rid"
    fi
    rids+=("$cohort $rid")
  done

  echo
  echo "Reference runs are scored. The four candidate probes, judged inside each cohort:"
  for pair in "${rids[@]}"; do
    local cohort rid; cohort="${pair% *}"; rid="${pair#* }"
    for cfg in $CANDIDATES; do
      echo "  sudo -H -u $RESEARCH bash -lc \"python3 $TRUSTED_HOST/experiment.py run $cfg --extract $cohort --contract $V2_HASH --bar pinball_p90_guarded --dir improve --vs $rid --note 'v2: does $(basename "$cfg" .yaml) beat the reference on the served p90'\""
    done
  done
  echo "Then: sudo -H -u $RESEARCH bash -lc \"host/results.sh --json | python3 $TRUSTED_HOST/research-loop/frontier.py --journal ~$RESEARCH/qf-research/journal\""
  echo "Or release the loop (rm $PAUSE_FILE, or close the pause issue) and let action 3/4 run them under the ACTIVE v2 contract."
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
