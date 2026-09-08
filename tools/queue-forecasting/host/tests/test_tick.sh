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
# `grep -q -e "$2"`, NOT `grep -q "$2"`: a pattern beginning with `-` is parsed
# as an OPTION, so `absent_from ... "--label"` "passed" as an absence against a
# file that contained it. Every pattern here is data, never a flag.
absent_from() {  # absent_from <file> <pattern> <label>
  if [ ! -s "$1" ]; then
    bad "$3 -- $1 is missing or empty, so the absence proves nothing"
  elif grep -q -e "$2" "$1"; then
    bad "$3 -- found '$2' in $1"
  else
    ok "$3"
  fi
}
present_in() {  # present_in <file> <pattern> <label>
  if [ ! -s "$1" ]; then
    bad "$3 -- $1 is missing or empty"
  elif grep -q -e "$2" "$1"; then
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
pwd > "$W/leader_cwd"
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
_n=\$(cat "$W/codex_attempts" 2>/dev/null || echo 0); _n=\$((_n + 1))
echo "\$_n" > "$W/codex_attempts"
if [ -f "$W/codex_fail_first_n" ] \
   && [ "\$_n" -le "\$(cat "$W/codex_fail_first_n")" ]; then
  echo "transient boom" >&2; exit 5
fi
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
      QF_TICK_COPILOT_BACKOFF_S="${TEST_BACKOFF:-1}" \
      "$@" \
      timeout 30 bash "$TICK" 2>&1
  # A HARD TIMEOUT, so a hang in the tick is a failing assertion rather than a
  # test run that never ends. 30s is far above any stubbed path.
}

# THE PAUSE ISSUE, STUBBED. `gh` is not installed in this sandbox and there is
# no network, so the wiring -- not the API -- is what these cases assert:
# `test_pause_issue.sh` owns the state table. This stub answers every read from
# one JSON file per kind and logs its argv.
gh_stub() {  # gh_stub  -- after setup, before run_tick
  mkdir -p "$W/gh"
  cat >"$W/bin/gh" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >>"$W/gh.log"
case "\$*" in
  *"/events"*)      f="$W/gh/events.json" ;;
  *"/comments"*)    f="$W/gh/comments.json" ;;
  *"issue list"*)   f="$W/gh/search.json" ;;
  *"issue create"*) echo "https://github.com/lotas/qf-research/issues/77"; exit 0 ;;
  *"label list"*)   f="$W/gh/labels.json" ;;
  *"label create"*) exit 0 ;;
  *)                f="$W/gh/issue.json" ;;
esac
filter=""; prev=""
for a in "\$@"; do [ "\$prev" != "--jq" ] || filter="\$a"; prev="\$a"; done
[ -f "\$f" ] || exit 0
if [ -n "\$filter" ] && command -v jq >/dev/null 2>&1; then
  jq -r "\$filter" <"\$f"
else
  cat "\$f"
fi
EOF
  chmod +x "$W/bin/gh"
  echo '[]' >"$W/gh/search.json"
  echo '[{"name":"qf-pause"}]' >"$W/gh/labels.json"
  echo '[]' >"$W/gh/comments.json"
  printf '{"state":"closed","body":"auto-paused %s: x"}\n' "$PSTAMP" \
    >"$W/gh/issue.json"
  printf '[{"event":"closed","actor":{"login":"lotas"}}]\n' >"$W/gh/events.json"
  echo tok >"$W/pause-token"
  PAUSE_ENV=(QF_PAUSE_ISSUE_REPO=lotas/qf-research
             QF_PAUSE_TOKEN_FILE="$W/pause-token")
}
PSTAMP=20260904T101112Z

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
# AND IT IS AN OUTAGE, NOT A REJECTION. A copilot that is not installed judged
# nothing, so counting this as drift would both accuse the leader and -- via
# `Escalation kind: rejection` -- retire a run nobody ever read.
present_in "$W/research/journal/escalations/"*.md \
  "Escalation kind: verifier-failure" \
  "an uninstalled copilot escalates as an outage, not a rejection"
[ "$(cat "$W/state/consecutive-verifier-failures" 2>/dev/null)" = 1 ] \
  && ok "...and advances the verifier-failure counter" \
  || bad "...and advances the verifier-failure counter (got $(cat "$W/state/consecutive-verifier-failures" 2>/dev/null))"
_d="$(cat "$W/state/consecutive-disagreements" 2>/dev/null)"
[ "${_d:-0}" = 0 ] \
  && ok "...and not the drift counter" \
  || bad "...and not the drift counter (got '$_d')"

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
# WHICH BRAKE FIRED, in the file itself. Drift and a verifier outage need
# different responses from a human, and the PAUSE file is the first thing read.
present_in "$W/research/PAUSE" "consecutive-disagreements" \
  "the PAUSE file names the drift brake"

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
echo x >> "$W/codex_calls"
echo "VERDICT: AGREE"
exit 4
EOF
chmod +x "$W/bin/codex"
out="$(run_tick)"
[ -n "$(find "$W/research/journal/escalations" -name '2*.md')" ] \
  && ok "a copilot that prints AGREE then crashes does NOT publish" \
  || bad "a copilot that prints AGREE then crashes does not publish -- $out"
# NOR IS IT RE-ASKED. It answered and then died; the answer is not trusted, but
# retrying it is shopping for a verdict at the price of a whole invocation.
[ "$(wc -l <"$W/codex_calls" | tr -d ' ')" = 1 ] \
  && ok "a copilot that answered before crashing is not retried" \
  || bad "a copilot that answered before crashing is not retried -- $(wc -l <"$W/codex_calls") call(s)"
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
            QF_TICK_MAX_EXTRACTS QF_TICK_MAX_DISAGREE \
            QF_TICK_MAX_VERIFIER_FAILS; do
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

# --------------------------------------------------------------------------
# THE KNOBS THE TICK IS ACTUALLY USING, AGAINST WHAT THE UNIT DECLARES.
#
# THE BLIND SPOT: `ExecStart=/bin/bash -lc` is load-bearing (the proxy variables
# live in `~/.profile`), so the login profile is read AFTER systemd has set the
# unit's environment -- and a stale `export QF_TICK_MAX_DISAGREE=5` there wins
# at execution time while `unit_matches` and `env_matches`, which compare
# systemd's CONFIGURATION, both report clean. The tick is the only place the
# ACTUAL values exist. Its sibling scan in `phase2-setup.sh` is textual and runs
# at deploy time; this one needs no dot-file parsing and runs hourly, which is
# the interval `~research` is writable over.
systemctl_stub() {  # systemctl_stub <load-state> [declared assignment...]
  # `${*:2}` IS EXPANDED WHILE THE STUB IS WRITTEN (unquoted heredoc), so the
  # declared set is baked in; only `\$*`, the argv log, is deferred to run time.
  cat >"$W/bin/systemctl" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >>"$W/systemctl.log"
echo "LoadState=$1"
echo "Environment=${*:2}"
EOF
  chmod +x "$W/bin/systemctl"
}
# THE LIVE QF_* SET `run_tick` PRODUCES, with no extra assignments: five from
# `run_tick` itself and `QF_REQUIRE_PREREG`, which `tick.sh` exports before it
# looks at anything. Spelled out here so the CLEAN case below is a real
# assertion rather than a stub that happens to declare whatever it likes.
live_qf_set() {  # live_qf_set -- the assignments a bare `run_tick` will have
  printf '%s' "QF_RESEARCH=$W/research QF_TRUSTED_HOST=$W/trusted \
QF_QUEUE_FILE=$W/queue/experiment-queue.md QF_TICK_STATE=$W/state \
QF_TICK_COPILOT_BACKOFF_S=1 QF_REQUIRE_PREREG=1"
}

setup envdrift
# THE FINDING ITSELF: the unit says 3, the tick is running on 5.
systemctl_stub loaded "QF_TICK_MAX_DISAGREE=3 QF_TICK_MAX_RUNS=4"
echo "VERDICT: DISAGREE" >"$W/codex_reply"
out="$(run_tick QF_TICK_MAX_DISAGREE=5)"
printf '%s' "$out" | grep -q "DRIFT QF_TICK_MAX_DISAGREE unit=3 effective=5" \
  && ok "an override of a declared knob is reported with BOTH values" \
  || bad "an override of a declared knob is reported with both values -- $out"
printf '%s' "$out" | grep -q "MISSING QF_TICK_MAX_RUNS (the unit declares it as 4" \
  && ok "...a declared knob the tick does not have is reported too" \
  || bad "...a declared knob the tick does not have is reported -- $out"
# NAMES ONLY FOR THE REST, and this output reaches a journal, a systemd log and
# a GitHub issue: an undeclared key's value is by definition unreviewed.
printf '%s' "$out" | grep -q "UNEXPECTED QF_RESEARCH" \
  && ok "...and an undeclared QF_* key is reported by NAME" \
  || bad "...and an undeclared QF_* key is reported by name -- $out"
printf '%s' "$out" | grep "UNEXPECTED QF_RESEARCH" | grep -qF "$W/research" \
  && bad "an undeclared key's VALUE was printed -- it is unreviewed" \
  || ok "...without its value"
# A REPORT, NOT A REFUSAL: a forgotten override must not block the deploy that
# ships its fix, and a tick that refuses to run is the worse outcome.
[ -e "$W/leader_prompt" ] \
  && ok "...and the tick still runs" || bad "...and the tick still runs -- $out"
# AND IT REACHES THE ALARM. `pause-issue.sh:issue_body` quotes the last three
# escalations by lifting the fence after `## NOT RECORDED`, so this is the only
# seam that puts the delta in front of a human who is not reading the journal --
# which is the 2026-09-04 failure this whole change exists to end.
present_in "$W/research/journal/escalations/"*.md \
  "DRIFT QF_TICK_MAX_DISAGREE unit=3 effective=5" \
  "...and the delta is carried into the escalation, so the issue body has it"
present_in "$W/research/journal/escalations/"*.md "not the copilot" \
  "...attributed to tick.sh, because that block is quoted as the copilot's reason"
[ "$(wc -l <"$W/systemctl.log" | tr -d ' ')" = 1 ] \
  && ok "...from exactly ONE systemctl call, on the hourly path" \
  || bad "...from one systemctl call (made $(wc -l <"$W/systemctl.log") of them)"

# --------------------------------------------------------------------------
setup envclean
# THE ALWAYS-FIRES GUARD, and it is the assertion that keeps this check worth
# reading: a check that reports on every tick is a check that gets ignored,
# which is this family's own failure mode.
systemctl_stub loaded "$(live_qf_set)"
echo "VERDICT: DISAGREE" >"$W/codex_reply"
out="$(run_tick)"
printf '%s' "$out" | grep -q "WARNING the knobs" \
  && bad "a matching environment must report NOTHING -- $out" \
  || ok "an environment that matches the unit reports nothing"
absent_from "$W/research/journal/escalations/"*.md "not the copilot" \
  "...and adds nothing to an ordinary escalation"

# --------------------------------------------------------------------------
setup envunknownbus
# UNKNOWN IS NOT CLEAN. A `systemctl` that cannot answer is a question nobody
# asked, and reporting that as a match is the exact failure being fixed. (This
# container is in that state for real: no systemd as PID 1.)
cat >"$W/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
echo "Failed to connect to bus: Host is down" >&2
exit 1
EOF
chmod +x "$W/bin/systemctl"
echo "VERDICT: DISAGREE" >"$W/codex_reply"
out="$(run_tick)"
printf '%s' "$out" | grep -q "UNVERIFIED" \
  && ok "a systemctl that cannot answer is UNVERIFIED, not clean" \
  || bad "a systemctl that cannot answer is UNVERIFIED -- $out"
printf '%s' "$out" | grep -q "exited 1" \
  && ok "...and its exit status and message are quoted" \
  || bad "...and its exit status is quoted -- $out"
[ -e "$W/leader_prompt" ] \
  && ok "...and the tick still runs" || bad "...and the tick still runs -- $out"
present_in "$W/research/journal/escalations/"*.md "UNVERIFIED" \
  "...and the escalation says the knobs were not confirmed"

# --------------------------------------------------------------------------
setup envunitnotfound
# A UNIT THAT IS NOT LOADED DECLARES NOTHING *THAT THIS CAN SEE*, which is not
# the same fact as "declares nothing" -- and the second reads as "every knob is
# unexpected", a report that would fire on every hand-run forever. `LoadState`
# is asked for in the SAME call precisely to tell them apart.
systemctl_stub not-found ""
echo "VERDICT: DISAGREE" >"$W/codex_reply"
out="$(run_tick)"
printf '%s' "$out" | grep -q "LoadState=not-found" \
  && ok "a unit that is not loaded is UNVERIFIED and names its LoadState" \
  || bad "a unit that is not loaded names its LoadState -- $out"
printf '%s' "$out" | grep -q "UNEXPECTED QF_" \
  && bad "a not-found unit must not report every knob as unexpected -- $out" \
  || ok "...and does not report every knob as unexpected"

# --------------------------------------------------------------------------
setup inflight
# A probe left RUNNING by an earlier tick. The leader must be TOLD -- the tick
# does not hold across an in-flight experiment (the leader is an agent with its
# own tool timeouts, so it submits, returns, and the tick exits), and without
# this action 4 looks available while one is already training.
cat >"$W/bin/qf" <<EOF
#!/usr/bin/env bash
[ "\${1:-}" != "list" ] || python3 - "$TODAY" <<'PYL'
import json, sys
today = sys.argv[1]
print(json.dumps({"jobs": [
  {"run_id": "probe-live", "kind": "probe", "state": "RUNNING",
   "submitted_at": f"{today}T09:00:00Z"},
  {"run_id": "probe-done", "kind": "probe", "state": "SUCCEEDED",
   "submitted_at": f"{today}T08:00:00Z"}]}))
PYL
EOF
chmod +x "$W/bin/qf"
echo "VERDICT: AGREE" >"$W/codex_reply"
out="$(run_tick)"
present_in "$W/leader_prompt" "STILL RUNNING from an earlier tick" \
  "the leader is told when a job is still in flight"
present_in "$W/leader_prompt" "Actions 4 and 5 are unavailable" \
  "...and told not to submit another experiment"

# --------------------------------------------------------------------------
setup noinflight
# Nothing running: the warning must NOT appear, or it would suppress action 4
# on every tick.
echo "VERDICT: AGREE" >"$W/codex_reply"
out="$(run_tick)"
absent_from "$W/leader_prompt" "STILL RUNNING from an earlier tick" \
  "no in-flight warning when nothing is running"

# --------------------------------------------------------------------------
setup inflightbuilding
# BUILDING is not terminal. Its omission from a state set has caused three
# silent bugs in this project already (store.py:27), so it is asserted.
cat >"$W/bin/qf" <<EOF
#!/usr/bin/env bash
[ "\${1:-}" != "list" ] || python3 - "$TODAY" <<'PYB'
import json, sys
print(json.dumps({"jobs": [
  {"run_id": "probe-building", "kind": "probe", "state": "BUILDING",
   "submitted_at": f"{sys.argv[1]}T09:00:00Z"}]}))
PYB
EOF
chmod +x "$W/bin/qf"
echo "VERDICT: AGREE" >"$W/codex_reply"
out="$(run_tick)"
present_in "$W/leader_prompt" "STILL RUNNING from an earlier tick" \
  "a BUILDING job counts as in flight"

