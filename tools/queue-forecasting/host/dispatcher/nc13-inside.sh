#!/bin/sh
# NC13, from inside the sandbox. Mounted read-only from the trusted checkout;
# never read from the research worktree (NC10).
#
# Same discipline as nc-suite.sh: a refusal only counts if the attempt was
# possible, so each group is preceded by a canary that must SUCCEED. VOID is a
# failure. Results go to stdout and /out/nc13.json; exit 1 on any failure.
set -u
PY=/opt/qfenv/bin/python
pass=0; fail=0
ok()   { echo "ok    $1"; pass=$((pass+1)); }
bad()  { echo "FAIL  $1"; fail=$((fail+1)); }
void() { echo "VOID  $1"; fail=$((fail+1)); }

# Canary: the environment is real. Without this, every "cannot" below could be
# "python is broken".
$PY -c 'import lightgbm, pandas, pytest' 2>/dev/null \
  && ok "canary: trusted environment imports" \
  || void "canary: trusted environment imports"

# Identity
[ "$(id -u)" = "10001" ] && ok "runs as uid 10001" || bad "runs as uid $(id -u)"
grep -q '^CapEff:\s*0\{16\}$' /proc/self/status \
  && ok "no effective capabilities" || bad "effective capabilities present"

# Network: --network none
$PY - <<'PY' 2>/dev/null && bad "DNS resolves" || ok "DNS does not resolve"
import socket; socket.gethostbyname("github.com")
PY
$PY - <<'PY' 2>/dev/null && bad "outbound TCP connects" || ok "outbound TCP refused"
import socket; socket.create_connection(("1.1.1.1", 443), timeout=5)
PY

# Container runtime
[ -d /run ] && ok "canary: /run exists" || void "canary: /run exists"
[ -S /var/run/docker.sock ] && bad "docker socket present" || ok "no docker socket"

# Source mount is read-only, and writable output exists
[ -r /app/trainer/pyproject.toml ] \
  && ok "canary: source mount readable" || void "canary: source mount readable"
( : > /app/trainer/.nc13 ) 2>/dev/null \
  && bad "source mount is writable" || ok "source mount is read-only"
( : > /app/trainer/data/.nc13 ) 2>/dev/null \
  && bad "trainer/data is writable" || ok "trainer/data is not writable"
( : > /out/.nc13 ) 2>/dev/null \
  && ok "canary: /out is writable" || void "canary: /out is writable"

# Credentials
[ -z "${DATABASE_URL:-}" ] && ok "DATABASE_URL unset" || bad "DATABASE_URL is set"
env | grep -qiE 'password|secret|token|pulse|taskcluster' \
  && bad "credential-shaped env var present" || ok "no credential-shaped env vars"
find / -xdev -maxdepth 3 -name '.env' -readable 2>/dev/null | grep -q . \
  && bad ".env readable" || ok "no readable .env"
[ -e /srv/queue-forecasting ] && bad "trusted checkout visible" \
  || ok "trusted checkout not visible"

printf '{"pass":%d,"fail":%d}\n' "$pass" "$fail" > /out/nc13.json
echo "== NC13: pass=$pass fail=$fail =="
[ "$fail" -eq 0 ]
