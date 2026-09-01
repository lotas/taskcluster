#!/usr/bin/env bash
# Tests `tick.sh`'s guards, with both agents and the trusted tools stubbed.
#
# WHY THIS FILE EXISTS. The research part of a tick is the leader's judgement and
# cannot be asserted here. Everything else can, and everything else is what makes
# an unattended loop safe: it must not run twice at once, must stop on PAUSE, must
# stop on budget, and -- the one that matters most -- must NOT record a claim the
# copilot did not agree with. Each of those failures is silent in production:
# the loop looks like it is working right up until a wrong finding is in the
# journal being cited by the next tick.
#
#   ./tests/test_tick.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TICK="$HERE/../research-loop/tick.sh"
[ -x "$TICK" ] || chmod +x "$TICK" 2>/dev/null
[ -f "$TICK" ] || { echo "cannot find $TICK" >&2; exit 2; }

pass=0; fail=0; skip=0
ok()   { echo "ok    $1"; pass=$((pass + 1)); }
bad()  { echo "FAIL  $1"; fail=$((fail + 1)); }
skip() { echo "skip  $1 ($2)"; skip=$((skip + 1)); }

# A NEGATIVE ASSERTION ON A MISSING FILE PROVES NOTHING. `grep -q X missing` and
# `grep -q X present-but-lacking-X` both "succeed" as absences, so every
# must-not-contain check first requires the file to exist and be non-empty.
absent_from() {  # absent_from <file> <pattern> <label>
  if [ ! -s "$1" ]; then
    bad "$3 -- $1 is missing or empty, so the absence proves nothing"
  elif grep -q "$2" "$1"; then
    bad "$3 -- found '$2' in $1"
  else
    ok "$3"
  fi
}
present_in() {  # present_in <file> <pattern> <label>
  if [ ! -s "$1" ]; then
    bad "$3 -- $1 is missing or empty"
  elif grep -q "$2" "$1"; then
    ok "$3"
  else
    bad "$3 -- '$2' not in $1"
  fi
}

# WHETHER `git commit` WORKS HERE AT ALL. Some sandboxes refuse outright
# ("Commits are disabled in devtainer"), which makes the publish half of the tick
# untestable there. Skipped rather than deleted, and skipped LOUDLY: the same
# helper exists in `test_experiment.py` for the same reason, and a silently
# absent assertion about pushing is how an unpushed journal ships.
CAN_COMMIT=1
_probe="$(mktemp -d)"
git init -q -b main "$_probe" 2>/dev/null
: >"$_probe/f"; git -C "$_probe" add -A 2>/dev/null
git -C "$_probe" -c user.name=t -c user.email=t@t commit -qm probe >/dev/null 2>&1 \
  || CAN_COMMIT=0
rm -rf "$_probe"

ROOT="$(mktemp -d)"; trap 'rm -rf "$ROOT"' EXIT

# --------------------------------------------------------------------------
# A world: a fake trusted host dir, a fake qf-research git repo with a remote,
# and stub `claude`/`codex` on PATH whose behaviour each case sets by file.
# --------------------------------------------------------------------------
setup() {  # setup <case-name>
  W="$ROOT/$1"
  mkdir -p "$W/trusted" "$W/bin" "$W/state" "$W/research/journal"
  TODAY="$(date -u +%Y-%m-%d)"

  # results.sh: one scored row, dated by $W/runs_today (so the budget gate can
  # be driven) -- and REAL in shape, because frontier.py parses it for real.
  cat >"$W/trusted/results.sh" <<EOF
#!/usr/bin/env bash
n=\$(cat "$W/runs_today" 2>/dev/null || echo 1)
python3 - "\$n" "$TODAY" <<'PY'
import json, sys
n, today = int(sys.argv[1]), sys.argv[2]
rows = [{"evaluation": f"eval-{i}", "probe": f"probe-{i}",
         "when": f"{today} 0{i}:00", "verdict": "no-go",
         "extract": "e" * 16, "baseline": "b" * 16, "contract": "c" * 16,
         "metrics": {"mae": 225.1}, "passed": {"mae": False},
         "note": "cfg=configs/wait_time.yaml | legacy"} for i in range(n)]
print(json.dumps(rows))
PY
EOF

  cat >"$W/trusted/experiment.py" <<'EOF'
#!/usr/bin/env bash
echo "ok    everything (stub doctor)"
EOF
  chmod +x "$W/trusted/results.sh" "$W/trusted/experiment.py"
  echo "# queue (stub)" >"$W/trusted/../experiment-queue.md" 2>/dev/null || true
  mkdir -p "$W/queue" && echo "# queue (stub)" >"$W/queue/experiment-queue.md"

  # The agent's repo, with a real remote so the push path is exercised.
  git init -q -b main "$W/remote" --bare
  git init -q -b main "$W/research"
  git -C "$W/research" remote add origin "$W/remote"
  echo seed >"$W/research/seed"
  git -C "$W/research" add -A
  git -C "$W/research" -c user.name=t -c user.email=t@t commit -qm seed
  git -C "$W/research" push -q -u origin main

  # `claude`: writes whatever $W/leader_entry holds into PENDING.md, or nothing.
  cat >"$W/bin/claude" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\${QF_REQUIRE_PREREG:-unset}" > "$W/leader_prereg_env"
printf '%s\n' "\$@" > "$W/leader_argv"
cat > "$W/leader_prompt"
[ ! -f "$W/leader_fails" ] || exit 3
if [ -f "$W/leader_entry" ]; then
  cat "$W/leader_entry" > "$W/research/journal/PENDING.md"
fi
python3 - <<'PYAGENT'
import json
print(json.dumps({
    "type": "result", "result": "leader done", "total_cost_usd": 0.0123,
    "usage": {"input_tokens": 1000, "cache_creation_input_tokens": 200,
              "cache_read_input_tokens": 3000, "output_tokens": 50},
}))
PYAGENT
EOF
  # `codex`: replies with whatever $W/codex_reply holds.
  cat >"$W/bin/codex" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$@" > "$W/codex_argv"
cat > "$W/codex_prompt"
[ ! -f "$W/codex_fails" ] || { echo "boom"; exit 4; }
[ ! -f "$W/codex_noise" ] || echo "npm notice: cosmetic stdout noise"
python3 - "$W/codex_reply" <<'PYAGENT'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
reply = path.read_text() if path.exists() else "VERDICT: AGREE\n"
print(json.dumps({"type": "thread.started", "thread_id": "test-thread"}))
print(json.dumps({"type": "item.completed", "item": {
    "id": "item-1", "type": "agent_message", "text": reply}}))
print(json.dumps({"type": "turn.completed", "usage": {
    "input_tokens": 2000, "cached_input_tokens": 1500,
    "output_tokens": 100, "reasoning_output_tokens": 25}}))
PYAGENT
EOF
  # `qf list --json`: the budget's only source. Driven by $W/probes_today and
  # $W/extracts_today, and able to FAIL so the fail-closed path is testable.
  cat >"$W/bin/qf" <<EOF
#!/usr/bin/env bash
[ ! -f "$W/qf_fails" ] || { echo "socket refused" >&2; exit 1; }
p=\$(cat "$W/probes_today" 2>/dev/null || echo 0)
x=\$(cat "$W/extracts_today" 2>/dev/null || echo 0)
python3 - "\$p" "\$x" "$TODAY" <<'PYQF'
import json, sys
p, x, today = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
jobs = [{"run_id": f"probe-{i}", "kind": "probe", "state": "SUCCEEDED",
         "submitted_at": f"{today}T0{i%10}:00:00Z"} for i in range(p)]
jobs += [{"run_id": f"extract-{i}", "kind": "extract", "state": "SUCCEEDED",
          "submitted_at": f"{today}T0{i%10}:00:00Z"} for i in range(x)]
jobs += [{"run_id": "probe-old", "kind": "probe", "state": "SUCCEEDED",
          "submitted_at": "2020-01-01T00:00:00Z"}]
print(json.dumps({"jobs": jobs}))
PYQF
EOF
  chmod +x "$W/bin/claude" "$W/bin/codex" "$W/bin/qf"
  echo "# a claim" >"$W/leader_entry"
}

