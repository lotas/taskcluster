#!/usr/bin/env bash
# Negative-control suite, Phase 0 (spec section 13.1).
#
# Every check below attempts a FORBIDDEN action as the research user and
# passes only if the action is REFUSED. Run as root:
#   sudo ./host/nc-suite.sh
#
# A refusal is only meaningful if the action was POSSIBLE to attempt. A missing
# binary or a missing target path makes `refuse` pass for the wrong reason, so
# every refusal group is preceded by a canary that must SUCCEED. If a canary
# fails, the group's result is void and the suite exits non-zero.
#
# Exit 0 = all controls fail closed. Exit 1 = at least one control is open or void.
#
# Note the invocation: `sudo -H -u research bash -lc`, NOT `sudo -i`. With -i,
# sudo re-joins and re-parses the command string, so a mangled command fails
# for the wrong reason and `refuse` reads that as a pass.

set -uo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-/srv/queue-forecasting}"
SECRETS_DIR="${SECRETS_DIR:-$HOME/qf-secrets}"
pass=0
fail=0

refuse() {   # refuse <name> <command...>  -> passes when the command FAILS
  local name="$1"; shift
  if sudo -H -u research bash -lc "$*" >/dev/null 2>&1; then
    echo "FAIL  $name  (action was PERMITTED)"
    fail=$((fail + 1))
  else
    echo "ok    $name  (refused)"
    pass=$((pass + 1))
  fi
}

canary() {   # canary <name> <command...> -> passes when the command SUCCEEDS
  local name="$1"; shift
  if sudo -H -u research bash -lc "$*" >/dev/null 2>&1; then
    echo "ok    $name  (canary: attempt is possible)"
    pass=$((pass + 1))
  else
    echo "VOID  $name  (canary failed - refusals in this group prove nothing)"
    fail=$((fail + 1))
  fi
}

echo "== NC1: write to any forecasting table =="
EXP_URL="postgresql://forecast_experiment:$(cat "$SECRETS_DIR/forecast_experiment.pw")@127.0.0.1:5433/forecasting"
# Canary first: proves psql exists, the port is reachable, and auth works.
# Without it, a missing psql would make both refusals below pass vacuously.
canary "NC1 select"       "psql '$EXP_URL' -c 'SELECT 1;'"
refuse "NC1 delete"       "psql '$EXP_URL' -c \"DELETE FROM queue_forecast_tasks WHERE false;\""
refuse "NC1 create table" "psql '$EXP_URL' -c 'CREATE TABLE agent_escape (x int);'"

echo "== NC2: container runtime =="
# Canary: the socket must EXIST, otherwise "not writable" is vacuous.
canary "NC2 socket exists" "test -e /var/run/docker.sock"
refuse "NC2 socket write"  "test -w /var/run/docker.sock"
refuse "NC2 docker ps"     "docker ps"
if command -v podman >/dev/null 2>&1; then
  refuse "NC2 podman ps"   "podman ps"
else
  echo "skip  NC2 podman   (not installed on this host)"
fi

echo "== NC3: credentials =="
canary "NC3 env exists"    "test -e $DEPLOY_DIR/.env"
refuse "NC3 .env"          "cat $DEPLOY_DIR/.env"
refuse "NC3 secrets dir"   "cat $SECRETS_DIR/forecast_app.pw"
refuse "NC3 root ssh dir"  "ls /root/.ssh"

echo "== NC4: trusted checkouts and units =="
refuse "NC4 deploy write"  "touch $DEPLOY_DIR/.nc-probe"
refuse "NC4 unit write"    "touch /etc/systemd/system/.nc-probe"
if [ -d /srv/qf-platform ]; then
  refuse "NC4 platform write" "touch /srv/qf-platform/.nc-probe"
else
  echo "skip  NC4 platform (created in Phase 1; re-run then)"
fi

echo "== NC5: live model directory =="
if [ -d "$DEPLOY_DIR/trainer/data/models" ]; then
  refuse "NC5 models write" "touch $DEPLOY_DIR/trainer/data/models/.nc-probe"
else
  echo "VOID  NC5 models   (directory missing - control is vacuous)"
  fail=$((fail + 1))
fi

echo "== NC6: egress =="
canary "NC6 allowed host"  "curl -sS -o /dev/null --max-time 20 https://api.github.com"
refuse "NC6 denied host"   "curl -sS -o /dev/null --max-time 20 https://pypi.org"
refuse "NC6 proxy bypass"  "curl -sS -o /dev/null --max-time 20 --noproxy '*' https://api.github.com"

echo
echo "passed=$pass failed=$fail"
[ "$fail" -eq 0 ] || exit 1
