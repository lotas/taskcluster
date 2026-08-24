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
#   ./phase0-setup.sh auth-check              # agent logins still working
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

# Every application service in this compose file declares `profiles:`; only
# postgres does not. With no profile active, `compose config` renders postgres
# alone and `compose up -d` would restart postgres alone. Anything that needs
# to SEE all services must enable every declared profile.
COMPOSE_PROFILE_ARGS=()
load_profile_args() {
  local p
  COMPOSE_PROFILE_ARGS=()
  while IFS= read -r p; do
    [ -n "$p" ] && COMPOSE_PROFILE_ARGS+=(--profile "$p")
  done < <(compose config --profiles 2>/dev/null)
}
compose_all() {
  docker compose -f "$DEPLOY_DIR/docker-compose.yml" "${COMPOSE_PROFILE_ARGS[@]}" "$@"
}

# Compose service names of containers actually running for this project.
running_services() {
  docker ps --format '{{.Label "com.docker.compose.project.config_files"}}\t{{.Label "com.docker.compose.service"}}' \
    | awk -F'\t' -v cfg="$DEPLOY_DIR/docker-compose.yml" '$1==cfg && $2!="" {print $2}' \
    | sort -u
}

require_secret() {  # require_secret <role> -> generates once, reuses thereafter
  # Separate `local` statements on purpose: `local a=$1 b=$a` does NOT work in
  # bash. All names are made local (and unset) before any assignment runs, so
  # b would expand an unset a and trip `set -u`.
  local role="$1"
  local f="$SECRETS_DIR/$role.pw"
  if [ ! -f "$f" ]; then
    # --check advertises that it touches nothing, so it must not create
    # secrets either. Hand back a placeholder that is never applied.
    if [ "$CHECK" = 1 ]; then
      printf 'WOULD-GENERATE-%s' "$role"
      return 0
    fi
    ( umask 077; mkdir -p "$SECRETS_DIR"; openssl rand -base64 32 | tr -d '\n' > "$f" )
    chmod 700 "$SECRETS_DIR"; chmod 600 "$f"
  fi
  cat "$f"
}

urlencode() { python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.stdin.read(),safe=""))'; }

