#!/usr/bin/env bash
# Tests for nc7-lib.sh. Run: bash host/nc7-lib.test.sh
# No network, no privileges. Every case below is a bug that a design review
# round actually found in NC7's scoring -- see auto-research-phase1-design.md
# rev table. A control that can pass for the wrong reason is worse than no
# control, so this file exists to make that impossible to reintroduce quietly.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/nc7-lib.sh"

t=0; f=0
eq() { # eq <label> <expected> <actual>
  t=$((t+1))
  if [ "$2" = "$3" ]; then echo "ok    $1"
  else echo "FAIL  $1: expected '$2', got '$3'"; f=$((f+1)); fi
}
ok_rc() { t=$((t+1)); if [ "$2" -eq 0 ]; then echo "ok    $1"; else echo "FAIL  $1: expected rc 0, got $2"; f=$((f+1)); fi; }
nz_rc() { t=$((t+1)); if [ "$2" -ne 0 ]; then echo "ok    $1"; else echo "FAIL  $1: expected non-zero rc"; f=$((f+1)); fi; }

echo "== score_http =="
eq "403 is a refusal"              refused "$(score_http 403)"
eq "404 is a refusal"              refused "$(score_http 404)"
eq "200 is a failure"              fail    "$(score_http 200)"
eq "201 is a failure"              fail    "$(score_http 201)"
eq "401 voids (bad credential)"    void    "$(score_http 401)"
eq "409 voids"                     void    "$(score_http 409)"
eq "422 voids (bad payload)"       void    "$(score_http 422)"
eq "429 voids (rate limit)"        void    "$(score_http 429)"
eq "500 voids"                     void    "$(score_http 500)"
eq "503 voids"                     void    "$(score_http 503)"
eq "empty status voids"            void    "$(score_http '')"
eq "garbage voids"                 void    "$(score_http 'curl: (7)')"

echo "== score_git =="
eq "success is a failure" fail "$(score_git 0 '')"
eq "permission denied is a refusal" refused \
  "$(score_git 128 'remote: Permission to lotas/taskcluster.git denied to x-access-token.')"
eq "write access not granted is a refusal" refused \
  "$(score_git 128 'remote: Write access to repository not granted.')"
eq "explicit 403 is a refusal" refused \
  "$(score_git 128 'fatal: unable to access: The requested URL returned error: 403')"
eq "authentication failure voids" void \
  "$(score_git 128 "fatal: Authentication failed for 'https://github.com/lotas/taskcluster/'")"
eq "missing username voids" void \
  "$(score_git 128 'fatal: could not read Username: terminal prompts disabled')"
eq "401 voids" void \
  "$(score_git 128 'fatal: unable to access: The requested URL returned error: 401')"
eq "dns failure voids" void \
  "$(score_git 128 'fatal: unable to access: Could not resolve host: github.com')"
eq "proxy failure voids" void \
  "$(score_git 128 'fatal: unable to access: Received HTTP code 403 from proxy after CONNECT')"
eq "unclassifiable non-zero voids" void "$(score_git 1 '')"

echo "== cred_fields_digest =="
d1=$(printf 'username=x-access-token\npassword=github_pat_AAA\n' | cred_fields_digest); r1=$?
ok_rc "well-formed input succeeds" "$r1"
t=$((t+1)); case "$d1" in [0-9a-f]*) echo "ok    digest looks like a hash";; *) echo "FAIL  digest: '$d1'"; f=$((f+1));; esac
printf '' | cred_fields_digest >/dev/null 2>&1; nz_rc "empty input fails closed" "$?"
printf 'username=x\n' | cred_fields_digest >/dev/null 2>&1; nz_rc "username only fails closed" "$?"
printf 'password=y\n' | cred_fields_digest >/dev/null 2>&1; nz_rc "password only fails closed" "$?"
printf 'username=\npassword=\n' | cred_fields_digest >/dev/null 2>&1; nz_rc "empty values fail closed" "$?"
printf 'username=a\nusername=b\npassword=y\n' | cred_fields_digest >/dev/null 2>&1; nz_rc "duplicate username fails closed" "$?"

# The rev-6 bug, asserted directly: two failed lookups must not compare equal.
a=$(printf '' | cred_fields_digest 2>/dev/null) || a="LOOKUP_FAILED_A"
b=$(printf '' | cred_fields_digest 2>/dev/null) || b="LOOKUP_FAILED_B"
t=$((t+1))
if [ "$a" = "LOOKUP_FAILED_A" ] && [ "$b" = "LOOKUP_FAILED_B" ]; then
  echo "ok    two failed lookups are caught before comparison"
else
  echo "FAIL  failed lookups produced comparable output: '$a' vs '$b'"; f=$((f+1))
fi

# Same credential in, same digest out; different credential, different digest.
p=$(printf 'username=x-access-token\npassword=github_pat_AAA\n' | cred_fields_digest)
q=$(printf 'username=x-access-token\npassword=github_pat_AAA\n' | cred_fields_digest)
z=$(printf 'username=x-access-token\npassword=github_pat_BBB\n' | cred_fields_digest)
eq "same credential digests equal" "$p" "$q"
t=$((t+1)); if [ "$p" != "$z" ]; then echo "ok    different credential digests differ"; else echo "FAIL  digest collision"; f=$((f+1)); fi

echo "== redact / secret_leaked =="
TOK=github_pat_TESTTOKEN123
eq "token is redacted" "auth <REDACTED-TOKEN> here" \
  "$(printf 'auth %s here\n' "$TOK" | redact "$TOK")"
eq "url userinfo is redacted" "https://x-access-token:<REDACTED>@github.com/lotas/x" \
  "$(printf 'https://x-access-token:%s@github.com/lotas/x\n' "$TOK" | redact "$TOK")"
printf 'a$b\n' | redact 'tok;rm -rf /' >/dev/null 2>&1; nz_rc "unsafe token charset is refused" "$?"

tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
printf 'clean output\n' > "$tmp"
secret_leaked "$tmp" "$TOK"; nz_rc "clean file reports no leak" "$?"
printf 'oops %s\n' "$TOK" > "$tmp"
secret_leaked "$tmp" "$TOK"; ok_rc "bare token is detected" "$?"
printf 'https://u:hunter2@github.com/x\n' > "$tmp"
secret_leaked "$tmp" "$TOK"; ok_rc "userinfo is detected even for another secret" "$?"

echo
echo "tests=$t failed=$f"
[ "$f" -eq 0 ] || exit 1