# --------------------------------------------------------------------------
# The idle gate. Two ticks on 2026-09-01 cost $2.74 to conclude "the probe is
# still training" and recorded nothing; paying an agent turn to be told to wait
# is the one cost here with no upside.
idle_world() {  # idle_world <case> -- in-flight job, no scored rows
  setup "$1"
  cat >"$W/bin/qf" <<EOF
#!/usr/bin/env bash
[ "\${1:-}" != "list" ] || python3 - "$TODAY" <<'PYI'
import json, sys
print(json.dumps({"jobs": [
  {"run_id": "probe-live", "kind": "probe", "state": "RUNNING",
   "submitted_at": f"{sys.argv[1]}T09:00:00Z"}]}))
PYI
EOF
  chmod +x "$W/bin/qf"
  cat >"$W/trusted/results.sh" <<'EOF'
#!/usr/bin/env bash
echo '[]'
EOF
  chmod +x "$W/trusted/results.sh"
  echo "VERDICT: AGREE" >"$W/codex_reply"
}

idle_world idlegate
out="$(run_tick)"
[ ! -e "$W/leader_prompt" ] \
  && ok "the leader is NOT invoked when the only action is to wait" \
  || bad "the leader was invoked with nothing to do -- $out"
printf '%s' "$out" | grep -q "leader is not invoked" \
  && ok "...and the tick says why" \
  || bad "the tick explains why it skipped -- $out"
[ "$(cat "$W/state/ticks-$TODAY" 2>/dev/null)" = 0 ] \
  && ok "a skipped tick does not consume the daily tick budget" \
  || bad "a skipped tick consumed budget (counter=$(cat "$W/state/ticks-$TODAY" 2>/dev/null))"

# --------------------------------------------------------------------------
idle_world idlegateoverride
out="$(run_tick QF_TICK_ALWAYS_LEAD=1)"
[ -e "$W/leader_prompt" ] \
  && ok "QF_TICK_ALWAYS_LEAD=1 overrides the idle gate" \
  || bad "QF_TICK_ALWAYS_LEAD=1 overrides the idle gate -- $out"

# --------------------------------------------------------------------------
idle_world idlegateunrecorded
# An unwritten scored result is action 1, so the leader IS worth invoking even
# with a probe in flight.
cat >"$W/trusted/results.sh" <<EOF
#!/usr/bin/env bash
python3 - "$TODAY" <<'PYU'
import json, sys
print(json.dumps([{
    "evaluation": "evaluate-20260101T000000Z-aaaaaaaaaaaa-1",
    "probe": "probe-20260101T000000Z-aaaaaaaaaaaa-1",
    "when": f"{sys.argv[1]} 01:00", "verdict": "no-go",
    "extract": "e" * 16, "baseline": "b" * 16, "contract": "c" * 16,
    "metrics": {"mae": 0.26}, "passed": {"mae": True},
    "note": "cfg=configs/x.yaml | unwritten"}]))
PYU
EOF
chmod +x "$W/trusted/results.sh"
out="$(run_tick)"
[ -e "$W/leader_prompt" ] \
  && ok "an unrecorded result still invokes the leader" \
  || bad "an unrecorded result still invokes the leader -- $out"

# --------------------------------------------------------------------------
idle_world idlegatenoflight
# Nothing in flight: actions 4 and 5 are available, so never skip.
cat >"$W/bin/qf" <<'EOF'
#!/usr/bin/env bash
[ "${1:-}" != "list" ] || echo '{"jobs": []}'
EOF
chmod +x "$W/bin/qf"
out="$(run_tick)"
[ -e "$W/leader_prompt" ] \
  && ok "with nothing in flight the leader always runs" \
  || bad "with nothing in flight the leader always runs -- $out"

# --------------------------------------------------------------------------
idle_world idlegatebadjson
# An unreadable frontier must FAIL OPEN. Skipping on a reporting glitch would
# turn it into a silently stalled loop.
cat >"$W/trusted/results.sh" <<'EOF'
#!/usr/bin/env bash
echo 'not json'
EOF
chmod +x "$W/trusted/results.sh"
out="$(run_tick)"
printf '%s' "$out" | grep -q "leader is not invoked" \
  && bad "an unreadable frontier must not skip the leader -- $out" \
  || ok "an unreadable frontier fails open"

# --------------------------------------------------------------------------
# THE EVIDENCE GAP. The tick measures the daily probe count and used to tell only
# the LEADER, so an entry saying "this spends probe 3 of 4" cited a true figure
# the copilot had no way to check -- and correctly escalated it (2026-09-01,
# journal/escalations/20260901T113305Z.md). A fact given to one agent and
# withheld from the other makes a sound entry unrecordable.
setup tickfacts
echo 2 >"$W/probes_today"
echo 1 >"$W/extracts_today"
# A REAL MARKER IN THE QUEUE, so the "copilot does not get the briefing"
# assertions below are refutable. Against the default stub queue they would pass
# without the split existing at all -- the failure mode `absent_from` exists for.
echo "# QUEUE-ONLY-MARKER" >"$W/queue/experiment-queue.md"
out="$(run_tick)"
present_in "$W/leader_prompt" "QUEUE-ONLY-MARKER" \
  "the leader does receive the queue excerpt"
present_in "$W/leader_prompt" "Probes submitted today: 2 of" \
  "the leader is told the probe budget"
present_in "$W/codex_prompt" "Probes submitted today: 2 of" \
  "the copilot is told the SAME probe budget"
present_in "$W/codex_prompt" "Extracts submitted today: 1 of" \
  "the copilot is told the extract budget"
present_in "$W/codex_prompt" "Jobs in flight" \
  "the copilot is told the in-flight count"
# THE ORDINAL, NOT THE PRE-COUNT. `TICKS` is read before the counter is
# persisted as TICKS + 1, so printing it said "0 of 12" during the first tick.
present_in "$W/codex_prompt" "This is tick 1 of" \
  "the tick reports its own ordinal, not the count before it"
# THE LEADER'S CWD IS SET, NOT INHERITED. tick.sh used to set it only at publish
# time, long after both agents had run, so it was whatever the caller had:
# `/` under systemd, the operator's shell under `install.sh once`. Pinning an
# input, not fixing a known failure -- see the block in tick.sh.
if [ "$(cat "$W/leader_cwd" 2>/dev/null)" = "$W/research" ]; then
  ok "the leader runs in the workspace, whatever cwd the tick inherited"
else
  bad "the leader runs in the workspace -- got '$(cat "$W/leader_cwd" 2>/dev/null)'"
fi
# ONLY THE FACTS, NOT THE BRIEFING. Handing the verifier the leader's whole
# context would have it check the entry against the leader's instructions rather
# than against the numbers -- and the queue is the bulkiest part of that.
absent_from "$W/codex_prompt" "QUEUE-ONLY-MARKER" \
  "the copilot does NOT receive the queue excerpt"
absent_from "$W/codex_prompt" "use these exact paths" \
  "the copilot does NOT receive the leader's command list"

# --------------------------------------------------------------------------
# THE COPILOT'S LAST REASON, BACK TO THE LEADER. Without this the leader
# rewrote the same entry against an objection it had never seen -- three rounds
# of that is the auto-PAUSE threshold, so a fixable entry could stop the loop.
setup feedback
# NOTHING TO FEED BACK is the common case on a clean journal, and it must
# produce no block at all rather than an empty heading.
out="$(run_tick)"
absent_from "$W/leader_prompt" "NOT recorded (feedback" \
  "with no previous rejection there is no feedback block"

mkdir -p "$W/research/journal/escalations"
cat >"$W/research/journal/escalations/20260902T190320Z.md" <<'ESC'
# a rejected entry

Evidence:

```
$ results.sh --json
ENTRY-FENCE-MARKER
```

---

## NOT RECORDED — the copilot did not agree

```
COPILOT-REASON-MARKER: the 12-feature delta describes qctx vs qctx_d
```
ESC
out="$(run_tick)"
present_in "$W/leader_prompt" "COPILOT-REASON-MARKER" \
  "the newest rejection's reason reaches the leader"
present_in "$W/leader_prompt" "20260902T190320Z.md" \
  "the leader is told which entry was rejected"
# THE REASON, NOT THE ENTRY. Anchoring on the first fence in the file would have
# quoted the rejected entry's own `Evidence:` block back instead.
absent_from "$W/leader_prompt" "ENTRY-FENCE-MARKER" \
  "the block quotes the reason, not the rejected entry's evidence"
# NEVER THE VERIFIER. Showing a copilot its own previous verdict anchors it; it
# must judge this entry on this tick's numbers.
absent_from "$W/codex_prompt" "COPILOT-REASON-MARKER" \
  "the copilot is NOT shown its own previous verdict"

# --------------------------------------------------------------------------
setup feedbackcap
mkdir -p "$W/research/journal/escalations"
{
  echo "# a rejected entry"
  echo
  echo "## NOT RECORDED — the copilot did not agree"
  echo
  echo '```'
  # ~7KB of reason, well past the 4096-byte cap.
  for i in $(seq 0 599); do printf 'PADLINE-%03d\n' "$i"; done
  echo '```'
} >"$W/research/journal/escalations/20260902T200000Z.md"
out="$(run_tick)"
present_in "$W/leader_prompt" "PADLINE-000" \
  "an overlong reason is still quoted"
absent_from "$W/leader_prompt" "PADLINE-599" \
  "an overlong reason is cut at the cap"
present_in "$W/leader_prompt" "TRUNCATED at 4096 bytes" \
  "the leader is told the reason was cut"

# --------------------------------------------------------------------------
# A COPILOT THAT COULD NOT START IS RETRIED. It has died on `Failed to load
# cloud config bundle` twice for two different reasons, and each time an entry
# that might have been recordable escalated instead.
setup copilotretry
echo 2 >"$W/codex_fail_first_n"
out="$(run_tick)"
[ "$(cat "$W/codex_attempts" 2>/dev/null)" = 3 ] \
  && ok "a crashed copilot is retried" \
  || bad "a crashed copilot is retried -- got $(cat "$W/codex_attempts" 2>/dev/null) attempt(s): $out"
[ -n "$(find "$W/research/journal" -maxdepth 1 -name '2*.md')" ] \
  && ok "the retry's verdict is the one that counts" \
  || bad "the retry's verdict is the one that counts -- $out"

# ONLY A CRASH IS RETRIED. A copilot that answered DISAGREE has ANSWERED;
# re-asking until it says something else is shopping for a verdict.
setup copilotnoretry
echo "VERDICT: DISAGREE" >"$W/codex_reply"
out="$(run_tick)"
[ "$(cat "$W/codex_attempts" 2>/dev/null)" = 1 ] \
  && ok "a copilot that disagreed is NOT re-asked" \
  || bad "a copilot that disagreed is NOT re-asked -- $(cat "$W/codex_attempts" 2>/dev/null) attempts"

# A COPILOT THAT IS DOWN, not merely flaky, MUST STILL PAUSE THE LOOP. The retry
# above absorbs the transient case; what survives three attempts is a verifier
# that cannot verify, and a loop that keeps spending a leader turn an hour while
# recording nothing is the thing PAUSE exists to stop.
#
# ON ITS OWN COUNTER SINCE TASK 6. This case used to seed
# `consecutive-disagreements` and assert it reached 3: one counter absorbed both
# a drifting leader and a dead verifier. That is unsafe once a repeated target
# stops advancing drift, so the outage now has its own brake -- and it must not
# borrow the drift counter's streak to reach the threshold.
setup copilotstreak
mkdir -p "$W/state"; echo 2 >"$W/state/consecutive-verifier-failures"
: >"$W/codex_fails"
out="$(run_tick QF_TICK_MAX_VERIFIER_FAILS=3)"
[ -n "$(find "$W/research/journal/escalations" -name '2*.md')" ] \
  && ok "a copilot that never ran still escalates the entry" \
  || bad "a copilot that never ran still escalates the entry -- $out"
[ "$(cat "$W/state/consecutive-verifier-failures")" = 3 ] \
  && ok "exhausted copilot retries advance the verifier-failure streak" \
  || bad "exhausted copilot retries advance the verifier streak -- got $(cat "$W/state/consecutive-verifier-failures")"
[ -f "$W/research/PAUSE" ] \
  && ok "a copilot that stays down pauses the loop" \
  || bad "a copilot that stays down pauses the loop -- $out"
_d="$(cat "$W/state/consecutive-disagreements" 2>/dev/null)"
[ "${_d:-0}" = 0 ] \
  && ok "a dead copilot is never counted as leader drift" \
  || bad "a dead copilot was counted as drift (got '$_d')"
present_in "$W/state/usage.log" "codex.*exit=4" \
  "every failed copilot attempt is charged to the cost log"

# --------------------------------------------------------------------------
# TWO BRAKES, BECAUSE THERE ARE TWO FAILURES (design §2). One counter used to
# absorb both a drifting leader and a dead verifier, and under same-target
# suppression that is unsafe: a `codex` outage on a resolvable target would stop
# advancing the streak and the loop would burn a leader turn an hour forever.
#
# CANONICAL IDS ARE NOT FREE HERE. `setup`'s scoreboard stub emits `eval-0`,
# which is not an `evaluate-<stamp>-<sha>-<seq>` id and therefore can NEVER be a
# suppressible target -- every assertion below would have passed vacuously
# against it. So each case that needs a resolvable target rebuilds the stub with
# real ids, the same way `idle_world` rebuilds it with none.
E1=evaluate-20260904T090941Z-0a217f40da8f-6446
E2=evaluate-20260905T101010Z-0b318f51eb9a-7001

canon_results() {  # canon_results <evaluation id>... -- rewrite the scoreboard
  local json
  json="$(python3 - "$TODAY" "$@" <<'PYC'
import json, sys
today = sys.argv[1]
print(json.dumps([
    {"evaluation": ev, "probe": ev.replace("evaluate-", "probe-"),
     "when": f"{today} 0{i}:00", "verdict": "no-go",
     "extract": "e" * 16, "baseline": "b" * 16, "contract": "c" * 15 + str(i),
     "metrics": {"mae": 225.1 + i}, "passed": {"mae": False},
     "note": f"cfg=configs/wait_time.yaml | legacy {i}"}
    for i, ev in enumerate(sys.argv[2:])]))
PYC
)"
  { echo '#!/usr/bin/env bash'; echo "cat <<'JSONEOF'"; echo "$json"
    echo JSONEOF; } >"$W/trusted/results.sh"
  chmod +x "$W/trusted/results.sh"
}

entry_for() {  # entry_for <target value> -- what the stub leader will write
  printf '# a claim\n\n**Target run:** %s\n' "$1" >"$W/leader_entry"
}

state_is() {  # state_is <basename> <expected|MISSING> <label>
  local got
  got="$(cat "$W/state/$1" 2>/dev/null)" || got=MISSING
  [ -n "$got" ] || got=MISSING
  if [ "$got" = "$2" ]; then ok "$3"; else bad "$3 -- $1 is '$got', wanted '$2'"; fi
}