# Run a command as the research user, with node on PATH.
#
# TWO traps, both of which bit us:
#
# 1. NOT `sudo -i`. With -i, sudo joins its arguments into one string and hands
#    that to the target's login shell, which re-parses it - quoting and
#    newlines are destroyed. `sudo -H -u user bash -lc "$cmd"` passes argv
#    through untouched. Verified: -i turned `export NVM_DIR=...` into a bare
#    `export` that dumped the environment.
#
# 2. Do not rely on nvm's shell FUNCTION. It only exists after sourcing
#    nvm.sh, which non-interactive shells skip (Debian's ~/.bashrc returns
#    early). Resolve the installed node's bin directory and prepend it to PATH.
#    Keep this on ONE line - embedded newlines are a needless risk.
NVM_PRELUDE='[ -r "$HOME/.profile.d-proxy" ] && . "$HOME/.profile.d-proxy"; export NVM_DIR="$HOME/.nvm"; _nvmbin="$(ls -d "$NVM_DIR"/versions/node/*/bin 2>/dev/null | sort -V | tail -1)"; [ -n "$_nvmbin" ] && export PATH="$_nvmbin:$PATH"; true; '
run_research() {
  sudo -H -u "$RESEARCH_USER" bash -lc "${NVM_PRELUDE}$*"
}

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
  # nft lives in /usr/sbin, which is not on a normal user's PATH - check with
  # sudo or the plain binary path, otherwise this reports a false negative.
  info "nftables: $(sudo nft --version 2>/dev/null || echo 'NOT INSTALLED')"
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
    local is_scram
    is_scram="$(psql_super -tAc \
      "SELECT starts_with(rolpassword,'SCRAM-SHA-256') FROM pg_authid WHERE rolname='postgres';")"
    [ "$is_scram" = "t" ] || die "postgres password is not stored as SCRAM-SHA-256.
     Check: SELECT left(rolpassword,20) FROM pg_authid WHERE rolname='postgres';"
    info "stored as SCRAM-SHA-256"
  fi

  # --- verify the services could authenticate, BEFORE changing anything ----
  # Ordering matters: this used to run after the pg_hba rewrite, so a failure
  # here left Postgres demanding SCRAM while the services still had no
  # credential. Nothing below this point is reversible without a restart.
  if [ "$CHECK" = 0 ]; then
    compose_all config --quiet \
      || die "docker compose config does not render (most likely a missing
     variable in $DEPLOY_DIR/.env). pg_hba has NOT been touched."
  fi

  # --- backup pg_hba ------------------------------------------------------
  if would "back up pg_hba.conf to pg_hba.conf.pre-scram"; then
    compose exec -T postgres bash -c \
      '[ -f /var/lib/postgresql/data/pg_hba.conf.pre-scram ] ||
       cp /var/lib/postgresql/data/pg_hba.conf /var/lib/postgresql/data/pg_hba.conf.pre-scram'
  fi

  # --- already done? ------------------------------------------------------
  # Two independent sources, because a single silent query returning the wrong
  # answer previously made this step claim success without doing anything.
  local trust_rules trust_in_file
  trust_rules="$(psql_super -tAc \
    "SELECT count(*) FROM pg_hba_file_rules WHERE type='host' AND auth_method='trust';")"
  trust_rules="$(printf '%s' "$trust_rules" | tr -d '[:space:]')"
  # grep -c exits 1 when the count is zero, so `|| echo 0` used to append a
  # second value and produce "00". Force success and take the last line only.
  trust_in_file="$(compose exec -T postgres bash -c \
    "grep -cE '^[[:space:]]*host[[:space:]]+.*[[:space:]]trust[[:space:]]*\$' \
       /var/lib/postgresql/data/pg_hba.conf || true" 2>/dev/null | tail -1)"
  trust_in_file="$(printf '%s' "${trust_in_file:-0}" | tr -d '[:space:]')"

  case "$trust_rules" in
    ''|*[!0-9]*) die "the pg_hba_file_rules count returned '$trust_rules', not a
     number. Run this by hand and report the output:
       docker compose exec -T postgres psql -U postgres -d forecasting \\
         -c \"SELECT type,auth_method FROM pg_hba_file_rules;\"" ;;
  esac

  info "host-type trust rules: view=$trust_rules file=$trust_in_file"
  if [ "$trust_rules" != "$trust_in_file" ]; then
    die "the pg_hba_file_rules view ($trust_rules) and the file itself
     ($trust_in_file) disagree about how many host trust rules exist.
     Refusing to act on a state I cannot read consistently."
  fi

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
    # Positive control first. Without it, a psql that fails for ANY reason
    # (wrong port, missing client, mangled URI) reads as "refused" and this
    # check silently passes while proving nothing. That exact false positive
    # previously reported a cutover that had not happened.
    local probe_pw probe_out probe_rc
    probe_pw="$(printf '%s' "$pg_pw" | urlencode)"
    if ! compose exec -T postgres psql -w \
           "postgresql://postgres:${probe_pw}@127.0.0.1:5432/forecasting" \
           -c 'SELECT 1;' >/dev/null 2>&1; then
      die "the positive control failed: even an AUTHENTICATED connection to
     127.0.0.1:5432 did not work. psql, the port, or the URI is the problem -
     not authentication. Nothing was changed."
    fi
    info "positive control: authenticated connection works"

    set +e
    probe_out="$(compose exec -T postgres psql -w \
      "postgresql://postgres@127.0.0.1:5432/forecasting" -c 'SELECT 1;' 2>&1)"
    probe_rc=$?
    set -e
    if [ "$probe_rc" = 0 ]; then
      die "an unauthenticated network connection still SUCCEEDED.
     pg_hba did not take effect. Run: $0 rollback-db-auth"
    fi
    case "$probe_out" in
      *[Pp]assword*|*authentication*) info "unauthenticated connection refused (as intended)" ;;
      *) die "the unauthenticated probe failed, but not for an authentication
     reason. Refusing to treat this as proof. psql said:
       $probe_out" ;;
    esac
  fi

  # --- .env cutover -------------------------------------------------------
  local envf="$DEPLOY_DIR/.env"
  [ -f "$envf" ] || die "no .env at $envf"
  # Must match user:password@, NOT the scheme colon. The naive pattern
  # '^DATABASE_URL=.*:.*@' matches postgresql://postgres@postgres:5432/...
  # (the colon from "postgresql:") and would wrongly skip the rewrite,
  # leaving the services with no password after the pg_hba cutover.
  if grep -Eq '^DATABASE_URL=postgresql://[^:@/]+:[^@/]+@' "$envf"; then
    skip ".env DATABASE_URL already carries a password"
  else
    if would "rewrite DATABASE_URL in .env to authenticate as postgres"; then
      grep -q '^DATABASE_URL=' "$envf" \
        || die "no DATABASE_URL line in $envf; refusing to guess. Add it by hand."
      cp "$envf" "$envf.pre-scram"
      local enc_pw
      enc_pw="$(printf '%s' "$pg_pw" | urlencode)"
      sed -i "s|^DATABASE_URL=.*|DATABASE_URL=postgresql://postgres:${enc_pw}@postgres:5432/forecasting|" "$envf"
      chmod 600 "$envf"
      grep -Eq '^DATABASE_URL=postgresql://[^:@/]+:[^@/]+@' "$envf" \
        || die "the DATABASE_URL rewrite did not take. Restore: cp $envf.pre-scram $envf"
      info "backup at $envf.pre-scram"
    fi
  fi

  # --- restart and health-gate, with rollback -----------------------------
  if [ "$CHECK" = 1 ]; then skip "restart + health gate (dry run)"; return 0; fi

  # Editing .env proves nothing. Compose gives an explicit `environment:` value
  # precedence over `env_file:`, so a hardcoded URL silently wins. Validate the
  # rendered config BEFORE restarting, then the container's effective env after.
  compose config --quiet \
    || die "docker compose config failed to render. Most likely DATABASE_URL or
     POSTGRES_PASSWORD is missing from $DEPLOY_DIR/.env. Nothing was restarted."

  # Parse the rendered config as data, not by grepping YAML whose exact shape
  # varies by compose version. Crucially: distinguish "found a URL with no
  # credential" (a real problem) from "could not find any URL" (my parser is
  # wrong). Reporting the second as the first is how this check lied before.
  local verdict
  verdict="$(compose_all config --format json 2>/dev/null | python3 -c '
