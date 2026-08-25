# Auto-Research Loop Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract `trainer/` into a private `lotas/qf-research` the research agent owns outright, give it one scoped GitHub token, and prove by negative control that it can write to that repo and nothing else.

**Architecture:** One new repository, one fine-grained PAT (`Contents: write` + `Issues: write` on `qf-research` only), and **no credential of any kind on `lotas/taskcluster`**. The monorepo stays the trusted source for service and platform code, read by the agent from a root-owned, secret-free mirror at `/srv/queue-forecasting` — which is *not* the deploy checkout; see the spec's §4.1. The three-repo split in the parent design is not built — see the spec for why. The live collector and live-predictor are never rebuilt or restarted.

**Tech Stack:** `git filter-repo`, bash, `curl`, `jq`, `uv`/pytest, GitHub fine-grained PATs, tinyproxy allowlist.

**Spec:** `tools/queue-forecasting/auto-research-phase1-design.md` (rev 7). Read §3 (decisions), §5 (extraction), §6 (Python environment), §7 (NC7) and §10 (amendments) before starting.

---

## Conventions

- **Commits are handled by the user.** Where a step says "Stage", run `git add` and stop. Never run `git commit`.
- Repo-side steps (Phase 1a) run in the current checkout and need no host access and no GitHub credential.
- GitHub steps (Phase 1b) need the user's own browser/credential; they are marked **USER**.
- Host steps (Phase 1c) run on the experimental server. `research` commands use `sudo -H -u research bash -lc "$cmd"` — **never `sudo -i`**, which re-parses the command string and destroys quoting (Phase 0 `host/README.md`).
- JS tests run per-file: `node test/<name>.test.js`. Python tests run from `trainer/`: `uv run pytest -q`.
- Bash test suites run as `bash host/<name>.test.sh`.

## Verified facts this plan depends on

Measured on 2026-08-24 in this checkout; each is re-asserted by a step rather than assumed.

| Fact | Value |
|---|---|
| Commits touching `tools/queue-forecasting/trainer` | 39 as of 2026-08-25 — **derived at run time**, not asserted |
| Tracked files under `trainer/` | 68 — likewise derived |
| Tracked paths containing a space | 0 (so `awk`-based listing comparison is safe) |
| Anything tracked under `trainer/data/` | none, in the current tree or in history |
| Largest tracked blob under `trainer/` | 114,012 bytes |
| `uv sync --locked` in `trainer/` | succeeds (lock agrees with manifest) |
| `uv run pytest -q` in the monorepo | 226 passed |
| `uv run pytest -q` in an extracted tree | **1 failed** before Task 1; `225 passed, 1 skipped` after. The script asserts "no failures and exactly one skip", not the counts |
| `filter-repo` output before ref cleanup | `.git` = 194 MB, `origin` remote and its refs survive |
| `filter-repo` output after ref cleanup | `.git` = 2.4 MB, one ref, 38 commits, 352 objects |
| Workflows in the monorepo declaring `workflow_dispatch` | none (4 workflows, none dispatchable) |
| Credential helper installed by Phase 0 | **none** — Phase 1 provisions it |

## File Structure

**Created, repo-side:**

| File | Responsibility |
|---|---|
| `host/nc7-lib.sh` | Pure scoring/redaction helpers for NC7. No network, no I/O. Unit-tested. |
| `host/nc7-lib.test.sh` | Tests for the above. Each case is a vacuous-pass bug a review round found. |
| `host/nc7-suite.sh` | The NC7 probes: canaries, refusals, evidence writing. Glue over the tested library. |
| `host/extract-qf-research.sh` | Idempotent, verifying extraction of `trainer/` into a standalone history. |

**Modified, repo-side:**

| File | Change |
|---|---|
| `trainer/tests/test_data_loader.py:73-82` | Serving-parity guard skips when the service tree is absent. |
| `trainer/README.md` (new) and `README.md` | Freeze notices naming the other copy. |
| `host/nc-suite.sh:76-80,92` | NC4's dead `/srv/qf-platform` message; NC6's denied host. |
| `host/phase0-setup.sh:696-704` | Allowlist generator gains pypi + files.pythonhosted.org. |
| `host/README.md` | Records the widened allowlist and the agent-owned venv. |
| `auto-research-loop-design.md` | 20 amendments per spec §10. |

**Created on the host (not in git):** `/home/research/.git-credentials`, `/home/research/qf-research/`.

---

## Phase 1a — repo-side work

### Task 1: Make the trainer's serving-parity guard portable

`trainer/tests/test_data_loader.py:78` reads `parents[2]/src/repo-family.js` to assert `REPO_FAMILY_DERIVATION_VERSION` matches between the Python trainer and the JS predictor. That path resolves into the **service** tree, which `qf-research` will not contain, so the test fails there. It is the only cross-boundary reference in the whole trainer tree (verified: no other `parents[2]`/`parents[3]` usage).

The guard is real and must keep working where it can. So it skips with a reason when the JS is absent, rather than failing.

**Files:**
- Modify: `tools/queue-forecasting/trainer/tests/test_data_loader.py:73-82`

- [x] **Step 1: Reproduce the failure the extraction will hit**

```bash
cd tools/queue-forecasting/trainer
mkdir -p /tmp/parity-probe/tests /tmp/parity-probe/src
cp tests/test_data_loader.py /tmp/parity-probe/tests/
python3 -c "
from pathlib import Path
js = Path('/tmp/parity-probe/tests/test_data_loader.py').resolve().parents[2] / 'src' / 'repo-family.js'
print('the guard would read:', js)
print('exists:', js.exists())
"
```

Expected: it prints a path outside the trainer tree and `exists: False`. That is the failure mode — in `qf-research` there is no `src/repo-family.js`.

- [x] **Step 2: Write the skip guard**

In `trainer/tests/test_data_loader.py`, replace the opening of `test_repo_family_derivation_version_matches_js` (lines 73-82) so that it reads:

```python
def test_repo_family_derivation_version_matches_js():
    """The Python constant must stay in lockstep with src/repo-family.js.

    The JS lives in the service tree, which the research repository does not
    contain (auto-research-phase1-design.md D1/D2). Where it is absent this
    parity guard cannot run, so the test skips with a reason instead of
    failing: the guard is enforced in the monorepo, and serving parity is a
    promotion-time concern there.
    """
    import re
    from pathlib import Path

    js = Path(__file__).resolve().parents[2] / "src" / "repo-family.js"
    if not js.exists():
        pytest.skip(f"serving-parity guard needs {js}, absent outside the monorepo")
    text = js.read_text()
    m = re.search(r"REPO_FAMILY_DERIVATION_VERSION\s*=\s*(\d+)", text)
    assert m, "could not find REPO_FAMILY_DERIVATION_VERSION in repo-family.js"
    assert int(m.group(1)) == dl.REPO_FAMILY_DERIVATION_VERSION
```

`pytest` is already imported at module scope (line 3); do not add a second import.

- [x] **Step 3: Verify the guard still runs in the monorepo**

```bash
cd tools/queue-forecasting/trainer
uv run pytest -q tests/test_data_loader.py::test_repo_family_derivation_version_matches_js -v 2>&1 | tail -3
```

Expected: `1 passed`, **not** skipped. `src/repo-family.js` exists here, so the guard must still assert.

- [x] **Step 4: Verify the whole suite is unchanged**

```bash
cd tools/queue-forecasting/trainer && uv run pytest -q 2>&1 | tail -2
```

Expected: `226 passed`. The count must not drop — in this tree nothing should skip.

- [x] **Step 5: Stage**