# --- the target helpers and the suppressible predicate --------------------
# SOURCED OUT OF `tick.sh`, the way `test_unit_drift.sh` sources `unit_matches`
# out of `phase2-setup.sh`. These four are pure functions of their arguments, so
# a whole stubbed tick would only obscure which one was wrong.
# shellcheck disable=SC1090
source <(sed -n '/^target()/,/^}/p; /^set_target()/,/^}/p;
                 /^entry_target()/,/^}/p;
                 /^handling_of()/,/^}/p; /^suppressible()/,/^}/p;
                 /^take_retry_target()/,/^}/p;
                 /^retry_row()/,/^}/p;
                 /^env_selfcheck()/,/^}/p' "$TICK")
# `say` IS NOT EXTRACTED -- it is a one-liner whose closing brace shares its
# line, so the `/^}/` range above would swallow the rest of the file. The
# extracted functions only use it to explain themselves, so a stub is enough --
# but it prints to STDERR here on purpose, because that is where `take_retry_target`
# must send it: in `tick.sh` the call site is a command substitution inside the
# group redirected into `context.md`, so a diagnostic on stdout would be
# captured as part of the target id and vanish from the tick log.
# ITS OUTPUT GOES TO STDOUT, exactly like `tick.sh`'s. A stub that helpfully
# wrote to stderr would make `say "..." >&2` and a bare `say "..."` behave
# identically here, and the assertions below about which stream a diagnostic
# lands on would pass with the redirection deleted -- which is the defect the
# plan's reference code actually had.
say() { printf '[stub say] %s\n' "$*"; }
# `entry_target` READS `$HERE`, which in this file is the tests directory, so
# every call below states which directory it is resolving `frontier.py` from --
# including one that deliberately cannot find it.
RL="$HERE/../research-loop"
for fn in target set_target entry_target handling_of suppressible take_retry_target retry_row; do
  declare -F "$fn" >/dev/null \
    || { echo "extraction missed $fn (is it still a top-level function?)" >&2; \
         bad "$fn can be extracted from tick.sh"; }
  # AN `if declare -F` GUARD MAKES A MISSING FUNCTION A SILENT SKIP, not a
  # failure: the unit block for it would simply not run and the count would
  # still read pass. So absence is asserted here, once, by name.
done
for fn in target set_target entry_target handling_of suppressible take_retry_target retry_row; do
  declare -F "$fn" >/dev/null && ok "$fn is extractable from tick.sh" \
                              || bad "$fn is extractable from tick.sh"
done

PRED="$ROOT/predicate"; mkdir -p "$PRED"
# NO `systemctl` AT ALL, which the stubbed ticks above cannot express: every
# PATH they can be given still reaches the real one in /usr/bin, and deleting a
# stub does not make a binary absent. Asserted on the function directly, in a
# SUBSHELL -- `PATH= env_selfcheck` would leave PATH empty for the rest of this
# file, because a variable assignment before a shell FUNCTION persists.
_es="$( (PATH=; env_selfcheck) 2>&1 )"; _esrc=$?
[ "$_esrc" != 0 ] \
  && ok "no systemctl on PATH is not a clean environment check" \
  || bad "no systemctl on PATH returned clean (rc=$_esrc, said '$_es')"
printf '%s' "$_es" | grep -q "UNVERIFIED (no .systemctl. on PATH" \
  && ok "...and it says the unit's declaration is UNKNOWN, not clean" \
  || bad "...and it says so -- got '$_es'"
printf '%s' "$_es" | grep -q "qf-tick.service" \
  && ok "...naming the unit it could not ask" \
  || bad "...naming the unit it could not ask -- got '$_es'"
unset _es _esrc

T="$PRED/last-reject-target"

if declare -F set_target >/dev/null && declare -F target >/dev/null; then
  set_target "$T" "$E1" && [ "$(target "$T")" = "$E1" ] \
    && ok "set_target/target round-trip a canonical evaluation id" \
    || bad "set_target/target round-trip a canonical evaluation id"

  set_target "$T" none && [ "$(target "$T")" = none ] \
    && ok "none round-trips" || bad "none round-trips"

  # A PROBE ID IS NOT A TARGET. `frontier.py`'s index maps an id to a LIST of
  # rows because one probe can be scored under two contracts, so a probe id does
  # not identify a row and suppressing on it would be unbounded.
  printf 'probe-20260830T202842Z-4a2ae967d664-5418\n' >"$T"
  target "$T" >/dev/null && bad "a probe id must not validate" \
                         || ok "a probe id must not validate"

  printf '%s\n%s\n' "$E1" "$E1" >"$T"
  target "$T" >/dev/null && bad "a multi-line target must not validate" \
                         || ok "a multi-line target must not validate"

  printf 'not-an-id\n' >"$T"
  target "$T" >/dev/null && bad "a malformed target must not validate" \
                         || ok "a malformed target must not validate"

  printf '%s-trailing junk\n' "$E1" >"$T"
  target "$T" >/dev/null && bad "a target with trailing text must not validate" \
                         || ok "a target with trailing text must not validate"

  : >"$T"
  target "$T" >/dev/null && bad "an empty target must not validate" \
                         || ok "an empty target must not validate"

  # AN UNREADABLE TARGET COUNTS AS CHANGED, which is the safe direction: the
  # streak increments and the loop fails towards the pause.
  printf '%s\n' "$E1" >"$T"; chmod 000 "$T" 2>/dev/null
  target "$T" >/dev/null && bad "an unreadable target must not validate" \
                         || ok "an unreadable target must not validate"
  chmod 600 "$T" 2>/dev/null

  rm -f "$T"
  [ "$(target "$T")" = none ] \
    && ok "a target that was never written reads as none" \
    || bad "a target that was never written reads as none"

  # NO `2>/dev/null` HERE ON PURPOSE: the helper must silence its own failed
  # redirection, and it could not while the `2>/dev/null` sat AFTER the
  # redirection it meant to cover.
  _st="$(set_target "$PRED/nodir/x" v 2>&1)" \
    && bad "set_target must fail when it cannot persist" \
    || ok "set_target fails when it cannot persist"
  [ -z "$_st" ] \
    && ok "...quietly, without shell noise in the tick log" \
    || bad "...quietly, without shell noise in the tick log -- got '$_st'"

  # ONE PREDICATE, TWO IMPLEMENTATIONS -- so they are compared. `target`'s ERE
  # hand-rolls `frontier.py:_EVAL_ID`, and the whole justification for routing
  # entry parsing through `frontier.target_of` is that the two must never
  # disagree. A divergence here would be silent and one-directional: if
  # `_EVAL_ID` loosened, `entry_target` would hand back an id `target` refuses
  # to read, `STORED` would always be empty, suppression would never apply, and
  # the spurious pause would be back with nothing in the log.
  for candidate in "$E1" "probe-20260830T202842Z-4a2ae967d664-5418" "not-an-id" \
                   "$E1-trailing junk" "evaluate-x-ZZZ-1"; do
    printf '%s\n' "$candidate" >"$T"
    if target "$T" >/dev/null 2>&1; then _mine=accept; else _mine=reject; fi
    _theirs="$(python3 - "$RL" "$candidate" <<'PYEV'
import sys
sys.path.insert(0, sys.argv[1])
import frontier
print("accept" if frontier._EVAL_ID.match(sys.argv[2]) else "reject")
PYEV
)"
    [ "$_mine" = "$_theirs" ] \
      && ok "target agrees with frontier._EVAL_ID on '$candidate'" \
      || bad "target says $_mine, frontier._EVAL_ID says $_theirs, on '$candidate'"
  done

  # --- entry_target, the bridge that must not disagree with the frontier ----
  printf '# a claim\n\n**Target run:** %s\n' "$E1" >"$PRED/entry.md"
  [ "$(HERE="$RL" entry_target "$PRED/entry.md")" = "$E1" ] \
    && ok "entry_target reads a declared target through frontier.target_of" \
    || bad "entry_target reads a declared target through frontier.target_of"
  # TWO DISAGREEING LINES: `target_of` refuses to resolve by position, so this
  # must be `none` here too -- the frontier retires nothing for such a file, and
  # suppressing on it would be unbounded.
  printf '**Target run:** %s\n\n**Target run:** %s\n' "$E1" "$E2" \
    >"$PRED/entry.md"
  [ "$(HERE="$RL" entry_target "$PRED/entry.md")" = none ] \
    && ok "entry_target refuses two disagreeing target lines" \
    || bad "entry_target refuses two disagreeing target lines"
  # A BROKEN IMPORT CHAIN MUST NOT BE SILENT. `frontier.py` reaches `prereg` and
  # `experiment` under `host/`; if that breaks, every rejection reads as `none`,
  # suppression is off, and three ticks later the loop pauses blaming the leader
  # for drift -- the exact 2026-09-04 misdiagnosis. So it says so on stderr,
  # which is uncaptured at the call site and therefore reaches the tick log.
  _et="$(HERE="$PRED" entry_target "$PRED/entry.md" 2>&1 >/dev/null)"
  printf '%s' "$_et" | grep -q "entry_target could not resolve" \
    && ok "entry_target says why it gave up" \
    || bad "entry_target says why it gave up -- got '$_et'"
  [ "$(HERE="$PRED" entry_target "$PRED/entry.md" 2>/dev/null)" = none ] \
    && ok "...and still answers none, which increments" \
    || bad "...and still answers none, which increments"
fi

# `handling_of` reads the frontier JSON the tick built. FAKED HERE rather than
# grown through five real journal states, because this asserts one JSON->string
# mapping and the five states are asserted end to end below where they matter.
cat >"$PRED/f.json" <<JSON
{"series":[{"rows":[
  {"evaluation":"$E1","handling":"unrecorded"},
  {"evaluation":"eRec","handling":"recorded"},
  {"evaluation":"eRet","handling":"retired"}]},
 {"rows":[
  {"evaluation":"eTwice","handling":"unrecorded"}]},
 {"rows":[
  {"evaluation":"eTwice","handling":"unrecorded"}]}]}
JSON
if declare -F handling_of >/dev/null; then
  [ "$(handling_of "$PRED/f.json" "$E1")" = unrecorded ] \
    && ok "an unrecorded row resolves" || bad "an unrecorded row resolves"
  [ "$(handling_of "$PRED/f.json" eRec)" = recorded ] \
    && ok "a recorded row resolves" || bad "a recorded row resolves"
  [ "$(handling_of "$PRED/f.json" eRet)" = retired ] \
    && ok "a retired row resolves" || bad "a retired row resolves"
  # ACROSS SERIES, not only within one: one probe scored under two contracts
  # lands in two different series entries, which is the shape that actually
  # produces an ambiguous id.
  [ "$(handling_of "$PRED/f.json" eTwice)" = ambiguous ] \
    && ok "one id in two series rows is ambiguous" \
    || bad "one id in two series rows is ambiguous"
  [ "$(handling_of "$PRED/f.json" eGone)" = absent ] \
    && ok "an id off the scoreboard is absent" \
    || bad "an id off the scoreboard is absent"
  [ "$(handling_of "$PRED/nosuch.json" "$E1" 2>/dev/null)" = absent ] \
    && ok "an unreadable frontier resolves nothing" \
    || bad "an unreadable frontier resolves nothing"
  # AND SAYS SO. `absent` is the right PREDICATE answer -- it is not
  # suppressible, so the streak advances -- but the caller's log line then reads
  # "is absent: not suppressible" when the truth is "the frontier did not
  # parse". The predicate stays; the diagnostic is what was missing.
  _ho="$(handling_of "$PRED/nosuch.json" "$E1" 2>&1 >/dev/null)"
  printf '%s' "$_ho" | grep -q "handling_of could not read" \
    && ok "...and says a parse failure is not an off-the-scoreboard id" \
    || bad "...and says a parse failure is not an off-the-scoreboard id -- '$_ho'"
fi

if declare -F suppressible >/dev/null; then
  for state in recorded retired ambiguous absent; do
    suppressible "$state" && bad "$state must not suppress" \
                          || ok "$state must not suppress"
  done
  suppressible unrecorded && ok "unrecorded suppresses" \
                          || bad "unrecorded suppresses"
fi

# --- take_retry_target: the retry decision, made BEFORE the leader runs --------
# THE SAME PREDICATE THE STREAK USED (design §3.1), and that is the whole point:
# if the two disagreed the tick could suppress a drift streak for a target it
# then refuses to instruct the leader to retry -- a rejection that costs a tick
# and teaches the loop nothing.
#
# CANONICAL IDS FOR EVERY STATE, not the short `eRec`/`eRet` names the plan's
# reference test used. Those are not `evaluate-<stamp>-<sha>-<seq>` ids, so
# `target` refuses them and `take_retry_target` returns at its FIRST branch: the
# recorded/retired/ambiguous cases would all have passed without the handling
# check existing at all.
ER_RET=evaluate-20260906T111213Z-0c419062fcab-7002
ER_AMB=evaluate-20260906T142233Z-0d51a173adbc-7003
ER_ABS=evaluate-20260907T031415Z-0e62b284becd-7004
cat >"$PRED/retry.json" <<JSON
{"series":[{"rows":[
  {"evaluation":"$E1","handling":"unrecorded"},
  {"evaluation":"$E2","handling":"recorded"},
  {"evaluation":"$ER_RET","handling":"retired"}]},
 {"rows":[{"evaluation":"$ER_AMB","handling":"unrecorded"}]},
 {"rows":[{"evaluation":"$ER_AMB","handling":"unrecorded"}]}]}
JSON

