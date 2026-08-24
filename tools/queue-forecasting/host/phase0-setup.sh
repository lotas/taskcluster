#!/usr/bin/env bash
#
# Phase 0 host bootstrap for the auto-research loop.
#   Spec: ../auto-research-loop-design.md  (sections 3, 13)
#   Plan: ../auto-research-phase0-plan.md  (tasks 2-9)
#
# WHO RUNS THIS: you, as a sudo-capable user. Not an agent. The whole point of
# this script existing is that no agent needs privilege on this host.
#
# PROPERTIES:
#   - Idempotent. Every subcommand is safe to re-run.
#   - --check does a dry run: reports what WOULD change, touches nothing.
#   - Hard stops instead of improvising. Anything unexpected exits non-zero
#     with an explanation. It never "works around" a surprise.
#   - The one risky step (db-auth) verifies collection is still ingesting
#     afterwards and rolls itself back if not.
#
# USAGE:
#   ./phase0-setup.sh discover                # read-only report, changes nothing
#   ./phase0-setup.sh db-auth --check         # show the plan for SCRAM cutover
#   ./phase0-setup.sh db-auth                 # apply it, verify, roll back on failure
#   ./phase0-setup.sh db-roles
#   ./phase0-setup.sh research-user
#   ./phase0-setup.sh egress
#   ./phase0-setup.sh agent-cli
#   ./phase0-setup.sh verify                  # negative controls 1-6
#   ./phase0-setup.sh all                     # every step in order, stop on first failure
#
# ENVIRONMENT:
#   DEPLOY_DIR   deploy checkout (default: autodetected, else /srv/queue-forecasting)
#   SECRETS_DIR  where generated passwords live (default: ~/qf-secrets, mode 0700)

set -euo pipefail

SECRETS_DIR="${SECRETS_DIR:-$HOME/qf-secrets}"
CHECK=0
RESEARCH_USER="research"
PROXY_PORT=8888

# --------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------