import json, sys
try:
    doc = json.load(sys.stdin)
except Exception as exc:
    print("UNPARSEABLE " + str(exc)); raise SystemExit(0)
bad, seen = [], 0
for name, svc in (doc.get("services") or {}).items():
    env = svc.get("environment") or {}
    if isinstance(env, list):
        env = dict(e.split("=", 1) for e in env if "=" in e)
    url = env.get("DATABASE_URL")
    if url is None:
        continue
    seen += 1
    host = url.split("://", 1)[-1].split("/", 1)[0]
    if "@" not in host or ":" not in host.split("@", 1)[0]:
        bad.append(name)
if seen == 0:
    print("NONE")
elif bad:
    print("MISSING " + ",".join(sorted(bad)))
else:
    print("OK %d" % seen)
' 2>/dev/null || echo "UNPARSEABLE")"

  case "$verdict" in
    OK\ *)
      info "rendered config: ${verdict#OK } service(s) carry a credential" ;;
    MISSING\ *)
      die "these services would start with a password-less DATABASE_URL:
       ${verdict#MISSING }
     An explicit environment: value overrides env_file:, so editing .env is
     not enough. Nothing was restarted." ;;
    NONE|UNPARSEABLE*)
      die "could not determine DATABASE_URL from the rendered compose config
     ($verdict). This is a defect in THIS CHECK, not necessarily in your
     configuration - so nothing was restarted rather than guessing.
     Show me:  docker compose config | grep -n -A2 -i database_url" ;;
  esac

  # Which services are actually up? Never start ones the operator chose not to
  # run: `--profile full` would silently bring up the whole stack.
  local svcs stale svc eff
  svcs="$(running_services)"
  [ -n "$svcs" ] || die "no running containers found for $DEPLOY_DIR/docker-compose.yml"
  info "running services: $(echo "$svcs" | tr '\n' ' ')"

  # A container's environment is fixed at creation, so a changed .env only
  # takes effect on RECREATE - `restart` is not enough.
  stale=""
  for svc in $svcs; do
    eff="$(compose_all exec -T "$svc" printenv DATABASE_URL 2>/dev/null || echo MISSING)"
    case "$eff" in
      *:*@*) : ;;
      *) stale="$stale $svc" ;;
    esac
  done

  local before after
  before="$(psql_super -tAc 'SELECT count(*) FROM queue_forecast_tasks;')"

  if [ -z "$stale" ]; then
    skip "every running service already has a credential-bearing DATABASE_URL"
  else
    info "recreating:$stale"
    # shellcheck disable=SC2086
    compose_all up -d --force-recreate $stale >/dev/null
    sleep 25
    for svc in $stale; do
      eff="$(compose_all exec -T "$svc" printenv DATABASE_URL 2>/dev/null || echo MISSING)"
      case "$eff" in
        *:*@*) info "$svc: effective DATABASE_URL carries a credential" ;;
        *) db_auth_rollback
           die "$svc still has no credential in DATABASE_URL after recreate. Rolled back." ;;
      esac
    done
  fi
  after="$before"

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
    compose_all logs --tail 30 collector || true
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
  # Use `docker exec` on the container id, NOT `docker compose exec`. Compose
  # refuses to run at all if interpolation fails (a missing required variable),
  # which would make the escape hatch depend on the thing being escaped from.
  local cid
  cid="$(docker ps -qf name=postgres | head -1)"
  if [ -z "$cid" ]; then
    warn "no running postgres container found; cannot restore pg_hba"
  else
    docker exec -i "$cid" bash -c \
      'cp /var/lib/postgresql/data/pg_hba.conf.pre-scram /var/lib/postgresql/data/pg_hba.conf' \
      || warn "pg_hba restore failed"
    # A copy alone changes nothing until the server re-reads it.
    docker exec -i "$cid" psql -U postgres -d forecasting -c 'SELECT pg_reload_conf();' >/dev/null \
      || warn "pg_reload_conf failed - the restored file is NOT active yet"
    local remaining
    remaining="$(docker exec -i "$cid" psql -U postgres -d forecasting -tAc \
      "SELECT count(*) FROM pg_hba_file_rules WHERE type='host' AND auth_method='trust';" \
      2>/dev/null | tr -d '[:space:]')"
    info "host trust rules after rollback: ${remaining:-unknown}"
  fi
  [ -f "$DEPLOY_DIR/.env.pre-scram" ] && cp "$DEPLOY_DIR/.env.pre-scram" "$DEPLOY_DIR/.env"
  compose up -d >/dev/null 2>&1 || warn "compose up failed; containers left as they are"
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

  compose exec -T postgres psql -w "$exp_url" -c 'SELECT 1;' >/dev/null 2>&1 \
    || die "forecast_experiment cannot even connect; grants are wrong."
  # WHERE false: even the refusal path must not risk data.
  if compose exec -T postgres psql -w "$exp_url" \
       -c 'DELETE FROM queue_forecast_tasks WHERE false;' >/dev/null 2>&1; then
    die "forecast_experiment was able to DELETE. The role is not read-only."
  fi
  if compose exec -T postgres psql -w "$exp_url" \
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

  if would "install tinyproxy and the nft tool"; then
    sudo apt-get update -qq
    # Installs the nft binary. The distro nftables.service is deliberately NOT
    # enabled - its stock config flushes the whole ruleset, Docker included.
    sudo apt-get install -y -qq tinyproxy nftables

    # Debian ships /etc/nftables.conf starting with `flush ruleset`. If the
    # package left its service enabled, that runs at every boot and takes
    # Docker's chains with it. On this host Docker uses the iptables-nft
    # backend, so those chains are in the same ruleset.
    if systemctl is-enabled nftables >/dev/null 2>&1; then
      warn "nftables.service is ENABLED and its config flushes the whole ruleset."
      warn "That would break Docker networking on the next boot."
      warn "Disabling it; our rules load from qf-nftables.service instead."
      sudo systemctl disable nftables >/dev/null 2>&1 || true
    fi
    info "nftables.service enabled: $(systemctl is-enabled nftables 2>&1 || true)"
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

  # Docker's NAT/filter rules go through iptables, which on current systems is
  # the iptables-nft backend - so they live in the nftables ruleset. A
  # `flush ruleset` (as a stock /etc/nftables.conf does) would wipe Docker's
  # chains and break container networking and the published postgres port.
  # Manage ONLY our own table, and never write /etc/nftables.conf.
  local docker_nft
  docker_nft="$(sudo nft list ruleset 2>/dev/null | grep -ciE 'DOCKER|br-[0-9a-f]{12}' || true)"
  info "existing nftables lines referencing docker: ${docker_nft:-0}"
  if [ -f /etc/nftables.conf ] && grep -q 'flush ruleset' /etc/nftables.conf 2>/dev/null; then
    warn "/etc/nftables.conf contains 'flush ruleset'."
    warn "This script does not modify that file, but if nftables.service is"
    warn "enabled it will wipe Docker's rules on the next boot. Worth fixing"
    warn "separately: systemctl is-enabled nftables"
  fi

  if would "install a dedicated 'inet qf' nftables table (Docker rules untouched)"; then
    local uid; uid="$(id -u "$RESEARCH_USER")"
    sudo mkdir -p /etc/nftables.d
    sudo tee /etc/nftables.d/qf-research.nft >/dev/null <<NFT
