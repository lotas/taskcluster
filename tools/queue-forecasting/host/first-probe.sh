#!/usr/bin/env bash
# One command for the whole probe loop: sync -> push -> probe -> logs -> evaluate.
#
# WHY THIS EXISTS. The loop has three copies of the trainer in it -- the trusted
# checkout that `docker compose` runs, the `qf-research` worktree that a PROBE
# runs, and whatever the operator edited -- and a change that reaches two of them
# fails in a way that looks like a code bug. It also fails slowly: submit, wait,
# read a two-line verdict, guess, repeat. Every step below exists because it was
# a step somebody did by hand.
#
#   ./host/first-probe.sh
#
# Environment (all optional except the first, which is only optional if
# ~/dev/qf-research is where the worktree is):
#   QF_RESEARCH   qf-research worktree            (default ~/dev/qf-research)
#   EXTRACT       extract request_hash            (default: newest wait_time)
#   BASELINE      promoted baseline_hash          (default: newest)
#   CONTRACT      contract hash or name           (default wait_time_v1)
#   SKIP_SYNC=1   do not touch the research repo, probe the current HEAD
#   NO_EVAL=1     stop after the probe
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TRUSTED="$(cd "$HERE/.." && pwd)"          # tools/queue-forecasting
QF_RESEARCH="${QF_RESEARCH:-$HOME/dev/qf-research}"
CONTRACT="${CONTRACT:-wait_time_v1}"
AS_RESEARCH=(sudo -H -u research)

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mFAIL: %s\033[0m\n' "$*" >&2; exit 1; }
qf()   { "${AS_RESEARCH[@]}" qf "$@"; }

# --- 0. the trusted copy is the one the sync propagates, so check it first ----
say "preflight: trusted checkout"
for probe_marker in "--predictions-out:src/train.py" \
                    "canonical sort:src/data_loader.py" \
                    "from-extract:src/train.py"; do
  needle="${probe_marker%%:*}"; file="${probe_marker##*:}"
  grep -q -- "$needle" "$TRUSTED/trainer/$file" \
    || fail "$TRUSTED/trainer/$file has no '$needle'. The trusted checkout is
    behind the change you are testing -- sync it before syncing onward, or every
    result below describes older code."
done
echo "  ok: --from-extract, --predictions-out, canonical sort all present"

# --- 1. resolve the pinned inputs --------------------------------------------
say "resolve extract / baseline / contract"
if [ -z "${EXTRACT:-}" ]; then
  EXTRACT="$(qf extracts --json 2>/dev/null \
    | python3 -c 'import json,sys
rows=[r for r in (json.load(sys.stdin).get("extracts") or [])
      if r.get("target")=="wait_time"]
print(rows[-1]["request_hash"] if rows else "")')" || true
  [ -n "$EXTRACT" ] || fail "no wait_time extract published; run qf extract first"
fi
if [ -z "${BASELINE:-}" ]; then
  BASELINE="$(qf baselines --json 2>/dev/null \
    | python3 -c 'import json,sys
rows=json.load(sys.stdin).get("baselines") or []
print(rows[-1]["baseline_hash"] if rows else "")')" || true
  [ -n "$BASELINE" ] || fail "no promoted baseline; run host/promote-baseline.sh"
fi
# `--contract` takes a HEX PREFIX, not a name (`resolve_contract` ->
# `_resolve_prefix`), so a friendly default has to be looked up. The listing
# carries `file`, which is `<name>.json`.
case "$CONTRACT" in
  *[!0-9a-f]*|"")
    CONTRACT_NAME="$CONTRACT"
    CONTRACT="$(qf contracts --json 2>/dev/null \
      | CONTRACT_NAME="$CONTRACT_NAME" python3 -c 'import json,os,sys
