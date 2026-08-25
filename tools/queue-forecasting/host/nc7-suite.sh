#!/usr/bin/env bash
# Negative control 7, Phase 1 -- credential scoping.
# Spec: auto-research-phase1-design.md section 7. Scoring: nc7-lib.sh.
#
# Asserts that the agent's GitHub token can write to qf-research and to nothing
# else. Run AS THE RESEARCH USER, because it uses that user's credential:
#   sudo -H -u research bash -lc '/srv/queue-forecasting/tools/queue-forecasting/host/nc7-suite.sh'
#
#   --check   run only the canaries and preflights; perform no refusal probes
#
# Exit 0 = every control failed closed. Exit 1 = at least one is open or void.
# Exit 2 = the suite could not run at all.
#
# VOID is a failure, not a skip: a probe that could not be meaningfully
# attempted tells us nothing, and "nothing" must never read as "contained".
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/nc7-lib.sh"

MONO=${MONO:-lotas/taskcluster}
RESEARCH_REPO=${RESEARCH_REPO:-lotas/qf-research}
DEPLOY_BRANCH=${DEPLOY_BRANCH:-feat/queue-forecasting}
CRED_FILE=${CRED_FILE:-$HOME/.git-credentials}
EVIDENCE=${EVIDENCE:-/tmp/nc7-evidence.txt}
API=https://api.github.com
RUNID=${RUNID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}
CHECK_ONLY=0; [ "${1:-}" = "--check" ] && CHECK_ONLY=1

pass=0; fail=0
die() { echo "nc7: $*" >&2; exit 2; }
is2xx() { case "${1:-}" in 2[0-9][0-9]) return 0 ;; *) return 1 ;; esac; }

# Private scratch, removed on EVERY exit path -- it holds a copy of the token.
# The trap is registered before anything is written into it.
WORK=$(mktemp -d) || die "mktemp failed"
chmod 700 "$WORK"
trap 'rm -rf "$WORK"' EXIT HUP INT TERM
HDR="$WORK/hdr"; BODY="$WORK/body"; ERR="$WORK/err"; : > "$ERR"

command -v jq >/dev/null 2>&1 || die "jq is required (apt-get install -y jq)"
command -v git >/dev/null 2>&1 || die "git is required"

TOK=$(sed -n 's|^https://[^:]*:\([^@]*\)@github\.com.*|\1|p' "$CRED_FILE" 2>/dev/null | head -1)
[ -n "$TOK" ] || die "no github.com credential in $CRED_FILE"
token_is_safe_for_sed "$TOK" || die "token charset would break redaction; refusing to run"

: > "$EVIDENCE"
record() { printf '%s\n' "$*" | redact "$TOK" >> "$EVIDENCE"; }
record "nc7 run=$RUNID mono=$MONO research=$RESEARCH_REPO check_only=$CHECK_ONLY"

# The token goes into a mode-600 file inside the 700 scratch dir, never argv.
umask 077
printf 'header = "Authorization: Bearer %s"\n' "$TOK" > "$HDR"

# gh <method> <path> [json] -> prints status; response body lands in $BODY
gh() {
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl --config "$HDR" -sS -o "$BODY" -w '%{http_code}' -X "$method" \
      -H 'Accept: application/vnd.github+json' -H 'X-GitHub-Api-Version: 2022-11-28' \
      --data "$body" "$API/$path" 2>>"$ERR"
  else
    curl --config "$HDR" -sS -o "$BODY" -w '%{http_code}' -X "$method" \
      -H 'Accept: application/vnd.github+json' -H 'X-GitHub-Api-Version: 2022-11-28' \
      "$API/$path" 2>>"$ERR"
  fi
}

# anon <path> -> prints status; no credential of any kind is sent
anon() {
  curl -sS -o "$BODY" -w '%{http_code}' \
    -H 'Accept: application/vnd.github+json' "$API/$1" 2>>"$ERR"
}