#!/usr/sbin/nft -f
# Egress restriction for the auto-research agent user.
#
# Scoped to a single table so it can be replaced atomically without touching
# Docker's chains. The create-then-delete-then-define idiom is the supported
# way to replace one table: the empty 'table' line makes the delete safe on a
# first run. There is deliberately NO 'flush ruleset' here.
table inet qf {}
delete table inet qf

table inet qf {
  chain output {
    type filter hook output priority 0; policy accept;

    # Only the research uid is constrained; everything else is untouched.
    meta skuid != ${uid} accept

    # Loopback reaches the filtering proxy on 127.0.0.1:${PROXY_PORT}.
    oifname "lo" accept

    # DNS.
    udp dport 53 accept
    tcp dport 53 accept

    # Everything else leaving the box from this uid is denied.
    #
    # icmpx, NOT icmp. In an inet table, 'reject with icmp' is IPv4-only, and
    # nft silently narrows the rule with 'meta nfproto ipv4' - leaving IPv6
    # egress to fall through to the accept policy. icmpx covers both families.
    counter reject with icmpx type admin-prohibited
  }
}
NFT
    sudo nft -f /etc/nftables.d/qf-research.nft \
      || die "nft failed to load /etc/nftables.d/qf-research.nft; nothing else was changed."

    # Persist across reboot with our own unit, rather than the distro
    # nftables.service whose default config flushes everything.
    sudo tee /etc/systemd/system/qf-nftables.service >/dev/null <<'UNIT'