if declare -F take_retry_target >/dev/null; then
  set_target "$T" "$E1"
  _rt="$(take_retry_target "$PRED/retry.json" "$T" 2>/dev/null)"
  [ "$_rt" = "$E1" ] \
    && ok "take_retry_target returns a stored unrecorded target" \
    || bad "take_retry_target returns a stored unrecorded target -- got '$_rt'"
  state_target_is() {  # state_target_is <expected> <label>
    local got
    got="$(cat "$T" 2>/dev/null)" || got=UNREADABLE
    [ "$got" = "$1" ] && ok "$2" || bad "$2 -- the file holds '$got', wanted '$1'"
  }
  state_target_is "$E1" "...and leaves the stored target in place"

  # EVERY NON-SUPPRESSIBLE STATE CLEARS THE STORED TARGET, so the next rejection
  # is counted as a NEW episode rather than suppressed against a target the
  # leader was never told to retry.
  for _pair in "recorded:$E2" "retired:$ER_RET" "ambiguous:$ER_AMB" \
               "absent:$ER_ABS"; do
    _state="${_pair%%:*}"; _id="${_pair#*:}"
    set_target "$T" "$_id" \
      || bad "the $_state fixture id is not storable (fix the test, not tick.sh)"
    take_retry_target "$PRED/retry.json" "$T" >/dev/null 2>&1 \
      && bad "a $_state target must not enter retry mode" \
      || ok "a $_state target must not enter retry mode"
    state_target_is none "...and a $_state target is cleared from the file"
  done

  set_target "$T" none
  take_retry_target "$PRED/retry.json" "$T" >/dev/null 2>&1 \
    && bad "a stored none must not enter retry mode" \
    || ok "a stored none must not enter retry mode"

  # THE `none` GUARD RETURNS BEFORE ANYTHING IS RESOLVED OR WRITTEN, and that
  # is all it does: deleting it still refuses the retry, because `none` resolves
  # to `absent` and `absent` is not suppressible. What it prevents is a target
  # file CREATED holding `none` on a fresh state directory, and a "the stored
  # retry target none is absent; cleared" line in the log of every ordinary
  # tick. So those are what is asserted.
  rm -f "$T"
  _rtnone="$(take_retry_target "$PRED/retry.json" "$T" 2>&1)"
  [ ! -e "$T" ] \
    && ok "a never-written target file is not created by the retry decision" \
    || bad "a never-written target file is not created by the retry decision"
  [ -z "$_rtnone" ] \
    && ok "...and an absent-or-none stored target produces no diagnostic at all" \
    || bad "...and an absent-or-none stored target produces no diagnostic -- '$_rtnone'"

  # A MALFORMED STORED TARGET IS ALSO NOT A RETRY, and it is cleared: `target`
  # fails before anything can be resolved, and leaving the bad value behind
  # would make every later tick re-derive the same failure.
  printf 'not-an-id\n' >"$T"
  take_retry_target "$PRED/retry.json" "$T" >/dev/null 2>&1 \
    && bad "a malformed stored target must not enter retry mode" \
    || ok "a malformed stored target must not enter retry mode"
  state_target_is none "...and a malformed stored target is cleared"
  # AND IT SAYS SO. This was the one silent branch: the sibling non-suppressible
  # path names the state that disqualified the target, while a corrupted target
  # file made the retry simply not happen, with nothing in the log.
  printf 'not-an-id\n' >"$T"
  _rtbad="$(take_retry_target "$PRED/retry.json" "$T" 2>&1 >/dev/null)"
  printf '%s' "$_rtbad" | grep -q "unreadable or malformed" \
    && ok "...and a malformed stored target is diagnosed, not silently dropped" \
    || bad "...and a malformed stored target is diagnosed -- got '$_rtbad'"

  # THE DIAGNOSTIC GOES TO STDERR. The call site captures stdout to read the id,
  # so a `say` on stdout would be appended to the id -- and in `tick.sh` that
  # substitution sits inside the group redirected into `context.md`, so the line
  # would have been silently absent from the tick log.
  set_target "$T" "$ER_ABS"
  _rtout="$(take_retry_target "$PRED/retry.json" "$T" 2>/dev/null)"
  [ -z "$_rtout" ] \
    && ok "take_retry_target prints nothing on stdout when it refuses" \
    || bad "take_retry_target printed '$_rtout' on stdout when it refused"
  set_target "$T" "$ER_ABS"
  _rterr="$(take_retry_target "$PRED/retry.json" "$T" 2>&1 >/dev/null)"
  printf '%s' "$_rterr" | grep -q "absent" \
    && ok "...and says on stderr why it refused" \
    || bad "...and says on stderr why it refused -- got '$_rterr'"
fi

# --- retry_row: the row the leader is PASTED, because it never gets the JSON --
# THE LEADER'S PROMPT IS `tick-prompt.md` + `context.md`, and `context.md`
# embeds the MARKDOWN render; `frontier.json` goes to the copilot alone. So a
# template citing `config_digest`, `vs`, `tol` and a full-precision metric
# mandated fields the leader could not fill -- the markdown row table carries
# none of them and its only figure is a `%.4g` cell for the WINNING config.
ER_NOVS=evaluate-20260907T040506Z-0f73c395cfde-7005
ER_BADVS=evaluate-20260907T050607Z-1a84d4a6d0ef-7006
ER_PEER=evaluate-20260801T010203Z-deadbeefdead-7000
cat >"$PRED/row.json" <<JSON
{"series":[
 {"holdout":["2026-08-01","2026-08-15"],
  "rows":[{"evaluation":"$E1","handling":"unrecorded","config_digest":"0a217f40da8f",
           "vs":"probe-20260801T010203Z-deadbeefdead-7000","tol":0.005,
           "bar":"p90_miss_tail","claim":"kept","direction":"improve",
           "metrics":{"p90_miss_tail":0.280830734}},
          {"evaluation":"$ER_PEER",
           "probe":"probe-20260801T010203Z-deadbeefdead-7000",
           "handling":"recorded","bar":"p90_miss_tail",
           "metrics":{"p90_miss_tail":0.298876182}},
          {"evaluation":"$ER_NOVS","handling":"unrecorded","vs":"",
           "metrics":{"p90_miss_tail":0.31}},
          {"evaluation":"$ER_BADVS","handling":"unrecorded",
           "vs":"evaluate-20200101T000000Z-000000000000-1",
           "metrics":{"p90_miss_tail":0.32}}]},
 {"holdout":null,"rows":[{"evaluation":"$ER_AMB","handling":"unrecorded"}]},
 {"holdout":null,"rows":[{"evaluation":"$ER_AMB","handling":"unrecorded"}]}]}
JSON
{"series":[
 {"holdout":["2026-08-01","2026-08-15"],
  "rows":[{"evaluation":"$E1","handling":"unrecorded","config_digest":"0a217f40da8f",
           "vs":"configs/wait_time.yaml","tol":0.005,"bar":"p90_miss_tail",
           "metrics":{"p90_miss_tail":0.280830734}}]},
 {"holdout":null,"rows":[{"evaluation":"$ER_AMB","handling":"unrecorded"}]},
 {"holdout":null,"rows":[{"evaluation":"$ER_AMB","handling":"unrecorded"}]}]}
JSON
if declare -F retry_row >/dev/null; then
  _row="$(retry_row "$PRED/row.json" "$E1" 2>/dev/null)"
  printf '%s' "$_row" | python3 -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null \
    && ok "retry_row emits parseable JSON" \
    || bad "retry_row emits parseable JSON -- got '$_row'"
  # `vs` IS A RUN ID, not a config path: `judge_claim` resolves it through an
  # index keyed by evaluation and probe. The fixture asserted a path here, which
  # is a shape `frontier.py` can never produce.
  for _want in '"config_digest": "0a217f40da8f"' \
               '"vs": "probe-20260801T010203Z-deadbeefdead-7000"' \
               '"tol": 0.005' '0.280830734'; do
    printf '%s' "$_row" | grep -qF "$_want" \
      && ok "the pasted row carries $_want" \
      || bad "the pasted row carries $_want -- got '$_row'"
  done
  # THE SERIES' `holdout` IS FOLDED IN. It is a SERIES field, not a row field,
  # so pasting the row alone would leave the mandated `Confidence` field
  # unsatisfiable in exactly the way the `Claim:` field was -- and the row
  # carries no `extract`, so the leader could not even find its own series
  # heading in the markdown to read the window off.
  printf '%s' "$_row" | grep -qF '"holdout"' \
    && ok "...and the series holdout window, folded in" \
    || bad "...and the series holdout window, folded in -- got '$_row'"
  printf '%s' "$_row" | grep -qF '2026-08-15' \
    && ok "...with the window's real dates" \
    || bad "...with the window's real dates -- got '$_row'"

  # THE COMPARATOR, MATCHED ON EITHER ID. `vs` is a RUN id and `judge_claim`
  # resolves it through an index keyed by BOTH the evaluation and the probe, so
  # a `vs` naming the PROBE must resolve to the same row here -- otherwise the
  # pasted delta and the pasted `claim` would disagree about which run they are
  # comparing against. The fixture's `vs` is a probe id for exactly that reason.
  _cmp="$(retry_row "$PRED/row.json" "$E1" 2>/dev/null)"
  printf '%s' "$_cmp" | python3 -c '
import json, sys
got = json.load(sys.stdin)
assert got["target"]["metrics"]["p90_miss_tail"] == 0.280830734, got
assert got["comparator"]["metrics"]["p90_miss_tail"] == 0.298876182, got
assert got["holdout"] == ["2026-08-01", "2026-08-15"], got
assert "comparator_note" not in got, got
' 2>/dev/null \
    && ok "retry_row pastes both operands under distinct labels, matched on a probe id" \
    || bad "retry_row pastes both operands under distinct labels -- got '$_cmp'"

  # A MISSING COMPARATOR IS A LEGITIMATE STATE, not a failed retry: a reference
  # run has no `vs` by declaration, and a `vs` may name a run this series never
  # scored. Both must paste the target alone AND say WHY -- each reason is
  # quoted in `retry-prompt.md`, where it is load-bearing.
  #
  # AND THE NOTE MUST NOT PRESCRIBE A REMEDY. Both notes used to end "obtain
  # <it> with a command and paste it under `Evidence:`", which is the exact
  # opposite of what the retry template now requires: a null comparator means NO
  # DELTA, because a reference row has none by construction and a `vs` resolving
  # to zero rows can mean it is scored in ANOTHER series -- so fetching it
  # builds the cross-series comparison `verify-prompt.md` calls the single most
  # consequential error possible. The leader received both instructions in one
  # prompt and the template only won because it names and overrides the note.
  # The note is DATA about the paste; the template owns what to do.
  for _pair in "$ER_NOVS:no .vs." "$ER_BADVS:resolves to 0 rows"; do
    _id="${_pair%%:*}"; _why="${_pair#*:}"
    _one="$(retry_row "$PRED/row.json" "$_id" 2>/dev/null)"
    printf '%s' "$_one" | grep -q '"comparator": null' \
      && ok "a row whose comparator cannot be resolved still pastes ($_id)" \
      || bad "a row whose comparator cannot be resolved still pastes -- '$_one'"
    printf '%s' "$_one" | grep -qE "$_why" \
      && ok "...and the block says why the comparator is missing" \
      || bad "...and the block says why the comparator is missing -- '$_one'"
    printf '%s' "$_one" | grep -qE 'obtain|Evidence' \
      && bad "...and does NOT tell the leader to go and fetch one -- '$_one'" \
      || ok "...and does NOT tell the leader to go and fetch one"
  done

  # A NON-DICT TOP LEVEL WARNS INSTEAD OF TRACEBACKING. Unreachable while
  # `frontier.py --json` is the only writer, but a traceback is
  # indistinguishable in the log from an unreadable file.
  echo '[]' >"$PRED/notadict.json"
  _nd="$(retry_row "$PRED/notadict.json" "$E1" 2>&1 >/dev/null)"
  printf '%s' "$_nd" | grep -q "not an object" \
    && ok "retry_row names a non-object frontier instead of tracebacking" \
    || bad "retry_row names a non-object frontier -- got '$_nd'"

  # EXACTLY ONE ROW, re-checked here even though `suppressible` already refused
  # `ambiguous`: this function produces the numbers a mandated field rests on,
  # and pasting the first of two would put a figure from the WRONG contract into
  # a claim.
  retry_row "$PRED/row.json" "$ER_AMB" >/dev/null 2>&1 \
    && bad "retry_row must refuse an ambiguous id" \
    || ok "retry_row must refuse an ambiguous id"
  retry_row "$PRED/row.json" "$ER_ABS" >/dev/null 2>&1 \
    && bad "retry_row must refuse an id that is not there" \
    || ok "retry_row must refuse an id that is not there"
  retry_row "$PRED/nosuch.json" "$E1" >/dev/null 2>&1 \
    && bad "retry_row must fail on an unreadable frontier" \
    || ok "retry_row must fail on an unreadable frontier"
  _rrerr="$(retry_row "$PRED/nosuch.json" "$E1" 2>&1 >/dev/null)"
  printf '%s' "$_rrerr" | grep -q "retry_row could not read" \
    && ok "...and says why on stderr" \
    || bad "...and says why on stderr -- got '$_rrerr'"

  # THE INVARIANT THAT MAKES THE INJECTION ALL-OR-NOTHING: whenever
  # `take_retry_target` accepts an id, `retry_row` must also resolve it from the
  # same JSON. If the two could disagree the tick would emit half a retry block
  # -- the target line and the shrink instruction with no figures behind the
  # mandated fields -- which steers the leader onto action 1 with nothing to
  # fill it from. `tick.sh` guards that case anyway; this is what keeps the
  # guard unreachable rather than load-bearing.
  _both=1
  for _id in "$E1" "$E2" "$ER_RET" "$ER_AMB" "$ER_ABS" none not-an-id; do
    set_target "$T" "$_id" 2>/dev/null || printf '%s\n' "$_id" >"$T"
    if take_retry_target "$PRED/retry.json" "$T" >/dev/null 2>&1; then
      retry_row "$PRED/retry.json" "$_id" >/dev/null 2>&1 || _both=0
    fi
  done
  [ "$_both" = 1 ] \
    && ok "every id take_retry_target accepts, retry_row can also paste" \
    || bad "an id was accepted for retry but its row could not be pasted"
fi

# --- the transition table, driven through whole stubbed ticks -------------
# DISAGREE on a DIFFERENT resolvable target each tick IS drift: two runs
# rejected in a row is the counter's whole purpose.
setup driftdifferent
canon_results "$E1" "$E2"
echo "VERDICT: DISAGREE" >"$W/codex_reply"
entry_for "$E1"
out="$(run_tick QF_TICK_MAX_DISAGREE=9)"
state_is consecutive-disagreements 1 "a first rejection advances the drift streak"
state_is last-reject-target "$E1" "...and the rejected target is stored"
state_is consecutive-verifier-failures 0 \
  "a usable DISAGREE resets the verifier-failure counter"
present_in "$W/research/journal/escalations/"*.md "Escalation kind: rejection" \
  "a real rejection is labelled a rejection"
entry_for "$E2"
out="$(run_tick QF_TICK_MAX_DISAGREE=9)"
state_is consecutive-disagreements 2 "a DIFFERENT target advances the streak again"
state_is last-reject-target "$E2" "...and replaces the stored target"

# --------------------------------------------------------------------------
# THE 2026-09-04 CASE. The same unrecorded run rewritten and rejected again is
# NOT drift -- it is one episode -- and it is bounded by retirement, not by the
# brake. Three ticks did this and the third paused the loop for 2.5 days.
setup driftsame
canon_results "$E1"
echo "VERDICT: DISAGREE" >"$W/codex_reply"
entry_for "$E1"
out="$(run_tick QF_TICK_MAX_DISAGREE=3)"
state_is consecutive-disagreements 1 "the first rejection of a target counts"
out="$(run_tick QF_TICK_MAX_DISAGREE=3)"
state_is consecutive-disagreements 1 \
  "re-rejecting the SAME unrecorded target does not advance the streak"
state_is last-reject-target "$E1" "...and the stored target is kept"
[ ! -f "$W/research/PAUSE" ] \
  && ok "a repeated target does not pause the loop" \
  || bad "a repeated target paused the loop -- $out"
# --------------------------------------------------------------------------
# AND SUPPRESSION IS BOUNDED -- the one assertion that makes suppression safe at
# all. Two committed rejections retire the row, so the third tick's target is no
# longer suppressible and the brake takes over.
#
# COMMITTED BY PLUMBING. `git commit` is refused in the dev container, so
# `tick.sh`'s own publish step dies after writing the escalation -- which leaves
# every escalation UNCOMMITTED, and `frontier.py` reads committed content only
# (design §1.1), so retirement could never fire and this case was skipped. The
# same `write-tree`/`commit-tree`/`update-ref` recipe `test_frontier.py`
# already uses for its journal fixtures works fine, so this is a real test.
plumb_commit() {  # plumb_commit -- commit the journal the way HEAD would have
  local tree head parent
  git -C "$W/research" add -A journal >/dev/null 2>&1 || return 1
  tree="$(git -C "$W/research" write-tree 2>/dev/null)" || return 1
  parent="$(git -C "$W/research" rev-parse -q --verify HEAD 2>/dev/null)"
  # IDENT IS SUPPLIED: `commit-tree` refuses to auto-detect one, and the seed
  # commit in `setup` never landed here either, so there may be no parent.
  head="$(git -C "$W/research" -c user.name=t -c user.email=t@t \
          commit-tree "$tree" -m fixture ${parent:+-p "$parent"} 2>/dev/null)" \
    || return 1
  git -C "$W/research" update-ref refs/heads/main "$head" 2>/dev/null || return 1
}

