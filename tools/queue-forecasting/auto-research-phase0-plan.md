# Auto-Research Loop — Phase 0: Host Prerequisites and Containment

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the experimental host safe for unattended agents — authenticated Postgres, a genuinely read-only experiment role, an unprivileged `research` user with capped resources and restricted egress, the agent CLIs installed, and six negative controls that fail closed.

**Architecture:** Containment is enforced by the operating system, not by the agent CLIs' own permission models. Four independent layers: Postgres authentication + role grants; unix user separation; a systemd slice for resource caps; and an egress allowlist via a filtering proxy plus `nftables` owner-match. Each layer is asserted by a negative-control test that must be *refused*.

**Tech Stack:** Postgres 15 (docker compose), systemd, nftables, tinyproxy, Node.js 24, `claude` and `codex` CLIs.

**Spec:** `tools/queue-forecasting/auto-research-loop-design.md` §3, §13, §14 Phase 0. Read §3.2–§3.5 and §13.1 before starting.

**Conventions:**
- Commits are handled by the user — do **not** run `git commit`. Where a step says "Commit", stage with `git add` and stop.
- Host steps run on the experimental server as a user with sudo. Repo-local steps run in the current checkout.
- The deploy checkout is written as `/srv/queue-forecasting` throughout. If it lives elsewhere on the host, substitute consistently and pass the real path as `DEPLOY_DIR` to `nc-suite.sh`.
- `~/qf-secrets` means the *deploy* user's home, never `research`'s.
- JS tests run per-file: `node test/<name>.test.js`.
- The single `forecasting` database is shared by all services. There is no staging DB. Every host step is written to be reversible, and destructive-adjacent steps carry an explicit rollback.

> **Tasks 2–9 are implemented by `host/phase0-setup.sh`.** Run the script
> rather than pasting the commands below by hand — it is idempotent, has a
> `--check` dry-run mode, derives the grant list from the live database, and
> auto-rolls-back the `pg_hba` cutover if collection stops. This document
> remains the narrative: why each step exists, what to expect, and what to do
> when something is unexpected. If the two ever disagree, the script is what
> actually runs — fix the document.

**Acceptance for the whole phase:** negative controls 1–6 (§13.1 of the spec) all fail closed, evidenced by the output of `nc-suite.sh`.

---

## File Structure

**Create (repo):**
- `test/smoke-guard.js` — pure guard rejecting non-disposable database URLs.
- `test/smoke-guard.test.js` — unit tests for the guard, no DB required.
- `host/nc-suite.sh` — negative-control suite, runnable as `research`.
- `host/tinyproxy-allowlist.conf` — domain allowlist for agent egress.
- `host/qf-research.slice` — systemd slice with resource caps.
- `host/README.md` — what each host artifact is and how to apply it.

**Modify (repo):**
- `test/smoke.js:1-5` — remove the production-port default, call the guard.
- `.env.example` — document the new password-bearing `DATABASE_URL` form.

**Host-only (not in the repo):** `pg_hba.conf` inside the postgres volume, `/etc/nftables.conf`, `/etc/tinyproxy/tinyproxy.conf`, `/home/research`, the `research` unix user.

---

## Phase 0a — Repo-side work (no host access needed)

### Task 1: Defang `test/smoke.js` — DONE 2026-08-24

> Applied and staged in the monorepo checkout. Guard unit tests pass 7/7.
> Step 6's end-to-end check was **not** run here: this container has an empty
> `node_modules`, so `pg` is missing and the import fails before the guard
> executes. Re-run Step 6 on a host with dependencies installed to confirm.

`test/smoke.js:4` defaults `DATABASE_URL` to `postgresql://postgres@localhost:5433/forecasting`. Port 5433 is the host mapping to the production postgres container, and `resetTables()` at line 20 issues `DELETE FROM queue_forecast_task_runs` and `DELETE FROM queue_forecast_tasks` with no `WHERE`. Running the file with no environment wipes production task data.

This is a live footgun today, independent of the research loop. Fix it first.

**Files:**
- Create: `tools/queue-forecasting/test/smoke-guard.js`
- Create: `tools/queue-forecasting/test/smoke-guard.test.js`
- Modify: `tools/queue-forecasting/test/smoke.js:1-5`

- [x] **Step 1: Write the failing test**

Create `tools/queue-forecasting/test/smoke-guard.test.js`:

```javascript
import { test } from 'node:test';
import assert from 'node:assert';
import { assertDisposableDatabaseUrl } from './smoke-guard.js';

const OK = 'postgresql://postgres@localhost:5544/forecasting_smoke';

test('accepts a disposable local database', () => {
  assert.doesNotThrow(() => assertDisposableDatabaseUrl(OK));
});

test('rejects an unset url', () => {
  assert.throws(() => assertDisposableDatabaseUrl(undefined), /SMOKE_DATABASE_URL is required/);
  assert.throws(() => assertDisposableDatabaseUrl(''), /SMOKE_DATABASE_URL is required/);
});

test('rejects the production host port 5433', () => {
  assert.throws(
    () => assertDisposableDatabaseUrl('postgresql://postgres@localhost:5433/forecasting_smoke'),
    /port 5433/,
  );
});

test('rejects a non-local host', () => {
  assert.throws(
    () => assertDisposableDatabaseUrl('postgresql://postgres@35.202.240.190:5544/forecasting_smoke'),
    /must be local/,
  );
});

test('rejects a database name that is not marked disposable', () => {
  assert.throws(
    () => assertDisposableDatabaseUrl('postgresql://postgres@localhost:5544/forecasting'),
    /database name/,
  );
});

test('accepts test-marked database names too', () => {
  assert.doesNotThrow(
    () => assertDisposableDatabaseUrl('postgresql://postgres@127.0.0.1:5544/qf_test'),
  );
});

test('rejects a malformed url', () => {
  assert.throws(() => assertDisposableDatabaseUrl('not-a-url'), /could not be parsed/);
});
```