[Unit]
Description=Load the inet qf egress table for the auto-research agents
After=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/nftables.d/qf-research.nft

[Install]
WantedBy=multi-user.target
UNIT
    sudo systemctl daemon-reload
    sudo systemctl enable --now qf-nftables.service
    sudo nft list table inet qf
    info "to undo just this table:  sudo nft delete table inet qf"

    local docker_after
    docker_after="$(sudo nft list ruleset 2>/dev/null | grep -ciE 'DOCKER|br-[0-9a-f]{12}' || true)"
    info "docker-related nftables lines after: ${docker_after:-0} (was ${docker_nft:-0})"
    if [ "${docker_nft:-0}" -gt 0 ] && [ "${docker_after:-0}" -eq 0 ]; then
      die "Docker's nftables rules disappeared. Restore with: sudo systemctl restart docker"
    fi
  fi

  if would "set proxy environment for $RESEARCH_USER"; then
    # Sourced from ~/.profile, NOT ~/.bashrc. Debian's .bashrc returns early
    # for non-interactive shells, so `bash -lc` (and therefore run_research and
    # anything scripted) would never see these. Verified: bash -lc reads
    # .profile and skips .bashrc.
    #
    # Lower- AND upper-case: libcurl (curl, git) prefers the lower-case names;
    # most Node HTTP stacks read the upper-case ones.
    sudo tee "/home/$RESEARCH_USER/.profile.d-proxy" >/dev/null <<ENVV