# TICK_PATH lets a case CONSTRAIN the PATH rather than delete a stub. Deleting
# `$W/bin/codex` does not make codex absent -- this container has a real one on
# PATH, and the tick then invoked it for real and blocked on the network. "The
# copilot is not installed" has to be expressed as a PATH that cannot reach one.
run_tick() {  # run_tick [extra env assignments...]
  env PATH="$W/bin:${TICK_PATH:-$PATH}" \
      HOME="$W" \
      QF_RESEARCH="$W/research" \
      QF_TRUSTED_HOST="$W/trusted" \
      QF_QUEUE_FILE="$W/queue/experiment-queue.md" \
      QF_TICK_STATE="$W/state" \
      "$@" \
      timeout 30 bash "$TICK" 2>&1
  # A HARD TIMEOUT, so a hang in the tick is a failing assertion rather than a
  # test run that never ends. 30s is far above any stubbed path.
}

# --------------------------------------------------------------------------
setup pause
: >"$W/research/PAUSE"
out="$(run_tick)"
if printf '%s' "$out" | grep -q "PAUSE exists"; then
  ok "PAUSE stops the tick"
else
  bad "PAUSE stops the tick -- got: $out"
fi
[ ! -f "$W/research/journal/PENDING.md" ] \
  && ok "PAUSE stops it before the leader runs" \
  || bad "PAUSE stops it before the leader runs"

# --------------------------------------------------------------------------
setup runbudget
# NINE PROBES SUBMITTED, ZERO SCORED. This is the case the old counter missed
# entirely: OOMs, refusals and probes still awaiting evaluation cost real host
# time and scored zero against the cap.
echo 9 >"$W/probes_today"
echo 0 >"$W/runs_today"
out="$(run_tick QF_TICK_MAX_RUNS=4)"
if printf '%s' "$out" | grep -q "probe(s) submitted today"; then
  ok "the budget counts submitted probes, not scored results"
else
  bad "the budget counts submitted probes -- got: $out"
fi
[ ! -e "$W/research/journal/PENDING.md" ] \
  && ok "the budget stops it before the leader runs" \
  || bad "the budget stops it before the leader runs"

# --------------------------------------------------------------------------
setup qfdown
: >"$W/qf_fails"
out="$(run_tick)"
printf '%s' "$out" | grep -q "budget cannot be enforced" \
  && ok "an unreadable job list fails CLOSED" \
  || bad "an unreadable job list fails closed -- got: $out"

# --------------------------------------------------------------------------
setup extractbudget
echo 1 >"$W/extracts_today"
out="$(run_tick QF_TICK_MAX_EXTRACTS=1)"
printf '%s' "$out" | grep -q "extract(s) submitted today" \
  && ok "the extract budget is reported" \
  || bad "the extract budget is reported -- got: $out"
# NOT a stop: the loop may still write up results, only not build a cohort.
printf '%s' "$out" | grep -q "leader done" \
  && ok "a spent extract budget does not stop the tick" \
  || bad "a spent extract budget does not stop the tick -- got: $out"

# --------------------------------------------------------------------------
setup tickbudget
echo 12 >"$W/state/ticks-$(date -u +%Y-%m-%d)"
out="$(run_tick QF_TICK_MAX_TICKS=12)"
printf '%s' "$out" | grep -q "ticks today" \
  && ok "the tick budget stops the tick" \
  || bad "the tick budget stops the tick -- got: $out"

# --------------------------------------------------------------------------
setup lock
# A live tick holds the lock; a second one must exit quietly rather than run.
exec 8>"$W/state/tick.lock"; flock -n 8
out="$(run_tick)"
exec 8>&-
printf '%s' "$out" | grep -q "already running" \
  && ok "a concurrent tick exits quietly" \
  || bad "a concurrent tick exits quietly -- got: $out"

# --------------------------------------------------------------------------
setup noop
rm -f "$W/leader_entry"
out="$(run_tick)"
printf '%s' "$out" | grep -q "NOOP" \
  && ok "a leader that writes nothing is a NOOP" \
  || bad "a leader that writes nothing is a NOOP -- got: $out"