setup driftsamebounded
canon_results "$E1"
echo "VERDICT: DISAGREE" >"$W/codex_reply"
entry_for "$E1"
# ONE SECOND BETWEEN TICKS. `STAMP` has second resolution, and two stubbed ticks
# run in well under a second -- so consecutive escalations collided on their
# filename, the second OVERWROTE the first, and retirement counted one episode
# where there were two. A fixture problem, not a `tick.sh` one: the real loop is
# on an hourly timer and each tick spends minutes inside two agents.
if ! (run_tick QF_TICK_MAX_DISAGREE=3 >/dev/null; plumb_commit); then
  skip "suppression is bounded by retirement, not open-ended" \
       "git plumbing cannot commit here either"
else
  state_is consecutive-disagreements 1 "the first rejection of a target counts"
  sleep 1
  run_tick QF_TICK_MAX_DISAGREE=3 >/dev/null
  plumb_commit || bad "the second escalation could not be committed"
  # BOTH EPISODES MUST BE ON DISK AND IN HEAD, or the retirement assertion below
  # would be measuring a stamp collision rather than the threshold.
  _esc="$(git -C "$W/research" ls-tree -r --name-only HEAD \
          | grep -c '^journal/escalations/')"
  [ "$_esc" = 2 ] \
    && ok "two distinct rejection episodes are committed" \
    || bad "two distinct rejection episodes are committed (found $_esc)"
  state_is consecutive-disagreements 1 \
    "a committed first rejection still leaves the target suppressible"
  # TWO COMMITTED REJECTIONS = RETIRED (QF_FRONTIER_RETIRE_AFTER defaults to 2).
  sleep 1
  out="$(run_tick QF_TICK_MAX_DISAGREE=3)"
  state_is consecutive-disagreements 2 \
    "suppression is bounded by retirement, not open-ended"
  state_is last-reject-target none \
    "...and a retired target is not stored for suppression"
  printf '%s' "$out" | grep -q "is retired: not suppressible" \
    && ok "...and the tick names retirement as the reason" \
    || bad "...and the tick names retirement as the reason -- $out"
  # AND THE BRAKE THEN ACTUALLY FIRES. Without this the case proves only that
  # one tick counted, not that a leader stuck on a retired run is ever stopped.
  sleep 1
  out="$(run_tick QF_TICK_MAX_DISAGREE=3)"
  state_is consecutive-disagreements 3 "a retired target keeps advancing"
  [ -f "$W/research/PAUSE" ] \
    && ok "...so a leader stuck on a retired run does reach the pause" \
    || bad "...so a leader stuck on a retired run reaches the pause -- $out"
  present_in "$W/research/PAUSE" "consecutive-disagreements at 3" \
    "the PAUSE line names the brake once, with its count"
fi

# --------------------------------------------------------------------------
setup driftsameunbounded
# THE CONTRAST, and it is why `frontier.py` reads COMMITTED content. With the
# escalations never committed nothing retires, so the same target stays
# suppressible for as long as the leader keeps picking it -- the streak never
# advances past its first episode. A future change that read the WORKING TREE
# instead would make this case retire, and this assertion is what would catch it.
canon_results "$E1"
echo "VERDICT: DISAGREE" >"$W/codex_reply"
entry_for "$E1"
for _ in 1 2 3 4; do run_tick QF_TICK_MAX_DISAGREE=3 >/dev/null; done
state_is consecutive-disagreements 1 \
  "with the escalations uncommitted nothing retires, so the streak stays put"
state_is last-reject-target "$E1" "...and the target stays stored"
[ ! -f "$W/research/PAUSE" ] \
  && ok "...and four rejections of one target never pause the loop" \
  || bad "...and four rejections of one target never pause the loop"
# --------------------------------------------------------------------------
setup driftfreshtarget
# THE TARGET THIS TICK ITSELF CREATED. A run submitted and scored during the
# leader's turn is ABSENT from the pre-leader snapshot and present in the
# refreshed one -- the snapshot the copilot was actually shown. Judging
# suppression against the stale one would read the ticks that did the most work
# as having no resolvable target: they would store `none`, and the next
# rejection of that same run would advance the drift brake instead of being
# recognised as one episode.
#
# So the scoreboard stub emits the row only from its SECOND call onward, which
# within a single tick is exactly the pre-leader/refresh boundary. With the
# default stub both snapshots are identical, which is why this case cannot be
# expressed without driving the two calls apart.
cat >"$W/trusted/results.sh" <<EOF
#!/usr/bin/env bash
c=\$(cat "$W/results_calls" 2>/dev/null || echo 0)
echo \$((c + 1)) > "$W/results_calls"
[ "\$c" != 0 ] || { echo '[]'; exit 0; }
python3 - "$TODAY" "$E1" <<'PYF'
import json, sys
today, ev = sys.argv[1], sys.argv[2]
print(json.dumps([{
    "evaluation": ev, "probe": ev.replace("evaluate-", "probe-"),
    "when": f"{today} 01:00", "verdict": "no-go",
    "extract": "e" * 16, "baseline": "b" * 16, "contract": "c" * 16,
    "metrics": {"mae": 225.1}, "passed": {"mae": False},
    "note": "cfg=configs/wait_time.yaml | fresh"}]))
PYF
EOF
chmod +x "$W/trusted/results.sh"
echo "VERDICT: DISAGREE" >"$W/codex_reply"
entry_for "$E1"
out="$(run_tick QF_TICK_MAX_DISAGREE=3)"
state_is consecutive-disagreements 1 "a rejection of a fresh target counts once"
state_is last-reject-target "$E1" \
  "a target absent from the PRE-leader snapshot still resolves from the refreshed one"
sleep 1
out="$(run_tick QF_TICK_MAX_DISAGREE=3)"
state_is consecutive-disagreements 1 \
  "...so re-rejecting it is one episode, not drift on the ticks that did the work"

# --------------------------------------------------------------------------
setup suppressedbadcounter
# AN UNREADABLE COUNTER IN THE SUPPRESSED BRANCH. Failing closed is right -- a
# streak that cannot be counted is a reason to stop -- but it used to happen
# SILENTLY, so the PAUSE file reported a drift streak at its threshold when the
# real cause was a counter that could not be read.
canon_results "$E1"
echo "VERDICT: DISAGREE" >"$W/codex_reply"
entry_for "$E1"
run_tick QF_TICK_MAX_DISAGREE=3 >/dev/null
state_is last-reject-target "$E1" "the target is stored before the counter breaks"
chmod 000 "$W/state/consecutive-disagreements" 2>/dev/null
out="$(run_tick QF_TICK_MAX_DISAGREE=3)"
chmod 600 "$W/state/consecutive-disagreements" 2>/dev/null
printf '%s' "$out" | grep -q "the streak cannot be tracked" \
  && ok "a suppressed rejection whose counter is unreadable says so" \
  || bad "a suppressed rejection whose counter is unreadable says so -- $out"
[ -f "$W/research/PAUSE" ] \
  && ok "...and still fails closed by pausing" \
  || bad "...and still fails closed by pausing -- $out"

# --------------------------------------------------------------------------
setup driftnone
# `none` is the honest answer for an action-6 waiting entry, and it always
# advances: it retires nothing, so nothing else bounds it.
echo "VERDICT: DISAGREE" >"$W/codex_reply"
entry_for none
out="$(run_tick QF_TICK_MAX_DISAGREE=9)"
state_is consecutive-disagreements 1 "a rejection with target none advances"
state_is last-reject-target none "...and stores none"
out="$(run_tick QF_TICK_MAX_DISAGREE=9)"
state_is consecutive-disagreements 2 "a second target-none rejection advances again"

# --------------------------------------------------------------------------
setup driftstale
# A TARGET THAT LEFT THE SCOREBOARD. Retirement can never reach it, so
# suppressing on it would be unbounded -- the 2026-09-04 livelock with the drift
# brake switched off.
canon_results "$E2"
echo "VERDICT: DISAGREE" >"$W/codex_reply"
entry_for "$E1"
out="$(run_tick QF_TICK_MAX_DISAGREE=9)"
state_is consecutive-disagreements 1 "a stale target advances the streak"
state_is last-reject-target none "...and is not stored"
printf '%s' "$out" | grep -q "not suppressible" \
  && ok "the tick says why the target was not suppressible" \
  || bad "the tick says why the target was not suppressible -- $out"
out="$(run_tick QF_TICK_MAX_DISAGREE=9)"
state_is consecutive-disagreements 2 "the same stale target advances AGAIN"

# --------------------------------------------------------------------------
setup driftprobeid
# A probe id in the field is not a target: it does not identify a row.
canon_results "$E1"
echo "VERDICT: DISAGREE" >"$W/codex_reply"
entry_for "probe-20260904T090941Z-0a217f40da8f-6445"
out="$(run_tick QF_TICK_MAX_DISAGREE=9)"
state_is consecutive-disagreements 1 "a probe-id target advances the streak"
state_is last-reject-target none "...and stores none"

# --------------------------------------------------------------------------
setup driftambiguoustarget
# TWO DISAGREEING `Target run:` LINES. `frontier.py:target_of` refuses to pick
# by position, so the escalation retires nothing -- and the tick must reach the
# same answer, or it would suppress a streak retirement cannot bound.
canon_results "$E1" "$E2"
echo "VERDICT: DISAGREE" >"$W/codex_reply"
printf '# a claim\n\n**Target run:** %s\n\n**Target run:** %s\n' "$E1" "$E2" \
  >"$W/leader_entry"
out="$(run_tick QF_TICK_MAX_DISAGREE=9)"
state_is consecutive-disagreements 1 "two disagreeing target lines advance"
state_is last-reject-target none "...and store none"
out="$(run_tick QF_TICK_MAX_DISAGREE=9)"
state_is consecutive-disagreements 2 \
  "an ambiguous target is never suppressed, however often it repeats"

# --------------------------------------------------------------------------
setup agreeclearstarget
# AGREE MUST CLEAR THE TARGET AS WELL AS THE NUMBER. Resetting only the count
# left the next rejection of a DIFFERENT run reading as a retry of this one.
canon_results "$E1" "$E2"
echo "VERDICT: DISAGREE" >"$W/codex_reply"
entry_for "$E1"
run_tick QF_TICK_MAX_DISAGREE=9 >/dev/null
state_is last-reject-target "$E1" "a rejection stores its target"
echo "VERDICT: AGREE" >"$W/codex_reply"
run_tick QF_TICK_MAX_DISAGREE=9 >/dev/null
state_is consecutive-disagreements 0 "AGREE resets the drift streak"
state_is last-reject-target none "AGREE also clears the stored target"

# --------------------------------------------------------------------------
setup noverdictisinfra
# HALF ONE OF THE USABLE-VERDICT DEFINITION: exit 0, prose, no anchored line.
# The copilot verified nothing, exactly like one that could not start.
canon_results "$E1"
echo "I have concerns but no conclusion" >"$W/codex_reply"
entry_for "$E1"
echo 1 >"$W/state/consecutive-verifier-failures"
# THE DRIFT COUNTER IS SEEDED NONZERO on purpose: asserting it stayed MISSING
# would also pass if the branch reset it, and "unchanged" is what the table
# says -- an outage is no evidence either way about the leader's reasoning.
echo 1 >"$W/state/consecutive-disagreements"
printf '%s\n' "$E1" >"$W/state/last-reject-target"
out="$(run_tick QF_TICK_MAX_VERIFIER_FAILS=9 QF_TICK_MAX_DISAGREE=9)"
state_is consecutive-verifier-failures 2 \
  "an exit-0 reply with no anchored verdict advances the verifier counter"
state_is consecutive-disagreements 1 \
  "...and leaves the drift streak exactly as it was"
state_is last-reject-target "$E1" \
  "...and leaves the stored target untouched, never storing one itself"
present_in "$W/research/journal/escalations/"*.md \
  "Escalation kind: verifier-failure" \
  "...and the escalation is labelled an outage, so it retires nothing"

# --------------------------------------------------------------------------
setup verdictthencrash
# HALF TWO: a NON-ZERO exit whose output DOES contain `VERDICT: AGREE`. The
# output of a failed command is not a verdict -- a copilot that printed AGREE and
# then died once published an entry as verified -- so this is an outage too, and
# it must NOT reset the counter it advances, or the outage never reaches the
# threshold.
canon_results "$E1"
cat >"$W/bin/codex" <<EOF
#!/usr/bin/env bash
cat > "$W/codex_prompt"
echo "VERDICT: AGREE"
exit 4
EOF
chmod +x "$W/bin/codex"
entry_for "$E1"
echo 1 >"$W/state/consecutive-verifier-failures"
echo 1 >"$W/state/consecutive-disagreements"
out="$(run_tick QF_TICK_MAX_VERIFIER_FAILS=9 QF_TICK_MAX_DISAGREE=9)"
state_is consecutive-verifier-failures 2 \
  "a non-zero exit carrying a verdict advances the verifier counter"
state_is consecutive-disagreements 1 "...and leaves the drift counter alone"
state_is last-reject-target MISSING "...and stores no target"
[ -z "$(find "$W/research/journal" -maxdepth 1 -name '2*.md')" ] \
  && ok "...and records nothing as a finding" \
  || bad "a crashed copilot's AGREE was published -- $out"
present_in "$W/research/journal/escalations/"*.md \
  "Escalation kind: verifier-failure" \
  "...and the escalation is labelled an outage"

# --------------------------------------------------------------------------
setup usabledisagreeresetsverifier
# A VERIFIER THAT ANSWERED IS UP, and whether it agreed is not an
# infrastructure fact. Without this a flaky copilot would accumulate towards a
# pause across ticks it actually verified.
canon_results "$E1"
echo "VERDICT: DISAGREE" >"$W/codex_reply"
entry_for "$E1"
echo 2 >"$W/state/consecutive-verifier-failures"
out="$(run_tick QF_TICK_MAX_VERIFIER_FAILS=3 QF_TICK_MAX_DISAGREE=9)"
state_is consecutive-verifier-failures 0 \
  "a usable DISAGREE resets the verifier-failure streak"
state_is consecutive-disagreements 1 "...and advances drift instead"
[ ! -f "$W/research/PAUSE" ] \
  && ok "...so a verifier that answered does not pause the loop" \
  || bad "a usable DISAGREE paused the loop -- $out"