export HTTPS_PROXY=http://127.0.0.1:${PROXY_PORT}
export HTTP_PROXY=http://127.0.0.1:${PROXY_PORT}
export https_proxy=http://127.0.0.1:${PROXY_PORT}
export http_proxy=http://127.0.0.1:${PROXY_PORT}
export NO_PROXY=127.0.0.1,localhost
export no_proxy=127.0.0.1,localhost
ENVV
    sudo chown "$RESEARCH_USER:$RESEARCH_USER" "/home/$RESEARCH_USER/.profile.d-proxy"
    sudo touch "/home/$RESEARCH_USER/.profile"
    grep -q 'profile.d-proxy' "/home/$RESEARCH_USER/.profile" 2>/dev/null \
      || echo ". /home/$RESEARCH_USER/.profile.d-proxy" \
         | sudo tee -a "/home/$RESEARCH_USER/.profile" >/dev/null
    sudo chown "$RESEARCH_USER:$RESEARCH_USER" "/home/$RESEARCH_USER/.profile"
    # cron gives an even barer environment than `bash -lc` - it reads neither
    # .profile nor .bashrc. The Phase 4 tick must source this file explicitly.
    info "proxy env in ~/.profile; cron must source .profile.d-proxy itself"
  fi

  [ "$CHECK" = 1 ] && return 0

  run_research 'curl -sS -o /dev/null --max-time 20 https://api.github.com' \
    || die "allowed host unreachable through the proxy. Egress is too tight to run agents."
  info "allowed host reachable"
  run_research 'curl -sS -o /dev/null --max-time 20 https://pypi.org' 2>/dev/null \
    && die "denied host was reachable. The allowlist is not being enforced."
  info "denied host blocked"
  run_research "curl -sS -o /dev/null --max-time 20 --noproxy '*' https://api.github.com" 2>/dev/null \
    && die "proxy bypass succeeded. nftables is not constraining the research uid."
  info "proxy bypass blocked (ipv4)"

  # Canary: only meaningful if this host has a global IPv6 address at all.
  if ip -6 addr show scope global 2>/dev/null | grep -q inet6; then
    run_research "curl -6 -sS -o /dev/null --max-time 20 --noproxy '*' https://api.github.com" 2>/dev/null \
      && die "IPv6 proxy bypass succeeded. The reject rule is IPv4-only - check
     that the table uses 'reject with icmpx', not 'reject with icmp'."
    info "proxy bypass blocked (ipv6)"
  else
    skip "no global IPv6 address on this host; IPv6 bypass check not applicable"
  fi
}

# --------------------------------------------------------------------------
# task 8 - agent CLIs
# --------------------------------------------------------------------------

