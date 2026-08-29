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
for t in wait_time run_duration; do
  tpl="$HOST/contracts/$t.v1.json.in"
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
cp "$HOST/contracts/wait_time.v1.json.in" "$work/wait_time.v2.json.in"
"$SCRIPT" "$work/wait_time.v2.json.in" "$BH" >"$TMP/log" 2>&1 \
  && bad "pinned a contract to a baseline edited since promotion" \
  || { grep -qi 'does not hash\|does not verify' "$TMP/log" \
       && ok "a baseline edited since promotion cannot be pinned" \
       || bad "wrong refusal for an edited baseline: $(tr '\n' ' ' < "$TMP/log" | cut -c1-160)"; }

# A template that is not one.
printf '{}' > "$work/notatemplate.json"
"$SCRIPT" "$work/notatemplate.json" "$BH" >/dev/null 2>&1 \
  && bad "accepted a file that is not a .json.in template" \
  || ok "a non-template path is refused"

echo
echo "instantiate-contract: pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