```bash
git add tools/queue-forecasting/trainer/tests/test_data_loader.py
```

Stop. The user commits.

---

### Task 2: NC7 scoring library, test-first

This is the task to be slowest and most careful on. Six rounds of design review on NC7 found six distinct ways for a control to **pass for the wrong reason**, and the last one was introduced while fixing another. So the scoring logic is separated from the probing and tested directly, and each test below encodes a specific bug that was found.

**Files:**
- Create: `tools/queue-forecasting/host/nc7-lib.test.sh`
- Create: `tools/queue-forecasting/host/nc7-lib.sh`

- [x] **Step 1: Write the failing tests**

Create `tools/queue-forecasting/host/nc7-lib.test.sh`:

```bash
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
```

- [x] **Step 2: Run the tests to verify they fail**

```bash
cd tools/queue-forecasting && bash host/nc7-lib.test.sh
```

Expected: failure on the very first line, because `host/nc7-lib.sh` does not exist yet — `. "$here/nc7-lib.sh"` cannot be sourced.

- [x] **Step 3: Write the library**

Create `tools/queue-forecasting/host/nc7-lib.sh`:

```bash
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
```

- [x] **Step 4: Run the tests to verify they pass**

```bash
cd tools/queue-forecasting && bash host/nc7-lib.test.sh
```

Expected, exactly: `tests=38 failed=0`, and every line begins `ok`.

- [x] **Step 5: Stage**

```bash
git add tools/queue-forecasting/host/nc7-lib.sh tools/queue-forecasting/host/nc7-lib.test.sh
```

Stop. The user commits.

---

### Task 3: The NC7 probe suite

Glue over the tested library: it performs the requests, classifies with `score_http`/`score_git`, and writes redacted evidence. It cannot be unit-tested against GitHub, which is exactly why all of its judgement lives in Task 2.

**Files:**
- Create: `tools/queue-forecasting/host/nc7-suite.sh`

- [x] **Step 1: Write the suite**

Create `tools/queue-forecasting/host/nc7-suite.sh`:

```bash
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
```

- [x] **Step 2: Check it parses and that the library tests still pass**

```bash
cd tools/queue-forecasting
bash -n host/nc7-suite.sh && echo "syntax OK"
bash host/nc7-lib.test.sh | tail -1
```

Expected: `syntax OK`, then `tests=38 failed=0`.

- [x] **Step 3: Confirm it refuses to run without a credential**

```bash
cd tools/queue-forecasting
CRED_FILE=/nonexistent bash host/nc7-suite.sh; echo "exit=$?"
```

Expected: `nc7: no github.com credential in /nonexistent` and `exit=2`. It must fail closed rather than reporting a contained system.

- [x] **Step 4: Make both executable and stage**

```bash
chmod +x tools/queue-forecasting/host/nc7-suite.sh
git add tools/queue-forecasting/host/nc7-suite.sh
```

Stop. The user commits.

---

### Task 4: The extraction script, validated locally

**Precondition — read this before running the extraction.** `git clone` copies
**committed** history, so the extraction sees whatever is committed at the
moment it runs, not what is staged. Two consequences:

1. **Task 1 must be committed first.** Otherwise the extracted tree lacks the
   serving-parity skip guard and check 4 fails with `1 failed` instead of
   `225 passed, 1 skipped`. That failure is correct behaviour — the script is
   telling you the tree is not the one you meant to extract.
2. **Task 5's `trainer/README.md` is handled automatically.** It describes the
   *production* copy, so it must not appear in `qf-research`. A second
   `filter-repo` pass drops it from the rewritten history, and check 3 asserts
   it survives neither in the tree nor in any commit. So the extraction is
   repeatable at any commit, which matters because `/tmp/qf-extract` does not
   survive a reboot and Task 8 may happen much later.

So the only hard ordering constraint is that **Task 1 must be committed before
this task runs**. Task 5 may be committed before or after.

**Files:**
- Create: `tools/queue-forecasting/host/extract-qf-research.sh`

- [x] **Step 1: Write the script**

Create `tools/queue-forecasting/host/extract-qf-research.sh`:

```bash
#!/usr/bin/env bash
# Extracts tools/queue-forecasting/trainer/ into a standalone history for
# qf-research, then verifies the result four ways.
# Spec: auto-research-phase1-design.md section 5.
#
#   ./extract-qf-research.sh            # clone from the remote and extract
#   SRC=/path/to/checkout ./extract...  # extract from a local checkout instead
#
# Run this on a machine with pypi access: check 4 runs `uv sync`.
#
# Every expectation below is DERIVED FROM THE SOURCE, not hardcoded. An earlier
# revision asserted "38 commits" and "68 files" as constants and broke the first
# time a commit touched trainer/ -- and a written-down number is a weaker claim
# than the property we actually want: that the rewrite preserved exactly the
# commits and blobs that touched the subtree, whatever their count.
set -uo pipefail

SRC=${SRC:-git@github.com:lotas/taskcluster.git}
SRC_BRANCH=${SRC_BRANCH:-feat/queue-forecasting}
WORK=${WORK:-/tmp/qf-extract}
SUBDIR=tools/queue-forecasting/trainer

# Paths deliberately NOT carried into qf-research, as they appear AFTER the
# path-rename. Single-sourced: the same list drives the history-drop pass and
# the expected-blob listing, so the two cannot drift apart.
#   trainer/README.md documents the FROZEN PRODUCTION copy ("research happens
#   elsewhere"), which is wrong inside the research repo.
DROP_PATHS="trainer/README.md"

die() { echo "extract: $*" >&2; exit 1; }
step() { echo; echo "== $*"; }
info() { echo "      $*"; }

command -v git-filter-repo >/dev/null 2>&1 || git filter-repo --version >/dev/null 2>&1 \
  || die "git-filter-repo is not installed (pipx install git-filter-repo)"
[ -e "$WORK" ] && die "$WORK already exists; remove it first (this script never overwrites)"

step "cloning $SRC ($SRC_BRANCH) into $WORK"
# A local path needs --no-local so the clone is a real copy, not hardlinks.
if [ -d "$SRC" ]; then
  git clone --quiet --no-local --single-branch --branch "$SRC_BRANCH" "$SRC" "$WORK" \
    || die "clone failed"
else
  git clone --quiet --single-branch --branch "$SRC_BRANCH" "$SRC" "$WORK" || die "clone failed"
fi

step "measuring the source"
# Taken from the CLONE, before any rewriting, so the comparisons below do not
# depend on what happens to be checked out anywhere else.
SRC_FOR_COUNT="$WORK/../qf-src-mirror.git"
rm -rf "$SRC_FOR_COUNT"
git clone --quiet --bare --single-branch --branch "$SRC_BRANCH" "$WORK" "$SRC_FOR_COUNT" \
  || die "could not mirror the source for counting"
SRC_COMMITS=$(git -C "$WORK" rev-list --count "$SRC_BRANCH" -- "$SUBDIR")
[ "${SRC_COMMITS:-0}" -gt 0 ] || die "no commits touch $SUBDIR on $SRC_BRANCH - wrong path?"
git -C "$WORK" ls-files -s "$SUBDIR" \
  | awk '{sub("tools/queue-forecasting/","",$4); print $2, $4}' | sort > "$WORK/../qf-src.txt"
# Expected = source minus the deliberate exclusions.
cp "$WORK/../qf-src.txt" "$WORK/../qf-before.txt"
for d in $DROP_PATHS; do
  grep -v " $d\$" "$WORK/../qf-before.txt" > "$WORK/../qf-before.tmp" || true
  mv "$WORK/../qf-before.tmp" "$WORK/../qf-before.txt"
done
SRC_FILES=$(wc -l < "$WORK/../qf-before.txt")
SRC_FILES_RAW=$(wc -l < "$WORK/../qf-src.txt")
[ "${SRC_FILES:-0}" -gt 0 ] || die "no tracked files under $SUBDIR - wrong path?"
info "source has $SRC_COMMITS commits touching $SUBDIR and $SRC_FILES_RAW tracked files"
info "expecting $SRC_FILES after excluding:$(for d in $DROP_PATHS; do printf ' %s' "$d"; done)"

step "rewriting history"
( cd "$WORK" && git filter-repo \
    --path "$SUBDIR/" \
    --path-rename "$SUBDIR/:trainer/" \
    --refs "$SRC_BRANCH" ) || die "filter-repo failed"

step "dropping production-only files from the rewritten history"
# trainer/README.md describes the FROZEN PRODUCTION copy ("research happens
# elsewhere"), so it is wrong inside qf-research. It lives in the monorepo from
# commit 05d96b5d52 on, which makes this the normal case rather than an edge
# case -- so drop it here instead of failing and telling a human to do it.
# A second filter-repo pass needs --force, the first having already rewritten.
drop_args=""
for d in $DROP_PATHS; do
  git -C "$WORK" ls-files --error-unmatch "$d" >/dev/null 2>&1 && drop_args="$drop_args --path $d"
done
if [ -n "$drop_args" ]; then
  # A second filter-repo pass needs --force, the first having already rewritten.
  ( cd "$WORK" && git filter-repo --force --invert-paths $drop_args ) \
    || die "could not drop production-only paths from history"
  info "dropped:$drop_args"
else
  info "no production-only files present"
fi

step "removing the source remote and its refs"
# --refs leaves `origin` and its remote-tracking refs in place, and those still
# point at UNREWRITTEN history: measured 194 MB of .git before this cleanup and
# 2.4 MB after. Skipping it ships the whole monorepo inside the new repo.
( cd "$WORK" \
  && (git remote remove origin 2>/dev/null || true) \
  && git for-each-ref --format='%(refname)' refs/remotes | xargs -r -n1 git update-ref -d \
  && git branch -m "$SRC_BRANCH" main \
  && git reflog expire --expire=now --all \
  && git gc --prune=now --quiet ) || die "cleanup failed"

refs=$(git -C "$WORK" for-each-ref --format='%(refname)')
[ "$refs" = "refs/heads/main" ] || die "expected only refs/heads/main, got: $refs"

step "check 1: the rewrite preserved every subtree commit"
# Dropping trainer/README.md can legitimately empty a commit that touched only
# that file, and filter-repo prunes empty commits -- so allow the count to fall
# by at most the number of such commits, and never to rise.
n=$(git -C "$WORK" rev-list --count main)
prunable=0
for d in $DROP_PATHS; do
  c=$(git -C "$SRC_FOR_COUNT" rev-list --count "$SRC_BRANCH" \
        -- "tools/queue-forecasting/$d" 2>/dev/null || echo 0)
  prunable=$(( prunable + c ))
done
lower=$(( SRC_COMMITS - prunable ))
if [ "$n" -gt "$SRC_COMMITS" ] || [ "$n" -lt "$lower" ]; then
  die "commit count $n is outside [$lower, $SRC_COMMITS] for $SUBDIR - unexpected rewrite"
fi
info "ok    $n commits (source had $SRC_COMMITS; up to $prunable excluded-path-only may prune)"

step "check 2: every tracked blob is byte-identical (this is the fidelity check)"
git -C "$WORK" ls-files -s | awk '{print $2, $4}' | sort > "$WORK/../qf-after.txt"
if ! diff -u "$WORK/../qf-before.txt" "$WORK/../qf-after.txt"; then
  die "tracked object listings differ - the extraction is NOT faithful"
fi
info "ok    $SRC_FILES blobs identical, paths rooted at trainer/"

step "check 3: no production-only file survived, in the tree or in history"
# Asserts the drop above actually worked -- in the working tree AND in every
# commit, since a file removed from HEAD but left in history would still ship.
for d in $DROP_PATHS; do
  git -C "$WORK" ls-files --error-unmatch "$d" >/dev/null 2>&1 \
    && die "$d is still tracked; the drop pass did not work"
  [ -n "$(git -C "$WORK" log --all --oneline -- "$d")" ] \
    && die "$d is gone from the tree but survives in history"
done
info "ok    no production-only files, in the tree or in history"

step "check 4: the test suite runs in the extracted tree"
out=$( cd "$WORK/trainer" && uv sync --locked >/dev/null 2>&1 \
       && uv run pytest -q 2>&1 | tail -1 )
printf '%s\n' "$out" | tee "$WORK/../qf-pytest.txt"
case "$out" in
  *failed*) die "tests FAILED in the extracted tree: $out" ;;
  *passed*) : ;;
  *)        die "could not read a pytest summary: $out" ;;
esac
# Exactly one skip is expected and load-bearing: the serving-parity guard needs
# src/repo-family.js from the service tree, which this repo does not contain.
# Zero skips would mean the guard silently vanished; more than one means
# something else stopped running and nobody noticed.
printf '%s\n' "$out" | grep -q '1 skipped' \
  || die "expected exactly '1 skipped' (the serving-parity guard); got: $out"
info "ok    no failures, exactly one expected skip"

echo
echo "extraction verified in $WORK -- push it with plan Task 8."
```

- [x] **Step 2: Install `git-filter-repo` if absent**

```bash
command -v git-filter-repo || pipx install git-filter-repo || pip install --user git-filter-repo
export PATH="$HOME/.local/bin:$PATH"
git filter-repo --version
```

Expected: a version string. It is not preinstalled.

- [x] **Step 3: Run the extraction against the local checkout**

Using the local checkout rather than the remote keeps this step offline and makes the comparison exact against the tree Task 1 just modified.

```bash
cd tools/queue-forecasting
rm -rf /tmp/qf-extract /tmp/qf-before.txt /tmp/qf-after.txt
SRC="$(git rev-parse --show-toplevel)" bash host/extract-qf-research.sh
```

Expected, as measured on 2026-08-25 against `05d96b5d52`:

```
      source has 40 commits touching tools/queue-forecasting/trainer and 69 tracked files
      expecting 68 after excluding: trainer/README.md
      dropped: --path trainer/README.md
      ok    39 commits (source had 40; up to 1 excluded-path-only may prune)
      ok    68 blobs identical, paths rooted at trainer/
      ok    no production-only files, in the tree or in history
225 passed, 1 skipped in ...
      ok    no failures, exactly one expected skip
extraction verified in /tmp/qf-extract -- push it with plan Task 8.
```

Every number is **derived from the source**, not asserted against a constant. Two earlier revisions got this wrong and are worth not repeating: the first hardcoded `EXPECT_COMMITS=38`, which broke the moment a commit touched `trainer/`; the second made `trainer/README.md` a fatal error, which made the whole extraction a one-shot once that file was committed. A written-down number is a weaker claim than "the rewrite preserved exactly what the source had, minus what we deliberately excluded".

The exclusion list is single-sourced in `DROP_PATHS`, which drives the history-drop pass, the expected-blob listing, and the commit-count allowance together, so they cannot drift apart.

Any `die` means stop and diagnose — do not push a repo that failed a check.

- [x] **Step 4: Confirm the cleanup actually shrank the repository**