[ ! -e "$W/research/journal/PENDING.md" ] \
  && ok "the empty PENDING.md is cleaned up" \
  || bad "the empty PENDING.md is cleaned up"

# --------------------------------------------------------------------------
setup agree
echo "VERDICT: AGREE" >"$W/codex_reply"
out="$(run_tick QF_CODEX_INPUT_USD_PER_MTOK=2 \
                 QF_CODEX_CACHED_INPUT_USD_PER_MTOK=0.2 \
                 QF_CODEX_OUTPUT_USD_PER_MTOK=10)"
n="$(find "$W/research/journal" -maxdepth 1 -name '2*.md' | wc -l | tr -d ' ')"
[ "$n" = 1 ] && ok "an agreed claim is recorded in the journal" \
             || bad "an agreed claim is recorded in the journal (found $n) -- $out"
[ ! -e "$W/research/journal/PENDING.md" ] \
  && ok "PENDING.md is consumed" || bad "PENDING.md is consumed"
present_in "$W/state/usage.log" "claude.*4.2K tokens.*est ~\\\$0.0123" \
  "Claude usage is appended to the central log"
present_in "$W/state/usage.log" "codex.*2.1K tokens.*input=2000 cached=1500 output=100" \
  "Codex usage is appended with recalculable token categories"
present_in "$W/state/usage.log" "codex.*est ~\\\$0.0023" \
  "configured Codex rates produce an API-equivalent estimate"
if [ "$CAN_COMMIT" = 0 ]; then
  skip "the journal is pushed" "this sandbox refuses git commit"
elif git -C "$W/research" log --oneline origin/main 2>/dev/null | grep -q journal; then
  ok "the journal is pushed"
else
  bad "the journal is pushed -- $out"
fi

# --------------------------------------------------------------------------
setup codexnoise
: >"$W/codex_noise"
echo "VERDICT: AGREE" >"$W/codex_reply"
out="$(run_tick)"
[ -n "$(find "$W/research/journal" -maxdepth 1 -name '2*.md')" ] \
  && ok "stdout noise does not hide a valid Codex verdict" \
  || bad "stdout noise hid a valid Codex verdict -- $out"
present_in "$W/state/usage.log" "codex.*2.1K tokens.*skipped_lines=1" \
  "ignored Codex stdout noise is visible in the usage log"

# --------------------------------------------------------------------------
setup partialrates
echo "VERDICT: AGREE" >"$W/codex_reply"
out="$(run_tick QF_CODEX_INPUT_USD_PER_MTOK=2)"
present_in "$W/state/usage.log" "codex.*est n/a.*rates=partial" \
  "a partial Codex rate configuration explains the missing estimate"

# --------------------------------------------------------------------------
setup disagree
echo "the mae figure is not in the JSON
VERDICT: DISAGREE" >"$W/codex_reply"
out="$(run_tick)"
n="$(find "$W/research/journal/escalations" -name '2*.md' | wc -l | tr -d ' ')"
[ "$n" = 1 ] && ok "a rejected claim becomes an escalation" \
             || bad "a rejected claim becomes an escalation (found $n) -- $out"
m="$(find "$W/research/journal" -maxdepth 1 -name '2*.md' | wc -l | tr -d ' ')"
[ "$m" = 0 ] && ok "a rejected claim is NOT recorded as a finding" \
             || bad "a rejected claim is NOT recorded as a finding (found $m)"
grep -q "NOT RECORDED" "$W/research/journal/escalations/"2*.md \
  && ok "the escalation says it is not a finding" \
  || bad "the escalation says it is not a finding"
grep -q "not in the JSON" "$W/research/journal/escalations/"2*.md \
  && ok "the copilot's reason is kept" || bad "the copilot's reason is kept"

# --------------------------------------------------------------------------
setup noverdict
echo "I have concerns but no conclusion" >"$W/codex_reply"
out="$(run_tick)"
[ -n "$(find "$W/research/journal/escalations" -name '2*.md')" ] \
  && ok "a reply with no VERDICT line is a disagreement" \
  || bad "a reply with no VERDICT line is a disagreement -- $out"

# --------------------------------------------------------------------------
setup bothwords
# A copilot that reasons out loud may name both words; the LAST verdict wins.
echo "at first this looked like VERDICT: DISAGREE territory, but no
VERDICT: AGREE" >"$W/codex_reply"
out="$(run_tick)"
[ -n "$(find "$W/research/journal" -maxdepth 1 -name '2*.md')" ] \
  && ok "the last VERDICT line is the one that counts" \
  || bad "the last VERDICT line is the one that counts -- $out"

# --------------------------------------------------------------------------
setup codexgone
rm -f "$W/bin/codex"
# /usr/bin and /bin hold git, python3, flock and the rest; neither holds codex,
# which lives under a node prefix.
out="$(TICK_PATH=/usr/bin:/bin run_tick)"
[ -n "$(find "$W/research/journal/escalations" -name '2*.md')" ] \
  && ok "no copilot means nothing is recorded" \
  || bad "no copilot means nothing is recorded -- $out"

# --------------------------------------------------------------------------
setup codexcrash
: >"$W/codex_fails"
out="$(run_tick)"
[ -n "$(find "$W/research/journal/escalations" -name '2*.md')" ] \
  && ok "a crashed copilot means nothing is recorded" \
  || bad "a crashed copilot means nothing is recorded -- $out"
present_in "$W/state/usage.log" "codex.*unknown tokens.*exit=4" \
  "a crashed copilot logs its real exit code"

# --------------------------------------------------------------------------
setup leadercrash
: >"$W/leader_fails"
out="$(run_tick)"
[ -z "$(find "$W/research/journal" -maxdepth 1 -name '2*.md')" ] \
  && ok "a crashed leader records nothing" || bad "a crashed leader records nothing"
[ ! -e "$W/research/journal/PENDING.md" ] \
  && ok "a crashed leader leaves no half-written entry" \
  || bad "a crashed leader leaves no half-written entry"
present_in "$W/state/usage.log" "claude.*unknown tokens.*exit=3" \
  "a crashed leader logs its real exit code"

