#!/usr/bin/env bash
# Tests `promote-baseline.sh` end to end, without being root.
#
# `QF_BASELINE_STORE_OWNER` parameterises the owner the store must have -- a
# parameter rather than a literal `root` precisely so this path can be exercised.
# Production leaves it at the default; the CHECK is what enforces the boundary
# either way, and this suite is what shows the check works.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROMOTE="$HERE/../promote-baseline.sh"
[ -x "$PROMOTE" ] || { echo "cannot run $PROMOTE" >&2; exit 2; }

pass=0; fail=0
ok()  { echo "ok    $1"; pass=$((pass + 1)); }
bad() { echo "FAIL  $1"; fail=$((fail + 1)); }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
export QF_BASELINE_STORE="$TMP/store"
export QF_BASELINE_STORE_OWNER="$(id -un)"

make_source() {  # make_source <dir> <rows>
  local dir="$1" rows="${2:-3}" i
  mkdir -p "$dir"
  : > "$dir/baseline_predictions.ndjson"
  for ((i = 0; i < rows; i++)); do
    printf '{"task_id":"t%d","run_id":0,"pending_at":"2026-08-2%dT01:00:00Z"}\n' \
      "$i" "$((i % 3))" >> "$dir/baseline_predictions.ndjson"
  done
  echo '{"day":"2026-08-20"}' > "$dir/2026-08-20.json"
  echo '{"day":"2026-08-21"}' > "$dir/2026-08-21.json"
}

run() { "$PROMOTE" "$@" 2>&1; }

# --- a good set publishes once -------------------------------------------
make_source "$TMP/src"
out="$(run "$TMP/src")"
if printf '%s' "$out" | grep -q 'published'; then
  ok "a valid baseline set publishes"
else
  bad "a valid set did not publish: $out"
fi
published="$(find "$QF_BASELINE_STORE" -mindepth 1 -maxdepth 1 -type d | wc -l)"
[ "$published" = 1 ] && ok "exactly one artifact" || bad "$published artifacts"

# --- the sidecar the pipeline actually writes -----------------------------
# THE FAILURE AN OPERATOR HITS FIRST. `ensure_baseline_ndjson.sh` writes a
# coverage sidecar next to the NDJSON, and the trainer's cache keeps per-day
# files from every earlier cohort of the same policy -- so the directory the
# pipeline produces is never promotable as-is. The refusal is correct (the store
# is closed-world), and what matters is that it names the fix rather than leaving
# somebody to work out why one file is unacceptable.
make_source "$TMP/sidecar"
echo '{"from":"2026-08-01","to":"2026-08-27"}' \
  > "$TMP/sidecar/baseline_predictions.ndjson.meta.json"
before="$(find "$QF_BASELINE_STORE" -mindepth 1 -maxdepth 1 -type d | wc -l)"
out="$(run "$TMP/sidecar")"
if printf '%s' "$out" | grep -q "not part of a baseline set"; then
  ok "the coverage sidecar is refused rather than published"
else
  bad "the sidecar was accepted or refused for another reason: $out"
fi
if printf '%s' "$out" | grep -q "meta.json" \
   && printf '%s' "$out" | grep -q "mktemp -d"; then
  ok "the refusal names the offending file and the staging remedy"
else
  bad "the refusal did not name the file and the fix: $out"
fi
# AND IT PUBLISHED NOTHING. A hint printed on the way past would be worse than
# no hint at all. Counted against the store as it was a moment ago, not against
# a literal: earlier clauses in this file have already published sets.
after="$(find "$QF_BASELINE_STORE" -mindepth 1 -maxdepth 1 -type d | wc -l)"
if [ "$before" = "$after" ]; then
  ok "the refused set was not published"
else
  bad "a refused set reached the store ($before -> $after)"
fi

hash_dir="$(find "$QF_BASELINE_STORE" -mindepth 1 -maxdepth 1 -type d)"
for name in MANIFEST.json baseline_predictions.ndjson 2026-08-20.json \
            2026-08-21.json; do
  [ -f "$hash_dir/$name" ] && ok "published $name" || bad "missing $name"
done