# --------------------------------------------------------------------------
# HALF THREE OF THE USABLE-VERDICT DEFINITION: FINALITY. `exit 0` plus an
# anchored line is not enough -- the anchored line has to be the LAST NONBLANK
# line of the reply. `tail -1` over the MATCHING lines read
#
#     VERDICT: AGREE
#     Correction: the central figure is absent; this must not be recorded.
#
# as a usable AGREE: the entry was PUBLISHED and both counters were reset by a
# copilot that had just retracted itself in the following sentence. The design's
# contract is "exit 0 AND a valid anchored FINAL `VERDICT:` line" (§2.2); the
# anchoring was implemented and the finality was not.
#
# THE OLD PROTECTION MUST SURVIVE IT. `bothwords` above is the reason the LAST
# match won in the first place -- a model that reasons out loud names both words
# before committing -- and the last NONBLANK LINE keeps that answer while
# refusing this one.
#
# THE DIRECTION IT ERRS IN IS DELIBERATE, and it is a real behaviour change: a
# copilot that appends a trailing log line now escalates instead of recording.
# That is the conservative direction, and `verify-prompt.md` already tells it to
# end with exactly one line.
setup verdictnotfinal
canon_results "$E1"
printf '%s\n' 'VERDICT: AGREE' \
  'Correction: the central figure is absent; this must not be recorded.' \
  >"$W/codex_reply"
entry_for "$E1"
echo 1 >"$W/state/consecutive-verifier-failures"
# BOTH COUNTERS SEEDED NONZERO on purpose: "unchanged" is what the §2.1 table
# says for no usable verdict, and asserting a counter stayed MISSING would also
# pass if the branch had reset it.
echo 1 >"$W/state/consecutive-disagreements"
printf '%s\n' "$E1" >"$W/state/last-reject-target"
out="$(run_tick QF_TICK_MAX_VERIFIER_FAILS=9 QF_TICK_MAX_DISAGREE=9)"
[ -z "$(find "$W/research/journal" -maxdepth 1 -name '2*.md')" ] \
  && ok "an AGREE with prose after it is NOT recorded as a finding" \
  || bad "an AGREE retracted by the next line was published -- $out"
[ -n "$(find "$W/research/journal/escalations" -name '2*.md')" ] \
  && ok "...it escalates instead" \
  || bad "...it escalates instead -- $out"
state_is consecutive-verifier-failures 2 \
  "...as NO usable verdict, so the verifier counter advances"
state_is consecutive-disagreements 1 \
  "...and the drift streak is left exactly as it was"
state_is last-reject-target "$E1" \
  "...and the stored target is untouched, never stored by this branch"
present_in "$W/research/journal/escalations/"*.md \
  "Escalation kind: verifier-failure" \
  "...and the escalation is labelled an outage, so it retires nothing"

# --------------------------------------------------------------------------
setup verdictnotfinaldisagree
# THE SAME RULE ON THE OTHER WORD, and the assertion that matters is the RESET:
# a DISAGREE followed by prose is no usable verdict, so it must NOT clear the
# verifier-failure streak. Seeded at 1, so a reset-then-increment would leave 1
# and a correctly untouched-then-incremented counter leaves 2.
canon_results "$E1"
printf '%s\n' 'VERDICT: DISAGREE' \
  'On reflection I could not check the entry at all; treat this as unverified.' \
  >"$W/codex_reply"
entry_for "$E1"
echo 1 >"$W/state/consecutive-verifier-failures"
echo 1 >"$W/state/consecutive-disagreements"
out="$(run_tick QF_TICK_MAX_VERIFIER_FAILS=9 QF_TICK_MAX_DISAGREE=9)"
state_is consecutive-verifier-failures 2 \
  "a DISAGREE with prose after it does not RESET the verifier counter"
state_is consecutive-disagreements 1 \
  "...and is not counted as leader drift either"
state_is last-reject-target MISSING \
  "...and stores no target, so no later rejection is suppressed against it"
present_in "$W/research/journal/escalations/"*.md \
  "Escalation kind: verifier-failure" \
  "...and it escalates as an outage rather than a rejection"

# --------------------------------------------------------------------------
setup verdictfinalagree
# THE ORDINARY REPLY IS UNCHANGED: reasoning first, the verdict last. Asserted
# with the counters as well as the journal, because the finality rule is one
# `grep` away from turning every well-formed tick into a verifier failure.
canon_results "$E1"
printf '%s\n' 'I checked the mae figure against the JSON and it is there.' \
  'VERDICT: AGREE' >"$W/codex_reply"
entry_for "$E1"
echo 2 >"$W/state/consecutive-verifier-failures"
out="$(run_tick QF_TICK_MAX_VERIFIER_FAILS=9 QF_TICK_MAX_DISAGREE=9)"
[ -n "$(find "$W/research/journal" -maxdepth 1 -name '2*.md')" ] \
  && ok "reasoning followed by a final AGREE still records the entry" \
  || bad "reasoning followed by a final AGREE still records -- $out"
state_is consecutive-verifier-failures 0 \
  "...and a usable verdict still resets the verifier counter"

# --------------------------------------------------------------------------
setup verdictfinaldisagree
canon_results "$E1"
printf '%s\n' 'the mae figure is not in the JSON' 'VERDICT: DISAGREE' \
  >"$W/codex_reply"
entry_for "$E1"
out="$(run_tick QF_TICK_MAX_VERIFIER_FAILS=9 QF_TICK_MAX_DISAGREE=9)"
state_is consecutive-disagreements 1 \
  "prose followed by a final DISAGREE is still a real rejection"
present_in "$W/research/journal/escalations/"*.md "Escalation kind: rejection" \
  "...labelled a rejection, not an outage"

# --------------------------------------------------------------------------
setup verdicttrailingblank
# TRAILING BLANK LINES ARE NOT PROSE. `codex` output routinely ends with them,
# and a finality rule implemented as "the last line" rather than "the last
# NONBLANK line" would fail every real reply -- three ticks of that pauses the
# loop on the verifier brake for a formatting artefact.
canon_results "$E1"
printf '%s\n' 'the figures check out.' 'VERDICT: AGREE' '' '   ' '' \
  >"$W/codex_reply"
entry_for "$E1"
out="$(run_tick QF_TICK_MAX_VERIFIER_FAILS=9 QF_TICK_MAX_DISAGREE=9)"
[ -n "$(find "$W/research/journal" -maxdepth 1 -name '2*.md')" ] \
  && ok "blank and whitespace-only lines after the verdict still count as final" \
  || bad "trailing blank lines hid a final verdict -- $out"
state_is consecutive-verifier-failures 0 \
  "...and the verifier counter is reset, so it is a usable verdict"

# --------------------------------------------------------------------------
# THE PAUSE CHECK IS NOW A QUESTION (design §4.2, Task 8). The state table
# itself is `test_pause_issue.sh`'s; what is asserted here is the WIRING --
# that a refusal still stops the tick, that an authorised close lets it run,
# and that the directive reaches the leader and only the leader.
setup pauseissueopen
gh_stub
printf 'auto-paused %s: consecutive-disagreements at 3\nsee x\nissue: lotas/qf-research#77\nstamp: %s\n' \
  "$PSTAMP" "$PSTAMP" >"$W/research/PAUSE"
printf '{"state":"open","body":"auto-paused %s: x"}\n' "$PSTAMP" \
  >"$W/gh/issue.json"
out="$(run_tick "${PAUSE_ENV[@]}")"
printf '%s' "$out" | grep -q "PAUSE exists" \
  && ok "a bound PAUSE whose issue is still open stops the tick" \
  || bad "a bound PAUSE whose issue is still open stops the tick -- $out"
[ -f "$W/research/PAUSE" ] \
  && ok "...and the brake is left in place" || bad "...and the brake is left"
[ ! -f "$W/research/journal/PENDING.md" ] \
  && ok "...before the leader runs" || bad "...before the leader runs"

# --------------------------------------------------------------------------
setup pauseissueresume
# AN ALLOWLISTED CLOSE RELEASES THE BRAKE, and the counters that caused the
# pause are zeroed BEFORE the tick continues -- otherwise the very first
# rejection of the resumed tick would pause it again, which is the livelock
# with an extra step.
gh_stub
canon_results "$E1"
entry_for "$E1"
printf 'auto-paused %s: consecutive-disagreements at 3\nsee x\nissue: lotas/qf-research#77\nstamp: %s\n' \
  "$PSTAMP" "$PSTAMP" >"$W/research/PAUSE"
printf '[{"user":{"login":"lotas"},"created_at":"2026-09-06T00:00:00Z","body":"HUMAN SAYS look at qctx_d"}]\n' \
  >"$W/gh/comments.json"
echo 3 >"$W/state/consecutive-disagreements"
echo 3 >"$W/state/consecutive-verifier-failures"
out="$(run_tick "${PAUSE_ENV[@]}")"
printf '%s' "$out" | grep -q "resumed by an allowlisted close" \
  && ok "an allowlisted close resumes the tick" \
  || bad "an allowlisted close resumes the tick -- $out"
[ ! -f "$W/research/PAUSE" ] \
  && ok "...and the PAUSE file is gone" || bad "...and the PAUSE file is gone"
state_is consecutive-disagreements 0 "...with the drift counter zeroed"
state_is consecutive-verifier-failures 0 "...and the verifier counter zeroed"
present_in "$W/leader_prompt" "HUMAN SAYS look at qctx_d" \
  "...and the human directive reaches the leader"
present_in "$W/leader_prompt" "INSTRUCTION, NOT EVIDENCE" \
  "...labelled an instruction rather than a source"
absent_from "$W/codex_prompt" "HUMAN SAYS" \
  "...and the copilot is never shown it, so a figure typed there cannot pass"
present_in "$W/leader_prompt" "No figure appearing in it may be cited" \
  "...with the citation ban intact"
[ ! -e "$W/state/human-directive.md" ] \
  && ok "...and the directive is consumed once, not left for the next tick" \
  || bad "...and the directive is consumed once"

# --------------------------------------------------------------------------
setup pauseissuebigdirective
# AN OVERSIZED DIRECTIVE STILL CARRIES ITS RULES. The cap used to be applied by
# BOTH layers: `pause-issue.sh` cut the comment text to the limit, appended the
# citation ban after it, and `tick.sh` then cut the whole file to the same limit
# -- deleting the ban exactly when the most human prose had arrived. One cap,
# upstream, with the rules above the prose.
gh_stub
canon_results "$E1"
entry_for "$E1"
printf 'auto-paused %s: consecutive-disagreements at 3\nsee x\nissue: lotas/qf-research#77\nstamp: %s\n' \
  "$PSTAMP" "$PSTAMP" >"$W/research/PAUSE"
python3 - "$W/gh/comments.json" <<'PYBIG'
import json, sys
json.dump([{"user": {"login": "lotas"},
            "created_at": "2026-09-06T00:00:00Z",
            "body": "HUMAN SAYS " + "y" * 6000}],
          open(sys.argv[1], "w"))
PYBIG
out="$(run_tick "${PAUSE_ENV[@]}")"
printf '%s' "$out" | grep -q "resumed by an allowlisted close" \
  && ok "an oversized directive still resumes the tick" \
  || bad "an oversized directive still resumes the tick -- $out"
present_in "$W/leader_prompt" "INSTRUCTION, NOT EVIDENCE" \
  "...and the non-evidence label reaches the leader"
present_in "$W/leader_prompt" "No figure appearing in it may be cited" \
  "...and so does the citation ban, which truncation used to eat"
present_in "$W/leader_prompt" "TRUNCATED at" \
  "...and the leader is told the thread was cut"
absent_from "$W/leader_prompt" "y\{5000\}" \
  "...while the COMMENT TEXT is what got cut"

