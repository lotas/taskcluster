#!/usr/bin/env bash
# Scoring helpers for the Phase 1 negative-control suite (NC7).
#
# Every function here is PURE: no network, no filesystem writes, no globals.
# That is deliberate. See auto-research-phase1-design.md 7.1 and 7.2, and
# nc7-lib.test.sh for the specific bugs each rule prevents.

# score_http <status> -> refused | fail | void
#
# Only codes that mean "not authorized" count as a refusal. Crediting any
# non-2xx would let a rate limit (429), a malformed payload (422), a bad token
# (401), or a GitHub outage (5xx) certify containment.
score_http() {
  case "${1:-}" in
    403|404)     printf 'refused\n' ;;
    2[0-9][0-9]) printf 'fail\n' ;;
    *)           printf 'void\n' ;;
  esac
}

# score_git <exit_status> <stderr_text> -> refused | fail | void
#
# Order matters. Authentication and transport failures are checked first
# because they also produce a non-zero exit: a missing credential proves
# nothing about the token's scope. A proxy CONNECT rejection can itself carry
# "403", which is why the transport check precedes the authorization check.
score_git() {
  local rc="${1:-1}" lower
  lower=$(printf '%s' "${2:-}" | tr '[:upper:]' '[:lower:]')

  case "$lower" in
    *"authentication failed"*|*"could not read username"*|*"could not read password"*|\
    *"invalid credentials"*|*"terminal prompts disabled"*|*"401"*)
      printf 'void\n'; return ;;
  esac
  case "$lower" in
    *"could not resolve host"*|*"proxy"*|*"ssl"*|*"tls"*|\
    *"connection refused"*|*"connection timed out"*|*"operation timed out"*)
      printf 'void\n'; return ;;
  esac

  [ "$rc" -eq 0 ] && { printf 'fail\n'; return; }

  case "$lower" in
    *"403"*|*"write access to repository not granted"*|*"permission to "*"denied"*|*"not authorized"*)
      printf 'refused\n'; return ;;
  esac
  printf 'void\n'
}

# cred_fields_digest  (reads `git credential fill` output on stdin)
#
# Prints a sha256 digest of (username, password) and returns 0 only when the
# input holds exactly one non-empty username and one non-empty password.
# Returns non-zero otherwise -- which is the whole point: two FAILED lookups
# must not both produce the digest of the empty string and compare equal.
cred_fields_digest() {
  local out user pass
  out=$(cat)
  user=$(printf '%s\n' "$out" | sed -n 's/^username=//p')
  pass=$(printf '%s\n' "$out" | sed -n 's/^password=//p')
  [ "$(printf '%s\n' "$user" | grep -c .)" -eq 1 ] || return 1
  [ "$(printf '%s\n' "$pass" | grep -c .)" -eq 1 ] || return 1
  printf '%s\n%s\n' "$user" "$pass" | sha256sum | cut -d' ' -f1
}

# token_is_safe_for_sed <token> -> 0 when the token cannot break the redactor
token_is_safe_for_sed() {
  case "${1:-}" in
    '') return 1 ;;
    *[!A-Za-z0-9_-]*) return 1 ;;
    *) return 0 ;;
  esac
}

# redact <token>  (filter: stdin -> stdout)
redact() {
  local tok="$1"
  token_is_safe_for_sed "$tok" || { echo "redact: unsafe token charset" >&2; return 1; }
  sed -e "s|$tok|<REDACTED-TOKEN>|g" \
      -e 's|\(://[^/@[:space:]]*\):[^/@[:space:]]*@|\1:<REDACTED>@|g'
}

# secret_leaked <file> <token> -> 0 when the file still contains a secret
secret_leaked() {
  local file="$1" tok="$2"
  grep -qF -- "$tok" "$file" 2>/dev/null && return 0
  grep -qE '://[^/@[:space:]]+:[^/@[:space:]]+@' "$file" 2>/dev/null && return 0
  return 1
}