# --------------------------------------------------------------------------
setup autopause
echo "VERDICT: DISAGREE" >"$W/codex_reply"
for _ in 1 2 3; do out="$(run_tick QF_TICK_MAX_DISAGREE=3)"; done
[ -f "$W/research/PAUSE" ] \
  && ok "three consecutive disagreements pause the loop" \
  || bad "three consecutive disagreements pause the loop -- $out"

# --------------------------------------------------------------------------
setup resetstreak
echo "VERDICT: DISAGREE" >"$W/codex_reply"
run_tick QF_TICK_MAX_DISAGREE=3 >/dev/null
echo "VERDICT: AGREE" >"$W/codex_reply"
run_tick QF_TICK_MAX_DISAGREE=3 >/dev/null
echo "VERDICT: DISAGREE" >"$W/codex_reply"
run_tick QF_TICK_MAX_DISAGREE=3 >/dev/null
run_tick QF_TICK_MAX_DISAGREE=3 >/dev/null
[ ! -f "$W/research/PAUSE" ] \
  && ok "an agreement resets the disagreement streak" \
  || bad "an agreement resets the disagreement streak"

# --------------------------------------------------------------------------
setup dryrun
out="$(run_tick QF_DRY=1 2>&1 || true)"
out="$(env PATH="$W/bin:$PATH" HOME="$W" QF_RESEARCH="$W/research" \
        QF_TRUSTED_HOST="$W/trusted" QF_QUEUE_FILE="$W/queue/experiment-queue.md" \
        QF_TICK_STATE="$W/state" bash "$TICK" --dry-run 2>&1)"
printf '%s' "$out" | grep -q "Research frontier" \
  && ok "--dry-run prints the context and invokes nothing" \
  || bad "--dry-run prints the context -- got: $out"

# --------------------------------------------------------------------------
setup preregenv
out="$(run_tick)"
[ "$(cat "$W/leader_prereg_env" 2>/dev/null)" = 1 ] \
  && ok "QF_REQUIRE_PREREG reaches the leader without the systemd unit" \
  || bad "QF_REQUIRE_PREREG reaches the leader (got: $(cat "$W/leader_prereg_env" 2>/dev/null))"

# --------------------------------------------------------------------------
setup freshevidence
# results.sh returns an EXTRA row from its second call onward, standing in for a
# result the leader produced during its own turn. The copilot must be given that
# row -- with the pre-leader snapshot it could only reject the new figures or
# accept them blind.
cat >"$W/trusted/results.sh" <<EOF
#!/usr/bin/env bash
c=\$(cat "$W/results_calls" 2>/dev/null || echo 0)
echo \$((c + 1)) > "$W/results_calls"
python3 - "\$c" "$TODAY" <<'PYR'
import json, sys
c, today = int(sys.argv[1]), sys.argv[2]
rows = [{"evaluation": "evaluate-20260101T000000Z-aaaaaaaaaaaa-1",
         "probe": "probe-20260101T000000Z-aaaaaaaaaaaa-1",
         "when": f"{today} 01:00", "verdict": "no-go",
         "extract": "e" * 16, "baseline": "b" * 16, "contract": "c" * 16,
         "metrics": {"mae": 225.1}, "passed": {"mae": False},
         "note": "cfg=configs/wait_time.yaml | old"}]
if c >= 1:
    rows.append({"evaluation": "evaluate-20260101T000000Z-fffffffffff0-9",
                 "probe": "probe-20260101T000000Z-fffffffffff0-9",
                 "when": f"{today} 02:00", "verdict": "go",
                 "extract": "e" * 16, "baseline": "b" * 16,
                 "contract": "c" * 16,
                 "metrics": {"mae": 171.6}, "passed": {"mae": True},
                 "note": "cfg=configs/qctx.yaml | bar=mae | dir=improve | hyp=new"})
print(json.dumps(rows))
PYR
EOF
chmod +x "$W/trusted/results.sh"
echo "VERDICT: AGREE" >"$W/codex_reply"
out="$(run_tick)"
if grep -q "fffffffffff0" "$W/codex_prompt" 2>/dev/null; then
  ok "the copilot is given evidence refreshed AFTER the leader ran"
else
  bad "the copilot got the pre-leader snapshot -- $out"
fi
grep -q "171.6" "$W/codex_prompt" 2>/dev/null \
  && ok "the new result's numbers are in the copilot's evidence" \
  || bad "the new result's numbers are in the copilot's evidence"

# --------------------------------------------------------------------------
setup staleevidence
# The refresh can fail. It must then be LABELLED, not passed off as current: a
# copilot that cannot tell stale evidence from current has to reject every new
# result.
cat >"$W/trusted/results.sh" <<EOF
#!/usr/bin/env bash
c=\$(cat "$W/results_calls" 2>/dev/null || echo 0)
echo \$((c + 1)) > "$W/results_calls"
[ "\$c" = 0 ] || { echo "transient failure" >&2; exit 1; }
echo '[]'
EOF
chmod +x "$W/trusted/results.sh"
echo "VERDICT: AGREE" >"$W/codex_reply"
out="$(run_tick)"
grep -q "predates the leader" "$W/codex_prompt" 2>/dev/null \
  && ok "a failed refresh is labelled stale for the copilot" \
  || bad "a failed refresh is labelled stale -- $out"

# --------------------------------------------------------------------------
setup onlyverifiedfile
if [ "$CAN_COMMIT" = 0 ]; then
  skip "an edit to an older journal entry is restored, not committed" \
       "this sandbox refuses git commit"
else
  echo "# an older finding, citing probe-20260101T000000Z-aaaaaaaaaaaa-1" \
    >"$W/research/journal/20260101T000000Z.md"
  git -C "$W/research" add -A journal
  git -C "$W/research" -c user.name=t -c user.email=t@t commit -qm "older entry"
  # The leader rewrites history it was not asked to touch, alongside its entry.
  cat >"$W/bin/claude" <<EOF