```bash
du -sh /tmp/qf-extract/.git
git -C /tmp/qf-extract for-each-ref --format='%(refname)'
git -C /tmp/qf-extract log --oneline | tail -2
```

Expected: a few MB (measured 2.4 MB, versus 194 MB before cleanup), exactly `refs/heads/main`, and the oldest commits being the first trainer commits — not monorepo history.

- [x] **Step 5: Make it executable and stage**

```bash
chmod +x tools/queue-forecasting/host/extract-qf-research.sh
git add tools/queue-forecasting/host/extract-qf-research.sh
```

Stop. The user commits.

---

### Task 5: Freeze notices on the monorepo trainer copy

D2: the monorepo copy is retained **indefinitely** and changes only through a human-curated port. "Frozen" means human-curated changes only, not unchanged — bets routinely add `trainer/src` modules. Both copies must name the other, or a fix lands in one and is lost.

**Files:**
- Create: `tools/queue-forecasting/trainer/README.md`
- Modify: `tools/queue-forecasting/README.md:137` (immediately after the `## Training workflows` heading)

- [x] **Step 1: Write the trainer freeze notice**

Create `tools/queue-forecasting/trainer/README.md`:

```markdown
# Trainer — production copy (frozen)

This is the **production** trainer. It serves `scripts/daily_walk_forward.sh`
and the `trainer` service in `docker-compose.yml`, and it is the copy the live
predictor's models come from.

**Research does not happen here.** It happens in `lotas/qf-research`, which
holds this directory's history from the same origin and which the research
agent owns outright. See `../auto-research-phase1-design.md`.

## What "frozen" means

Human-curated changes only. Not "unchanged" — new features legitimately add
modules here (bet 1 added `src/queue_context.py`; bet 2 added
`src/hazard_labels.py` and `src/hazard_model.py`). What is excluded is any
automated or agent-driven write.

Promoting a research result is therefore a **curated port**: the code, the
config, and any dependency change, read and applied by a human, followed by a
retrain. It is not a config copy, and there is no branch to merge.

## Retention

This copy is **not** scheduled for deletion. An earlier draft of the Phase 1
design promised Phase 2 would delete it once the dispatcher trained from
`qf-research`; that was withdrawn, because `qf-research` is untrusted by
construction. Four things must be designed and reviewed before deletion is even
proposed: an immutable human-approved revision pinned by object ID, a rewired
production job with a rollback, a hard separation keeping experiment output out
of `data/models/`, and the promotion path itself.

## If you change dependencies here

`pyproject.toml` and `uv.lock` in this directory are the **trusted** manifests.
From Phase 2 the dispatcher builds the training image from these and from a
root-owned Dockerfile in the trusted checkout — never from `qf-research`'s
copies. Refresh them with an explicit reviewed `uv lock`, then `uv sync
--locked`; `--frozen` would skip the check that the lock still agrees with the
manifest.
```

- [x] **Step 2: Point the main README's training section at it**

In `tools/queue-forecasting/README.md`, insert immediately after the `## Training workflows` heading on line 137 and before the "There are three nested workflows" line:

```markdown
> **`trainer/` here is the frozen production copy.** Research iteration happens
> in `lotas/qf-research`, which owns this directory's history. Changes here are
> human-curated ports only — see `trainer/README.md` and
> `auto-research-phase1-design.md` D2.
```

- [x] **Step 3: Verify neither file broke anything**

```bash
cd tools/queue-forecasting/trainer && uv run pytest -q 2>&1 | tail -1
```

Expected: `226 passed`. A README cannot break tests, but this is the cheap check that Task 1 is still intact before the extraction consumes this tree.

- [x] **Step 4: Stage**

```bash
git add tools/queue-forecasting/trainer/README.md tools/queue-forecasting/README.md
```

Stop. The user commits.

**Ordering matters, and this direction is the correct one: Task 4 extracts before Task 5 adds this file.**

`trainer/README.md` describes the *production* copy — "this is the production trainer, research happens elsewhere". That text is actively wrong inside `qf-research`, so it must **not** appear in the extracted history. Extracting first (68 files) and adding the notice afterwards achieves that with no filtering.

If you commit this task before running Task 4, the extraction's check 3 fails by name — `trainer/README.md is in the extract. It describes the production copy and is wrong inside qf-research` — and tells you the two recoveries: extract from an earlier commit, or `git rm trainer/README.md` in `/tmp/qf-extract` before pushing. That is the intended outcome, not a snag.

---

### Task 6: Amend the Phase 0 host artifacts

Three edits, each of which is a live control today. Spec §10, Phase 0 table.

**Files:**
- Modify: `tools/queue-forecasting/host/nc-suite.sh:76-80` and `:92`
- Modify: `tools/queue-forecasting/host/phase0-setup.sh:696-704`
- Modify: `tools/queue-forecasting/host/README.md`

- [x] **Step 1: Retire NC4's dead `/srv/qf-platform` branch**

`host/nc-suite.sh:76-80` currently reads:

```bash
if [ -d /srv/qf-platform ]; then
  refuse "NC4 platform write" "touch /srv/qf-platform/.nc-probe"
else
  echo "skip  NC4 platform (created in Phase 1; re-run then)"
fi
```

That path is never created under this design, so the message is permanently misleading — it invites a future reader to think a control is pending. Replace the whole block with:

```bash
# Platform controls (nc-suite.sh, phase0-setup.sh, and the Phase 2 dispatcher)
# live in the monorepo checkout, not in a separate /srv/qf-platform. There is
# no second path to probe: `NC4 deploy write` above already covers them.
# See auto-research-phase1-design.md D1.
echo "ok    NC4 platform  (controls live in \$DEPLOY_DIR; covered above)"
pass=$((pass + 1))
```

- [x] **Step 2: Move NC6's denied host off pypi**

`host/nc-suite.sh:92` currently reads:

```bash
refuse "NC6 denied host"   "curl -sS -o /dev/null --max-time 20 https://pypi.org"
```

pypi becomes an *allowed* host in Step 3, so this probe would invert. Replace with a target the loop genuinely never needs:

```bash
# pypi.org is allowlisted from Phase 1 on (the agent owns its own venv, so root
# never runs `uv sync` in an agent-writable worktree -- see the design's section 6).
# huggingface.co is a plausible model/dataset egress target we deliberately deny.
refuse "NC6 denied host"   "curl -sS -o /dev/null --max-time 20 https://huggingface.co"
```

- [x] **Step 3: Widen the allowlist in the generator, not just on the host**

`host/phase0-setup.sh:696-704` *generates* `/etc/tinyproxy/allowlist.txt`. Editing only the live file means the next `phase0-setup.sh egress` silently reverts it. Add two entries to the heredoc so it reads:

```bash
    sudo tee /etc/tinyproxy/allowlist.txt >/dev/null <<'LIST'
^api\.anthropic\.com$
^api\.openai\.com$
^chatgpt\.com$
^github\.com$
^api\.github\.com$
^codeload\.github\.com$
^objects\.githubusercontent\.com$
^pypi\.org$
^files\.pythonhosted\.org$
LIST
```

- [x] **Step 4: Record both changes in the host README**

Append to `tools/queue-forecasting/host/README.md`, replacing the existing `## Egress exceptions` section body ("None. If a CLI is found not to honour...") with:

```markdown
## Egress exceptions

`pypi.org` and `files.pythonhosted.org` were added 2026-08-24 for Phase 1. The
research agent creates and owns its own Python virtualenv, because the
alternative — root running `uv sync` inside an agent-writable worktree — would
let a one-line `[build-system]` addition to `pyproject.toml` execute
agent-authored code as root, and sdist dependencies build as root regardless.
Nothing root-owned now reads or executes anything from the worktree.

This gives up less than it appears to: `github.com` was already allowlisted, so
arbitrary code was already fetchable. Dependency review is enforced where it
matters, at the Phase 2 trusted image build, from a root-owned Dockerfile and
the human-promoted manifests in the trusted checkout.

NC6's denied-host probe moved from `pypi.org` to `huggingface.co` accordingly.

If a CLI is found not to honour `HTTPS_PROXY` and a direct nftables allowance is
added for it, record the endpoint, the reason, and the date here.
```

- [x] **Step 5: Move `phase0-setup.sh`'s own denied-host probe too**

`cmd_egress` does not only *generate* the allowlist, it then verifies it. Around line 836:

```bash
  run_research 'curl -sS -o /dev/null --max-time 20 https://pypi.org' 2>/dev/null \
    && die "denied host was reachable. The allowlist is not being enforced."
```

That is a **denial** assertion, not a reachability one. Once Step 3 allowlists pypi, this `die`s — aborting `cmd_egress` right after it rewrote the allowlist and restarted tinyproxy, with a message claiming the allowlist is unenforced. Task 9 Step 2 runs exactly this subcommand, so the failure would land on the host mid-change.

Point it at the same target as NC6:

```bash
  # Must match nc-suite.sh's NC6 target. pypi.org was the denied host until
  # Phase 1 allowlisted it (the agent owns its own venv), at which point this
  # check would have died claiming the allowlist was unenforced -- immediately
  # after this same function rewrote and reloaded it.
  run_research 'curl -sS -o /dev/null --max-time 20 https://huggingface.co' 2>/dev/null \
    && die "denied host was reachable. The allowlist is not being enforced."
```

- [x] **Step 6: Verify the scripts still parse and no denial probe still targets pypi**

```bash
cd tools/queue-forecasting
bash -n host/nc-suite.sh && bash -n host/phase0-setup.sh && echo "both parse"
grep -n 'huggingface' host/nc-suite.sh host/phase0-setup.sh
grep -n 'pypi' host/nc-suite.sh host/phase0-setup.sh
```

Expected: `both parse`; `huggingface.co` appears once in each script; and every remaining mention of `pypi` is either the allowlist entry `^pypi\.org$` in the generator or an explanatory comment. **No `run_research`/`refuse` line may still probe `pypi.org`** — that is the check that matters, not the absence of the string.

- [x] **Step 7: Stage**

```bash
git add tools/queue-forecasting/host/nc-suite.sh \
        tools/queue-forecasting/host/phase0-setup.sh \
        tools/queue-forecasting/host/README.md
```

Stop. The user commits.

---

### Task 7: Amend the parent design

Spec §10 lists 20 edits to the parent design (plus 5 to the Phase 0 artefacts, which Task 6 covered). Without them the parent still describes three repos and two tokens, and a future session will follow it. Work through the table top to bottom.

**Files:**
- Modify: `tools/queue-forecasting/auto-research-loop-design.md`

- [x] **Step 1: Add a superseding pointer at the top of §3.1**

Insert immediately before the repo table at line 71:

```markdown
> **Superseded in part by `auto-research-phase1-design.md` (rev 7).** Phase 1
> creates only `qf-research`; `qf-service` and `qf-platform` remain the
> monorepo, read from root-owned checkouts, and the agent's single credential
> is `Contents: write` + `Issues: write` on `qf-research` with **no credential
> of any kind** on `lotas/taskcluster`. The three-repo split below stays
> documented as the path to take if the monorepo stops being a suitable home;
> the reasoning about why repository boundaries beat path globs is unchanged.
```

- [x] **Step 2: Work the remaining 19 rows of §10's table**

For each row, make the substitution the table specifies. The rows are ordered by location, so working top to bottom keeps line numbers meaningful for the rows below. The substantive ones, in order:

| §  | Edit |
|---|---|
| §3.1 L75-79 | agents read the service via the public repo and `/srv/queue-forecasting`, not a token |
| §3.1 L80-89 | platform controls live in `/srv/queue-forecasting`; the boundary argument is now realised as credential absence |
| §3.1 L110-112 | proposals go in `qf-research/research/proposals/`; the issue is filed on `qf-research` |
| §3.2 L129-131 | both checkout rows read `lotas/taskcluster @ feat/queue-forecasting` |
| §3.3 L150-152 | egress bullet gains `pypi.org` and `files.pythonhosted.org`, with the reason |
| §3.3 L156-158 | one token, not two |
| §3.4 L173-175 | **the build-provenance rules** — root-owned Dockerfile from the trusted checkout, human-promoted `pyproject.toml`/`uv.lock`, `uv sync --locked --no-install-project`, candidate source mounted only after the image is built |
| §3.4 after L181 | root never executes code that an agent-writable path selects, build inputs included |
| §4.1 L210 | keep `/var/lib/qf-platform/state.db`, note the name no longer implies a separate repo |
| §4.2 L229-231 | rephrase the containment sentence in terms of the monorepo |
| §8.2 L409 | contracts live in the monorepo, read from the trusted checkout |
| §8.5 L575-597 | evaluator and its dependency closure likewise |
| §11.1 L631-641 | no credential on the monorepo; the diagram's "open an issue on qf-service" becomes `qf-research` |
| §12 L679-681 | both escalation kinds land on `qf-research` |
| §13.1 L724 | NC4 covers `/srv/queue-forecasting` and unit files; `/srv/qf-platform` is never created |
| §13.1 L730-734 | NC7's positive control is the canary table in the Phase 1 design §7, not issue-filing on `qf-service` |
| §14 Phase 2 L768-776 | *Accept:* also requires that the trainer image builds from trusted-checkout Dockerfile and manifests, and that a deliberately poisoned `pyproject.toml` in `qf-research` provably does not affect the built image |
| §14 Phase 1 L757-765 | replace the paragraph with a pointer to `auto-research-phase1-design.md` §1-§8 |
| §15 L827 | add that root executing agent-selected code, build inputs included, is the same failure class |

- [x] **Step 3: Verify no stale reference survives**

```bash
cd tools/queue-forecasting
grep -n "qf-service\|qf-platform" auto-research-loop-design.md
```

Expected: every remaining hit is either inside the Step 1 superseding note, the retained "path to take later" discussion, or `/var/lib/qf-platform` (a filesystem path, deliberately kept). No hit should assert that an agent holds a credential on `qf-service`, or that `/srv/qf-platform` exists.

- [x] **Step 4: Verify the two designs agree on the token**

```bash
grep -n "Contents: write" auto-research-loop-design.md auto-research-phase1-design.md
```

Expected: both name `Contents: write` **and** `Issues: write` on `qf-research`, and neither grants anything on the monorepo.

- [x] **Step 5: Stage**

```bash
git add tools/queue-forecasting/auto-research-loop-design.md
```

Stop. The user commits.

---

## Phase 1b — GitHub

### Task 8: Create `qf-research`, mint the token, push the history

**USER** — needs the user's own GitHub session. Nothing here is scriptable from the host, and deliberately so: creating the repository and minting the credential are the two acts the agent must never be able to perform.

**Files:** none in this checkout. Creates `lotas/qf-research`.

- [x] **Step 1: Create the repository — empty**

On GitHub: **New repository**, owner `lotas`, name `qf-research`, **Private**. Do **not** initialise it with a README, `.gitignore`, or licence — the push in Step 3 carries the whole history and an initial commit would collide.

Confirm Issues are enabled (Settings → Features → Issues). They are on by default for a fresh repository, unlike a fork — and the token's `Issues: write` is useless without them.