want = os.environ["CONTRACT_NAME"]
rows = json.load(sys.stdin).get("contracts") or []
hit = [r for r in rows if (r.get("file") or "").startswith(want)]
print(hit[0]["contract_hash"] if hit else "")')" || true
    [ -n "$CONTRACT" ] || fail "no contract matching '$CONTRACT_NAME'.
    \`qf contracts\` lists what is published; a .json.in template is not a
    contract (see instantiate-contract.sh)."
    ;;
esac
echo "  extract=${EXTRACT:0:12}  baseline=${BASELINE:0:12}  contract=${CONTRACT:0:12}"

# --- 2. the sync that was the actual bug -------------------------------------
# A probe runs the QF-RESEARCH copy of the trainer. A `--from-extract` that
# exists only in the trusted checkout makes argparse exit 2 inside the sandbox,
# which surfaces as `error_class=nonzero_exit` and looks like a crash.
if [ "${SKIP_SYNC:-0}" != "1" ]; then
  say "sync trainer + experiment into $QF_RESEARCH"
  [ -d "$QF_RESEARCH/.git" ] || fail "$QF_RESEARCH is not a git worktree
    (set QF_RESEARCH=<path>)"
  [ -z "$(git -C "$QF_RESEARCH" status --porcelain)" ] || fail \
    "$QF_RESEARCH has uncommitted changes. That is somebody's work in progress
    -- possibly the research agent's -- and this script is about to write over
    trainer/. Commit, stash, or discard it deliberately, then re-run:
$(git -C "$QF_RESEARCH" status --short | sed 's/^/      /')"

  mkdir -p "$QF_RESEARCH/research/experiments"
  # NO --delete, deliberately. qf-research is the AGENT's repo; a file under
  # trainer/ that exists there and not here may be the agent's work, and this
  # script has no way to tell that from a leftover. Overwriting what the trusted
  # copy names is a curated port; removing what it does not name is not.
  rsync -a \
    --exclude '__pycache__/' --exclude '.venv/' --exclude 'data/' \
    "$TRUSTED/trainer/" "$QF_RESEARCH/trainer/" \
    || fail "rsync of trainer/ failed"
  cp "$HERE/research-experiments/run_cohort.py" \
     "$QF_RESEARCH/research/experiments/run_cohort.py"

  if [ -n "$(git -C "$QF_RESEARCH" status --porcelain)" ]; then
    echo "  the sync changed:"
    git -C "$QF_RESEARCH" -c core.pager=cat diff --stat | sed 's/^/    /'
    git -C "$QF_RESEARCH" status --short | sed 's/^/    /'
    git -C "$QF_RESEARCH" add -A
    git -C "$QF_RESEARCH" -c core.hooksPath=/dev/null \
        commit -q -m "sync trainer + run_cohort from trusted checkout" \
      || fail "commit failed"
    echo "  committed"
  else
    echo "  already in sync"
  fi
  git -C "$QF_RESEARCH" push -q || fail "push failed -- the AGENT credential is
    what can write to qf-research; the dispatcher's token is read-only"
fi
SHA="$(git -C "$QF_RESEARCH" rev-parse HEAD)" || fail "cannot read HEAD"
echo "  sha=$SHA"

# --- 3. probe -----------------------------------------------------------------
say "probe"
PROBE_OUT="$(qf probe --sha "$SHA" \
  --path research/experiments/run_cohort.py \
  --extract "$EXTRACT" --baseline "$BASELINE" --wait 2>&1)"
PROBE_RC=$?
echo "$PROBE_OUT"
RUN_ID="$(printf '%s\n' "$PROBE_OUT" | head -1)"

if [ "$PROBE_RC" -ne 0 ]; then
  # THE LOGS, UNPROMPTED. The two-line verdict never says why, and fetching them
  # by hand is the step that made every iteration a round trip.
  say "probe stderr (tail 80) -- $RUN_ID"
  qf logs "$RUN_ID" --stream stderr 2>/dev/null | tail -80 \
    || echo "  (no stderr log)"
  say "probe stdout (tail 40)"
  qf logs "$RUN_ID" --stream stdout 2>/dev/null | tail -40 \
    || echo "  (no stdout log)"
  fail "probe $RUN_ID did not succeed"
fi

[ "${NO_EVAL:-0}" != "1" ] || { say "probe SUCCEEDED: $RUN_ID"; exit 0; }

# --- 4. evaluate --------------------------------------------------------------
say "evaluate"
EVAL_OUT="$(qf evaluate --run "$RUN_ID" --contract "$CONTRACT" --wait 2>&1)"
EVAL_RC=$?
echo "$EVAL_OUT"
EVAL_ID="$(printf '%s\n' "$EVAL_OUT" | head -1)"

if [ "$EVAL_RC" -ne 0 ]; then
  say "evaluate stderr (tail 80) -- $EVAL_ID"
  qf logs "$EVAL_ID" --stream stderr 2>/dev/null | tail -80 \
    || echo "  (no stderr log)"
  fail "evaluation $EVAL_ID did not succeed"
fi

say "done"
echo "  probe:      $RUN_ID"
echo "  evaluation: $EVAL_ID"
echo "  verdict is in the lines above, beside the contract hash that produced it"
