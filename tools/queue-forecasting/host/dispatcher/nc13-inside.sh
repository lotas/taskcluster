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

# Source mount is read-only, and writable output exists.
#
# The canary asks whether the MOUNT is there and readable, not whether some
# particular file is. Naming `pyproject.toml` made this canary a statement about
# qf-research's directory layout: the repository keeps it elsewhere, so the
# canary voided on a sandbox that was in fact working perfectly, and the void
# read as a containment failure.
[ -d /app/trainer ] && [ -n "$(ls -A /app/trainer 2>/dev/null)" ] \
  && ok "canary: source mount readable" || void "canary: source mount readable"
( : > /app/trainer/.nc13 ) 2>/dev/null \
  && bad "source mount is writable" || ok "source mount is read-only"
# There WAS a second write here, to /app/trainer/data. It is gone rather than
# canaried, because it is an assertion that cannot fail. `:ro` applies to the
# whole subtree, so the write at the mount root above already tests the mount
# flag; the only way `data/` could differ is a separate rw bind mount over it,
# and `sandbox._check_extra_dest` allows extra destinations ONLY at /artifacts
# or under /trusted/ -- so that configuration cannot be built. What the check
# actually did was pass for whichever reason applied, EROFS or ENOENT, and count
# as evidence either way. The allowlist is where this property is really
# enforced, and `tests/test_sandbox.py` is where it is really tested.
( : > /out/.nc13 ) 2>/dev/null \
  && ok "canary: /out is writable" || void "canary: /out is writable"

# Credentials
[ -z "${DATABASE_URL:-}" ] && ok "DATABASE_URL unset" || bad "DATABASE_URL is set"

# --- 2b-2: THE DATA PLANE IS NOT AMBIENT ---------------------------------
#
# This script runs as a SELFTEST, and a selftest asks for no extract. So the
# assertion here is the ABSENCE of one -- which is a control the plan did not
# have and is the stronger half of the pair.
#
# The plan called for "/extract present, readable, not writable" in NC13. Those
# can only be asserted from a job that HAS an extract, i.e. a probe; and a probe
# runs only agent-authored code under research/experiments/, so they live in the
# 2b-2 cohort fixture instead. What belongs HERE is the other direction: a job
# that did not request data must not find data lying around. If /extract were
# mounted for every kind, every job would carry a read of the production dataset
# it never asked for, and nothing would notice.
if [ -e /extract ]; then
  bad "/extract exists in a selftest, which requested no extract: the data plane is ambient"
else
  ok "/extract is absent in a job that requested none"
fi
# The writable hole is equally not ambient. `trainer/data` is a mount only for a
# probe; here the tree is read-only all the way down, and a write that succeeded
# would mean the source mount had lost its :ro.
if [ -d /app/trainer/data ] && (: > /app/trainer/data/.nc13-probe) 2>/dev/null; then
  bad "/app/trainer/data is writable in a selftest: the source mount is not read-only"
  rm -f /app/trainer/data/.nc13-probe 2>/dev/null || true
else
  ok "/app/trainer/data is not writable in a job that requested no data mount"
fi
env | grep -qiE 'password|secret|token|pulse|taskcluster' \
  && bad "credential-shaped env var present" || ok "no credential-shaped env vars"
find / -xdev -maxdepth 3 -name '.env' -readable 2>/dev/null | grep -q . \
  && bad ".env readable" || ok "no readable .env"
[ -e /srv/queue-forecasting ] && bad "trusted checkout visible" \
  || ok "trusted checkout not visible"

printf '{"pass":%d,"fail":%d}\n' "$pass" "$fail" > /out/nc13.json
echo "== NC13: pass=$pass fail=$fail =="
[ "$fail" -eq 0 ]