- [x] **Step 2: Mint the fine-grained token**

Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token.

- Resource owner: `lotas`
- Repository access: **Only select repositories** → `qf-research` **and nothing else**
- Permissions: `Contents: Read and write`, `Issues: Read and write`. `Metadata: Read` is added automatically and is mandatory.
- Grant **nothing** else — no Pull requests, no Workflows, no Administration.
- Expiry: as short as you are willing to rotate. NC7 will tell you loudly when it lapses (C1 voids).

Copy the token once. Do not paste it into a shell command, a file in this repo, or a chat message; Task 10 Step 2 reads it from a prompt that does not echo.

- [x] **Step 3: Push the verified extraction**

Task 4 left a verified repository in `/tmp/qf-extract`. Push that, not a fresh clone:

```bash
cd /tmp/qf-extract
git remote add origin https://github.com/lotas/qf-research
git push -u origin main
```

Expected: 38 objects-worth of history, a few MB. If git asks for credentials, authenticate as yourself — the agent's token is not involved in this step.

- [x] **Step 4: Verify what landed**

```bash
cd /tmp/qf-extract
git ls-remote --heads origin
git log --oneline origin/main | wc -l
```

Expected: exactly one head, `refs/heads/main`, and `38`. If any other ref appears, the cleanup in Task 4 Step 3 did not run — delete the remote repository and start again rather than pruning refs after the fact.

- [x] **Step 5: Seed the scaffolding**

Still in `/tmp/qf-extract`, create three files. Keep it minimal — `ledger.jsonl`, `bus.jsonl`, and `features.yaml` arrive with the Phase 4 code that writes them, and empty scaffolding invites drift.

`README.md`:

```markdown
# qf-research

Research workspace for the Taskcluster queue-forecasting program. The research
agent owns this repository and pushes to it directly.

## This repository is untrusted input

Nothing here is a control. No contract, evaluator, linter, or negative-control
suite is ever read from this repository — those live in the monorepo
(`lotas/taskcluster`, branch `feat/queue-forecasting`, under
`tools/queue-forecasting/`) and are read only from a root-owned checkout at
`/srv/queue-forecasting`. Trainer code from here runs only inside the
dispatcher's sandbox, and its outputs are validated by trusted code.

CI on this repository is advisory. Its unenforceability does not matter,
because nothing downstream trusts it.

## Layout

- `trainer/` — the trainer, carrying its history from the monorepo. Experiments
  live and die here.
- `research/experiments/` — scratch probes; the dispatcher's `probe` job kind is
  restricted to this path.
- `research/proposals/` — proposed changes to the service or platform, as
  `<date>-<slug>.md` plus an optional `.patch`. The agent cannot write to the
  monorepo, so proposals are read and applied by a human.

## Where things are NOT

- Service code (collector, live-predictor, `db.js`, `init.sql`, migrations),
  platform controls, and the human-authored design documents all stay in the
  monorepo. That repository is public, so read it directly; on the host, prefer
  `/srv/queue-forecasting`, which is always current.
- The production trainer is `tools/queue-forecasting/trainer/` in the monorepo
  and is frozen to human-curated changes. This copy and that one never merge.

## The virtualenv is yours

`trainer/.venv` is created and owned by the `research` user. Nothing
root-owned reads or executes anything in this worktree — see
`auto-research-phase1-design.md` §6.

```
cd trainer && uv sync --locked && uv run pytest -q
```

Expected: `225 passed, 1 skipped`. The skip is the serving-parity guard for
`REPO_FAMILY_DERIVATION_VERSION`, which needs `src/repo-family.js` from the
service tree; it is enforced in the monorepo instead.

**Changing `pyproject.toml` needs a human.** `uv sync` can install from pypi,
but the *trusted* manifests are the monorepo's, and the Phase 2 training image
is built from those. A dependency change is a proposal, not a commit here.
```

`research/experiments/README.md`:

```markdown
# experiments

Scratch space for probes. The dispatcher's `probe` job kind is restricted to
this directory, so anything exploratory belongs here rather than in `trainer/`.

Nothing here is authoritative: a result becomes real only when it has a
pre-registration and a verdict from the trusted evaluator.
```

`research/proposals/README.md`:

```markdown
# proposals

Changes this repository cannot make itself — anything in the service or platform
code, which lives in the monorepo and for which no credential exists here.

One file per proposal, `<YYYY-MM-DD>-<slug>.md`, stating: what to change, why,
the evidence, and how to verify it afterwards. Attach a `.patch` beside it as a
convenience if you like, but **the prose is the contract** — a patch rots as
soon as the target file moves, and a human who cannot apply it should still be
able to act on the description.

File an issue on this repository pointing at the proposal so it surfaces.
```

- [x] **Step 6: Commit and push the scaffolding**

```bash
cd /tmp/qf-extract
mkdir -p research/experiments research/proposals
# write the three files above, then:
git add README.md research/
git commit -m "docs: qf-research scaffolding and trust boundaries"
git push
```

This is the one place in this plan where you do commit — it is a different repository, and the "user commits" convention applies to the monorepo checkout.

---

## Phase 1c — host

### Two paths, and which is which

This host has **two** checkouts of the same repo and branch, and the commands
below are precise about which one they mean. Getting it backwards VOIDs controls
rather than failing them, which is the failure mode this plan works hardest to
avoid.

| Shell variable | What it is | Owner | Used for |
|---|---|---|---|
| `$DEPLOY_DIR` | the **running deployment**, inside the deploy user's home | the deploy user | `docker compose`, and `nc-suite.sh`'s probes — `.env` and `trainer/data/models` only exist here |
| `/srv/queue-forecasting` | a root-owned, secret-free **mirror** (shallow clone of the public fork) | root | everything `research` reads: `host/*.sh`, service source |

`research` cannot traverse the deploy user's home, and must not be able to — see
`auto-research-phase1-design.md` §4.1 for why a symlink does not help and why
`chmod o+x` on that home would undercut NC3.

Export the deploy path once, and use it throughout this phase:

```bash
export DEPLOY_DIR=/home/<deploy-user>/dev/taskcluster/tools/queue-forecasting
[ -f "$DEPLOY_DIR/.env" ] || echo "wrong DEPLOY_DIR: no .env here"
```

If the mirror does not exist yet, create it:

```bash
sudo git clone --depth 1 --single-branch --branch feat/queue-forecasting \
  https://github.com/lotas/taskcluster /srv/queue-forecasting
sudo -H -u research test -r /srv/queue-forecasting/tools/queue-forecasting/host/nc7-lib.sh \
  && echo "research can read the controls"
```

### Task 9: Widen the allowlist and re-prove Phase 0

Do this before anything needs pypi. Phase 0's suite must still pass afterwards, or the change broke a control.

**Files:** host state only.

- [x] **Step 1: Pull the amended controls into the trusted checkout**

```bash
sudo git -C /srv/queue-forecasting fetch --depth 1 origin feat/queue-forecasting
sudo git -C /srv/queue-forecasting reset --hard FETCH_HEAD
sudo git -C /srv/queue-forecasting log --oneline -1
```

Expected: the commits from Tasks 1-7. `reset --hard` is correct **for the mirror** and only for the mirror — it is a disposable, root-owned copy with no local state to lose, and a shallow clone cannot fast-forward in the usual way. Never run this against `$DEPLOY_DIR`.

- [x] **Step 2: Apply the allowlist change**

```bash
cd /srv/queue-forecasting/tools/queue-forecasting
sudo ./host/phase0-setup.sh egress
sudo grep -c . /etc/tinyproxy/allowlist.txt
sudo systemctl is-active tinyproxy
```

