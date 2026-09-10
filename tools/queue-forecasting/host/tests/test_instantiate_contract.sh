#!/usr/bin/env bash
# Task 18: the contract templates, and pinning one to a promoted baseline.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="$(dirname "$HERE")"
SCRIPT="$HOST/instantiate-contract.sh"

pass=0; fail=0
ok()  { echo "ok    $*"; pass=$((pass+1)); }
bad() { echo "FAIL  $*"; fail=$((fail+1)); }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
export QF_BASELINE_STORE="$TMP/store"
mkdir -p "$QF_BASELINE_STORE"

# A promoted baseline, built the way promote-baseline.sh builds one.
make_baseline() {  # -> prints the hash
  local src="$TMP/src.$RANDOM"; mkdir -p "$src"
  python3 - "$src" "$1" <<'PY'
import json, os, sys
d, tag = sys.argv[1], sys.argv[2]
with open(os.path.join(d, "baseline_predictions.ndjson"), "w") as fh:
    fh.write(json.dumps({"task_id": tag, "run_id": 0,
                         "pending_at": "2026-08-01T00:00:00+00:00"}) + "\n")
with open(os.path.join(d, "2026-08-01.json"), "w") as fh:
    json.dump({"day": "2026-08-01"}, fh)
PY
  PYTHONPATH="$HOST/shared" python3 - "$src" "$QF_BASELINE_STORE" <<'PY'
import json, os, shutil, sys
import baseline
src, store = sys.argv[1], sys.argv[2]
manifest = baseline.describe(src, exclude_dates=[])
manifest["baseline_hash"] = baseline.baseline_hash(manifest)
target = os.path.join(store, manifest["baseline_hash"])
os.makedirs(target, exist_ok=True)
for name in manifest["files"]:
    shutil.copy2(os.path.join(src, name), os.path.join(target, name))
with open(os.path.join(target, "MANIFEST.json"), "w") as fh:
    json.dump(manifest, fh)
print(manifest["baseline_hash"])
PY
}

BH="$(make_baseline one | tail -1)"
[ "${#BH}" -eq 64 ] && ok "fixture promoted a baseline ($BH)" \
  || { bad "fixture produced no baseline hash: '$BH'"; echo "pass=$pass fail=$fail"; exit 1; }

# --- the shipped templates ------------------------------------------------
for t in wait_time.v1 run_duration.v1 wait_time.v2; do
  tpl="$HOST/contracts/$t.json.in"
  [ -f "$tpl" ] && ok "$t template exists" || bad "$t template missing"
  grep -q '@BASELINE_HASH@' "$tpl" && ok "$t carries the placeholder" \
    || bad "$t has no placeholder: it may have been instantiated in place"
  # THE PLACEHOLDER MUST NOT VALIDATE. That is what makes the incompleteness a
  # refusal rather than a note somebody has to read.
  if PYTHONPATH="$HOST/shared" python3 -c '
import json, sys, contract
contract.validate(json.load(open(sys.argv[1])))' "$tpl" 2>/dev/null; then
    bad "$t validates WITH the placeholder still in it"
  else
    ok "$t refuses to validate until a baseline is pinned"
  fi
done

# --- instantiation --------------------------------------------------------
work="$TMP/contracts"; mkdir -p "$work"
cp "$HOST/contracts/wait_time.v1.json.in" "$work/"
out="$work/wait_time.v1.json"

if "$SCRIPT" "$work/wait_time.v1.json.in" "$BH" >"$TMP/log" 2>&1; then
  ok "instantiation succeeds against a promoted baseline"
else
  bad "instantiation failed: $(tr '\n' ' ' < "$TMP/log" | cut -c1-200)"
fi
[ -f "$out" ] && ok "it wrote the contract" || bad "no contract written"

if grep -q '@BASELINE_HASH@' "$out" 2>/dev/null; then
  bad "the placeholder survived into the output"
else
  ok "the placeholder was substituted"
fi

# The written file must LOAD -- which rehashes it -- so the declared
# contract_hash is verified rather than merely present.
if PYTHONPATH="$HOST/shared" python3 -c '
import sys, contract
body, digest = contract.load(sys.argv[1])
assert body["baseline_hash"] == sys.argv[2], body["baseline_hash"]
print(digest)' "$out" "$BH" >"$TMP/h" 2>"$TMP/e"; then
  ok "the output loads and its declared hash verifies ($(cut -c1-12 "$TMP/h"))"
else
  bad "the output does not load: $(tr '\n' ' ' < "$TMP/e" | cut -c1-200)"
fi