refuse_http() { # refuse_http <name> <method> <path> [json]
  local name="$1"; shift
  local method="$1" st verdict
  st=$(gh "$@"); verdict=$(score_http "$st")
  record "REFUSE $name method=$method status=${st:-none} verdict=$verdict body=$(head -c 300 "$BODY" | tr '\n' ' ')"
  case "$verdict" in
    refused) echo "ok    $name  (refused, HTTP $st)"; pass=$((pass+1)) ;;
    fail)    echo "FAIL  $name  (PERMITTED, HTTP $st)"; fail=$((fail+1)) ;;
    void)    echo "VOID  $name  (HTTP ${st:-none} is not an authorization result)"; fail=$((fail+1)) ;;
  esac
}

echo "== NC7 canaries: the token must work where it is supposed to =="

st=$(gh GET user)
record "CANARY C1 /user status=$st"
if is2xx "$st"; then echo "ok    C1 token authenticates  (HTTP $st)"; pass=$((pass+1))
else echo "VOID  C1 token authenticates  (HTTP ${st:-none}) - every refusal below would be vacuous"; fail=$((fail+1)); fi

HAS_WIKI=""
st=$(anon "repos/$MONO")
record "CANARY C4 anonymous read of $MONO status=$st"
if is2xx "$st"; then
  HAS_WIKI=$(jq -r '.has_wiki' "$BODY")
  echo "ok    C4 monorepo readable with no credential  (HTTP $st, has_wiki=$HAS_WIKI)"; pass=$((pass+1))
else
  echo "VOID  C4 monorepo readable with no credential  (HTTP ${st:-none})"; fail=$((fail+1))
fi

CLONE_OK=0
if git -c advice.detachedHead=false clone --quiet --depth 1 \
     "https://github.com/$RESEARCH_REPO" "$WORK/qf" 2>>"$ERR"; then
  rc=0
  ( cd "$WORK/qf" \
    && git checkout -q -b "nc7-canary-$RUNID" \
    && git -c user.email=nc7@local -c user.name=nc7 commit -q --allow-empty -m "nc7 canary $RUNID" \
    && GIT_TERMINAL_PROMPT=0 git push -q origin "nc7-canary-$RUNID" \
    && GIT_TERMINAL_PROMPT=0 git push -q origin --delete "nc7-canary-$RUNID" ) 2>>"$ERR" || rc=$?
  record "CANARY C2 push+delete on $RESEARCH_REPO rc=$rc"
  if [ "$rc" -eq 0 ]; then
    echo "ok    C2 push to $RESEARCH_REPO succeeds"; pass=$((pass+1)); CLONE_OK=1
  else
    echo "VOID  C2 push to $RESEARCH_REPO failed (rc=$rc) - the token cannot do its job"; fail=$((fail+1))
  fi
else
  record "CANARY C2 clone of $RESEARCH_REPO failed"
  echo "VOID  C2 clone of $RESEARCH_REPO failed"; fail=$((fail+1))
fi

body=$(jq -n --arg t "nc7 canary $RUNID" '{title:$t,body:"Created by nc7-suite.sh; closed automatically."}')
st=$(gh POST "repos/$RESEARCH_REPO/issues" "$body")
if is2xx "$st"; then
  num=$(jq -r '.number' "$BODY")
  st2=$(gh PATCH "repos/$RESEARCH_REPO/issues/$num" '{"state":"closed"}')
  record "CANARY C3 issue create=$st close=$st2 number=$num"
  if is2xx "$st2"; then echo "ok    C3 issue filed and closed on $RESEARCH_REPO  (#$num)"; pass=$((pass+1))
  else echo "VOID  C3 issue #$num created but not closable (HTTP $st2)"; fail=$((fail+1)); fi
else
  record "CANARY C3 issue create status=$st body=$(head -c 300 "$BODY" | tr '\n' ' ')"
  echo "VOID  C3 cannot file an issue on $RESEARCH_REPO (HTTP ${st:-none}) - Issues:write missing?"; fail=$((fail+1))
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
  echo; echo "canaries only (--check); passed=$pass failed=$fail"
  [ "$fail" -eq 0 ] || exit 1
  exit 0
fi

echo "== NC7 refusals: the token must reach nothing on $MONO =="