cmd_agent_cli() {
  step "Task 8: agent CLIs for $RESEARCH_USER"

  # psql is required by negative control NC1; without it the canary voids the
  # whole group and the read-only assertion proves nothing.
  if would "install postgresql-client and bubblewrap"; then
    sudo apt-get install -y -qq postgresql-client bubblewrap
  fi

  if [ "$CHECK" = 1 ]; then skip "node + CLI install (dry run)"; return 0; fi

  # The allowlist only exists once `egress` has run. Installing agent-cli first
  # (the recommended order) means there is no restriction yet and nothing to
  # punch through - so only do the temporary-allow dance if the file is there.
  local ALLOWLIST=/etc/tinyproxy/allowlist.txt
  local TEMP_HOST='^raw\.githubusercontent\.com$'

  temp_allow() {
    [ -f "$ALLOWLIST" ] || { info "no egress allowlist yet; installing directly"; return 0; }
    grep -qxF "$TEMP_HOST" "$ALLOWLIST" && return 0
    info "temporarily allowing raw.githubusercontent.com"
    printf '%s\n' "$TEMP_HOST" | sudo tee -a "$ALLOWLIST" >/dev/null
    sudo systemctl restart tinyproxy
  }
  temp_revoke() {
    [ -f "$ALLOWLIST" ] || return 0
    grep -vxF "$TEMP_HOST" "$ALLOWLIST" | sudo tee "$ALLOWLIST".new >/dev/null
    sudo mv "$ALLOWLIST".new "$ALLOWLIST"
    sudo systemctl restart tinyproxy
    info "removed the temporary allowlist entry"
  }

  if run_research 'node --version' >/dev/null 2>&1; then
    info "node found: $(run_research 'node --version' 2>/dev/null)"
  else
    info "no node on PATH for $RESEARCH_USER; installing via nvm"
    temp_allow
    # Revoke even if this fails, so a failure cannot leave egress wider than
    # intended. nvm's installer defines `nvm` only in the shell that sources
    # nvm.sh, so run the source and the install in ONE shell invocation.
    if ! run_research 'curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash' \
       || ! run_research 'export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; nvm install 24'; then
      temp_revoke
      die "node installation failed. Run this by hand and report the output:
       sudo -H -u $RESEARCH_USER bash -lc 'export NVM_DIR=\"\$HOME/.nvm\"; . \"\$NVM_DIR/nvm.sh\"; nvm install 24'
     Alternatively install node system-wide (apt/NodeSource) - for a cron-driven
     loop a /usr/bin/node is more robust than a per-user version manager."
    fi
    temp_revoke
    run_research 'node --version' >/dev/null 2>&1 \
      || die "node still not on PATH after install. Check:
       sudo -H -u $RESEARCH_USER bash -lc 'ls -d \$HOME/.nvm/versions/node/*/bin'"
  fi
  run_research 'node --version'

  run_research 'command -v claude' >/dev/null 2>&1 \
    || run_research 'npm install -g @anthropic-ai/claude-code'
  run_research 'command -v codex' >/dev/null 2>&1 \
    || run_research 'npm install -g @openai/codex'
  run_research 'claude --version && codex --version'

  # Auth is interactive SSO login, not API keys - so there is no key file to
  # write. The probe below IS the check: if a CLI can complete a request, it is
  # authenticated; if it cannot, it needs a one-time login.
  info "probing both CLIs..."
  local claude_ok=1 codex_ok=1
  run_research 'claude -p "reply with the single word: ready"' >/dev/null 2>&1 || claude_ok=0
  run_research 'codex exec --skip-git-repo-check "reply with the single word: ready"'  >/dev/null 2>&1 || codex_ok=0

  if [ "$claude_ok" = 1 ] && [ "$codex_ok" = 1 ]; then
    info "both CLIs authenticated and reachable"
    return 0
  fi

  warn "one or both CLIs are not authenticated yet:"
  [ "$claude_ok" = 1 ] && info "  claude: ok" || warn "  claude: NOT authenticated"
  [ "$codex_ok" = 1 ]  && info "  codex:  ok" || warn "  codex:  NOT authenticated"
  cat <<INSTRUCTIONS

   Log in interactively as the research user, ONE TIME, before running
   '$0 egress'. The OAuth flow reaches your SSO provider and the vendors' auth
   domains, none of which the egress allowlist permits - so it must happen
   first.

       sudo -u $RESEARCH_USER -i
       claude            # then: /login   (paste the URL into a browser,
                         #                 paste the code back)
       codex login
       exit

   Then re-run:  $0 agent-cli

INSTRUCTIONS
  # Not an error: the operator has work to do, and the script has done its part.
  return 0
}

# Standing check that authentication still works. Interactive SSO tokens are
# refreshed against an auth endpoint; if the egress allowlist does not permit
# it, the agents keep working for days and then stop silently. Run this after
# egress, and periodically thereafter.
cmd_auth_check() {
  step "Agent authentication health"
  local failed=0
  local out
  for cli in claude codex; do
    if [ "$cli" = claude ]; then
      out="$(run_research 'claude -p "reply with the single word: ready"' 2>&1)" || out="FAILED: $out"
    else
      out="$(run_research 'codex exec --skip-git-repo-check "reply with the single word: ready"' 2>&1)" || out="FAILED: $out"
    fi
    case "$out" in
      FAILED:*) warn "$cli: $out"; failed=1 ;;
      *) info "$cli: ok" ;;
    esac
  done
  [ "$failed" = 0 ] || die "an agent CLI cannot reach its API. If this began after
     '$0 egress', a token-refresh or auth domain is missing from
     /etc/tinyproxy/allowlist.txt. Add it, restart tinyproxy, record the
     addition in host/README.md, and re-run."
  info "both CLIs authenticated"
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
  load_profile_args

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
    auth-check)       cmd_auth_check ;;
    all)
      cmd_discover; cmd_db_auth; cmd_db_roles
      cmd_research_user; cmd_agent_cli; cmd_egress; cmd_auth_check; cmd_verify
      step "Phase 0 complete except db-app-cutover"
      info "Run '$0 db-app-cutover' separately once you have watched the"
      info "services run healthily as the superuser for a while."
      ;;
    -h|--help) usage 0 ;;
    *) die "unknown command: $cmd" ;;
  esac
}

main "$@"
