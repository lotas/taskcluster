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
#   ./host/first-probe.sh                  bootstrap: trusted trainer -> research
#   EXPERIMENT=1 ./host/first-probe.sh     experiment: probe the agent's edits
#
# The two modes differ in ONE way that matters: bootstrap OVERWRITES
# `qf-research/trainer/` from the trusted checkout, and experiment mode does not
# touch it. Running bootstrap on a worktree that holds an experiment would
# discard the experiment.
#
# Environment (all optional except the first, which is only optional if
# ~/dev/qf-research is where the worktree is):
#   QF_RESEARCH   qf-research worktree            (default ~/dev/qf-research)
#   EXTRACT       extract request_hash            (default: newest wait_time)
#   BASELINE      promoted baseline_hash          (default: newest)
#   CONTRACT      contract hash or name           (default wait_time_v1)
#   COHORT_CONFIG trainer config the probe trains (default: whatever the
#                 research copy of run_cohort.py already names)
#   SKIP_SYNC=1   do not touch the research repo, probe the current HEAD
#   NO_EVAL=1     stop after the probe
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TRUSTED="$(cd "$HERE/.." && pwd)"          # tools/queue-forecasting
QF_RESEARCH="${QF_RESEARCH:-$HOME/dev/qf-research}"
CONTRACT="${CONTRACT:-wait_time_v1}"
# The `probe` kind DEFAULTS to 8g (spec.py KINDS) and this config measured a
# 17.4GB peak on the extract path -- the first probe was SIGKILLed (exit 137)
# with no log, because a container the kernel kills writes nothing on the way
# out. 20g stays under MEM_CEILING_MB (22g) and derives the heavy lane, which is
# correct: a cohort trains, so it must serialise against the nightly.
PROBE_MEM="${PROBE_MEM:-20g}"
AS_RESEARCH=(sudo -H -u research)
COHORT_CONFIG="${COHORT_CONFIG:-}"

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mFAIL: %s\033[0m\n' "$*" >&2; exit 1; }
qf()   { "${AS_RESEARCH[@]}" qf "$@"; }

# WHICH CONFIG THE COHORT TRAINS, written into the research copy of
# `run_cohort.py`. A probe gets no arguments and no injected environment, so the
# config is a CONSTANT IN A FILE -- selecting one is necessarily a commit, and
# the only question is whether the operator makes that edit by hand and then
# describes it in EXPERIMENT_NOTE from memory.
#
# Only rewrites when COHORT_CONFIG is set: the default is to leave the research
# copy alone, because that file is where an experiment lives.
set_cohort_config() {
  [ -n "$COHORT_CONFIG" ] || return 0
  local target="$QF_RESEARCH/research/experiments/run_cohort.py"
  [ -f "$target" ] || fail "no $target yet -- run bootstrap mode once first"
  # Checked against the RESEARCH trainer, not the trusted one: that is the copy
  # the probe opens, and a config present only here would fail inside the
  # sandbox as a missing file, minutes after submission.
  [ -f "$QF_RESEARCH/trainer/$COHORT_CONFIG" ] || fail \
    "$QF_RESEARCH/trainer/$COHORT_CONFIG does not exist. The probe reads the
    config from the RESEARCH copy of the trainer, so it has to be there:
$(ls "$QF_RESEARCH/trainer/configs" 2>/dev/null | sed 's/^/      /')"
  COHORT_CONFIG="$COHORT_CONFIG" python3 -c '
import os, re, sys
path, cfg = sys.argv[1], os.environ["COHORT_CONFIG"]
src = open(path).read()
out, n = re.subn(r"^CONFIG = \".*\"$", f"CONFIG = \"{cfg}\"", src, flags=re.M)
if n != 1:
    raise SystemExit(f"expected one CONFIG assignment in {path}, found {n}")
open(path, "w").write(out)
' "$target" || fail "could not set CONFIG in $target"
  echo "  CONFIG = $COHORT_CONFIG"
}

# A run id is `<kind>-<UTC stamp>-<12 hex>-<pid>`; nothing else in the output
# looks like that.
# `run_id_of <kind> <text>`. The KIND matters: an evaluation's output also
# carries `judged_run: probe-...`, and matching any kind returned the probe's id
# as the evaluation's.
run_id_of() {
  printf '%s\n' "$2" \
    | grep -oE "$1-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}-[0-9]+" \
    | head -1
}

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
# Every lookup PRINTS THE LISTING when it misses. The first version said "run
# `qf contracts`" instead, which cost a round trip to discover that the
# contract's name is `wait_time_v1` and its file is `wait_time.v1.json`.
say "resolve extract / baseline / contract"

# `.` and `-` normalise to `_` so a contract NAME (`wait_time_v1`) matches its
# FILE (`wait_time.v1.json`). Injected into the python below rather than
# duplicated in each selector.
NORM_PY='
def norm(s):
    s = (s or "")
    if s.endswith(".json"):
        s = s[: -len(".json")]
    return s.replace(".", "_").replace("-", "_")
'

# `pick <op> <key> <filter-expr> [<newest-expr>]`.
#
# THE FOURTH ARGUMENT IS NOT A REFINEMENT. Both listings arrive in
# `sorted(os.listdir())` order -- sorted by HASH -- so "the last row" is
# effectively random, and it was only ever right because exactly one wait_time
# extract existed. The moment a second one is published (which is what bumping
# `generation` does) an unsorted `hit[-1]` becomes a coin flip between two
# extracts that differ in their column list, and the wrong side of that flip
# either refuses the read or trains a cohort nobody asked for. So a caller that
# means "newest" says what newest MEANS in that listing.
pick() {
  local op="$1" key="$2" expr="$3" newest="${4:-}" json out
  json="$(qf "$op" --json 2>/dev/null)" || return 1
  out="$(printf '%s' "$json" \
    | OP="$op" KEY="$key" EXPR="$expr" NEWEST="$newest" python3 -c "
import json, os, sys
$NORM_PY
op, key, expr = os.environ['OP'], os.environ['KEY'], os.environ['EXPR']
newest = os.environ['NEWEST']
rows = json.load(sys.stdin).get(op) or []
hit = [r for r in rows if eval(expr, {'r': r, 'norm': norm})]
if hit and newest:
    hit.sort(key=lambda r: eval(newest, {'r': r}))
if hit:
    print(hit[-1][key])
else:
    print('', end='')
    print(f'  nothing in {op} matched: {expr}', file=sys.stderr)
    print(f'  {len(rows)} published:', file=sys.stderr)
    for r in rows:
        flat = '  '.join(f'{k}={v}' for k, v in sorted(r.items())
                         if not isinstance(v, (dict, list)))
        print('    ' + flat, file=sys.stderr)
")" || return 1
  printf '%s' "$out"
}

# Newest = latest snapshot, then highest generation: a re-extract of the SAME
# window (the `generation` bump) shares `as_of_date` with the one it replaces, so
# `as_of_date` alone does not order them.
[ -n "${EXTRACT:-}" ] || EXTRACT="$(pick extracts request_hash \
  "r.get('target') == 'wait_time'" \
  "(r.get('snapshot_start_ts') or '', r.get('generation') or 0)")"
[ -n "$EXTRACT" ] || fail "no wait_time extract published (listing above)"

# `broken` baselines are excluded rather than merely deprioritised: a promoted
# baseline that failed its own checks is not a fallback.
[ -n "${BASELINE:-}" ] || BASELINE="$(pick baselines baseline_hash \
  "not r.get('broken')" "r.get('promoted_at') or ''")"
[ -n "$BASELINE" ] || fail "no promoted baseline (listing above);
    see host/promote-baseline.sh"

# A hex prefix passes through; anything else is a NAME, matched against the
# contract file with `.` and `-` normalised to `_` so `wait_time_v1` finds
# `wait_time.v1.json`.
case "$CONTRACT" in
  *[!0-9a-f]*|"")
    CONTRACT="$(pick contracts contract_hash \
      "norm(r.get('file')) == '$CONTRACT' or norm(r.get('file')).startswith('$CONTRACT')")"
    [ -n "$CONTRACT" ] || fail "no contract matched (listing above). A .json.in
    template is not a contract -- it pins no baseline. See
    host/instantiate-contract.sh"
    ;;