#!/usr/bin/env bash
echo "# a claim" > "$W/research/journal/PENDING.md"
cat > "$W/leader_prompt"
echo "REWRITTEN" > "$W/research/journal/20260101T000000Z.md"
echo "leader done"
EOF
  chmod +x "$W/bin/claude"
  echo "VERDICT: AGREE" >"$W/codex_reply"
  out="$(run_tick)"
  if grep -q "an older finding" "$W/research/journal/20260101T000000Z.md"; then
    ok "an edit to an older journal entry is restored, not committed"
  else
    bad "an edit to an older journal entry is restored -- $out"
  fi
  printf '%s' "$out" | grep -q "not the leader's to revise" \
    && ok "the unauthorized edit is reported" \
    || bad "the unauthorized edit is reported"
  staged="$(git -C "$W/research" show --stat --name-only --format= HEAD | wc -l | tr -d ' ')"
  [ "$staged" = 1 ] \
    && ok "only the verified file is committed" \
    || bad "only the verified file is committed (got $staged paths)"
fi

# --------------------------------------------------------------------------
setup badfrontier
# results.sh SUCCEEDS on the refresh but emits something frontier.py cannot
# parse. The JSON half of the refresh used to be `|| : >"$2.json"`, which left an
# EMPTY evidence file behind and let the refresh look successful -- so the
# copilot would have been handed `{}` labelled as current, and every cited figure
# would have read as fabricated.
cat >"$W/trusted/results.sh" <<EOF
#!/usr/bin/env bash
c=\$(cat "$W/results_calls" 2>/dev/null || echo 0)
echo \$((c + 1)) > "$W/results_calls"
[ "\$c" = 0 ] || { echo "not json at all"; exit 0; }
python3 - "$TODAY" <<'PYB'
import json, sys
today = sys.argv[1]
print(json.dumps([{
    "evaluation": "evaluate-20260101T000000Z-cccccccccccc-3",
    "probe": "probe-20260101T000000Z-cccccccccccc-3",
    "when": f"{today} 01:00", "verdict": "no-go",
    "extract": "e" * 16, "baseline": "b" * 16, "contract": "c" * 16,
    "metrics": {"mae": 225.1}, "passed": {"mae": False},
    "note": "cfg=configs/wait_time.yaml | old"}]))
PYB
EOF
chmod +x "$W/trusted/results.sh"
echo "VERDICT: AGREE" >"$W/codex_reply"
out="$(run_tick)"
grep -q "predates the leader" "$W/codex_prompt" 2>/dev/null \
  && ok "an unparseable refresh is labelled stale, not passed off as fresh" \
  || bad "an unparseable refresh is labelled stale -- $out"
# THE REGRESSION ITSELF: the old code left an EMPTY json file and the refresh
# looked successful. The fallback must carry the real pre-leader report.
if grep -q "cccccccccccc" "$W/codex_prompt" 2>/dev/null \
   && grep -q '"health"' "$W/codex_prompt" 2>/dev/null; then
  ok "the fallback evidence is the real pre-leader report, not an empty file"
else
  bad "the fallback evidence is the real pre-leader report -- $out"
fi

# --------------------------------------------------------------------------
setup codexagreethencrash
# A copilot that prints AGREE and then EXITS NONZERO. The old code prepended a
# DISAGREE line to the partial output and re-parsed it -- and the tail-wins rule
# then picked the trailing AGREE, publishing an entry whose verification had
# crashed.
cat >"$W/bin/codex" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$@" > "$W/codex_argv"
cat > "$W/codex_prompt"
echo "VERDICT: AGREE"
exit 4
EOF
chmod +x "$W/bin/codex"
out="$(run_tick)"
[ -n "$(find "$W/research/journal/escalations" -name '2*.md')" ] \
  && ok "a copilot that prints AGREE then crashes does NOT publish" \
  || bad "a copilot that prints AGREE then crashes does not publish -- $out"
[ -z "$(find "$W/research/journal" -maxdepth 1 -name '2*.md')" ] \
  && ok "the crashed-copilot entry is not recorded as a finding" \
  || bad "the crashed-copilot entry is not recorded as a finding"

# --------------------------------------------------------------------------
setup verdictinprose
# An unanchored match would accept `VERDICT: AGREE` quoted inside a sentence
# that argues against it.
printf '%s\n' 'I considered writing "VERDICT: AGREE" here but the mae figure is absent.' \
  'VERDICT: DISAGREE' >"$W/codex_reply"
out="$(run_tick)"
[ -n "$(find "$W/research/journal/escalations" -name '2*.md')" ] \
  && ok "a verdict quoted mid-sentence does not count" \
  || bad "a verdict quoted mid-sentence does not count -- $out"

# --------------------------------------------------------------------------
setup extractshim
echo 1 >"$W/extracts_today"
# The leader tries to submit an extract anyway, as an untrusted leader would.
cat >"$W/bin/claude" <<EOF
#!/usr/bin/env bash
cat > "$W/leader_prompt"
qf extract --target wait_time --as-of 2026-07-27T00:00:00Z >/dev/null 2>&1
echo "\$?" > "$W/extract_rc"
qf probe --sha abc --extract c179c7f5b961 >/dev/null 2>&1
echo "\$?" > "$W/probe_rc"
echo "# a claim" > "$W/research/journal/PENDING.md"
echo "leader done"
EOF
chmod +x "$W/bin/claude"
echo "VERDICT: AGREE" >"$W/codex_reply"
out="$(run_tick QF_TICK_MAX_EXTRACTS=1)"
[ "$(cat "$W/extract_rc" 2>/dev/null)" = 3 ] \
  && ok "a spent extract budget MECHANICALLY refuses \`qf extract\`" \
  || bad "a spent extract budget refuses qf extract (rc=$(cat "$W/extract_rc" 2>/dev/null)) -- $out"
[ "$(cat "$W/probe_rc" 2>/dev/null)" = 0 ] \
  && ok "\`qf probe --extract <hash>\` still works under the shim" \
  || bad "qf probe --extract still works (rc=$(cat "$W/probe_rc" 2>/dev/null))"