# The SHA every ref-creation probe needs. Fetched WITHOUT the token so a token
# problem cannot be mistaken for a malformed request.
st=$(anon "repos/$MONO/git/ref/heads/$DEPLOY_BRANCH")
SHA=$(is2xx "$st" && jq -r '.object.sha' "$BODY" || echo "")
[ -n "$SHA" ] || die "could not resolve $DEPLOY_BRANCH anonymously (HTTP $st); R2/R3 would be vacuous"
record "preflight deploy-branch sha=$SHA"

# R1 -- git smart-HTTP write to a DISPOSABLE ref. Never the deploy branch:
# a broken boundary would otherwise move the branch the deploy checkout pulls.
if [ "$CLONE_OK" -eq 1 ]; then
  probe_ref="nc7-git-probe-$RUNID"
  before=$(anon "repos/$MONO/git/ref/heads/$probe_ref")
  if [ "$before" != "404" ]; then
    echo "VOID  R1 git push (probe ref already exists, HTTP $before)"; fail=$((fail+1))
  else
    install -m 600 "$CRED_FILE" "$WORK/creds"
    rc=0
    ( cd "$WORK/qf" && GIT_TERMINAL_PROMPT=0 git \
        -c credential.helper= \
        -c "credential.helper=store --file=$WORK/creds" \
        -c credential.useHttpPath=false \
        push "https://github.com/$MONO" "HEAD:refs/heads/$probe_ref" ) 2>"$WORK/r1err" || rc=$?
    err=$(cat "$WORK/r1err")
    verdict=$(score_git "$rc" "$err")
    after=$(anon "repos/$MONO/git/ref/heads/$probe_ref")
    record "REFUSE R1 git push rc=$rc verdict=$verdict ref_before=$before ref_after=$after stderr=$(printf '%s' "$err" | tr '\n' ' ')"
    if [ "$after" != "404" ]; then
      echo "FAIL  R1 git push  (the ref EXISTS on $MONO - delete it with your own credential)"; fail=$((fail+1))
    else
      case "$verdict" in
        refused) echo "ok    R1 git push  (refused)"; pass=$((pass+1)) ;;
        fail)    echo "FAIL  R1 git push  (PERMITTED)"; fail=$((fail+1)) ;;
        void)    echo "VOID  R1 git push  ($(printf '%s' "$err" | tr '\n' ' ' | head -c 120))"; fail=$((fail+1)) ;;
      esac
    fi
    # The denial must not have erased the agent's durable credential.
    if grep -q 'github\.com' "$CRED_FILE"; then
      echo "ok    R1b durable credential intact after the probe"; pass=$((pass+1))
    else
      echo "FAIL  R1b the probe ERASED $CRED_FILE - restore it"; fail=$((fail+1))
    fi
  fi
else
  echo "VOID  R1 git push (no working clone from C2)"; fail=$((fail+1))
fi

# R2/R3 -- ref creation via REST. Both `ref` and `sha` are required fields;
# omitting either draws a 422 regardless of permission.
refuse_http "R2 create a branch" POST "repos/$MONO/git/refs" \
  "$(jq -n --arg r "refs/heads/nc7-probe-$RUNID" --arg s "$SHA" '{ref:$r,sha:$s}')"
refuse_http "R3 create a tag" POST "repos/$MONO/git/refs" \
  "$(jq -n --arg r "refs/tags/nc7-probe-$RUNID" --arg s "$SHA" '{ref:$r,sha:$s}')"