esac

echo "  extract=${EXTRACT:0:12}  baseline=${BASELINE:0:12}  contract=${CONTRACT:0:12}"

# --- 2. the sync that was the actual bug -------------------------------------
# A probe runs the QF-RESEARCH copy of the trainer. A `--from-extract` that
# exists only in the trusted checkout makes argparse exit 2 inside the sandbox,
# which surfaces as `error_class=nonzero_exit` and looks like a crash.
[ -d "$QF_RESEARCH/.git" ] || fail "$QF_RESEARCH is not a git worktree
    (set QF_RESEARCH=<path>)"

if [ "${EXPERIMENT:-0}" = "1" ]; then
  # EXPERIMENT MODE. The default mode rsyncs the trusted trainer OVER
  # `qf-research/trainer/`, which is right for bootstrapping and destructive for
  # an experiment -- it is exactly the agent's edits that are the experiment.
  # So here nothing is copied: whatever is in the worktree is committed, pushed
  # and probed as-is.
  say "experiment: probing $QF_RESEARCH as it stands"
  set_cohort_config
  if [ -n "$(git -C "$QF_RESEARCH" status --porcelain)" ]; then
    git -C "$QF_RESEARCH" status --short | sed 's/^/    /'
    git -C "$QF_RESEARCH" add -A
    git -C "$QF_RESEARCH" -c core.hooksPath=/dev/null \
        commit -q -m "${EXPERIMENT_NOTE:-experiment}" || fail "commit failed"
    echo "  committed"
  else
    echo "  worktree clean; probing HEAD"
  fi
  git -C "$QF_RESEARCH" push -q || fail "push failed"