# --------------------------------------------------------------------------
setup shimoffwhenbudgetleft
echo 0 >"$W/extracts_today"
cat >"$W/bin/claude" <<EOF
#!/usr/bin/env bash
cat > "$W/leader_prompt"
qf extract --target wait_time --as-of 2026-07-27T00:00:00Z >/dev/null 2>&1
echo "\$?" > "$W/extract_rc"
echo "# a claim" > "$W/research/journal/PENDING.md"
echo "leader done"
EOF
chmod +x "$W/bin/claude"
out="$(run_tick QF_TICK_MAX_EXTRACTS=1)"
[ "$(cat "$W/extract_rc" 2>/dev/null)" = 0 ] \
  && ok "with budget left, \`qf extract\` is not shimmed" \
  || bad "with budget left, qf extract is not shimmed (rc=$(cat "$W/extract_rc" 2>/dev/null))"

# --------------------------------------------------------------------------
setup unwritablestate
# The whole directory unwritable: the tick cannot even take its lock. The
# assertion is on the PROPERTY -- the leader never runs -- rather than on which
# guard caught it first.
chmod 500 "$W/state" 2>/dev/null
out="$(run_tick)"
chmod 700 "$W/state" 2>/dev/null
[ ! -s "$W/research/journal/PENDING.md" ] \
  && ok "an unwritable state directory stops the tick before the leader" \
  || bad "an unwritable state directory stops the tick -- got: $out"

# --------------------------------------------------------------------------
setup unwritablecounter
# Directory writable, COUNTER file not: this is the path where the budget would
# silently never accumulate, so the message matters here.
TF="$W/state/ticks-$(date -u +%Y-%m-%d)"
echo 3 >"$TF"; chmod 400 "$TF" 2>/dev/null
out="$(run_tick)"
chmod 600 "$TF" 2>/dev/null
printf '%s' "$out" | grep -q "cannot persist the tick counter" \
  && ok "a tick counter that cannot be written stops the tick" \
  || bad "an unwritable tick counter stops the tick -- got: $out"
[ ! -s "$W/research/journal/PENDING.md" ] \
  && ok "it stops before the leader runs" \
  || bad "it stops before the leader runs"

# --------------------------------------------------------------------------
setup corruptstate
echo "not-a-number" >"$W/state/ticks-$(date -u +%Y-%m-%d)"
out="$(run_tick)"
printf '%s' "$out" | grep -q "unreadable or not a number" \
  && ok "a corrupt tick counter stops the tick instead of reading as zero" \
  || bad "a corrupt tick counter stops the tick -- got: $out"

# --------------------------------------------------------------------------
setup prestaged
if [ "$CAN_COMMIT" = 0 ]; then
  skip "a pre-STAGED journal edit is not committed" "this sandbox refuses git commit"
else
  echo "# an older finding" >"$W/research/journal/20260101T000000Z.md"
  git -C "$W/research" add -A journal
  git -C "$W/research" -c user.name=t -c user.email=t@t commit -qm "older"
  # The leader STAGES its rewrite itself. `git diff` does not report staged
  # changes, and `git commit` commits the whole index.
  cat >"$W/bin/claude" <<EOF
#!/usr/bin/env bash
cat > "$W/leader_prompt"
echo "REWRITTEN" > "$W/research/journal/20260101T000000Z.md"
git -C "$W/research" add journal/20260101T000000Z.md
echo "# a claim" > "$W/research/journal/PENDING.md"
echo "leader done"
EOF
  chmod +x "$W/bin/claude"
  echo "VERDICT: AGREE" >"$W/codex_reply"
  out="$(run_tick)"
  grep -q "an older finding" "$W/research/journal/20260101T000000Z.md" \
    && ok "a pre-STAGED journal edit is restored, not committed" \
    || bad "a pre-staged journal edit is restored -- $out"
  n="$(git -C "$W/research" show --stat --name-only --format= HEAD | wc -l | tr -d ' ')"
  [ "$n" = 1 ] && ok "the commit holds only the verified file" \
               || bad "the commit holds only the verified file (got $n)"
fi

# --------------------------------------------------------------------------
setup nvmonly
# THE REAL 2026-09-01 FAILURE. Both CLIs installed via nvm and reachable from an
# interactive shell; the tick aborted with "no `claude` on PATH". nvm's init is
# in ~/.bashrc, which returns early for non-interactive shells, so `bash -lc`
# never sees it. Here the stubs exist ONLY under $HOME/.nvm and nowhere on PATH.
NVMBIN="$W/.nvm/versions/node/v24.19.0/bin"
mkdir -p "$NVMBIN"
mv "$W/bin/claude" "$NVMBIN/claude"
mv "$W/bin/codex" "$NVMBIN/codex"
echo "VERDICT: AGREE" >"$W/codex_reply"
out="$(TICK_PATH="$W/bin:/usr/bin:/bin" run_tick)"
printf '%s' "$out" | grep -q "no \`claude\` on PATH" \
  && bad "agent-env.sh did not put the nvm bin dir on PATH -- $out" \
  || ok "CLIs installed only under ~/.nvm are found (the 09-01 abort)"
[ -n "$(find "$W/research/journal" -maxdepth 1 -name '2*.md')" ] \
  && ok "the tick completes end to end with nvm-only CLIs" \
  || bad "the tick completes with nvm-only CLIs -- $out"

# --------------------------------------------------------------------------
setup nvmnewest
# Two node versions installed: the NEWEST must win, and `sort -V` is why -- a
# lexical sort puts v9 above v24.
mkdir -p "$W/.nvm/versions/node/v9.0.0/bin" \
         "$W/.nvm/versions/node/v24.19.0/bin"
cp "$W/bin/claude" "$W/.nvm/versions/node/v24.19.0/bin/claude"
cp "$W/bin/codex" "$W/.nvm/versions/node/v24.19.0/bin/codex"
cat >"$W/.nvm/versions/node/v9.0.0/bin/claude" <<EOF
#!/usr/bin/env bash
echo "WRONG NODE VERSION" >&2
exit 9
EOF
chmod +x "$W/.nvm/versions/node/v9.0.0/bin/claude"
rm -f "$W/bin/claude" "$W/bin/codex"
echo "VERDICT: AGREE" >"$W/codex_reply"
out="$(TICK_PATH="$W/bin:/usr/bin:/bin" run_tick)"
# BOTH HALVES. Rejecting only "WRONG NODE VERSION" passes vacuously if claude
# never ran at all -- e.g. if agent detection were removed and the tick aborted.
printf '%s' "$out" | grep -q "WRONG NODE VERSION" \
  && bad "the OLDEST node was chosen -- $out" \
  || ok "the newest installed node wins (sort -V, not lexical)"
