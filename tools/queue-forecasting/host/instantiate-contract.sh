#!/usr/bin/env bash
# Pin a contract template to a promoted baseline. Phase 2c Task 18.
#
#   ./instantiate-contract.sh contracts/wait_time.v1.json.in <baseline_hash>
#
# WHY A TEMPLATE AND NOT A FINISHED FILE. A contract must name the baseline it
# judges against (D25): "MAE improves by >=15% over baseline" is not a claim
# until the baseline is named. But `baseline_hash` is the content key of a
# directory that has to be PROMOTED before it exists, so a contract cannot be
# written complete -- and one shipped with a plausible-looking placeholder hash
# would be a rule that validates and judges against nothing.
#
# So the template carries `@BASELINE_HASH@`, which `contract.validate` refuses
# because it is not 64 hex. The incompleteness is enforced by the validator and
# visible in `ls`, rather than resting on somebody noticing.
#
# THE OUTPUT IS COMMITTED, not generated at run time. The whole control in NC9 is
# that the contract lives in the trusted checkout and the candidate cannot edit
# it; a file this script regenerated on the host each night would be a contract
# whose provenance was a shell argument.
set -Eeuo pipefail
trap 'echo "FATAL: ${BASH_SOURCE[0]}:${LINENO} failed (exit $?)" >&2; exit 1' ERR

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STORE="${QF_BASELINE_STORE:-/var/lib/qf-baselines}"

info() { echo "  $*"; }
ok()   { echo "  ok    $*"; }
die()  { echo "FATAL: $*" >&2; exit 2; }

TEMPLATE="${1:-}"
HASH="${2:-}"
[ -n "$TEMPLATE" ] && [ -n "$HASH" ] \
  || die "usage: $0 <contract template .json.in> <baseline_hash>"
[ -f "$TEMPLATE" ] || die "$TEMPLATE is not a file"
case "$TEMPLATE" in
  *.json.in) ;;
  *) die "$TEMPLATE is not a .json.in template" ;;
esac
case "$HASH" in
  ""|*[!0-9a-f]*) die "baseline_hash must be 64 lowercase hex, got '$HASH'" ;;
esac
[ "${#HASH}" -eq 64 ] || die "baseline_hash must be 64 characters, got ${#HASH}"

# THE BASELINE MUST ACTUALLY BE PROMOTED, and its manifest must hash to its own
# name -- the same check `_probe_baseline` makes, for the same reason: a content
# key is the one identity that can be verified rather than trusted. Pinning a
# contract to an unpublished hash produces a rule that refuses every evaluation,
# and it would refuse in a way that looks like a missing baseline rather than
# like a mistake made here.
[ -d "$STORE/$HASH" ] || die "no promoted baseline $HASH in $STORE.
  'qf baselines' lists what is promoted; promote-baseline.sh promotes one."
PYTHONPATH="$HERE/dispatcher" python3 - "$STORE/$HASH/MANIFEST.json" "$HASH" <<'PY' \
  || die "the promoted baseline does not verify; refusing to pin a contract to it"
import json, sys
import baseline
path, want = sys.argv[1], sys.argv[2]
with open(path) as fh:
    manifest = json.load(fh)
if manifest.get("baseline_hash") != want:
    sys.exit(f"{path} declares {manifest.get('baseline_hash')}, not {want}")
if baseline.baseline_hash(manifest) != want:
    sys.exit(f"{path} does not hash to {want}: edited since promotion")
PY
ok "baseline $HASH verifies against its manifest"

OUT="${TEMPLATE%.in}"
[ ! -e "$OUT" ] || die "$OUT already exists. A contract is VERSIONED, not
  edited: bump v1 -> v2 rather than repointing an existing one, or every result
  that cited the old contract_hash becomes unreadable."

TMP="$(mktemp "${OUT}.XXXXXX")"
trap 'rm -f "$TMP"' EXIT
sed "s/@BASELINE_HASH@/$HASH/" "$TEMPLATE" > "$TMP"
if grep -q '@BASELINE_HASH@' "$TMP"; then
  die "the substitution did not take; $TEMPLATE may use a different placeholder"
fi

# VALIDATED AND HASHED BEFORE PUBLICATION, with the hash written into the file so
# a later reader can verify rather than trust. `contract.load` refuses a file
# whose declared hash disagrees with its body, so this is not decoration.
PYTHONPATH="$HERE/shared" python3 - "$TMP" <<'PY' \
  || die "the instantiated contract does not validate"
import json, sys
import contract
path = sys.argv[1]
with open(path) as fh:
    body = contract.validate(json.load(fh))
body["contract_hash"] = contract.contract_hash(body)
with open(path, "w") as fh:
    json.dump(body, fh, indent=2, sort_keys=True)
    fh.write("\n")
PY

mv -T "$TMP" "$OUT"
trap - EXIT
CH="$(PYTHONPATH="$HERE/shared" python3 -c '
import json, sys
print(json.load(open(sys.argv[1]))["contract_hash"])' "$OUT")"
ok "wrote $OUT"
info "contract_hash: $CH"
info "baseline_hash: $HASH"
info ""
info "COMMIT IT. The control NC9 asserts is that this file is in the trusted"
info "checkout and the candidate cannot edit it; a file that exists only on this"
info "host is a rule with no provenance."