# The identity is a content key, so the directory name IS the manifest's hash.
stored="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["baseline_hash"])' "$hash_dir/MANIFEST.json")"
[ "$stored" = "$(basename "$hash_dir")" ] \
  && ok "the directory name is the manifest's baseline_hash" \
  || bad "directory $(basename "$hash_dir") != manifest $stored"

# --- the promotion time, OUTSIDE the identity ---------------------------
# A sidecar rather than a manifest field: the manifest IS the content key, so a
# timestamp inside it would make every promotion of the same bytes a different
# artifact. And a sidecar rather than the directory mtime, which survives a
# filesystem copy as a confident wrong answer.
if [ -f "$hash_dir/PROMOTED_AT" ]; then
  ok "the promotion time is recorded beside the manifest"
  stamp="$(cat "$hash_dir/PROMOTED_AT")"
  case "$stamp" in
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T*Z)
      ok "PROMOTED_AT is an ISO-8601 UTC instant ($stamp)" ;;
    *) bad "PROMOTED_AT is not an ISO UTC instant: $stamp" ;;
  esac
else
  bad "no PROMOTED_AT beside the manifest"
fi
if python3 -c 'import json,sys; sys.exit("promoted_at" in json.load(open(sys.argv[1])))' "$hash_dir/MANIFEST.json"; then
  ok "the manifest itself carries no promotion time"
else
  bad "a promotion time leaked into the manifest, so the identity is not a content key"
fi

# --- FIRST PUBLICATION WINS ----------------------------------------------
before="$(find "$hash_dir" -type f -exec sha256sum {} \; | sort)"
out="$(run "$TMP/src")"
after="$(find "$hash_dir" -type f -exec sha256sum {} \; | sort)"
if printf '%s' "$out" | grep -q 'already published'; then
  ok "a second promotion of the same files is a no-op"
else
  bad "a second promotion did not report already-published: $out"
fi
[ "$before" = "$after" ] && ok "the published bytes are unchanged" \
  || bad "a re-promotion rewrote the artifact"
published="$(find "$QF_BASELINE_STORE" -mindepth 1 -maxdepth 1 -type d | wc -l)"
[ "$published" = 1 ] && ok "still exactly one artifact" || bad "$published"

# --- different content is a different artifact ---------------------------
make_source "$TMP/src2" 5
run "$TMP/src2" >/dev/null
published="$(find "$QF_BASELINE_STORE" -mindepth 1 -maxdepth 1 -type d | wc -l)"
[ "$published" = 2 ] && ok "changed content publishes a second artifact" \
  || bad "expected 2 artifacts, found $published"

# --- exclude_dates changes the identity ---------------------------------
run "$TMP/src" --exclude 2026-07-04 >/dev/null
published="$(find "$QF_BASELINE_STORE" -mindepth 1 -maxdepth 1 -type d | wc -l)"
[ "$published" = 3 ] \
  && ok "the same files with different exclusions are a different baseline" \
  || bad "expected 3 artifacts, found $published"

# --- the closed-world file set ------------------------------------------
make_source "$TMP/bad"
echo stray > "$TMP/bad/notes.txt"
out="$(run "$TMP/bad")"
if printf '%s' "$out" | grep -q 'notes.txt'; then
  ok "an unrecognised file is refused BY NAME"
else
  bad "a stray file was not refused by name: $out"
fi
[ ! -d "$QF_BASELINE_STORE/.staging" ] && ok "no staging left behind" \
  || bad "staging survived a refusal"
leftover="$(find "$QF_BASELINE_STORE" -maxdepth 1 -name '.staging*' | wc -l)"
[ "$leftover" = 0 ] && ok "no staging directory after a refusal" || bad "$leftover"

# --- a store the deploy domain could write is refused -------------------
chmod 0775 "$QF_BASELINE_STORE"
out="$(run "$TMP/src")"
if printf '%s' "$out" | grep -qi 'writable'; then
  ok "a group-writable store is refused"
else
  bad "a group-writable store was accepted: $out"
fi
chmod 0755 "$QF_BASELINE_STORE"

QF_BASELINE_STORE_OWNER=nobody out="$(QF_BASELINE_STORE_OWNER=nobody run "$TMP/src")"
if printf '%s' "$out" | grep -q "not 'nobody'"; then
  ok "a store owned by the wrong identity is refused"
else
  bad "a wrongly-owned store was accepted: $out"
fi

echo
echo "promote-baseline: pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