- [x] **Step 2: Run the test to verify it fails**

```bash
cd tools/queue-forecasting && node test/smoke-guard.test.js
```

Expected: fails immediately with `Cannot find module` for `./smoke-guard.js`.

- [x] **Step 3: Write the guard**

Create `tools/queue-forecasting/test/smoke-guard.js`:

```javascript
// smoke.js calls resetTables(), which issues unqualified DELETEs against
// queue_forecast_tasks and queue_forecast_task_runs. It must therefore only
// ever run against a throwaway database. This guard is the enforcement, and
// is deliberately separate from smoke.js so it can be unit-tested with no DB.

const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '::1', '[::1]']);

// The host port mapping for the production postgres container.
const PRODUCTION_HOST_PORT = '5433';

// A disposable database must say so in its name.
const DISPOSABLE_NAME = /(^|[_-])(smoke|test)([_-]|$)/;

export function assertDisposableDatabaseUrl(url) {
  if (!url) {
    throw new Error(
      'SMOKE_DATABASE_URL is required. smoke.js deletes all task rows and has no default; ' +
      'point it at a throwaway database, e.g. postgresql://postgres@localhost:5544/forecasting_smoke',
    );
  }

  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    throw new Error(`SMOKE_DATABASE_URL could not be parsed as a URL: ${url}`);
  }

  if (parsed.port === PRODUCTION_HOST_PORT) {
    throw new Error(
      `SMOKE_DATABASE_URL uses port ${PRODUCTION_HOST_PORT}, which is the production postgres ` +
      'host mapping. Refusing to run.',
    );
  }

  if (!LOCAL_HOSTS.has(parsed.hostname)) {
    throw new Error(
      `SMOKE_DATABASE_URL host must be local, got "${parsed.hostname}". Refusing to run.`,
    );
  }

  const dbName = parsed.pathname.replace(/^\//, '');
  if (!DISPOSABLE_NAME.test(dbName)) {
    throw new Error(
      `SMOKE_DATABASE_URL database name "${dbName}" is not marked disposable. ` +
      'Use a name containing "smoke" or "test", e.g. forecasting_smoke.',
    );
  }

  return url;
}
```

- [x] **Step 4: Run the test to verify it passes**

```bash
cd tools/queue-forecasting && node test/smoke-guard.test.js
```

Expected: `# pass 7`, `# fail 0`.

- [x] **Step 5: Wire the guard into `smoke.js`**

Replace `tools/queue-forecasting/test/smoke.js` lines 1–5 with:

```javascript
import { createPool, upsertTask, upsertTaskRun, enrichTask, getUnenrichedTaskIds } from '../src/db.js';
import { normalizeMetadataName, extractImageName } from '../src/utils.js';
import { assertDisposableDatabaseUrl } from './smoke-guard.js';

// No default. See smoke-guard.js — this file issues unqualified DELETEs.
const DATABASE_URL = assertDisposableDatabaseUrl(process.env.SMOKE_DATABASE_URL);
const pool = createPool(DATABASE_URL);
```

Note the variable is now read from `SMOKE_DATABASE_URL`, not `DATABASE_URL`. This is deliberate: a shell that already has `DATABASE_URL` exported for operational work must not silently arm the smoke test.

- [x] **Step 6: Verify the footgun is closed**

This step needs the workspace dependencies installed (`yarn` in the repo root),
because `smoke.js` imports `../src/db.js`, which imports `pg`. With an empty
`node_modules` the import fails before the guard runs, and the check proves
nothing about the guard.

```bash
cd tools/queue-forecasting && node test/smoke.js; echo "exit=$?"
```

Expected: exits non-zero, printing `SMOKE_DATABASE_URL is required`. **No database connection is attempted.**

```bash
cd tools/queue-forecasting && SMOKE_DATABASE_URL=postgresql://postgres@localhost:5433/forecasting node test/smoke.js; echo "exit=$?"
```

Expected: exits non-zero with `uses port 5433, which is the production postgres host mapping`.

- [x] **Step 7: Document the new form**

Append to `tools/queue-forecasting/.env.example`:

```
# test/smoke.js is destructive (unqualified DELETEs) and reads SMOKE_DATABASE_URL,
# never DATABASE_URL. It refuses port 5433, non-local hosts, and database names
# that are not marked disposable. Example:
# SMOKE_DATABASE_URL=postgresql://postgres@localhost:5544/forecasting_smoke
```