[ -n "$(find "$W/research/journal" -maxdepth 1 -name '2*.md')" ] \
  && ok "...and the tick actually completed, so that absence means something" \
  || bad "the tick did not complete, so the version assertion is vacuous -- $out"

# --------------------------------------------------------------------------
setup nvmnewestmissingcli
# THE CASE CODEX FOUND. `nvm install 24` does not migrate global packages, so
# the newest version dir routinely exists WITHOUT the CLIs while an older one
# still has them. Picking newest-by-name then hides an installed CLI.
mkdir -p "$W/.nvm/versions/node/v22.0.0/bin" \
         "$W/.nvm/versions/node/v24.19.0/bin"
mv "$W/bin/claude" "$W/.nvm/versions/node/v22.0.0/bin/claude"
mv "$W/bin/codex" "$W/.nvm/versions/node/v22.0.0/bin/codex"
printf '#!/bin/sh\necho node\n' >"$W/.nvm/versions/node/v24.19.0/bin/node"
chmod +x "$W/.nvm/versions/node/v24.19.0/bin/node"
echo "VERDICT: AGREE" >"$W/codex_reply"
out="$(TICK_PATH="$W/bin:/usr/bin:/bin" run_tick)"
[ -n "$(find "$W/research/journal" -maxdepth 1 -name '2*.md')" ] \
  && ok "a CLI in an OLDER node version is still found" \
  || bad "a CLI in an older node version is found -- $out"

# --------------------------------------------------------------------------
setup nvmsplitversions
# The two CLIs under DIFFERENT node versions -- also legitimate.
mkdir -p "$W/.nvm/versions/node/v22.0.0/bin" \
         "$W/.nvm/versions/node/v24.19.0/bin"
mv "$W/bin/codex" "$W/.nvm/versions/node/v22.0.0/bin/codex"
mv "$W/bin/claude" "$W/.nvm/versions/node/v24.19.0/bin/claude"
echo "VERDICT: AGREE" >"$W/codex_reply"
out="$(TICK_PATH="$W/bin:/usr/bin:/bin" run_tick)"
[ -n "$(find "$W/research/journal" -maxdepth 1 -name '2*.md')" ] \
  && ok "CLIs split across two node versions are both reachable" \
  || bad "CLIs split across node versions are both reachable -- $out"

# --------------------------------------------------------------------------
setup pathnogrowth
AGENT_ENV="$HERE/../research-loop/agent-env.sh"
# tick.sh sources agent-env.sh and the leader inherits PATH; an unguarded
# prepend would grow it every generation.
NVMBIN="$W/.nvm/versions/node/v24.19.0/bin"
mkdir -p "$NVMBIN"
cp "$W/bin/codex" "$NVMBIN/codex"
cat >"$NVMBIN/claude" <<EOF
#!/usr/bin/env bash
# Count how many times the nvm bin dir appears in the inherited PATH.
cat > "$W/leader_prompt"
# SOURCED AGAIN, TWICE, from inside the child -- which is what proves the
# duplicate-prepend guard works. Sourcing once and counting one occurrence would
# still report 1 with the guard deleted.
. "$AGENT_ENV"
. "$AGENT_ENV"
printf '%s' "\$PATH" | tr ':' '\n' | grep -cxF "$NVMBIN" > "$W/path_count"
echo "# a claim" > "$W/research/journal/PENDING.md"
echo "leader done"
EOF
chmod +x "$NVMBIN/claude"
rm -f "$W/bin/claude"
echo "VERDICT: AGREE" >"$W/codex_reply"
out="$(TICK_PATH="$W/bin:/usr/bin:/bin" run_tick)"
[ "$(cat "$W/path_count" 2>/dev/null)" = 1 ] \
  && ok "the nvm bin dir appears exactly once in the leader's PATH" \
  || bad "PATH duplication: count=$(cat "$W/path_count" 2>/dev/null) -- $out"

# --------------------------------------------------------------------------
setup leakednvmdir
# An operator's NVM_DIR carried into the research shell (sudo -E, env_keep, or a
# hand-run tick). Pointing at another home must NOT be honoured, or the symptom
# is "no claude on PATH" for a CLI that is installed.
NVMBIN="$W/.nvm/versions/node/v24.19.0/bin"
# OUTSIDE $HOME on purpose -- that is what makes it a leak. A path UNDER $HOME
# is a legitimately relocated nvm and is honoured (next case).
mkdir -p "$NVMBIN" "$ROOT/otherhome/.nvm/versions/node/v24.19.0/bin"
mv "$W/bin/claude" "$NVMBIN/claude"
mv "$W/bin/codex" "$NVMBIN/codex"
echo "VERDICT: AGREE" >"$W/codex_reply"
out="$(TICK_PATH="$W/bin:/usr/bin:/bin" NVM_DIR="$ROOT/otherhome/.nvm" run_tick)"
[ -n "$(find "$W/research/journal" -maxdepth 1 -name '2*.md')" ] \
  && ok "a leaked NVM_DIR from another home is ignored" \
  || bad "a leaked NVM_DIR is ignored -- $out"

# --------------------------------------------------------------------------
setup relocatednvm
# A legitimately relocated nvm, set by the user's own profile: still honoured,
# because it is under $HOME.
mkdir -p "$W/alt-nvm/versions/node/v24.19.0/bin"
mv "$W/bin/claude" "$W/alt-nvm/versions/node/v24.19.0/bin/claude"
mv "$W/bin/codex" "$W/alt-nvm/versions/node/v24.19.0/bin/codex"
echo "VERDICT: AGREE" >"$W/codex_reply"
out="$(TICK_PATH="$W/bin:/usr/bin:/bin" NVM_DIR="$W/alt-nvm" run_tick)"
[ -n "$(find "$W/research/journal" -maxdepth 1 -name '2*.md')" ] \
  && ok "a relocated nvm under \$HOME is honoured" \
  || bad "a relocated nvm under \$HOME is honoured -- $out"