c_red=$'\033[31m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_dim=$'\033[2m'; c_off=$'\033[0m'

step() { printf '\n%s== %s ==%s\n' "$c_grn" "$1" "$c_off"; }
info() { printf '   %s\n' "$1"; }
skip() { printf '   %sskip%s %s\n' "$c_dim" "$c_off" "$1"; }
warn() { printf '   %swarn%s %s\n' "$c_ylw" "$c_off" "$1"; }
die()  { printf '\n%sSTOP%s %s\n\n' "$c_red" "$c_off" "$1" >&2; exit 1; }

would() {  # would <description> ; returns 0 if we should actually act
  if [ "$CHECK" = 1 ]; then
    printf '   %swould%s %s\n' "$c_ylw" "$c_off" "$1"
    return 1
  fi
  printf '   %s\n' "$1"
  return 0
}

# --------------------------------------------------------------------------
# environment discovery
# --------------------------------------------------------------------------

detect_deploy_dir() {
  if [ -n "${DEPLOY_DIR:-}" ]; then
    [ -f "$DEPLOY_DIR/docker-compose.yml" ] \
      || die "DEPLOY_DIR=$DEPLOY_DIR has no docker-compose.yml"
    return
  fi
  local candidate
  for candidate in /srv/queue-forecasting /opt/queue-forecasting "$HOME/queue-forecasting"; do
    if [ -f "$candidate/docker-compose.yml" ]; then DEPLOY_DIR="$candidate"; return; fi
    if [ -f "$candidate/tools/queue-forecasting/docker-compose.yml" ]; then
      DEPLOY_DIR="$candidate/tools/queue-forecasting"; return
    fi
  done
  die "could not find the deploy checkout. Set DEPLOY_DIR explicitly."
}

psql_super() {  # psql as the superuser over the container's local socket
  docker compose -f "$DEPLOY_DIR/docker-compose.yml" exec -T postgres \
    psql -U postgres -d forecasting -v ON_ERROR_STOP=1 "$@"
}

compose() { docker compose -f "$DEPLOY_DIR/docker-compose.yml" "$@"; }

require_secret() {  # require_secret <role> -> generates once, reuses thereafter
  local role="$1" f="$SECRETS_DIR/$role.pw"
  if [ ! -f "$f" ]; then
    ( umask 077; mkdir -p "$SECRETS_DIR"; openssl rand -base64 32 | tr -d '\n' > "$f" )
    chmod 700 "$SECRETS_DIR"; chmod 600 "$f"
  fi
  cat "$f"
}

urlencode() { python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.stdin.read(),safe=""))'; }

# --------------------------------------------------------------------------
# task 2 - discovery (read-only)
# --------------------------------------------------------------------------

cmd_discover() {
  step "Discovery (read-only)"
  info "deploy dir: $DEPLOY_DIR"

  info "--- services ---"
  compose ps

  info "--- password_encryption ---"
  local enc
  enc="$(psql_super -tAc 'SHOW password_encryption;')"
  info "password_encryption = $enc"
  if [ "$enc" != "scram-sha-256" ]; then
    die "password_encryption is '$enc', not scram-sha-256.
     Setting passwords now would store them in the wrong scheme and the
     pg_hba cutover would lock the services out. Fix first:
       ALTER SYSTEM SET password_encryption='scram-sha-256'; SELECT pg_reload_conf();
     then re-run discover."
  fi

  info "--- roles ---"
  psql_super -c '\du'

  info "--- tables (this is the grant list db-roles will use) ---"
  psql_super -tAc \
    "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename;"

  info "--- current pg_hba rules ---"
  psql_super -c \
    "SELECT type, database, user_name, address, auth_method FROM pg_hba_file_rules ORDER BY line_number;"

  info "--- host facts ---"
  info "nftables: $(command -v nft >/dev/null && nft --version || echo 'NOT INSTALLED')"
  info "tinyproxy: $(command -v tinyproxy >/dev/null && echo present || echo 'NOT INSTALLED')"
  info "psql client: $(command -v psql >/dev/null && psql --version || echo 'NOT INSTALLED')"
  info "docker group members: $(getent group docker | cut -d: -f4)"
  info "memory (GB): $(free -g | awk '/^Mem:/{print $2}')   cpus: $(nproc)"
  free -g; df -h /

  step "Discovery complete"
  info "Read the table list above. If it contains tables beyond the six the"
  info "spec expects, db-roles will still grant them (it reads the live list),"
  info "but you should know why they are there."
}

# --------------------------------------------------------------------------
# tasks 3+4 - passwords and SCRAM cutover (the risky one)
# --------------------------------------------------------------------------

cmd_db_auth() {
  step "Task 3-4: passwords and SCRAM cutover"

  local enc
  enc="$(psql_super -tAc 'SHOW password_encryption;')"
  [ "$enc" = "scram-sha-256" ] || die "password_encryption='$enc'; run discover and fix it first."

  # --- passwords ----------------------------------------------------------
  local pg_pw
  pg_pw="$(require_secret postgres)"
  if would "set the postgres role password (stored in $SECRETS_DIR/postgres.pw)"; then
    psql_super -c "ALTER ROLE postgres WITH PASSWORD '$pg_pw';" >/dev/null
    local scheme
    scheme="$(psql_super -tAc "SELECT left(rolpassword,14) FROM pg_authid WHERE rolname='postgres';")"
    [ "$scheme" = "SCRAM-SHA-256" ] || die "password stored as '$scheme', expected SCRAM-SHA-256."
    info "stored as SCRAM-SHA-256"
  fi

  # --- backup pg_hba ------------------------------------------------------
  if would "back up pg_hba.conf to pg_hba.conf.pre-scram"; then
    compose exec -T postgres bash -c \
      '[ -f /var/lib/postgresql/data/pg_hba.conf.pre-scram ] ||
       cp /var/lib/postgresql/data/pg_hba.conf /var/lib/postgresql/data/pg_hba.conf.pre-scram'
  fi

  # --- already done? ------------------------------------------------------
  local trust_rules
  trust_rules="$(psql_super -tAc \
    "SELECT count(*) FROM pg_hba_file_rules WHERE type='host' AND auth_method='trust';")"
  if [ "$trust_rules" = "0" ]; then
    skip "no host-type trust rules remain; pg_hba already migrated"
  else
    if would "replace $trust_rules host-type trust rule(s) with scram-sha-256"; then
      compose exec -T postgres bash -c 'cat > /var/lib/postgresql/data/pg_hba.conf <<HBA
# TYPE  DATABASE  USER  ADDRESS       METHOD
# Container-local socket keeps peer/trust so maintenance psql still works.
local   all       all                 trust
# Every network connection - service containers, trainer, any tunnel - must
# authenticate. Leaving this as trust is what made a read-only role decoration.
host    all       all   127.0.0.1/32  scram-sha-256
host    all       all   ::1/128       scram-sha-256
host    all       all   0.0.0.0/0     scram-sha-256
host    all       all   ::/0          scram-sha-256
HBA'
      psql_super -c 'SELECT pg_reload_conf();' >/dev/null
      local errs
      errs="$(psql_super -tAc "SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL;")"
      [ "$errs" = "0" ] || die "pg_hba has $errs malformed rule(s). Run: $0 rollback-db-auth"
      info "pg_hba reloaded, no malformed rules"
    fi
  fi

  # --- prove unauthenticated network access is refused --------------------
  if [ "$CHECK" = 0 ]; then
    if compose exec -T postgres \
         psql "postgresql://postgres@127.0.0.1:5432/forecasting" -c 'SELECT 1;' >/dev/null 2>&1; then
      die "an unauthenticated network connection still succeeded.
     pg_hba did not take effect. Run: $0 rollback-db-auth"
    fi
    info "unauthenticated network connection refused (as intended)"
  fi

  # --- .env cutover -------------------------------------------------------
  local envf="$DEPLOY_DIR/.env"
  [ -f "$envf" ] || die "no .env at $envf"
  if grep -q '^DATABASE_URL=.*:.*@' "$envf"; then
    skip ".env DATABASE_URL already carries a password"
  else
    if would "rewrite DATABASE_URL in .env to authenticate as postgres"; then
      cp "$envf" "$envf.pre-scram"
      local enc_pw
      enc_pw="$(printf '%s' "$pg_pw" | urlencode)"
      sed -i "s|^DATABASE_URL=.*|DATABASE_URL=postgresql://postgres:${enc_pw}@postgres:5432/forecasting|" "$envf"
      chmod 600 "$envf"
      info "backup at $envf.pre-scram"
    fi
  fi

  # --- restart and health-gate, with rollback -----------------------------
  if [ "$CHECK" = 1 ]; then skip "restart + health gate (dry run)"; return 0; fi

  local before after
  before="$(psql_super -tAc 'SELECT count(*) FROM queue_forecast_tasks;')"
  info "restarting services..."
  compose up -d >/dev/null
  sleep 25

  if ! compose ps --status running --quiet | grep -q .; then
    db_auth_rollback; die "no services running after restart; rolled back."
  fi
  after="$(psql_super -tAc 'SELECT count(*) FROM queue_forecast_tasks;' 2>/dev/null || echo FAIL)"
  if [ "$after" = "FAIL" ]; then
    db_auth_rollback; die "postgres unreachable after restart; rolled back."
  fi

  info "waiting 90s to confirm collection is still ingesting..."
  sleep 90
  local later
  later="$(psql_super -tAc 'SELECT count(*) FROM queue_forecast_tasks;')"
  if [ "$later" -le "$after" ]; then
    warn "task count did not grow ($after -> $later)."
    warn "This can be a genuinely quiet period, or a broken collector."
    compose logs --tail 30 collector || true
    die "not auto-rolling back, because a quiet queue looks identical to a
     broken collector and reverting blindly could be the wrong move.
     Check the log above. If the collector is failing to authenticate:
       $0 rollback-db-auth"
  fi
  info "collection confirmed ingesting ($before -> $later)"
  step "SCRAM cutover complete"
}

db_auth_rollback() {
  warn "rolling back pg_hba and .env"
  compose exec -T postgres bash -c \
    'cp /var/lib/postgresql/data/pg_hba.conf.pre-scram /var/lib/postgresql/data/pg_hba.conf' || true
  [ -f "$DEPLOY_DIR/.env.pre-scram" ] && cp "$DEPLOY_DIR/.env.pre-scram" "$DEPLOY_DIR/.env"
  compose up -d >/dev/null || true
  sleep 10
}

cmd_rollback_db_auth() { step "Rolling back db-auth"; db_auth_rollback; info "done"; }

# --------------------------------------------------------------------------
# task 5 - roles and grants
# --------------------------------------------------------------------------

cmd_db_roles() {
  step "Task 5: experiment, migrator, and app roles"

  local tables
  tables="$(psql_super -tAc \
    "SELECT string_agg(quote_ident(tablename), ', ' ORDER BY tablename)
       FROM pg_tables WHERE schemaname='public';")"
  [ -n "$tables" ] || die "no tables found in schema public"
  info "grant list (derived from the live database):"
  printf '     %s\n' "$tables"

  local role
  for role in forecast_app forecast_experiment forecast_migrator; do
    local pw; pw="$(require_secret "$role")"
    if psql_super -tAc "SELECT 1 FROM pg_roles WHERE rolname='$role';" | grep -q 1; then
      skip "role $role already exists (password left unchanged)"
    else
      if would "create role $role"; then
        local extra=""
        [ "$role" = forecast_experiment ] && extra="CONNECTION LIMIT 4"
        [ "$role" = forecast_migrator ]   && extra="CONNECTION LIMIT 2"
        psql_super -c "CREATE ROLE $role LOGIN PASSWORD '$pw' $extra;" >/dev/null
      fi
    fi
  done

  if would "revoke implicit PUBLIC privileges"; then
    psql_super <<'SQL' >/dev/null
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON DATABASE forecasting FROM PUBLIC;
SQL
  fi

  if would "grant read-only access + session limits to forecast_experiment"; then
    psql_super <<SQL >/dev/null
GRANT CONNECT ON DATABASE forecasting TO forecast_experiment;
GRANT USAGE ON SCHEMA public TO forecast_experiment;
GRANT SELECT ON $tables TO forecast_experiment;
ALTER ROLE forecast_experiment SET default_transaction_read_only = on;
ALTER ROLE forecast_experiment SET statement_timeout = '30min';
ALTER ROLE forecast_experiment SET idle_in_transaction_session_timeout = '5min';
ALTER ROLE forecast_experiment SET lock_timeout = '10s';
ALTER ROLE forecast_experiment SET temp_file_limit = '20GB';
ALTER ROLE forecast_experiment SET work_mem = '512MB';
SQL
  fi

  if would "grant migrator and app privileges"; then
    psql_super <<'SQL' >/dev/null
GRANT CONNECT ON DATABASE forecasting TO forecast_migrator;
GRANT USAGE, CREATE ON SCHEMA public TO forecast_migrator;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO forecast_migrator;

GRANT CONNECT ON DATABASE forecasting TO forecast_app;
GRANT USAGE ON SCHEMA public TO forecast_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO forecast_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO forecast_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO forecast_app;
SQL
  fi

  [ "$CHECK" = 1 ] && { skip "read-only proof (dry run)"; return 0; }

  # --- prove forecast_experiment really is read-only ----------------------
  local exp_pw exp_url
  exp_pw="$(printf '%s' "$(require_secret forecast_experiment)" | urlencode)"
  exp_url="postgresql://forecast_experiment:${exp_pw}@127.0.0.1:5432/forecasting"

  compose exec -T postgres psql "$exp_url" -c 'SELECT 1;' >/dev/null 2>&1 \
    || die "forecast_experiment cannot even connect; grants are wrong."
  # WHERE false: even the refusal path must not risk data.
  if compose exec -T postgres psql "$exp_url" \
       -c 'DELETE FROM queue_forecast_tasks WHERE false;' >/dev/null 2>&1; then
    die "forecast_experiment was able to DELETE. The role is not read-only."
  fi
  if compose exec -T postgres psql "$exp_url" \
       -c 'CREATE TABLE agent_escape (x int);' >/dev/null 2>&1; then
    psql_super -c 'DROP TABLE IF EXISTS agent_escape;' >/dev/null
    die "forecast_experiment was able to CREATE TABLE. Schema grants are wrong."
  fi
  info "forecast_experiment: SELECT ok, DELETE refused, CREATE refused"

  step "Roles complete"
  warn "Services still connect as the postgres superuser. Migrating them to"
  warn "forecast_app is deliberately a separate command with its own rollback:"
  warn "    $0 db-app-cutover"
}

cmd_db_app_cutover() {
  step "Task 5 Step 6: move services off the superuser"
  local envf="$DEPLOY_DIR/.env"
  if grep -q '^DATABASE_URL=postgresql://forecast_app:' "$envf"; then
    skip "services already connect as forecast_app"; return 0
  fi
  local enc_pw
  enc_pw="$(printf '%s' "$(require_secret forecast_app)" | urlencode)"
  if would "rewrite DATABASE_URL to forecast_app and restart"; then
    cp "$envf" "$envf.pre-app"
    sed -i "s|^DATABASE_URL=.*|DATABASE_URL=postgresql://forecast_app:${enc_pw}@postgres:5432/forecasting|" "$envf"
    compose up -d >/dev/null
    sleep 25
    if compose logs --tail 60 2>&1 | grep -Eiq 'permission denied|authentication failed'; then
      cp "$envf.pre-app" "$envf"; compose up -d >/dev/null
      die "permission or auth errors after cutover; reverted to the previous identity."
    fi
    info "services running as forecast_app, no permission errors in recent logs"
    info "revert with: cp $envf.pre-app $envf && docker compose up -d"
  fi
}

# --------------------------------------------------------------------------
# task 6 - the research user
# --------------------------------------------------------------------------

cmd_research_user() {
  step "Task 6: research user and resource caps"
  local here; here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

  if id "$RESEARCH_USER" >/dev/null 2>&1; then
    skip "user $RESEARCH_USER already exists"
  else
    if would "create user $RESEARCH_USER (no password login)"; then
      sudo useradd --create-home --shell /bin/bash "$RESEARCH_USER"
      sudo passwd -l "$RESEARCH_USER" >/dev/null
    fi
  fi

  # This is the invariant that makes everything else meaningful: docker group
  # membership is root-equivalent.
  if getent group docker | cut -d: -f4 | tr ',' '\n' | grep -qx "$RESEARCH_USER"; then
    die "$RESEARCH_USER is in the docker group. That is root-equivalent and
     defeats the entire containment model. Remove it:
       sudo gpasswd -d $RESEARCH_USER docker"
  fi
  info "$RESEARCH_USER is not in the docker group"

  if would "install qf-research.slice"; then
    [ -f "$here/qf-research.slice" ] || die "missing $here/qf-research.slice"
    sudo cp "$here/qf-research.slice" /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl start qf-research.slice
    local uid; uid="$(id -u "$RESEARCH_USER")"
    sudo mkdir -p "/etc/systemd/system/user@${uid}.service.d"
    printf '[Service]\nSlice=qf-research.slice\n' \
      | sudo tee "/etc/systemd/system/user@${uid}.service.d/slice.conf" >/dev/null
    sudo systemctl daemon-reload
    systemctl show qf-research.slice -p MemoryMax -p CPUQuotaPerSecUSec -p TasksMax
  fi

  if would "tighten permissions on .env and $SECRETS_DIR"; then
    chmod 600 "$DEPLOY_DIR/.env"
    chmod 700 "$SECRETS_DIR"
  fi

  [ "$CHECK" = 1 ] && return 0
  sudo -u "$RESEARCH_USER" cat "$DEPLOY_DIR/.env" >/dev/null 2>&1 \
    && die "$RESEARCH_USER can read $DEPLOY_DIR/.env"
  sudo -u "$RESEARCH_USER" cat "$SECRETS_DIR/postgres.pw" >/dev/null 2>&1 \
    && die "$RESEARCH_USER can read $SECRETS_DIR"
  info "credentials unreadable by $RESEARCH_USER"
}

# --------------------------------------------------------------------------
# task 7 - egress restriction
# --------------------------------------------------------------------------

cmd_egress() {
  step "Task 7: egress allowlist"
  local here; here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

  if would "install tinyproxy and nftables"; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq tinyproxy nftables
  fi

  if would "write the domain allowlist"; then
    sudo tee /etc/tinyproxy/allowlist.txt >/dev/null <<'LIST'
^api\.anthropic\.com$
^api\.openai\.com$
^chatgpt\.com$
^github\.com$
^api\.github\.com$
^codeload\.github\.com$
^objects\.githubusercontent\.com$
LIST
    [ -f "$here/tinyproxy-allowlist.conf" ] || die "missing $here/tinyproxy-allowlist.conf"
    sudo cp "$here/tinyproxy-allowlist.conf" /etc/tinyproxy/tinyproxy.conf
    sudo systemctl restart tinyproxy
    sudo systemctl is-active tinyproxy
  fi

  if would "install nftables rules pinning $RESEARCH_USER to the proxy"; then
    local uid; uid="$(id -u "$RESEARCH_USER")"
    sudo tee /etc/nftables.conf >/dev/null <<NFT
#!/usr/sbin/nft -f
flush ruleset

table inet qf {
  chain output {
    type filter hook output priority 0; policy accept;

    # Only the research uid is constrained.
    meta skuid != ${uid} accept

    # Loopback reaches the proxy on 127.0.0.1:${PROXY_PORT}.
    oifname "lo" accept

    # DNS.
    udp dport 53 accept
    tcp dport 53 accept

    # Everything else leaving the box from this uid is denied.
    counter reject with icmp type admin-prohibited
  }
}
NFT
    sudo systemctl enable --now nftables
  fi

  if would "set proxy environment for $RESEARCH_USER"; then
    sudo tee "/home/$RESEARCH_USER/.profile.d-proxy" >/dev/null <<ENVV
export HTTPS_PROXY=http://127.0.0.1:${PROXY_PORT}
export HTTP_PROXY=http://127.0.0.1:${PROXY_PORT}
export NO_PROXY=127.0.0.1,localhost
ENVV
    sudo chown "$RESEARCH_USER:$RESEARCH_USER" "/home/$RESEARCH_USER/.profile.d-proxy"
    grep -q 'profile.d-proxy' "/home/$RESEARCH_USER/.bashrc" 2>/dev/null \
      || echo ". /home/$RESEARCH_USER/.profile.d-proxy" \
         | sudo tee -a "/home/$RESEARCH_USER/.bashrc" >/dev/null
  fi

  [ "$CHECK" = 1 ] && return 0

  local as_research="sudo -u $RESEARCH_USER -i bash -lc"
  $as_research 'curl -sS -o /dev/null --max-time 20 https://api.github.com' \
    || die "allowed host unreachable through the proxy. Egress is too tight to run agents."
  info "allowed host reachable"
  $as_research 'curl -sS -o /dev/null --max-time 20 https://pypi.org' 2>/dev/null \
    && die "denied host was reachable. The allowlist is not being enforced."
  info "denied host blocked"
  $as_research "curl -sS -o /dev/null --max-time 20 --noproxy '*' https://api.github.com" 2>/dev/null \
    && die "proxy bypass succeeded. nftables is not constraining the research uid."
  info "proxy bypass blocked"
}

# --------------------------------------------------------------------------
# task 8 - agent CLIs
# --------------------------------------------------------------------------

cmd_agent_cli() {
  step "Task 8: agent CLIs for $RESEARCH_USER"
  local as_research="sudo -u $RESEARCH_USER -i bash -lc"

  # psql is required by negative control NC1; without it the canary voids the
  # whole group and the read-only assertion proves nothing.
  if would "install postgresql-client"; then
    sudo apt-get install -y -qq postgresql-client
  fi

  if [ "$CHECK" = 1 ]; then skip "node + CLI install (dry run)"; return 0; fi

  if ! $as_research 'node --version' >/dev/null 2>&1; then
    info "installing node via nvm (temporarily allowing raw.githubusercontent.com)"
    sudo sed -i '1i ^raw\.githubusercontent\.com$' /etc/tinyproxy/allowlist.txt
    sudo systemctl restart tinyproxy
    $as_research 'curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash'
    $as_research 'nvm install 24'
    sudo sed -i '/^\^raw\\\.githubusercontent\\\.com\$$/d' /etc/tinyproxy/allowlist.txt
    sudo systemctl restart tinyproxy
    info "removed the temporary allowlist entry"
  fi
  $as_research 'node --version'

  $as_research 'command -v claude' >/dev/null 2>&1 \
    || $as_research 'npm install -g @anthropic-ai/claude-code'
  $as_research 'command -v codex' >/dev/null 2>&1 \
    || $as_research 'npm install -g @openai/codex'
  $as_research 'claude --version && codex --version'

  local keyfile="/home/$RESEARCH_USER/.config/qf/agent-env"
  if sudo test -f "$keyfile"; then
    skip "agent-env already present"
  else
    warn "API keys are the one thing this script will not write for you."
    warn "Create $keyfile as $RESEARCH_USER, mode 0600, containing:"
    warn "    export ANTHROPIC_API_KEY=..."
    warn "    export OPENAI_API_KEY=..."
    warn "Then re-run: $0 agent-cli"
    return 0
  fi

  info "checking both CLIs work non-interactively through the proxy..."
  local ok=1
  $as_research '. ~/.config/qf/agent-env; claude -p "reply with the single word: ready"' \
    || { warn "claude could not complete a request"; ok=0; }
  $as_research '. ~/.config/qf/agent-env; codex exec "reply with the single word: ready"' \
    || { warn "codex could not complete a request"; ok=0; }
  if [ "$ok" = 0 ]; then
    die "a CLI failed to reach its API. This is fail-closed, not a hole, but it
     blocks the loop. Most likely it does not honour HTTPS_PROXY. Applying an
     nftables IP-set exception is a decision, not an automation - see the plan,
     Task 8 Step 4, and record any exception in host/README.md."
  fi
  info "both CLIs reachable through the allowlist"
}

# --------------------------------------------------------------------------
# task 9 - negative controls
# --------------------------------------------------------------------------

cmd_verify() {
  step "Task 9: negative controls 1-6"
  local here; here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  [ -x "$here/nc-suite.sh" ] || die "missing or non-executable $here/nc-suite.sh"
  DEPLOY_DIR="$DEPLOY_DIR" SECRETS_DIR="$SECRETS_DIR" "$here/nc-suite.sh" \
    | tee "$here/nc-evidence-phase0.txt"
  info "evidence written to $here/nc-evidence-phase0.txt"
}

# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

usage() { sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "${1:-1}"; }

main() {
  local cmd="${1:-}"; shift || true
  # Help must work with no dependencies installed and no deploy dir present.
  case "$cmd" in -h|--help|'') usage 0 ;; esac
  for arg in "$@"; do
    case "$arg" in
      --check) CHECK=1 ;;
      -h|--help) usage 0 ;;
      *) die "unknown argument: $arg" ;;
    esac
  done
  command -v docker >/dev/null || die "docker not found"
  command -v python3 >/dev/null || die "python3 not found (needed to URL-encode passwords)"
  detect_deploy_dir

  case "$cmd" in
    discover)         cmd_discover ;;
    db-auth)          cmd_db_auth ;;
    rollback-db-auth) cmd_rollback_db_auth ;;
    db-roles)         cmd_db_roles ;;
    db-app-cutover)   cmd_db_app_cutover ;;
    research-user)    cmd_research_user ;;
    egress)           cmd_egress ;;
    agent-cli)        cmd_agent_cli ;;
    verify)           cmd_verify ;;
    all)
      cmd_discover; cmd_db_auth; cmd_db_roles
      cmd_research_user; cmd_egress; cmd_agent_cli; cmd_verify
      step "Phase 0 complete except db-app-cutover"
      info "Run '$0 db-app-cutover' separately once you have watched the"
      info "services run healthily as the superuser for a while."
      ;;
    -h|--help) usage 0 ;;
    *) die "unknown command: $cmd" ;;
  esac
}

main "$@"