# --------------------------------------------------------------------------
setup pauseissuenohelper
# A MISSING HELPER IS ITS OWN DIAGNOSIS. Folding "the script is not there" into
# "stay paused" rebuilt the 2026-09-04 failure in a new place: brake on, the
# documented release path dead, and the log saying only "PAUSE exists; stopping"
# -- indistinguishable from a pause nobody has got round to releasing. A
# forgotten `chmod +x` or a half-finished deploy is exactly how it happens.
#
# TICK_PATH-STYLE, NOT `rm`: the helper lives beside `tick.sh` in the real
# checkout, so "it is not installed" has to be expressed as a copy of
# `research-loop/` that lacks it.
mkdir -p "$W/rl"
for _f in "$HERE/../research-loop"/*; do
  case "$(basename "$_f")" in
    pause-issue.sh) ;;
    *) cp -r "$_f" "$W/rl/" ;;
  esac
done
unset _f
printf 'auto-paused %s: consecutive-disagreements at 3\nsee x\nstamp: %s\n' \
  "$PSTAMP" "$PSTAMP" >"$W/research/PAUSE"
out="$(env PATH="$W/bin:${TICK_PATH:-$PATH}" HOME="$W" \
        QF_RESEARCH="$W/research" QF_TRUSTED_HOST="$W/trusted" \
        QF_QUEUE_FILE="$W/queue/experiment-queue.md" QF_TICK_STATE="$W/state" \
        QF_TICK_COPILOT_BACKOFF_S=1 \
        timeout 30 bash "$W/rl/tick.sh" 2>&1)"
printf '%s' "$out" | grep -q "PAUSE exists" \
  && ok "a missing pause-issue.sh still stops the tick" \
  || bad "a missing pause-issue.sh still stops the tick -- $out"
[ -f "$W/research/PAUSE" ] \
  && ok "...and the brake is untouched" || bad "...and the brake is untouched"
printf '%s' "$out" | grep -q "pause-issue.sh is missing or not executable" \
  && ok "...and says the release path is dead, not just that PAUSE exists" \
  || bad "...and says the release path is dead -- $out"
printf '%s' "$out" | grep -q "chmod +x" \
  && ok "...and names the remedy" || bad "...and names the remedy -- $out"

# --------------------------------------------------------------------------
setup pauseissuestranger
# EVERY OTHER ANSWER STAYS PAUSED. One representative row here -- a close by a
# login that is not on the allowlist -- proves the wiring treats a non-zero
# `check` as the brake rather than as a resume.
gh_stub
printf 'auto-paused %s: consecutive-disagreements at 3\nsee x\nissue: lotas/qf-research#77\nstamp: %s\n' \
  "$PSTAMP" "$PSTAMP" >"$W/research/PAUSE"
printf '[{"event":"closed","actor":{"login":"drifter"}}]\n' >"$W/gh/events.json"
out="$(run_tick "${PAUSE_ENV[@]}")"
printf '%s' "$out" | grep -q "PAUSE exists" \
  && ok "a close by a stranger does not resume the tick" \
  || bad "a close by a stranger does not resume the tick -- $out"
[ -f "$W/research/PAUSE" ] && ok "...and the brake survives" \
  || bad "...and the brake survives"

# --------------------------------------------------------------------------
setup pauseissuefiled
# THE ALARM AT THE MOMENT THE BRAKE IS WRITTEN. This is the whole point of §4:
# on 2026-09-04 the pause was correct and nobody knew for two and a half days.
gh_stub
canon_results "$E1"
: >"$W/codex_fails"
entry_for "$E1"
echo 2 >"$W/state/consecutive-verifier-failures"
out="$(run_tick QF_TICK_MAX_VERIFIER_FAILS=3 QF_TICK_MAX_DISAGREE=9 \
               "${PAUSE_ENV[@]}")"
[ -f "$W/research/PAUSE" ] && ok "the brake is written" || bad "the brake is written"
present_in "$W/gh.log" "issue create" "...and the pause issue is filed" 
present_in "$W/gh.log" "PAUSED .*consecutive-verifier-failures at 3" \
  "...with the brake and the count in its title"
present_in "$W/gh.log" "--label qf-pause" \
  "...labelled, which is the state of every pause after the first"
present_in "$W/research/PAUSE" "^issue: lotas/qf-research#77" \
  "...and PAUSE is bound to the issue that can release it"
absent_from "$W/gh.log" "issue view" \
  "...and the closing actor is never asked of \`issue view\`, which cannot say"

# --------------------------------------------------------------------------
setup verifierpause
# THE TWO BRAKES PAUSE INDEPENDENTLY, and the PAUSE file says which -- so an
# operator can tell an outage from drift without opening anything.
canon_results "$E1"
: >"$W/codex_fails"
entry_for "$E1"
echo 2 >"$W/state/consecutive-verifier-failures"
echo 1 >"$W/state/consecutive-disagreements"
out="$(run_tick QF_TICK_MAX_VERIFIER_FAILS=3 QF_TICK_MAX_DISAGREE=9)"
state_is consecutive-verifier-failures 3 "a copilot that stays down advances"
state_is consecutive-disagreements 1 "...without touching the drift streak"
[ -f "$W/research/PAUSE" ] \
  && ok "the verifier-failure counter can pause the loop on its own" \
  || bad "the verifier counter did not pause the loop -- $out"
present_in "$W/research/PAUSE" "consecutive-verifier-failures" \
  "the PAUSE file names the brake that fired"
present_in "$W/research/PAUSE" "^stamp: 2" \
  "the PAUSE file carries a machine-readable stamp"
# `pause-issue.sh` NOW EXISTS (Task 8), so this case no longer asserts a missing
# script -- it asserts the UNCONFIGURED box, which is the state every host is in
# until the root-owned unit sets QF_PAUSE_ISSUE_REPO. A failed alarm must only
# warn: the local brake is the PAUSE file, and losing the pause because the
# notification failed would be the worse bug by far.
printf '%s' "$out" | grep -q "could not file the pause issue" \
  && ok "an alarm that cannot be filed warns instead of aborting" \
  || bad "an alarm that cannot be filed warns instead of aborting -- $out"
printf '%s' "$out" | grep -q "the loop is still paused" \
  && ok "...and says the loop is paused regardless" \
  || bad "...and says the loop is paused regardless -- $out"
printf '%s' "$out" | grep -q "QF_PAUSE_ISSUE_REPO is unset" \
  && ok "...and names the unset setting as the reason" \
  || bad "...and names the unset setting as the reason -- $out"

# --------------------------------------------------------------------------
# THE BRAKE'S FAILURE IS THE TICK'S EXIT STATUS (design §4b). `pause_now`
# returns non-zero when it cannot write PAUSE, and BOTH callers used to ignore
# that status and carry on through publish to a ZERO exit -- so
# `OnFailure=qf-tick-failure.service` never fired. No brake, no issue, repeating
# hourly: the 2026-09-04 silence exactly, reached by the one route the alarm
# exists to cover.
#
# A DANGLING SYMLINK IS THE REAL SHAPE OF IT. `[ -e PAUSE ]` is FALSE for one,
# so the startup check waves it through, and `>PAUSE` then fails on the target
# path -- which is what a symlink into a directory the unit's hardening protects
# does.
#
# GIT'S COMMIT AND PUSH ARE STUBBED, here and in the control below only. This
# sandbox refuses `git commit` outright, so the real publish path dies with exit
# 1 on EVERY tick -- and an exit-status assertion would then pass for a reason
# that has nothing to do with the brake. With those two steps no-oped the tick's
# own exit decision is the only thing left, and the CONTROL case is what proves
# the assertion is not vacuous: the same world with a writable PAUSE must exit 0.
publish_stub() {  # publish_stub -- no-op `git commit`/`git push`, real git otherwise
  local real; real="$(command -v git)"
  # AND `setup`'S SEED COMMIT DID NOT HAPPEN EITHER, for the same reason -- so
  # `seed` is still sitting in the INDEX, and publish refuses an index holding
  # anything but the verified entry ("the index holds more than the verified
  # entry", which is how every publish assertion in this file ends up skipped).
  # Emptied only when the repo has no commit at all: on a host where `git
  # commit` works the seed IS committed, and removing it from the index there
  # would stage a deletion beside the entry and trip the same refusal.
  "$real" -C "$W/research" rev-parse --verify -q HEAD >/dev/null 2>&1 \
    || "$real" -C "$W/research" rm --cached -r -q . >/dev/null 2>&1 || true
  cat >"$W/bin/git" <<EOF
#!/usr/bin/env bash
# Generated by test_tick.sh. This sandbox refuses commits; everything except
# the two steps it refuses is the real git.
for _a in "\$@"; do
  case "\$_a" in commit|push) exit 0 ;; esac
done
exec "$real" "\$@"
EOF
  chmod +x "$W/bin/git"
}

setup pausewritecontrol
canon_results "$E1"
entry_for "$E1"
publish_stub
: >"$W/codex_fails"
echo 2 >"$W/state/consecutive-verifier-failures"
out="$(run_tick QF_TICK_MAX_VERIFIER_FAILS=3 QF_TICK_MAX_DISAGREE=9)"; rc=$?
[ -f "$W/research/PAUSE" ] \
  && ok "control: the brake is written when PAUSE is writable" \
  || bad "control: the brake is written when PAUSE is writable -- $out"
[ "$rc" = 0 ] \
  && ok "control: a tick that paused and published exits ZERO" \
  || bad "control: a tick that paused cleanly must exit 0, got $rc -- $out"

# --------------------------------------------------------------------------
setup pausewritefails
canon_results "$E1"
entry_for "$E1"
publish_stub
: >"$W/codex_fails"
ln -s "$W/nosuchdir/PAUSE" "$W/research/PAUSE"
echo 2 >"$W/state/consecutive-verifier-failures"
out="$(run_tick QF_TICK_MAX_VERIFIER_FAILS=3 QF_TICK_MAX_DISAGREE=9)"; rc=$?
printf '%s' "$out" | grep -q "CRITICAL cannot write" \
  && ok "a PAUSE that cannot be written is reported as CRITICAL" \
  || bad "a PAUSE that cannot be written is reported as CRITICAL -- $out"
# THE ESCALATION IS STILL PUBLISHED. It is written before the pause attempt and
# `publish` is what commits and pushes it, so dying before that would lose the
# record as well as the brake -- two harms instead of one.
present_in "$W/research/journal/escalations/"*.md \
  "Escalation kind: verifier-failure" \
  "...and the escalation is still written"
printf '%s' "$out" | grep -q "published " \
  && ok "...and still published before the tick gives up" \
  || bad "...and still published before the tick gives up -- $out"
[ "$rc" != 0 ] \
  && ok "...and the tick exits NON-ZERO so OnFailure= can raise the alarm" \
  || bad "an unwritable brake exited $rc, so OnFailure= never fires -- $out"
printf '%s' "$out" | grep -q "the brake could not be written" \
  && ok "...naming the brake as the cause, not a publish failure" \
  || bad "...naming the brake as the cause -- $out"

# --------------------------------------------------------------------------
# THE DIRECTIVE MUST NOT ESCAPE THE VERIFIED RESUME SEQUENCE (design §4.3).
# `pause-issue.sh` persists the directive, zeroes and reads back all three state
# files, and removes PAUSE last -- in that order, verified. The copy into `CTX`
# then had NO status check, on the far side of the brake: on a nearly full
# filesystem a partial copy reached the leader's prompt and the state copy was
# then deleted, or no copy arrived at all and the state copy survived unread,
# because nothing looks at it once PAUSE is gone. The resume stays safe either
# way; what is lost, silently, is the instruction a human deliberately left.
#
# `cp` IS STUBBED, because a copy failure is not otherwise reachable: the
# destination is a fresh `mktemp -d` the tick made two lines earlier. The stub
# fails for that ONE destination and forwards everything else to the real `cp`.
directive_cp_stub() {  # directive_cp_stub fail|partial
  local real rc=1
  real="$(command -v cp)"
  # `partial` IS THE CASE A STATUS CHECK ALONE WOULD MISS: exit 0 with a
  # truncated destination. `cp` can be interrupted mid-write, and a half
  # directive injected into the leader's prompt is worse than none.
  [ "$1" != partial ] || rc=0
  cat >"$W/bin/cp" <<EOF
#!/usr/bin/env bash
# Generated by test_tick.sh: break ONE destination, forward everything else.
_dst="\${@: -1}"
case "\$_dst" in
  */human-directive.md)
    [ "$1" != partial ] || printf 'HUMAN SAYS look at qc' >"\$_dst"
    echo "cp: stub: no space left on device" >&2
    exit $rc ;;
esac
exec "$real" "\$@"
EOF
  chmod +x "$W/bin/cp"
}

released_pause() {  # released_pause -- a PAUSE an allowlisted human just closed
  gh_stub
  canon_results "$E1"
  entry_for "$E1"
  printf 'auto-paused %s: consecutive-disagreements at 3\nsee x\nissue: lotas/qf-research#77\nstamp: %s\n' \
    "$PSTAMP" "$PSTAMP" >"$W/research/PAUSE"
  printf '[{"user":{"login":"lotas"},"created_at":"2026-09-06T00:00:00Z","body":"HUMAN SAYS look at qctx_d"}]\n' \
    >"$W/gh/comments.json"
  echo 3 >"$W/state/consecutive-disagreements"
  echo 3 >"$W/state/consecutive-verifier-failures"
}

setup directivecopyfails
released_pause
directive_cp_stub fail
out="$(run_tick "${PAUSE_ENV[@]}")"
printf '%s' "$out" | grep -q "resumed by an allowlisted close" \
  && ok "a failed directive copy does not undo the resume" \
  || bad "a failed directive copy does not undo the resume -- $out"
printf '%s' "$out" | grep -q "could not be copied intact" \
  && ok "...and the loss of the human's instruction is said LOUDLY" \
  || bad "...and the loss of the human's instruction is said loudly -- $out"
[ -s "$W/state/human-directive.md" ] \
  && ok "...and the state copy is retained, not deleted behind a failed copy" \
  || bad "...and the state copy is retained (it is gone or empty)"
[ -e "$W/leader_prompt" ] \
  && ok "...and the tick still proceeds" \
  || bad "...and the tick still proceeds -- $out"
absent_from "$W/leader_prompt" "HUMAN SAYS" \
  "...with the leader running on no directive rather than half of one"

# --------------------------------------------------------------------------
setup directivecopypartial
released_pause
directive_cp_stub partial
out="$(run_tick "${PAUSE_ENV[@]}")"
printf '%s' "$out" | grep -q "could not be copied intact" \
  && ok "a copy that exits zero but truncates is caught too" \
  || bad "a truncated directive copy went unnoticed -- $out"
[ -s "$W/state/human-directive.md" ] \
  && ok "...and the state copy is retained, because the destination is not intact" \
  || bad "...and the state copy is retained (it is gone or empty)"
absent_from "$W/leader_prompt" "HUMAN SAYS" \
  "...and half a directive never reaches the leader's prompt"

# --------------------------------------------------------------------------
# A RETRY SHRINKS; IT IS NOT REWRITTEN (design §3). On 2026-09-04 the same run
# was written up and rejected three ticks running, and round 2 fixed the defect
# it was told about while INTRODUCING a new one -- rewriting the whole entry
# resamples the defect surface. So the shrink instruction has to be in the
# leader's context, which means the retry is decided BEFORE the leader runs.
setup retryinject
# A ROW THAT REALLY REGISTERS, ITS COMPARATOR, AND A CONTRACT THAT ORDERS THE
# METRIC. Three separate ways this fixture was degenerate, each of which made an
# assertion about a mandated field pass against a placeholder:
#
#   `dir=lower` IS NOT A DIRECTION. `prereg.DIRECTIONS` is ("improve", "hold"),
#   so the note failed to register: the pasted row carried
#   `"claim": "unregistered"` and `"direction": "lower"`, two mandated fields
#   both degenerate, neither asserted. `dir=improve` is a real registration.
#
#   `vs` IS A RUN ID, NOT A CONFIG PATH. `judge_claim` resolves it through an
#   index keyed by evaluation and probe, so `vs=configs/wait_time.yaml` gave
#   `claim: "unjudgeable: vs not scored"` -- and with no comparator row in the
#   series there is no second operand for ban 1's signed delta either.
#
#   NO RANKED METRIC IS NO ORDERING. With only `holdout_days` in the contract,
#   `metric_ranks` is empty and `improve` cannot be judged at all
#   ("no contract, so the metric has no ordering"). The metric needs a `bar`
#   kind and a direction for `claim` to reach a real verdict.
#
# `canon_results` cannot express any of this, and the default `qf` stub answers
# every subcommand with a job list, so the series would also have no
# `as_of_date` and no `holdout_days`.
retry_world() {  # retry_world -- a registered row, its comparator, a real contract
  mkdir -p "$W/contracts"
  # `holdout_days` GIVES THE WINDOW; the metric entry gives the ORDERING that
  # makes `improve` judgeable. `frontier.py` reads this off disk (the file is
  # root-owned in the trusted checkout, so qfd is not its reader).
  python3 -c 'import json,sys; open(sys.argv[1],"w").write(json.dumps({
      "holdout_days": 14,
      "metrics": {"p90_miss_tail": {"bar": {"kind": "absolute"},
                                    "direction": "lower_is_better"}}}))' \
    "$W/contracts/wait.json"
  cat >"$W/trusted/results.sh" <<EOF
#!/usr/bin/env bash
python3 - "$E1" "$E2" <<'PYR'
import json, sys
target, comparator = sys.argv[1], sys.argv[2]
common = {"when": "2026-08-15 01:00", "verdict": "no-go",
          "extract": "e" * 16, "baseline": "b" * 16, "contract": "c" * 16}
print(json.dumps([
    # THE COMPARATOR, declared a reference so it needs no `vs` of its own.
    dict(common, evaluation=comparator,
         probe=comparator.replace("evaluate-", "probe-"),
         metrics={"p90_miss_tail": 0.298876182, "mae": 226.52},
         passed={"p90_miss_tail": False, "mae": False},
         note="cfg=configs/wait_time.yaml | bar=p90_miss_tail | dir=improve"
              " | cfgh=deadbeefdead | ref=1"),
    dict(common, evaluation=target,
         probe=target.replace("evaluate-", "probe-"),
         metrics={"p90_miss_tail": 0.280830734, "mae": 225.13},
         passed={"p90_miss_tail": True, "mae": False},
         note="cfg=configs/val7_qctx.yaml | bar=p90_miss_tail | dir=improve"
              f" | cfgh=0a217f40da8f | vs={comparator} | tol=0.005"
              " | hyp=queue context should shrink the tail miss"),
]))
PYR
EOF
  chmod +x "$W/trusted/results.sh"
  cat >"$W/bin/qf" <<EOF
#!/usr/bin/env bash
case "\$*" in
  *extracts*) python3 -c 'import json; print(json.dumps({"extracts":[
      {"request_hash": "e"*16, "as_of_date": "2026-08-15"}]}))' ;;
  *contracts*) python3 -c 'import json,sys; print(json.dumps({"dir": sys.argv[1],
      "contracts":[{"contract_hash": "c"*16, "file": "wait.json"}]}))' \
      "$W/contracts" ;;
  *) python3 -c 'import json; print(json.dumps({"jobs": []}))' ;;
