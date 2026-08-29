#!/usr/bin/env bash
# Tests `phase2-setup.sh`'s stale-unit check.
#
# WHY THIS FILE EXISTS. `mirror-refresh` reset the checkout and restarted the
# daemon while /etc/systemd/system held a unit copied from an older commit, so
# the dispatcher ran NEW CODE UNDER OLD CONFIGURATION -- silently. 2b-1 added
# `Environment=PYTHONPATH=.../host/shared` to qf-dispatch.service, the refresh
# did not reinstall it, and every `qf extract` failed with
# `ModuleNotFoundError: No module named 'extract_spec'`: an error about a module,
# for a cause that was a missing directive.
#
# The check that closes it must ignore substituted placeholders and notice
# everything else. Its first implementation chained two `sed` expressions to
# derive the excluded keys, and the second fired on the first's output --
# reducing `Environment=QFD_ADMIN_UID=%%DEPLOY_UID%%` to the key `Environment`,
# which excluded EVERY environment line. It passed on identical files, which is
# what a check that has stopped checking looks like from outside.
#
#   ./tests/test_unit_drift.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETUP="$HERE/../phase2-setup.sh"
[ -f "$SETUP" ] || { echo "cannot find $SETUP" >&2; exit 2; }

# shellcheck disable=SC1090
source <(sed -n '/^unit_matches()/,/^}/p; /^_unit_key_filter()/,/^}/p' "$SETUP")
for fn in unit_matches _unit_key_filter; do
  declare -F "$fn" >/dev/null || { echo "extraction missed $fn" >&2; exit 2; }
done

pass=0; fail=0
ok()  { echo "ok    $1"; pass=$((pass + 1)); }
bad() { echo "FAIL  $1"; fail=$((fail + 1)); }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
cat > "$TMP/checkout" <<'UNIT'
[Service]
ExecStart=/usr/bin/python3 /srv/x/qfd.py
Environment=PYTHONPATH=/srv/x/host/shared
Environment=QFD_ADMIN_UID=%%DEPLOY_UID%%
Environment=QFD_LOG_CAP_MB=16
User=qfd
UNIT

variant() { sed "s/%%DEPLOY_UID%%/999/; ${1:-}" "$TMP/checkout" > "$TMP/installed"; }

expect() {
  if unit_matches "$TMP/checkout" "$TMP/installed"; then
    [ "$1" = match ] && ok "$2" || bad "$2 (matched, wanted drift)"
  else
    [ "$1" = drift ] && ok "$2" || bad "$2 (drifted, wanted match)"
  fi
}

variant
expect match "a substituted placeholder is not drift"

variant 's|^Environment=PYTHONPATH=.*|Environment=PYTHONPATH=/old|'
expect drift "a changed PYTHONPATH is drift"

variant '/^Environment=PYTHONPATH=/d'
expect drift "a REMOVED directive is drift"

variant 's|^ExecStart=.*|ExecStart=/old/python|'
expect drift "a changed ExecStart is drift"

variant 's/^Environment=QFD_LOG_CAP_MB=16/Environment=QFD_LOG_CAP_MB=99/'
expect drift "a changed non-placeholder env value is drift"

variant 's/^User=qfd/User=root/'
expect drift "a changed User is drift"

variant 's/999/1234/'
expect match "the substituted VALUE may differ"

# The regression that made the first implementation vacuous: one substituted
# environment line must not excuse the others.
variant 's/^Environment=QFD_ADMIN_UID=999/Environment=QFD_ADMIN_UID=1/; s|^Environment=PYTHONPATH=.*|Environment=PYTHONPATH=/old|'
expect drift "one substituted env line does not excuse the others"

# --- EVERY unit is in the drift list --------------------------------------
# WHY THIS IS A TEST AND NOT A READING. `assert_units_current` is what stops
# `mirror-refresh` restarting the daemon into configuration from an older commit,
# and it works off a HAND-WRITTEN list. A phase that adds a unit and forgets the
# list gets exactly the failure the function exists to prevent -- silently, and
# one phase later, which is what happened: the evaluator's two units were absent
# from it until this test was written.
LIST="$(sed -n '/^assert_units_current()/,/^}/p' "$SETUP")"
missing=0
while IFS= read -r unit; do
  rel="${unit#"$HERE"/../}"
  if printf '%s' "$LIST" | grep -q "$rel"; then
    ok "$rel is in the drift list"
  else
    bad "$rel is NOT in assert_units_current's list: mirror-refresh would
     restart into a stale copy of it without saying so"
    missing=$((missing + 1))
  fi
done < <(find "$HERE/.." -mindepth 2 -maxdepth 2 \
              \( -name '*.service' -o -name '*.socket' -o -name '*.timer' \) \
         | sort)
[ "$missing" -eq 0 ] || echo "  (remedy: add them, with the setup script that installs each)"

echo
echo "unit-drift: pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