# --------------------------------------------------------------------------
setup stdinnotargv
# THE PROMPT GOES ON STDIN, NOT IN ARGV. A single argv entry is capped at
# MAX_ARG_STRLEN = 131072 bytes, and the frontier grows with every scored run --
# so argv would eventually fail with E2BIG for a reason nothing in the loop
# explains. Argv is also world-readable in `ps`.
echo "VERDICT: AGREE" >"$W/codex_reply"
out="$(run_tick)"
grep -q "research leader" "$W/leader_prompt" 2>/dev/null \
  && ok "the leader receives the prompt on stdin" \
  || bad "the leader receives the prompt on stdin -- $out"
absent_from "$W/leader_argv" "research leader" \
  "the prompt is not in argv (not visible in ps, no 128KiB cap)"
if grep -qx -- "--output-format" "$W/leader_argv" 2>/dev/null \
   && grep -qx -- "json" "$W/leader_argv" 2>/dev/null; then
  ok "Claude is asked for structured usage output"
else
  bad "Claude is not asked for structured usage output -- $out"
fi
grep -q "Reject the entry" "$W/codex_prompt" 2>/dev/null \
  && ok "the copilot receives its prompt on stdin" \
  || bad "the copilot receives its prompt on stdin"
grep -qx -- "--json" "$W/codex_argv" 2>/dev/null \
  && ok "Codex is asked for structured usage output" \
  || bad "Codex is not asked for structured usage output -- $out"
grep -qx -- "-" "$W/codex_argv" 2>/dev/null \
  && ok "codex is invoked with an explicit \`-\` for stdin" \
  || bad "codex should get an explicit \`-\` (else a piped prompt is wrapped)"
printf '%s' "$out" | grep -qE "leader input: [0-9][0-9]* bytes" \
  && ok "the assembled prompt size is reported" \
  || bad "the assembled prompt size is reported"

# --------------------------------------------------------------------------
setup queuecap
# A queue file far larger than the cap must be trimmed AND the leader told, so a
# missing entry never reads as an entry that does not exist.
python3 - "$W/queue/experiment-queue.md" <<'PYQ'
import sys
with open(sys.argv[1], "w") as fh:
    fh.write("# queue head marker\n")
    fh.write("filler line\n" * 4000)
    fh.write("## THE RANKED LIST IS HERE at the very end\n")
PYQ
out="$(run_tick QF_TICK_MAX_QUEUE_BYTES=2048)"
grep -q "TRUNCATED" "$W/leader_prompt" 2>/dev/null \
  && ok "an oversized queue excerpt is trimmed with a visible notice" \
  || bad "an oversized queue excerpt announces the trim -- $out"
grep -q "queue head marker" "$W/leader_prompt" 2>/dev/null \
  && ok "the head of the queue survives the trim" \
  || bad "the head of the queue survives the trim"
absent_from "$W/leader_prompt" "RANKED LIST IS HERE" \
  "the trim really bounded the excerpt"

# --------------------------------------------------------------------------
setup queuenocap
# Under the cap: passed through whole, with no misleading truncation notice.
# THE SAME FIXTURE as the trim case -- `setup` rebuilds a fresh world each time,
# so without re-creating it this case was asserting the absence of a marker that
# was never written. The added positive assertion is what exposed that.
python3 - "$W/queue/experiment-queue.md" <<'PYQ2'
import sys
with open(sys.argv[1], "w") as fh:
    fh.write("# queue head marker\n")
    fh.write("filler line\n" * 200)
    fh.write("## THE RANKED LIST IS HERE at the very end\n")
PYQ2
out="$(run_tick QF_TICK_MAX_QUEUE_BYTES=65536)"
absent_from "$W/leader_prompt" "TRUNCATED" \
  "a queue under the cap does not claim truncation"
# THE POSITIVE HALF, without which the above passes when the queue -- or the
# whole prompt -- was simply omitted.
present_in "$W/leader_prompt" "RANKED LIST IS HERE" \
  "...and the whole queue really is present"

# --------------------------------------------------------------------------
setup badknobs
# Every numeric knob bounds what the loop may spend, and a malformed bound is
# not a smaller bound -- it is no bound. `head -c bogus` failed silently inside a
# `{ ... }` block that still succeeded, so the leader ran with no queue excerpt
# and a notice claiming otherwise; GNU `head -c -1` means "all but the last
# byte", so a negative value REMOVED the cap it was setting.
for knob in QF_TICK_MAX_QUEUE_BYTES QF_TICK_MAX_RUNS QF_TICK_MAX_TICKS \
            QF_TICK_MAX_EXTRACTS QF_TICK_MAX_DISAGREE; do
  for value in bogus -1 3.5; do
    out="$(run_tick "$knob=$value" 2>&1)"
    if printf '%s' "$out" | grep -q "non-negative integer"; then
      :
    else
      bad "$knob=$value is refused -- got: $(printf '%s' "$out" | head -1)"
      continue 2
    fi
  done
  ok "$knob refuses bogus, negative and fractional values"
done
[ ! -s "$W/research/journal/PENDING.md" ] \
  && ok "a malformed knob stops the tick before the leader runs" \
  || bad "a malformed knob stops the tick before the leader runs"

# --------------------------------------------------------------------------
setup emptyknob
# An EMPTY value is not malformed: `${VAR:-default}` treats it as unset, which is
# the documented shell behaviour and the right one.
echo "VERDICT: AGREE" >"$W/codex_reply"
out="$(run_tick QF_TICK_MAX_QUEUE_BYTES= 2>&1)"
printf '%s' "$out" | grep -q "non-negative integer" \
  && bad "an empty knob should fall back to the default, not abort -- $out" \
  || ok "an empty knob falls back to its default"

echo
echo "pass=$pass fail=$fail skip=$skip"
[ "$fail" = 0 ]