# R4 -- open a PR. Preflight BOTH conditions, or a 422 is vacuous.
owner=${MONO%%/*}
st=$(anon "repos/$MONO/compare/$DEPLOY_BRANCH...main")
ahead=$(is2xx "$st" && jq -r '.ahead_by // empty' "$BODY" || echo "")
st2=$(anon "repos/$MONO/pulls?head=$owner:main&base=$DEPLOY_BRANCH&state=open")
open_prs=$(is2xx "$st2" && jq -r 'length' "$BODY" || echo "")
record "preflight R4 compare=$st ahead_by=$ahead pulls=$st2 open=$open_prs"
if [ -z "$ahead" ] || [ "$ahead" -le 0 ] 2>/dev/null; then
  echo "VOID  R4 open a PR (preflight: main is not ahead of $DEPLOY_BRANCH; a 422 would be vacuous)"; fail=$((fail+1))
elif [ "$open_prs" != "0" ]; then
  echo "VOID  R4 open a PR (preflight: a matching open PR exists; a 422 would be vacuous)"; fail=$((fail+1))
else
  refuse_http "R4 open a PR" POST "repos/$MONO/pulls" \
    "$(jq -n --arg t "nc7 probe $RUNID" --arg b "$DEPLOY_BRANCH" '{title:$t,head:"main",base:$b}')"
fi

# R5 -- settings write, made a NO-OP by echoing the value C4 just read, so an
# unexpected success changes nothing.
if [ -n "$HAS_WIKI" ]; then
  refuse_http "R5 change repository settings" PATCH "repos/$MONO" \
    "$(jq -n --argjson w "$HAS_WIKI" '{has_wiki:$w}')"
else
  echo "VOID  R5 change repository settings (C4 did not yield has_wiki; refusing to guess)"; fail=$((fail+1))
fi

# R6 -- write a workflow file. message + base64 content + explicit branch are
# all required; without them GitHub 422s whatever the permissions.
refuse_http "R6 write a workflow file" PUT \
  "repos/$MONO/contents/.github/workflows/nc7-probe-$RUNID.yml" \
  "$(jq -n --arg m "nc7 probe $RUNID" \
           --arg c "$(printf '# nc7 probe\n' | base64 | tr -d '\n')" \
           --arg b "$DEPLOY_BRANCH" '{message:$m,content:$c,branch:$b}')"

# R7 -- the credential file itself.
mode=$(stat -c '%a' "$CRED_FILE"); owner_u=$(stat -c '%U' "$CRED_FILE")
record "REFUSE R7 credential file mode=$mode owner=$owner_u"
if [ "$mode" = "600" ] && [ "$owner_u" = "$(id -un)" ]; then
  echo "ok    R7 credential file is $mode owned by $owner_u"; pass=$((pass+1))
else
  echo "FAIL  R7 credential file is $mode owned by $owner_u (want 600 / $(id -un))"; fail=$((fail+1))
fi

# Optional: prove the token cannot see a private repo outside its scope.
# Only meaningful if such a repo exists, so it is SKIPPED WITH A MESSAGE rather
# than omitted -- an absent probe reads as "covered" to the next reader.
if [ -n "${OUT_OF_SCOPE_REPO:-}" ]; then
  refuse_http "R8 read a private repo outside the token's scope" GET "repos/$OUT_OF_SCOPE_REPO"
else
  echo "skip  R8 out-of-scope private repo (set OUT_OF_SCOPE_REPO=owner/name to enable)"
  record "SKIP R8 no OUT_OF_SCOPE_REPO supplied"
fi

# Informational only: an issue is a notification, not a mutation of code, so an
# unexpected success here is not a containment breach and does not fail the
# suite. It must not be SILENT, though -- this is the only probe whose success
# goes unscored, so say so loudly and name what needs cleaning up.
st=$(gh POST "repos/$MONO/issues" "$(jq -n --arg t "nc7 informational $RUNID" '{title:$t}')")
record "INFO issue-filing on $MONO status=$st body=$(head -c 200 "$BODY" | tr '\n' ' ')"
if is2xx "$st"; then
  num=$(jq -r '.number // "?"' "$BODY")
  echo "warn  issue-filing on $MONO SUCCEEDED (HTTP $st, issue #$num) - not scored,"
  echo "warn  but the token reaches further than expected: close #$num and re-check its scope"
  record "INFO created issue #$num on $MONO - needs manual closure"
else
  echo "info  issue-filing on $MONO returned HTTP ${st:-none} (recorded, not scored)"
fi

# The evidence file is staged into git, so prove it carries no secret.
if secret_leaked "$EVIDENCE" "$TOK"; then
  echo "FAIL  evidence file contains a secret - do NOT stage $EVIDENCE"; fail=$((fail+1))
else
  echo "ok    evidence file contains no token and no URL userinfo"; pass=$((pass+1))
fi

echo
echo "passed=$pass failed=$fail  evidence=$EVIDENCE"
[ "$fail" -eq 0 ] || exit 1