# --- the v2 contract ------------------------------------------------------
# Pinned in the WORK COPY, like v1 above, and never in `$HOST/contracts`: once
# the operator commits the real `wait_time.v2.json` this test would otherwise
# refuse ("already exists") and then delete the tracked contract on its way out.
H2="$(make_baseline v2 | tail -1)"
cp "$HOST/contracts/wait_time.v2.json.in" "$work/"
OUT2="$("$SCRIPT" "$work/wait_time.v2.json.in" "$H2" 2>&1)" \
  && ok "v2 template pins to a promoted baseline" \
  || bad "v2 template refused: $OUT2"
PYTHONPATH="$HOST/shared" python3 - "$work/wait_time.v2.json" <<'PY' \
  && ok "v2 validates: four gates, four report metrics, no raw tail gate" \
  || bad "v2 did not validate as expected"
import json, sys, contract
body, _ = contract.load(sys.argv[1])
roles = {k: v.get("role", "gate") for k, v in body["metrics"].items()}
assert sum(r == "gate" for r in roles.values()) == 4, roles
assert sum(r == "report" for r in roles.values()) == 4, roles
assert roles["p90_miss_tail_guarded"] == "report", roles
assert "p90_miss_tail" not in body["metrics"], "raw tail miss must not be in v2"
PY

# --- refusals -------------------------------------------------------------
"$SCRIPT" "$work/wait_time.v1.json.in" "$BH" >"$TMP/log" 2>&1 \
  && bad "a second instantiation overwrote an existing contract" \
  || { grep -qi 'versioned' "$TMP/log" && ok "re-instantiating is refused, naming versioning" \
       || bad "refused without explaining why: $(tr '\n' ' ' < "$TMP/log" | cut -c1-160)"; }

cp "$HOST/contracts/run_duration.v1.json.in" "$work/"
"$SCRIPT" "$work/run_duration.v1.json.in" "$(printf 'f%.0s' $(seq 64))" \
  >"$TMP/log" 2>&1 \
  && bad "an unpromoted baseline was accepted" \
  || { grep -q 'no promoted baseline' "$TMP/log" && ok "an unpromoted baseline is refused" \
       || bad "wrong refusal: $(tr '\n' ' ' < "$TMP/log" | cut -c1-160)"; }

for badhash in "" "abc" "A$(printf 'a%.0s' $(seq 63))" "$(printf 'a%.0s' $(seq 65))"; do
  "$SCRIPT" "$work/run_duration.v1.json.in" "$badhash" >/dev/null 2>&1 \
    && bad "accepted a malformed hash '$badhash'" \
    || ok "rejected malformed hash '$(printf '%.10s' "${badhash:-<empty>}")'"
done

# AN EDITED BASELINE. The contract must not be pinnable to a directory whose
# manifest no longer hashes to its own name.
python3 - "$QF_BASELINE_STORE/$BH/MANIFEST.json" <<'PY'
import json, sys
p = sys.argv[1]
with open(p) as fh:
    m = json.load(fh)
m["ndjson_rows"] = (m.get("ndjson_rows") or 0) + 5   # leaves baseline_hash
with open(p, "w") as fh:
    json.dump(m, fh)
PY
cp "$HOST/contracts/wait_time.v1.json.in" "$work/wait_time.edited.json.in"
"$SCRIPT" "$work/wait_time.edited.json.in" "$BH" >"$TMP/log" 2>&1 \
  && bad "pinned a contract to a baseline edited since promotion" \
  || { grep -qi 'does not hash\|does not verify' "$TMP/log" \
       && ok "a baseline edited since promotion cannot be pinned" \
       || bad "wrong refusal for an edited baseline: $(tr '\n' ' ' < "$TMP/log" | cut -c1-160)"; }

# A template that is not one.
printf '{}' > "$work/notatemplate.json"
"$SCRIPT" "$work/notatemplate.json" "$BH" >/dev/null 2>&1 \
  && bad "accepted a file that is not a .json.in template" \
  || ok "a non-template path is refused"

# --- --activate -----------------------------------------------------------
# The cutover. Appended here, in a directory of its own, so nothing above is
# disturbed: `--activate` writes an ACTIVE file NEXT TO the contract it wrote,
# and a shared work directory would let one case's setting decide another's.
act="$TMP/act"; mkdir -p "$act"
H3="$(make_baseline three | tail -1)"
cp "$HOST/contracts/wait_time.v1.json.in" "$act/"
cp "$HOST/contracts/run_duration.v1.json.in" "$act/"
"$SCRIPT" "$act/wait_time.v1.json.in" "$H3" --activate >"$TMP/log" 2>&1 \
  && ok "--activate is accepted" \
  || bad "--activate refused: $(tr '\n' ' ' < "$TMP/log" | cut -c1-200)"