esac
EOF
  chmod +x "$W/bin/qf"
}
retry_world
# THE TARGET IS STORED BY A REAL REJECTION, not written into the state
# directory by hand -- and the escalation is COMMITTED, because `frontier.py`
# reads committed content only, and an uncounted rejection leaves
# `escalation_latest` empty. That mattered: `Action taken:` cites it, and in the
# earlier version of this case BOTH of that field's sources were absent, so the
# test world was the one place a mandated field was unsatisfiable.
entry_for "$E1"
echo "VERDICT: DISAGREE" >"$W/codex_reply"
run_tick QF_TICK_MAX_DISAGREE=9 >/dev/null
state_is last-reject-target "$E1" "a real rejection stores the target a retry needs"
plumb_commit || bad "the escalation could not be committed (fixture plumbing)"
rm -f "$W/leader_prompt"
echo "VERDICT: AGREE" >"$W/codex_reply"
out="$(run_tick QF_TICK_MAX_DISAGREE=9)"
# ASSERTED ON THE CAPTURED PROMPT, NOT ON THE LOG. "The retry block exists"
# would also be true of a block appended after the leader had already written
# its entry, which is precisely the failure §3.1 exists to prevent: the
# instruction is worthless unless it is read before the entry is planned.
# `$W/leader_prompt` is the stdin the stub leader actually received.
present_in "$W/leader_prompt" "## This tick is a RETRY" \
  "a suppressible stored target puts the retry block in the LEADER'S PROMPT"
present_in "$W/leader_prompt" "The target of this retry is .$E1." \
  "...and the prompt names the exact target, rather than hoping the leader picks it"
present_in "$W/leader_prompt" "the other five actions are unavailable" \
  "...and closes the action: a retry is action 1 on one named run"
# THE OPERATOR'S ONE HANDLE ON A RETRY TICK. Asserted on MEANING -- a retry, and
# which run -- rather than on the wording, so rephrasing the line does not fail
# a test for nothing; but the line itself is how an operator tells a retry tick
# from an ordinary one in the log, and nothing else pinned it.
printf '%s' "$out" | grep -i retry | grep -q "$E1" \
  && ok "the tick log says a retry happened and names the target" \
  || bad "the tick log says a retry happened and names the target -- $out"
# THE FOUR BANS, each of which has already cost a finding. Asserted on stable
# phrases rather than on the whole file, so rewording the prose does not fail
# the test while deleting a ban does.
present_in "$W/leader_prompt" "No rounding-equivalence" \
  "the retry block bans rounding-equivalence (0.0817 vs 0.0818 was not 'unchanged')"
present_in "$W/leader_prompt" "No superlatives and no firsts" \
  "...bans superlatives and firsts (val7_nop90 had 0.286152780)"
# UNCONDITIONALLY, and that is a fix rather than a tightening: the old escape
# hatch ("unless you paste the JSON rows that establish the comparison") had
# nowhere legal to land, because `Evidence:` is constrained to output for
# figures NOT in the row, and a row is neither.
present_in "$W/leader_prompt" "unconditionally" \
  "...and the ban is unconditional, since its escape hatch had nowhere to land"
absent_from "$RL/retry-prompt.md" "unless you paste the JSON rows" \
  "...so the unsatisfiable escape hatch is gone"
# BAN 3 MUST NOT CONTRADICT THE TITLE IT GOVERNS. It used to say the title
# "names the action and the config, nothing else" while the template's title
# line carries four things (action, config, digest, target run id), so a literal
# agent had to violate one of the two. The ban now defers to the template line
# and keeps its real point.
present_in "$W/leader_prompt" "exactly the line the template gives" \
  "ban 3 defers to the template's own title line instead of contradicting it"
present_in "$W/leader_prompt" "whether a metric got better or worse" \
  "...and still bans a metric direction in the title (a -1.43pp MAE move is not an 'improvement')"
present_in "$W/leader_prompt" "except the pre-registered" \
  "...bans comparison to any config but the pre-registered vs"
# AND THE FIELD SPEC CARRIES BAN 4'S CARVE-OUT. "No other config" dropped it, so
# a literal agent omitted the comparator entirely -- leaving a `hold` claim with
# nothing to hold against, which is what `--vs` exists to prevent and what the
# verifier checks.
# SHORTENED DELIBERATELY: the prompt wraps this sentence at 80 columns,
  # and reflowing agent-facing prose to satisfy a single-line grep is the
  # tail wagging the dog. The distinctive half sits on one line.
  present_in "$RL/retry-prompt.md" "No config other than the" \
  "the Claim field names the vs exception rather than forbidding every config"
# THE TEMPLATE SAYS IT REPLACES THE STANDING ONE. The leader read `tick-prompt`'s
# output template ~240 lines earlier and it is still in force; two live
# templates is a contradiction the leader has to resolve on its own.
present_in "$W/leader_prompt" "REPLACES the one under" \
  "...and the reduced template says it replaces the standing one"
# THE REDUCED TEMPLATE, with `tick-prompt.md`'s five mandatory fields.
# THE RETRY TEMPLATE'S OWN `Target run:` LINE, not `tick-prompt.md`'s. A bare
# `**Target run:**` is already in the leader prompt from the standing template,
# so asserting that would have passed with no retry block at all.
present_in "$W/leader_prompt" "Target run:\\*\\* <the target named above>" \
  "the reduced template keeps the mandatory Target run field, bound to the named target"
present_in "$W/leader_prompt" "^# Retry: <config>@<config_digest>" \
  "the reduced template's title line is the shrunken one"
# NO INDEPENDENT-COHORT FIGURE. `frontier.py`'s `configs{}` is built only from
# rows where every bar PASSED, so `independent_cohorts` does not exist for a
# rejected run -- which is the whole population a retry is about. The series
# `holdout` window carries the same meaning and does exist.
# ASSERTED ON THE TEMPLATE FILE, not on the assembled prompt: `holdout` appears
# in the frontier prose anyway, so a prompt-level assertion would pass whether
# the template mentioned it or not, and `independent_cohorts` could appear there
# for an unrelated reason and fail a test about this file.
# THE `Action taken:` FIELD HAS A CITABLE SOURCE OF ITS OWN. Pointing it only
# at the feedback block dangles: that block exists only when the previous
# escalation's reason extracted non-empty, and `retryinject` above has no
# escalation on disk at all -- so the leader would be told to name something it
# cannot see. The row carries the answer independently.
# THE PASTED ROW -- the Critical this round fixed. Every figure the template
# mandates is asserted as a REAL value in the prompt the leader received.
present_in "$W/leader_prompt" '```json' \
  "the retry block pastes the target row in a fenced json block"
for want in '"config_digest": "0a217f40da8f"' "\"vs\": \"$E2\"" \
            '"tol": 0.005' '"bar": "p90_miss_tail"' '0.280830734' \
            '"2026-08-01"' '"2026-08-15"' \
            '"target"' '"comparator"' \
            '"claim": "kept"' '"direction": "improve"' \
            '0.298876182'; do
  if grep -qF "$want" "$W/leader_prompt"; then
    ok "the leader can see $want"
  else
    bad "the leader can see $want -- not in the assembled prompt"
  fi
done
# AND THE PASTE IS WHAT MAKES IT VISIBLE. Without this the assertions above
# would also pass if the markdown render happened to carry the figure -- it does
# not: its row table is `| when | config | verdict | claimed | outcome | written
# up |` and its only figure is a `%.4g` cell for the WINNING config, which
# cannot express the 4-significant-figure distinction ban 1 demands.
awk '/^```json/{skip=1; next} skip && /^```/{skip=0; next} !skip' \
  "$W/leader_prompt" >"$W/prompt-sans-row"
absent_from "$W/prompt-sans-row" "0.280830734" \
  "the full-precision metric reaches the leader ONLY through the pasted row"
absent_from "$W/prompt-sans-row" 'config_digest": "0a217f40da8f' \
  "...as does the row's config_digest"
# BOTH OPERANDS OF A SIGNED DELTA, which is what ban 1 demands and what the
# target row alone could not supply: it carries `vs` as an ID, not as a figure.
absent_from "$W/prompt-sans-row" "0.298876182" \
  "...and so does the COMPARATOR's metric, the second operand of ban 1's delta"
# LABELLED, so the leader cannot mix them up. Asserted structurally rather than
# by grepping two numbers: the target's figure must sit under `target` and the
# comparator's under `comparator`.
python3 - "$W/leader_prompt" <<'PYLBL' \
  && ok "the pasted block labels which row is the target and which the comparator" \
  || bad "the pasted block labels which row is the target and which the comparator"
import json, re, sys
text = open(sys.argv[1]).read()
block = re.search(r"```json\n(.*?)\n```", text, re.S)
if not block:
    raise SystemExit("no json block")
got = json.loads(block.group(1))
assert got["target"]["metrics"]["p90_miss_tail"] == 0.280830734, got["target"]
assert got["comparator"]["metrics"]["p90_miss_tail"] == 0.298876182, got["comparator"]
assert got["holdout"] == ["2026-08-01", "2026-08-15"], got["holdout"]
# `claim` AND `direction` AS REAL VALUES, not merely present: the fixture used
# to register nothing, so these were "unregistered" and "lower".
assert got["target"]["claim"] == "kept", got["target"]["claim"]
assert got["target"]["direction"] == "improve", got["target"]["direction"]
# `Action taken:` CITES `escalation_latest` FIRST, so it must be populated in
# the world the tests exercise -- otherwise this is the one case where a
# mandated field has no source at all, which is the defect class of this round.
assert got["target"]["escalation_latest"].startswith("escalations/"), \
    got["target"]["escalation_latest"]
PYLBL

present_in "$RL/retry-prompt.md" "escalation_latest" \
  "the reduced template sources the escalation from the row, not only from the feedback block"
absent_from "$RL/retry-prompt.md" "independent_cohorts" \
  "the reduced template asks for no independent-cohort count"
present_in "$RL/retry-prompt.md" "holdout" \
  "...and asks for the series holdout window instead"

# --------------------------------------------------------------------------
setup retrynone
# NO STORED TARGET IS NOT A RETRY. Without this the block would be
# unconditional, and every ordinary tick would be told to shrink an entry it
# has not written yet.
canon_results "$E1"
printf 'none\n' >"$W/state/last-reject-target"
entry_for "$E1"
echo "VERDICT: AGREE" >"$W/codex_reply"
out="$(run_tick)"
absent_from "$W/leader_prompt" "This tick is a RETRY" \
  "a stored target of none does not inject the retry block"

# --------------------------------------------------------------------------
setup retrystale
# A STORED TARGET THAT IS NO LONGER SUPPRESSIBLE. The frontier this tick built
# does not carry it, so retirement can never bound a retry of it -- and the same
# predicate that refuses to suppress the streak must refuse to instruct a retry,
# or the tick would suppress a brake for a target it will not name.
#
# THE COPILOT IS DOWN ON PURPOSE. The verifier-failure branch leaves
# `last-reject-target` exactly as it was, so `none` afterwards can ONLY have
# come from `take_retry_target` clearing it. Under AGREE or a DISAGREE the verdict
# path clears it too, and the assertion would have passed with no clearing code
# at all.
canon_results "$E2"
printf '%s\n' "$E1" >"$W/state/last-reject-target"
entry_for "$E1"
: >"$W/codex_fails"
out="$(run_tick QF_TICK_MAX_VERIFIER_FAILS=9 QF_TICK_COPILOT_TRIES=1)"
absent_from "$W/leader_prompt" "This tick is a RETRY" \
  "a stored target that has left the scoreboard does not inject the retry block"
state_is last-reject-target none \
  "...and is cleared, so the next rejection counts as a new episode"
printf '%s' "$out" | grep -q "the stored retry target $E1 is absent" \
  && ok "...and the tick log says which state disqualified it" \
  || bad "...and the tick log says which state disqualified it -- $out"

# --------------------------------------------------------------------------
setup retryrecorded
# ALREADY RECORDED BY ANOTHER ROUTE. A retry of a row the journal now records
# would re-narrate a finding that is already in the journal.
canon_results "$E1"
entry_for "$E1"
echo "VERDICT: AGREE" >"$W/codex_reply"
run_tick >/dev/null
# The AGREE above put a committed-free but recorded entry in the journal; the
# frontier reads COMMITTED content only, so the row is made `recorded` by
# committing it the way the publish step would have.
if [ "$CAN_COMMIT" = 0 ] && ! plumb_commit; then
  skip "a recorded stored target does not inject the retry block" \
       "the journal could not be committed here"
else
  plumb_commit || true
  printf '%s\n' "$E1" >"$W/state/last-reject-target"
  : >"$W/codex_fails"
  out="$(run_tick QF_TICK_MAX_VERIFIER_FAILS=9 QF_TICK_COPILOT_TRIES=1)"
  absent_from "$W/leader_prompt" "This tick is a RETRY" \
    "a recorded stored target does not inject the retry block"
  state_is last-reject-target none \
    "...and a recorded stored target is cleared too"
fi

# --------------------------------------------------------------------------
setup promptpreflight
# EVERY PROMPT FILE FAILS OPEN WHEN IT IS `cat`ED: the surrounding group
# succeeds, the tick logs nothing unusual, and an agent runs with its
# instructions missing. For `retry-prompt.md` that is the worst of the three,
# because the retry block's OTHER half still emits -- the leader would be
# steered onto action 1 on one named row with the shrink instruction, the closed
# action list and all four bans absent, which is the 2026-09-04 rewrite with the
# brake off and strictly worse than not retrying.
#
# RUN FROM A COPY of the research-loop directory, not by chmod-ing the real
# files: a test that leaves a repo file unreadable when it aborts is a test that
# breaks the next run for reasons nothing explains.
mkdir -p "$W/rl" && cp -R "$RL"/. "$W/rl/" 2>/dev/null
_real_tick="$TICK"
for _pf in tick-prompt.md verify-prompt.md retry-prompt.md; do
  for _how in missing empty; do
    # ALL THREE RESTORED EACH TIME, not just the one under test: restoring only
    # the current file left the PREVIOUS iteration's file still broken, so every
    # case after the first was really re-asserting the first one's abort.
    for _r in tick-prompt.md verify-prompt.md retry-prompt.md; do
      cp -f "$RL/$_r" "$W/rl/$_r"
    done
    case "$_how" in
      missing) rm -f "$W/rl/$_pf" ;;
      empty)   : >"$W/rl/$_pf" ;;
    esac
    rm -f "$W/leader_prompt"
    out="$(TICK="$W/rl/tick.sh" run_tick)"
    printf '%s' "$out" | grep -q "Refusing to run" \
      && printf '%s' "$out" | grep -q "$_pf" \
      && ok "a $_how $_pf stops the tick and names the file" \
      || bad "a $_how $_pf stops the tick and names the file -- $out"
    [ ! -e "$W/leader_prompt" ] \
      && ok "...before either agent is invoked" \
      || bad "...before either agent is invoked"
  done
done
TICK="$_real_tick"

echo
echo "pass=$pass fail=$fail skip=$skip"
[ "$fail" = 0 ]