Expected: the allowlist has 9 entries, and tinyproxy is `active`. Re-running `egress` is idempotent — it rewrites the allowlist from the generator and restarts the proxy.

- [x] **Step 3: Verify the new hosts resolve and the denied one does not**

```bash
sudo -H -u research bash -lc 'curl -sS -o /dev/null -w "pypi %{http_code}\n" --max-time 20 https://pypi.org'
sudo -H -u research bash -lc 'curl -sS -o /dev/null -w "pythonhosted %{http_code}\n" --max-time 20 https://files.pythonhosted.org'
sudo -H -u research bash -lc 'curl -sS -o /dev/null -w "hf %{http_code}\n" --max-time 20 https://huggingface.co; echo "hf exit=$?"'
```

Expected: a 2xx or 4xx for the first two (reachable), and a failure or `403` for `huggingface.co`.

- [x] **Step 4: Re-run the Phase 0 negative controls**

```bash
sudo DEPLOY_DIR="$DEPLOY_DIR" SECRETS_DIR=$HOME/qf-secrets \
  /srv/queue-forecasting/tools/queue-forecasting/host/nc-suite.sh
```

Note the split: the **suite** comes from the mirror (it is a control, so it must be root-owned), while **`DEPLOY_DIR`** is the real deployment. Pointing `DEPLOY_DIR` at the mirror would VOID NC3 and NC5 — their canaries require `.env` and `trainer/data/models` to exist, and a fresh mirror has neither.

Expected: every line begins `ok` or `skip`, and the final line reads `passed=18 failed=0`. That is two higher than Phase 0's 16: NC4's platform branch now counts instead of skipping, and NC5's directory check is now an explicit `exists` assertion rather than a silent `if`. Any `VOID` or `FAIL` line means stop: the allowlist change regressed a control.

- [x] **Step 5: Refresh the Phase 0 evidence**

```bash
sudo DEPLOY_DIR="$DEPLOY_DIR" SECRETS_DIR=$HOME/qf-secrets \
  /srv/queue-forecasting/tools/queue-forecasting/host/nc-suite.sh \
  | tee /tmp/nc-evidence-phase0.txt
```

Copy that file back into the working checkout and stage it there — the deploy checkout is root-owned and is not where commits come from.

---

### Task 10: Provision the credential and the agent's checkout

**Files:** host state only. Creates `/home/research/.git-credentials` and `/home/research/qf-research/`.

- [x] **Step 1: Install `uv` and `jq` system-wide, as root**

`research` cannot fetch the `uv` installer — `astral.sh` is not allowlisted, and adding it would widen egress for a one-time need. Root's egress is unrestricted, so root installs the *binary* system-wide. Root still never runs `uv` inside the worktree.

```bash
sudo apt-get update && sudo apt-get install -y pipx jq libgomp1
sudo PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx install uv
uv --version && jq --version
sudo -H -u research bash -lc 'uv --version && jq --version'
```

`libgomp1` is the OpenMP runtime LightGBM links against. Without it Step 6's
pytest fails at import with `OSError: libgomp.so.1: cannot open shared object
file`. `trainer/Dockerfile` already installs it for the container — *"LightGBM
requires the OpenMP runtime; the -slim base image omits it"* — and running the
suite natively needs the same package on the host. It is a system library, so
root installs it; `research` still owns the venv.

Expected: both tools report versions for root and for `research`. `curl | sh` is deliberately avoided: piping a remote script into a root shell is a worse habit than a pinned package install, and `jq` is needed by `nc7-suite.sh`.

- [x] **Step 2: Write the credential without it reaching argv or shell history**

```bash
read -rs -p "paste the qf-research PAT: " PAT; echo
printf 'https://x-access-token:%s@github.com\n' "$PAT" \
  | sudo -u research tee /home/research/.git-credentials >/dev/null
sudo chmod 600 /home/research/.git-credentials
sudo chown research:research /home/research/.git-credentials
unset PAT
sudo stat -c '%a %U %n' /home/research/.git-credentials
```

Expected: `600 research /home/research/.git-credentials`.

`read -rs` does not echo and does not enter history. `printf` is a bash builtin, so the token never becomes a process argument; it reaches `tee` on stdin only.

- [x] **Step 3: Configure the helper, with `useHttpPath` set explicitly**

```bash
sudo -H -u research bash -lc '
  git config --global credential.helper store &&
  git config --global credential.useHttpPath false &&
  git config --global --get-regexp "^credential\." '
```

Expected: `credential.helper store` and `credential.useHttpPath false`.

`useHttpPath=false` is the default, but it is set explicitly because it is load-bearing: with it `true`, git matches by path, the one stored entry would not be offered for the monorepo, and NC7's `R1` would go **VOID** for want of a credential instead of testing the token's scope. It affects `R1` only — the REST probes carry their own `Authorization` header.

- [x] **Step 4: Verify both URLs resolve to the same credential, without printing it**

```bash
sudo -H -u research bash -lc '
  . /srv/queue-forecasting/tools/queue-forecasting/host/nc7-lib.sh
  d() { printf "protocol=https\nhost=github.com\npath=%s\n\n" "$1" \
        | GIT_TERMINAL_PROMPT=0 git credential fill | cred_fields_digest; }
  a=$(d lotas/qf-research)  || { echo "lookup for qf-research FAILED"; exit 1; }
  b=$(d lotas/taskcluster)  || { echo "lookup for taskcluster FAILED"; exit 1; }
  if [ "$a" = "$b" ]; then echo "OK both URLs resolve to the same credential"; else echo "MISMATCH"; exit 1; fi
  unset a b '
```

Expected: `OK both URLs resolve to the same credential`.

This reuses `cred_fields_digest` from the tested library rather than reimplementing it, which matters: a naive version hashes the empty string on failure, so two *failed* lookups compare equal and the check passes vacuously. `cred_fields_digest` returns non-zero unless it sees exactly one non-empty username and one non-empty password, which is why the `|| { ...; exit 1; }` arms can be trusted.

- [x] **Step 5: Clone the research repository as `research`**

```bash
sudo -H -u research bash -lc 'cd ~ && GIT_TERMINAL_PROMPT=0 git clone https://github.com/lotas/qf-research'
sudo -H -u research bash -lc 'cd ~/qf-research && git log --oneline | wc -l && git status --short'
```

Expected: `39` commits (38 extracted + the scaffolding commit) and a clean status. The clone goes through the proxy; `github.com` has been allowlisted since Phase 0.

- [x] **Step 6: Build the venv as `research`, and confirm root is not involved**

```bash
sudo -H -u research bash -lc 'cd ~/qf-research/trainer && uv sync --locked' 2>&1 | tail -3
sudo -H -u research bash -lc 'cd ~/qf-research/trainer && uv run pytest -q' 2>&1 | tail -2
sudo find /home/research/qf-research -not -user research -printf '%u %p\n' | head
```

Expected: the sync resolves from pypi; pytest reports `225 passed, 1 skipped`; and the `find` prints **nothing** — no file in the worktree is owned by anyone but `research`. That last check is the point of §6: if root had run the sync, `.venv` would carry root-owned artifacts.

---

### Task 11: Run NC7 and record the evidence

**Files:**
- Create: `tools/queue-forecasting/host/nc-evidence-phase1.txt`

- [x] **Step 1: Run the library tests on the host**

```bash
sudo -H -u research bash -lc \
  'bash /srv/queue-forecasting/tools/queue-forecasting/host/nc7-lib.test.sh' | tail -1
```