CH3="$(PYTHONPATH="$HOST/shared" python3 -c '
import json, sys
print(json.load(open(sys.argv[1]))["contract_hash"])' "$act/wait_time.v1.json")"
grep -qx "wait_time $CH3" "$act/ACTIVE" \
  && ok "--activate wrote the target line into ACTIVE" \
  || bad "ACTIVE does not name the contract: $(tr '\n' ' ' < "$act/ACTIVE" 2>&1)"

# THE POINT OF THE FILE: the contract stays published. The whole reason this is
# a setting and not a deletion is that the frontier reads old bodies.
[ -f "$act/wait_time.v1.json" ] && ok "activating does not remove the contract" \
  || bad "the contract file disappeared"

# A SECOND TARGET does not displace the first: the file is keyed by target.
"$SCRIPT" "$act/run_duration.v1.json.in" "$H3" --activate >"$TMP/log" 2>&1 \
  && ok "a second target activates too" \
  || bad "second activation refused: $(tr '\n' ' ' < "$TMP/log" | cut -c1-200)"
grep -qx "wait_time $CH3" "$act/ACTIVE" \
  && ok "activating another target leaves the first line alone" \
  || bad "the wait_time line was lost when run_duration was activated"

# IDEMPOTENT, and this is the case that matters: a target must never end up
# with two lines, because `qfd.read_active_contracts` keeps the FIRST -- so an
# appended second line would leave the OLD contract active while the command
# reported success.
cp "$HOST/contracts/wait_time.v2.json.in" "$act/"
"$SCRIPT" "$act/wait_time.v2.json.in" "$H3" --activate >"$TMP/log" 2>&1 \
  && ok "activating a v2 over a v1 is accepted" \
  || bad "v2 activation refused: $(tr '\n' ' ' < "$TMP/log" | cut -c1-200)"
CH4="$(PYTHONPATH="$HOST/shared" python3 -c '
import json, sys
print(json.load(open(sys.argv[1]))["contract_hash"])' "$act/wait_time.v2.json")"
[ "$(grep -c '^wait_time ' "$act/ACTIVE")" -eq 1 ] \
  && ok "one line per target after a re-activation" \
  || bad "ACTIVE has $(grep -c '^wait_time ' "$act/ACTIVE") wait_time lines"
grep -qx "wait_time $CH4" "$act/ACTIVE" \
  && ok "the line was REPLACED with v2, not appended beside v1" \
  || bad "ACTIVE still names v1 after activating v2"

# MODE 0644 ON THE PUBLISHED CONTRACT. `mktemp` makes 0600 and this runs as
# root, so a missing chmod leaves a contract that is committed and unreadable by
# qfd: omitted from `available_contracts`, and therefore an ACTIVE entry the
# dispatcher cannot resolve right after the script said "activated".
mode="$(stat -c '%a' "$act/wait_time.v1.json" 2>/dev/null \
        || stat -f '%Lp' "$act/wait_time.v1.json")"
[ "$mode" = "644" ] && ok "the published contract is mode 0644" \
  || bad "the published contract is mode $mode, not 644: qfd cannot read it"

# AND THE DEFAULT IS UNCHANGED: no flag, no ACTIVE file. Publishing a rule and
# cutting over to it are two decisions.
noact="$TMP/noact"; mkdir -p "$noact"
cp "$HOST/contracts/wait_time.v1.json.in" "$noact/"
# BOTH templates copied. The unknown-option case below names the run_duration
# one, and against a path that does not exist the script refuses for "not a
# file" -- which would pass the assertion while proving nothing about option
# parsing.
cp "$HOST/contracts/run_duration.v1.json.in" "$noact/"
"$SCRIPT" "$noact/wait_time.v1.json.in" "$H3" >"$TMP/log" 2>&1 \
  && [ ! -e "$noact/ACTIVE" ] \
  && ok "without --activate nothing is activated" \
  || bad "an ACTIVE file appeared without --activate"

"$SCRIPT" "$noact/run_duration.v1.json.in" "$H3" --bogus >"$TMP/log" 2>&1 \
  && bad "an unknown option was accepted" \
  || { grep -q 'unknown option' "$TMP/log" \
       && ok "an unknown option is refused, naming it" \
       || bad "wrong refusal for an unknown option"; }

echo
echo "instantiate-contract: pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