elif [ "${SKIP_SYNC:-0}" != "1" ]; then
  say "sync trainer + experiment into $QF_RESEARCH"
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
  # AT THE ROOT, not beside the script: this is the file an agent reads on its
  # own, and one two directories down is one nothing reads. Operator-owned like
  # `run_cohort.py`, and overwritten for the same reason -- the loop's rules are
  # not a thing the agent running inside it gets to edit.
  cp "$HERE/research-experiments/AGENTS.md" "$QF_RESEARCH/AGENTS.md"
  set_cohort_config   # after the cp, which would otherwise overwrite it

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

# WHAT THIS RUN IS, recorded ON the run. A run record carries the commit and the
# pinned inputs, and neither says which config trained -- that is a constant
# inside the commit, so telling two experiments apart in a listing meant
# `git show` per run. The note is stored in `jobs.spec_json`, which `qf list
# --json` returns, so putting the config in it is what makes a results table
# possible at all.
#
# READ BACK from the research copy rather than taken from $COHORT_CONFIG: that
# variable is empty on a run that did not change the config, and "unknown" is
# exactly the wrong thing to record about the majority of runs.
CONFIG_IN_USE="$(sed -n 's/^CONFIG = "\(.*\)"$/\1/p' \
  "$QF_RESEARCH/research/experiments/run_cohort.py" | head -1)"
[ -n "$CONFIG_IN_USE" ] || fail "no CONFIG constant in
    $QF_RESEARCH/research/experiments/run_cohort.py -- a run whose config cannot
    be named is a result nobody can attribute"
NOTE="cfg=$CONFIG_IN_USE"
[ -z "${EXPERIMENT_NOTE:-}" ] || NOTE="$NOTE | $EXPERIMENT_NOTE"
echo "  note=$NOTE"

# --- 3. probe -----------------------------------------------------------------
say "probe"
PROBE_OUT="$(qf probe --sha "$SHA" \
  --path research/experiments/run_cohort.py \
  --extract "$EXTRACT" --baseline "$BASELINE" \
  --mem "$PROBE_MEM" --note "$NOTE" --wait 2>&1)"
PROBE_RC=$?
echo "$PROBE_OUT"
# NOT `head -1`. `qf` prints the run id on stdout and the state on stderr, and
# merging the two reorders them (stderr is unbuffered) -- the first version took
# "FAILED exit_code=137" as the run id and then reported "no stderr log" for it.
RUN_ID="$(run_id_of probe "$PROBE_OUT")"

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
# The SAME note on the evaluation. It could be joined through the `judged_run`
# pin instead, but a row that has to be joined to say what it is is a row people
# read wrong.
EVAL_OUT="$(qf evaluate --run "$RUN_ID" --contract "$CONTRACT" \
  --note "$NOTE" --wait 2>&1)"
EVAL_RC=$?
echo "$EVAL_OUT"
EVAL_ID="$(run_id_of evaluate "$EVAL_OUT")"

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