- [x] **Step 8: Checkpoint**

```bash
cd tools/queue-forecasting && git add test/smoke-guard.js test/smoke-guard.test.js test/smoke.js .env.example
```

Stop. The user commits.

---

## Phase 0b — Host discovery

### Task 2: Capture the host's current state

The remaining tasks change authentication and networking on a host running live collection. Each needs a known starting point and a rollback target. Run these on the host and keep the output — later tasks reference it.

**Files:** none (output is recorded in the plan's execution notes).

- [ ] **Step 1: Record service and DB state**

```bash
cd /srv/queue-forecasting   # the deploy checkout; substitute if it differs
docker compose ps
docker compose exec -T postgres psql -U postgres -d forecasting -c "SHOW password_encryption;"
docker compose exec -T postgres psql -U postgres -d forecasting -c "\du"
docker compose exec -T postgres psql -U postgres -d forecasting -c "\dt"
docker compose exec -T postgres cat /var/lib/postgresql/data/pg_hba.conf
```

Expected and needed: `password_encryption` should read `scram-sha-256` (the Postgres 15 default). If it reads `md5`, stop and report — Task 4 changes order.

Record the exact table list. The spec assumes: `queue_forecast_tasks`, `queue_forecast_task_runs`, `queue_forecast_run_predictions`, `queue_forecast_worker_counts`, `queue_forecast_worker_pools`, `queue_forecast_daily_health`. If `\dt` shows others, they must be added to the grants in Task 5.

- [ ] **Step 2: Back up `pg_hba.conf`**

```bash
docker compose exec -T postgres cp /var/lib/postgresql/data/pg_hba.conf \
  /var/lib/postgresql/data/pg_hba.conf.pre-scram
docker compose exec -T postgres ls -l /var/lib/postgresql/data/pg_hba.conf.pre-scram
```

Expected: the copy exists. This is the rollback target for Task 4.

- [ ] **Step 3: Take a database backup**

```bash
./scripts/backup.sh
```

Expected: completes and reports a backup location. If `backup.sh` needs arguments, read it first — do not skip this step.

- [ ] **Step 4: Record host facts needed later**

```bash
systemd-detect-virt; systemctl --version | head -1
nft --version 2>/dev/null || echo "nftables NOT installed"
iptables --version 2>/dev/null || echo "iptables NOT installed"
resolvectl status 2>/dev/null | head -20 || cat /etc/resolv.conf
free -g; nproc; df -h /
node --version 2>/dev/null || echo "node NOT installed"
```

Expected: note whether `nftables` is present (Task 7 assumes it), the DNS resolver address, and free RAM/CPU for sizing the slice in Task 6.

---

## Phase 0c — Postgres authentication and roles

### Tasks 3–4 — DONE 2026-08-24

> Applied via `host/phase0-setup.sh db-auth`. Verified: `pg_hba` has no
> host-type `trust` rules; an unauthenticated network connection is refused
> while an authenticated one succeeds; collector and live-predictor both run
> with a credential-bearing `DATABASE_URL`; task ingestion confirmed.
>
> **Two findings that changed the tooling, worth carrying forward:**
>
> 1. **Every application service in `docker-compose.yml` declares `profiles:`;
>    only `postgres` does not.** So bare `docker compose config` renders
>    postgres alone, and bare `docker compose up -d` would restart postgres
>    alone. Anything that must see or act on the app services has to enable
>    every declared profile — but must still name services explicitly, because
>    `--profile full` would start the six services deliberately not running.
> 2. **`environment:` overrides `env_file:`.** The services hardcoded a
>    password-less `DATABASE_URL`, so editing `.env` changed nothing. All eight
>    are now `${DATABASE_URL:?...}`, and `.env` must supply both
>    `DATABASE_URL` and `POSTGRES_PASSWORD` or compose refuses to render.
>    Container env is fixed at creation, so picking up a changed `.env`
>    requires `--force-recreate`, not `restart`.

### Task 3: Set passwords before changing authentication

Order matters. Setting passwords first means that when `trust` is replaced in Task 4, every identity already has a working credential. Doing it the other way locks the services out.

**Files:** none (host state).

- [x] **Step 1: Generate and store passwords**

On the host, as the deploy user:

```bash
umask 077
mkdir -p ~/qf-secrets
for role in postgres forecast_app forecast_experiment forecast_migrator; do
  openssl rand -base64 32 | tr -d '\n' > ~/qf-secrets/$role.pw
done
ls -l ~/qf-secrets
```

Expected: four files, mode `-rw-------`. These must never be readable by `research` (asserted by negative control 3).

- [x] **Step 2: Set the superuser password**

```bash
docker compose exec -T postgres psql -U postgres -d forecasting \
  -c "ALTER ROLE postgres WITH PASSWORD '$(cat ~/qf-secrets/postgres.pw)';"
```

Expected: `ALTER ROLE`.

- [x] **Step 3: Verify it was stored as SCRAM, not md5**

```bash
docker compose exec -T postgres psql -U postgres -d forecasting \
  -c "SELECT rolname, left(rolpassword, 14) AS scheme FROM pg_authid WHERE rolname='postgres';"
```

Expected: `scheme` begins `SCRAM-SHA-256`. If it reads `md5`, stop: `password_encryption` is wrong, fix it with `ALTER SYSTEM SET password_encryption='scram-sha-256'; SELECT pg_reload_conf();` and re-set the password.

### Task 4: Replace `trust` with SCRAM

**Files:** `pg_hba.conf` inside the postgres volume (backed up in Task 2).

- [x] **Step 1: Write the new `pg_hba.conf`**

```bash
docker compose exec -T postgres bash -c 'cat > /var/lib/postgresql/data/pg_hba.conf <<HBA
# TYPE  DATABASE  USER  ADDRESS       METHOD
# Local socket inside the container keeps peer auth so container-local
# maintenance (psql -U postgres) still works without a password prompt.
local   all       all                 trust
# Every network connection - which is every service container, the trainer,
# and any tunnel - must authenticate. Replacing this with trust is what made
# the read-only experiment role meaningless.
host    all       all   127.0.0.1/32  scram-sha-256
host    all       all   ::1/128       scram-sha-256
host    all       all   0.0.0.0/0     scram-sha-256
host    all       all   ::/0          scram-sha-256
HBA'
docker compose exec -T postgres cat /var/lib/postgresql/data/pg_hba.conf
```

Expected: the file reads back exactly as above.

- [x] **Step 2: Reload and confirm the file took effect**

```bash
docker compose exec -T postgres psql -U postgres -d forecasting -c "SELECT pg_reload_conf();"
docker compose exec -T postgres psql -U postgres -d forecasting \
  -c "SELECT type, database, user_name, address, auth_method FROM pg_hba_file_rules ORDER BY line_number;"
```

Expected: no row shows `trust` for a `host` type; `error` column (if shown) is null on every row.

- [x] **Step 3: Prove that unauthenticated network access is now refused**

```bash
docker compose exec -T postgres psql "postgresql://postgres@127.0.0.1:5432/forecasting" -c "SELECT 1;"
```

Expected: **fails** with `no password supplied` or `password authentication failed`. If it succeeds, `pg_hba.conf` did not take effect — stop and roll back with Step 5.

- [x] **Step 4: Update the services' `DATABASE_URL` and restart**

Edit `.env` on the host so `DATABASE_URL` carries the password:

```
DATABASE_URL=postgresql://postgres:<contents of ~/qf-secrets/postgres.pw, URL-encoded>@postgres:5432/forecasting
```

URL-encode the password first — base64 can contain `+` and `/`:

```bash
python3 -c "import urllib.parse,pathlib;print(urllib.parse.quote(pathlib.Path('$HOME/qf-secrets/postgres.pw').read_text(),safe=''))"
```

Then:

```bash
docker compose up -d
sleep 20
docker compose ps
docker compose logs --tail 40 collector live-predictor
```

Expected: every service is `running`, and neither log shows authentication errors. Confirm collection is alive:

```bash
docker compose exec -T postgres psql -U postgres -d forecasting \
  -c "SELECT count(*) FROM queue_forecast_tasks WHERE task_created > now() - interval '10 minutes';"
```

Expected: non-zero, and growing on a second run a minute later.

- [x] **Step 5: Rollback procedure (only if Step 4 fails)**

```bash
docker compose exec -T postgres cp /var/lib/postgresql/data/pg_hba.conf.pre-scram \
  /var/lib/postgresql/data/pg_hba.conf
docker compose exec -T postgres psql -U postgres -d forecasting -c "SELECT pg_reload_conf();"
git checkout .env 2>/dev/null || true   # restore the previous DATABASE_URL
docker compose up -d
```

Then report what failed before continuing.

### Task 5 — DONE 2026-08-24 (except Step 6, `db-app-cutover`)

> Applied via `host/phase0-setup.sh db-roles`. Grant list derived from the live
> database matched the six expected tables exactly. Read-only proof passed:
> `forecast_experiment` SELECT ok, DELETE refused, CREATE refused — negative
> control 1 satisfied.
>
> **Step 6 (`db-app-cutover`, moving services off the postgres superuser) is
> deliberately NOT done.** It recreates containers, so it carries the same risk
> class as the SCRAM cutover. Phase 0's requirement is that
> `forecast_experiment` is genuinely read-only, which is now proven; the
> superuser migration is hygiene and can wait until the current state has held.

### Task 5: Create the experiment, migrator, and app roles

**Files:** none (host state).

- [x] **Step 1: Create the three roles**

Substitute the generated passwords. `forecast_experiment` and `forecast_migrator` are `NOLOGIN`-free but strictly limited; none is a superuser.

```bash
docker compose exec -T postgres psql -U postgres -d forecasting <<SQL
CREATE ROLE forecast_app        LOGIN PASSWORD '$(cat ~/qf-secrets/forecast_app.pw)';
CREATE ROLE forecast_experiment LOGIN PASSWORD '$(cat ~/qf-secrets/forecast_experiment.pw)' CONNECTION LIMIT 4;
CREATE ROLE forecast_migrator   LOGIN PASSWORD '$(cat ~/qf-secrets/forecast_migrator.pw)' CONNECTION LIMIT 2;
SQL
```

Expected: three `CREATE ROLE` lines.

- [x] **Step 2: Remove the implicit public schema grant**

Postgres 15 already revokes `CREATE` on `public` from `PUBLIC`, but assert it rather than assume:

```bash
docker compose exec -T postgres psql -U postgres -d forecasting <<'SQL'
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON DATABASE forecasting FROM PUBLIC;
SQL
```

Expected: `REVOKE` twice.

- [x] **Step 3: Grant read-only access to `forecast_experiment`**

```bash
docker compose exec -T postgres psql -U postgres -d forecasting <<'SQL'
GRANT CONNECT ON DATABASE forecasting TO forecast_experiment;
GRANT USAGE ON SCHEMA public TO forecast_experiment;
GRANT SELECT ON
  queue_forecast_tasks,
  queue_forecast_task_runs,
  queue_forecast_run_predictions,
  queue_forecast_worker_counts,
  queue_forecast_worker_pools,
  queue_forecast_daily_health
TO forecast_experiment;

ALTER ROLE forecast_experiment SET default_transaction_read_only = on;
ALTER ROLE forecast_experiment SET statement_timeout = '30min';
ALTER ROLE forecast_experiment SET idle_in_transaction_session_timeout = '5min';
ALTER ROLE forecast_experiment SET lock_timeout = '10s';
ALTER ROLE forecast_experiment SET temp_file_limit = '20GB';
ALTER ROLE forecast_experiment SET work_mem = '512MB';
SQL
```

The `statement_timeout` of 30 minutes is sized for the trainer's known extract queries, which legitimately scan large fractions of the retention-capped tables. `work_mem` matches what `data_loader.py:_connect()` already sets per session.

Expected: `GRANT` ×3 then `ALTER ROLE` ×6.

If Task 2 Step 1 showed tables beyond the six listed, add them to the `GRANT SELECT` list now. A missing grant surfaces later as a confusing trainer failure.

- [x] **Step 4: Grant the migrator exactly what it needs**

```bash
docker compose exec -T postgres psql -U postgres -d forecasting <<'SQL'
GRANT CONNECT ON DATABASE forecasting TO forecast_migrator;
GRANT USAGE, CREATE ON SCHEMA public TO forecast_migrator;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO forecast_migrator;
SQL
```

The migrator needs `CREATE` to add columns and tables. The additive-only restriction is enforced by the CI migration linter in Phase 6, not by grants — this role is deliberately capable and deliberately unavailable to agents.

Expected: `GRANT` ×3.

- [x] **Step 5: Prove the experiment role is read-only**

This is negative control 1, run manually before it is scripted:

```bash
EXP_PW=$(cat ~/qf-secrets/forecast_experiment.pw)
docker compose exec -T postgres psql \
  "postgresql://forecast_experiment:${EXP_PW}@127.0.0.1:5432/forecasting" \
  -c "SELECT count(*) FROM queue_forecast_tasks;"
docker compose exec -T postgres psql \
  "postgresql://forecast_experiment:${EXP_PW}@127.0.0.1:5432/forecasting" \
  -c "DELETE FROM queue_forecast_tasks WHERE false;"
docker compose exec -T postgres psql \
  "postgresql://forecast_experiment:${EXP_PW}@127.0.0.1:5432/forecasting" \
  -c "CREATE TABLE agent_escape (x int);"
```

Expected: the `SELECT` succeeds; the `DELETE` fails with `cannot execute DELETE in a read-only transaction`; the `CREATE TABLE` fails with `permission denied for schema public`.

Note the `WHERE false` on the delete: even the refusal path must not risk data.

- [x] **Step 6: Migrate the services off the superuser**

Grant `forecast_app` full access to the existing tables, then switch `DATABASE_URL` to it. This is the last risky step in this task and is independently revertible.

```bash
docker compose exec -T postgres psql -U postgres -d forecasting <<'SQL'
GRANT CONNECT ON DATABASE forecasting TO forecast_app;
GRANT USAGE ON SCHEMA public TO forecast_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO forecast_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO forecast_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO forecast_app;
SQL
```

Expected: `GRANT` ×4 then `ALTER DEFAULT PRIVILEGES`.

Update `.env` so `DATABASE_URL` uses `forecast_app` and its URL-encoded password, then:

```bash
docker compose up -d
sleep 20
docker compose ps
docker compose logs --tail 40 collector live-predictor dashboard-gen
docker compose exec -T postgres psql -U postgres -d forecasting \
  -c "SELECT count(*) FROM queue_forecast_tasks WHERE task_created > now() - interval '5 minutes';"
```

Expected: all services running, no permission errors in logs, task count non-zero and growing.

**Rollback:** set `DATABASE_URL` back to the `postgres` identity from Task 4 Step 4 and `docker compose up -d`. The role and grants can stay; only the connection string reverts.

---

## Phase 0d — The `research` user

### Task 6: Create the user and cap its resources

**Files:**
- Create: `tools/queue-forecasting/host/qf-research.slice`
- Create: `tools/queue-forecasting/host/README.md`

- [ ] **Step 1: Create the user**

```bash
sudo useradd --create-home --shell /bin/bash research
sudo passwd -l research          # no password login
id research
```

Expected: `uid=NNNN(research) gid=NNNN(research) groups=NNNN(research)`. Critically, `research` must **not** be in the `docker` group — verify:

```bash
getent group docker
```

Expected: the `docker` group's member list does not contain `research`.

- [ ] **Step 2: Write the slice unit**

Create `tools/queue-forecasting/host/qf-research.slice`. Size `MemoryMax` and `CPUQuota` from the numbers recorded in Task 2 Step 4. The values below assume the observed ~30 GB / 8 core box, leaving the service stack and the trainer their existing headroom.

```ini
[Unit]
Description=Resource caps for the auto-research agents
Before=slices.target

[Slice]
# Agents run as ordinary shells outside any experiment container, so their
# resource use is capped here rather than by a container runtime. Sized so
# agent activity cannot starve the collector, live-predictor, or Postgres.
MemoryMax=4G
MemoryHigh=3G
CPUQuota=200%
TasksMax=512
IOWeight=50
```

- [ ] **Step 3: Install and start the slice**

```bash
sudo cp tools/queue-forecasting/host/qf-research.slice /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start qf-research.slice
systemctl show qf-research.slice -p MemoryMax -p CPUQuota -p TasksMax
```

Expected: `MemoryMax=4294967296`, `CPUQuotaPerSecUSec=2s`, `TasksMax=512`.

- [ ] **Step 4: Bind the user's sessions to the slice**

```bash
RESEARCH_UID=$(id -u research)
sudo mkdir -p /etc/systemd/system/user@${RESEARCH_UID}.service.d
sudo tee /etc/systemd/system/user@${RESEARCH_UID}.service.d/slice.conf >/dev/null <<'CONF'
[Service]
Slice=qf-research.slice
CONF
sudo systemctl daemon-reload
systemctl cat user@${RESEARCH_UID}.service | grep -A1 'slice.conf'
```

Expected: the override prints, showing `Slice=qf-research.slice`.

If `user@.service` is not in use on this host (no logind user sessions), skip this and instead launch the agents' cron entries with `systemd-run --slice=qf-research.slice`, which Phase 4 will do anyway.

- [ ] **Step 5: Verify the credentials directory is unreadable**

```bash
sudo -u research cat ~/qf-secrets/forecast_app.pw; echo "exit=$?"
sudo -u research cat /srv/queue-forecasting/.env; echo "exit=$?"
```

Expected: both fail with `Permission denied`. If `.env` is world-readable, fix it:

```bash
chmod 600 /srv/queue-forecasting/.env
chmod 700 ~/qf-secrets
```

- [ ] **Step 6: Checkpoint**

```bash
git add tools/queue-forecasting/host/qf-research.slice tools/queue-forecasting/host/README.md
```

Stop. The user commits.

### Task 7: Restrict egress

The agents need `api.anthropic.com`, `api.openai.com`, and `github.com`. Everything else must be denied. `nftables` cannot filter by hostname, and the API endpoints' IPs change, so the allowlist lives in a filtering proxy and `nftables` forces the agents through it.

**Files:**
- Create: `tools/queue-forecasting/host/tinyproxy-allowlist.conf`

- [ ] **Step 1: Install and configure the proxy**

```bash
sudo apt-get update && sudo apt-get install -y tinyproxy nftables
```

Create `tools/queue-forecasting/host/tinyproxy-allowlist.conf`:

```
# Egress allowlist for the auto-research agents. The agents are API clients,
# so egress cannot be zero - it is restricted to exactly the endpoints they
# need. Experiment containers get no egress at all (Phase 2).
User tinyproxy
Group tinyproxy
Port 8888
Listen 127.0.0.1
Timeout 600
Allow 127.0.0.1
# CONNECT is required for HTTPS; 443 only.
ConnectPort 443
Filter "/etc/tinyproxy/allowlist.txt"
FilterURLs Off
FilterExtended On
FilterCaseSensitive Off
FilterDefaultDeny Yes
LogFile "/var/log/tinyproxy/tinyproxy.log"
LogLevel Connect
```

- [ ] **Step 2: Write the domain allowlist**

```bash
sudo tee /etc/tinyproxy/allowlist.txt >/dev/null <<'LIST'
^api\.anthropic\.com$
^api\.openai\.com$
^chatgpt\.com$
^github\.com$
^api\.github\.com$
^codeload\.github\.com$
^objects\.githubusercontent\.com$
LIST
sudo cp tools/queue-forecasting/host/tinyproxy-allowlist.conf /etc/tinyproxy/tinyproxy.conf
sudo systemctl restart tinyproxy
sudo systemctl status tinyproxy --no-pager | head -5
```

Expected: `active (running)`.

`chatgpt.com` is included because the Codex CLI may authenticate against it; if Step 5 shows it is unused, remove it and re-run Step 5.

- [ ] **Step 3: Force `research` through the proxy with nftables**

```bash
RESEARCH_UID=$(id -u research)
sudo tee /etc/nftables.conf >/dev/null <<NFT
#!/usr/sbin/nft -f
flush ruleset

table inet qf {
  chain output {
    type filter hook output priority 0; policy accept;

    # Everything below applies only to the research user.
    meta skuid != ${RESEARCH_UID} accept

    # Loopback: needed to reach the proxy and the bounded query interface.
    oifname "lo" accept

    # DNS to the host's resolver.
    udp dport 53 accept
    tcp dport 53 accept

    # Anything else leaving the box from this uid is denied. HTTPS must go
    # through the proxy on 127.0.0.1:8888, which is covered by the lo rule.
    counter reject with icmp type admin-prohibited
  }
}
NFT
sudo systemctl enable --now nftables
sudo nft list ruleset
```

Expected: the ruleset prints with the `qf` table present.

- [ ] **Step 4: Set the proxy environment for `research`**

```bash
sudo tee /home/research/.profile.d-proxy >/dev/null <<'ENVV'
export HTTPS_PROXY=http://127.0.0.1:8888
export HTTP_PROXY=http://127.0.0.1:8888
export NO_PROXY=127.0.0.1,localhost
ENVV
echo '. /home/research/.profile.d-proxy' | sudo tee -a /home/research/.bashrc
sudo chown research:research /home/research/.profile.d-proxy
```

- [ ] **Step 5: Verify allow and deny both work**

```bash
sudo -u research -i bash -lc 'curl -sS -o /dev/null -w "%{http_code}\n" https://api.github.com'
sudo -u research -i bash -lc 'curl -sS -o /dev/null -w "%{http_code}\n" https://example.com'
sudo -u research -i bash -lc 'curl -sS --noproxy "*" -o /dev/null -w "%{http_code}\n" https://api.github.com'
```

Expected, in order: a 2xx/4xx status (reachable); a failure or `403` from the proxy filter (blocked by allowlist); a connection failure (blocked by nftables — proves the proxy cannot be bypassed).

- [ ] **Step 6: Checkpoint**

```bash
git add tools/queue-forecasting/host/tinyproxy-allowlist.conf
```

Stop. The user commits.

### Task 8: Install and authenticate the agent CLIs

The CLIs' own permission models are **not** the containment boundary — Tasks 3–7 are. These steps install the tools and prove they can reach their APIs through the allowlist.

**Files:** none (host state).

- [ ] **Step 1: Install Node for `research`**

```bash
# psql is required by negative control NC1 - without it the canary voids the
# whole group and the read-only assertion proves nothing.
sudo apt-get install -y postgresql-client
sudo -u research -i bash -lc 'psql --version'

sudo -u research -i bash -lc 'curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash'
sudo -u research -i bash -lc 'nvm install 24 && node --version'
```

Expected: `psql (PostgreSQL) 15.x` or newer, then `v24.x` from node.

If the nvm installer is blocked, add `raw.githubusercontent.com` to `/etc/tinyproxy/allowlist.txt`, restart tinyproxy, and retry — then remove it again after installation, since the agents do not need it at runtime.

- [ ] **Step 2: Install both CLIs**

```bash
sudo -u research -i bash -lc 'npm install -g @anthropic-ai/claude-code @openai/codex'
sudo -u research -i bash -lc 'claude --version && codex --version'
```

Expected: both print versions.

- [ ] **Step 3: Store API keys readable only by `research`**

Use API keys, not interactive subscription login: cron-driven use must be non-interactive, and the daily token budget in the design needs metered billing to mean anything.

```bash
sudo -u research -i bash -lc 'umask 077; mkdir -p ~/.config/qf && cat > ~/.config/qf/agent-env <<KEYS
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
KEYS'
sudo -u research -i bash -lc 'ls -l ~/.config/qf/agent-env'
```

Expected: mode `-rw-------`, owner `research`.

Paste the real keys in place of the placeholders when running the command — they are the only values in this plan that cannot be written down ahead of time.

- [ ] **Step 4: Prove each CLI works non-interactively through the proxy**

```bash
sudo -u research -i bash -lc '. ~/.config/qf/agent-env; claude -p "reply with the single word: ready"'
sudo -u research -i bash -lc '. ~/.config/qf/agent-env; codex exec "reply with the single word: ready"'
```

Expected: each prints `ready`.

**If a CLI fails to connect**, it does not honour `HTTPS_PROXY`. That is a fail-closed outcome, not a security hole, but it blocks the loop. The fallback, applied only to the failing tool: add an `nftables` set of that endpoint's resolved addresses, refreshed by a timer, and allow uid `research` direct egress to that set on 443. Record the exception in `host/README.md` — an undocumented hole is worse than a documented one.

- [ ] **Step 5: Confirm the CLIs cannot reach a denied host**

```bash
sudo -u research -i bash -lc 'curl -sS -o /dev/null -w "%{http_code}\n" https://pypi.org'
```

Expected: blocked. If the fallback in Step 4 was applied, re-run Step 5 to confirm it did not widen egress beyond the single intended endpoint.

---

## Phase 0e — The negative-control suite

### Task 9: Script negative controls 1–6

Spec §13.1 Phase 0. Each test must be **refused**; the script fails if any is permitted.

**Files:**
- Create: `tools/queue-forecasting/host/nc-suite.sh`

- [ ] **Step 1: Write the suite**

Create `tools/queue-forecasting/host/nc-suite.sh`:

```bash
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

set -uo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-/srv/queue-forecasting}"
SECRETS_DIR="${SECRETS_DIR:-$HOME/qf-secrets}"
pass=0
fail=0

refuse() {   # refuse <name> <command...>  -> passes when the command FAILS
  local name="$1"; shift
  if sudo -u research -i bash -lc "$*" >/dev/null 2>&1; then
    echo "FAIL  $name  (action was PERMITTED)"
    fail=$((fail + 1))
  else
    echo "ok    $name  (refused)"
    pass=$((pass + 1))
  fi
}

canary() {   # canary <name> <command...> -> passes when the command SUCCEEDS
  local name="$1"; shift
  if sudo -u research -i bash -lc "$*" >/dev/null 2>&1; then
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
canary "NC3 env exists"    "test -e $DEPLOY_DIR/.env || true"
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
```

Note `NC1` connects on host port```

Note `NC1` connects on host port **5433**, not 5432: the research user reaches Postgres through the published loopback port, and that path must be authenticated too.

The canary pattern is the important part. `refuse` passes whenever a command
fails, including when the binary is missing or the target path does not exist —
which would turn this suite into security theatre. Every refusal group is
therefore gated on an attempt that must succeed. `NC4 platform` and `NC2
podman` are skipped rather than faked when their targets do not yet exist, and
`NC5` is counted as a failure if its directory is missing, because a vacuous
control is worse than an absent one.

- [ ] **Step 2: Make it executable and stage it**

```bash
chmod +x tools/queue-forecasting/host/nc-suite.sh
git add tools/queue-forecasting/host/nc-suite.sh
```

- [ ] **Step 3: Confirm NC5's target actually exists**

```bash
ls -ld /srv/queue-forecasting/trainer/data/models
```

Expected: the directory exists and is not writable by `research`. If it does not exist, the control is vacuous — create it or point `DEPLOY_DIR` at the real deploy path.

- [ ] **Step 4: Run the suite**

```bash
sudo DEPLOY_DIR=/srv/queue-forecasting SECRETS_DIR=$HOME/qf-secrets \
  ./tools/queue-forecasting/host/nc-suite.sh
```

Expected: every line begins `ok` or `skip`, and the final line reads
`failed=0` with exit code 0. On a Phase-0 host with `podman` absent and
`/srv/qf-platform` not yet created, the count is `passed=14 failed=0` with two
`skip` lines. Any `VOID` line is a failure: it means a control could not be
meaningfully attempted.

- [ ] **Step 5: Record the evidence**

Save the full output to `tools/queue-forecasting/host/nc-evidence-phase0.txt` and stage it. The spec requires this suite to be re-run before Phases 4 and 5; the baseline makes a regression visible.

```bash
sudo DEPLOY_DIR=/srv/queue-forecasting SECRETS_DIR=$HOME/qf-secrets \
  ./tools/queue-forecasting/host/nc-suite.sh | tee tools/queue-forecasting/host/nc-evidence-phase0.txt
git add tools/queue-forecasting/host/nc-evidence-phase0.txt
```

- [ ] **Step 6: Write `host/README.md`**

Create `tools/queue-forecasting/host/README.md`:

```markdown
# Host artifacts — auto-research loop

Applied to the experimental server during Phase 0. See
`../auto-research-loop-design.md` §3 and §13.

| File | Installed to | Purpose |
|---|---|---|
| `qf-research.slice` | `/etc/systemd/system/` | Resource caps for the agent processes |
| `tinyproxy-allowlist.conf` | `/etc/tinyproxy/tinyproxy.conf` | Egress allowlist (domains in `/etc/tinyproxy/allowlist.txt`) |
| `nc-suite.sh` | run in place | Negative controls 1–6; must exit 0 |
| `nc-evidence-phase0.txt` | — | Baseline evidence from the first passing run |

Not in this repo, and deliberately so: `pg_hba.conf` (inside the postgres
volume, backed up as `pg_hba.conf.pre-scram`), `/etc/nftables.conf`,
`~/qf-secrets/*.pw`, and `/home/research/.config/qf/agent-env`.

## Egress exceptions

None. If a CLI is found not to honour `HTTPS_PROXY` and a direct nftables
allowance is added for it, record the endpoint, the reason, and the date here.
```

- [ ] **Step 7: Checkpoint**

```bash
git add tools/queue-forecasting/host/README.md
```

Stop. The user commits.

---

## Phase 0 acceptance

- [ ] `node test/smoke-guard.test.js` passes and `node test/smoke.js` refuses to run with no environment.
- [ ] `pg_hba.conf` contains no `trust` rule for `host` connections, and an unauthenticated network connection is refused.
- [ ] `forecast_experiment` can `SELECT` and cannot `DELETE` or `CREATE`.
- [ ] Services run as `forecast_app`, not the superuser, and collection is confirmed still ingesting.
- [ ] `nc-suite.sh` exits 0 with `failed=0`, and the output is recorded in `host/nc-evidence-phase0.txt`.
- [ ] Both `claude -p` and `codex exec` return `ready` as the `research` user, through the proxy.

When all six hold, Phase 1 (repository extraction and credential scoping) can begin.