Expected: `tests=38 failed=0`. Run this first every time — the suite's judgement is only as good as this file.

- [x] **Step 2: Dry-run the canaries**

```bash
sudo -H -u research bash -lc '/srv/queue-forecasting/tools/queue-forecasting/host/nc7-suite.sh --check'
```

Expected: `ok` for C1 (token authenticates), C2 (push to `qf-research`), C3 (issue filed and closed), C4 (monorepo readable anonymously), then `canaries only (--check); passed=4 failed=0`.

A `VOID` here means the token is wrong before any refusal has been attempted — fix that first, because every refusal would otherwise be vacuous. `VOID C3` specifically means `Issues: write` is missing or Issues are disabled on the repository.

- [x] **Step 3: Run the full suite**

```bash
sudo -H -u research bash -lc \
  'EVIDENCE=/tmp/nc7-evidence.txt /srv/queue-forecasting/tools/queue-forecasting/host/nc7-suite.sh'
echo "exit=$?"
```

Expected: `ok` for C1-C4, R1, R1b, R2-R7, one `skip` line for R8 unless you set `OUT_OF_SCOPE_REPO`, one `info` line for the monorepo issue probe, `ok` for the evidence-file secret check, and a final `passed=13 failed=0`.

If the account has a private repository the token should not see, run it once with that name to exercise R8 as well:

```bash
sudo -H -u research bash -lc \
  'OUT_OF_SCOPE_REPO=lotas/taskcluster EVIDENCE=/tmp/nc7-evidence.txt \
   /srv/queue-forecasting/tools/queue-forecasting/host/nc7-suite.sh'
```

Expected then: `ok    R8 ... (refused, HTTP 404)` and `passed=14`. GitHub returns 404 rather than 403 for out-of-scope private resources, which the scoring credits as a refusal.

Read every line rather than the summary alone:
- **`FAIL R1`** with "the ref EXISTS" — a real containment breach. Delete `refs/heads/nc7-git-probe-*` on `lotas/taskcluster` **with your own credential**, then revoke the PAT and re-check its repository scope.
- **`FAIL R1b`** — the probe erased `/home/research/.git-credentials`. Restore it via Task 10 Step 2 and report it: it means git took the reject path on a 403.
- **`VOID`** anywhere — the probe could not be meaningfully attempted. Diagnose before re-running; a VOID is a failure precisely so it cannot be mistaken for containment.

- [x] **Step 4: Confirm the evidence carries no secret, then stage it**

```bash
sudo -u research grep -c . /tmp/nc7-evidence.txt
sudo -H -u research bash -lc '
  . /srv/queue-forecasting/tools/queue-forecasting/host/nc7-lib.sh
  TOK=$(sed -n "s|^https://[^:]*:\([^@]*\)@github\.com.*|\1|p" ~/.git-credentials | head -1)
  if secret_leaked /tmp/nc7-evidence.txt "$TOK"; then echo "LEAK - do not stage"; exit 1; else echo "clean"; fi '
```

Expected: a line count, then `clean`. The suite already asserts this; it is re-asserted here because the next command copies the file into a git repository, and a leaked secret in a staged file is worse than any control this suite tests.

Then copy it into the working checkout, prepend the run's summary, and stage:

```bash
# from the working checkout, not the deploy checkout.
# QF_HOST is the experimental server; it is deliberately not written into this
# repo, which is a public fork.
scp "$QF_HOST:/tmp/nc7-evidence.txt" tools/queue-forecasting/host/nc-evidence-phase1.txt
grep -n "REDACTED\|password\|github_pat" tools/queue-forecasting/host/nc-evidence-phase1.txt
git add tools/queue-forecasting/host/nc-evidence-phase1.txt
```

Expected from the grep: `<REDACTED-TOKEN>` placeholders at most, and **no** `github_pat_` anywhere. Stop and re-check the redactor if any appears.

Stop. The user commits.

---

### Task 12: Acceptance

Every item is a command with an expected result, not a judgement call.

- [x] **Step 1: The live services were never touched**

```bash
sudo docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'collector|live-predictor|worker-counter|health-monitor'
```

Expected: uptimes spanning the whole phase — hours or days, not minutes. If any of these restarted, find out why before continuing; this phase is defined by not disturbing them.

- [x] **Step 2: Collection is still ingesting**

```bash
sudo docker compose -f "$DEPLOY_DIR/docker-compose.yml" \
  exec -T postgres psql -U postgres -d forecasting -tAc \
  "select count(*) from queue_forecast_tasks where task_created > now() - interval '1 hour'"
```

`$DEPLOY_DIR`, not the mirror: the mirror has no `.env`, so compose could not resolve its variables, and the running containers belong to the deployment anyway.

Expected: a non-zero count in the thousands. fxci ingests ~200-300k rows/day.

- [x] **Step 3: Production training still runs from the monorepo copy**

```bash
git -C "$DEPLOY_DIR" status --short trainer
ls -d "$DEPLOY_DIR/trainer/src"
grep -n 'docker compose run --rm --entrypoint uv trainer' "$DEPLOY_DIR/scripts/daily_walk_forward.sh"
```

Expected: a clean status, the directory present, and the `daily_walk_forward.sh` line intact at ~249. Nothing about the production training path changed in this phase.

- [x] **Step 4: Walk the spec's acceptance list**

Confirm each item of `auto-research-phase1-design.md` §8:

1. `qf-research` is private; `trainer/` carries 38 commits; the blob listing matched (Task 4 Step 3); `pytest` reports 225 passed + 1 skipped (Task 10 Step 6).
2. `research` can clone, commit, and push, and `uv sync` succeeded **as `research`** with no root-owned file in the worktree (Task 10 Steps 5-6).
3. `git credential fill` returns the same credential for both URLs (Task 10 Step 4).
4. NC7 exits 0, `failed=0`, no `VOID`, evidence carries no secret, durable credential intact (Task 11 Step 3-4).
5. `nc-suite.sh` reports `passed=18 failed=0` after the NC6 change, and Phase 0 evidence is refreshed (Task 9 Steps 4-5).
6. `trainer/` is marked frozen in both places and `daily_walk_forward.sh` is unchanged (Tasks 5, 12 Step 3).
7. Collector and live-predictor were never rebuilt or restarted (Step 1 above).
8. The parent design, `nc-suite.sh`, and `phase0-setup.sh` are amended (Tasks 6-7).

- [ ] **Step 5: Stage anything outstanding and stop**

```bash
git status --short
```

Everything from Tasks 1-7 and 11 should be staged. The user commits.

---

## What Phase 1 deliberately does not do

Recorded here so a later reader does not mistake absence for oversight:

- **No `qf-service` or `qf-platform` repository.** Not needed while the monorepo holds no agent-writable content. Revisit only if the monorepo stops being a suitable home.
- **No vendored `lib-pulse`, no self-contained Dockerfiles, no `/srv` re-pointing.** All of it exists only to make `qf-service` buildable, which nothing needs yet.
- **No deletion of the monorepo `trainer/`.** Blocked on the four prerequisites in the spec's D2.
- **No dispatcher, evaluator, or contracts.** Phase 2. The build-provenance rules for them are recorded in the spec §6 and now in the parent design §3.4, so Phase 2 inherits them rather than rediscovering them.
- **No `ledger.jsonl`, `bus.jsonl`, or `features.yaml`.** Phase 4 writes them.
- **`db-app-cutover` remains deferred** from Phase 0 — services still connect as the postgres superuser. Unrelated to this phase, and it recreates containers, which conflicts with acceptance item 7.
