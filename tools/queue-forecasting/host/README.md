# Host artifacts — auto-research loop, Phase 0

Applied to the experimental server. See `../auto-research-loop-design.md` §3
and §13 for why each control exists, and `../auto-research-phase0-plan.md` for
the task-by-task narrative.

## Running it

`phase0-setup.sh` implements plan Tasks 2–9. **You run it, not an agent** —
Phase 0 is inherently privileged work (creates a unix user, writes
`/etc/systemd` and `/etc/nftables.conf`, restarts the live stack), and the
point of scripting it is that no agent ever needs root on this host.

```bash
./phase0-setup.sh discover           # read-only; changes nothing
./phase0-setup.sh db-auth --check    # dry run of the SCRAM cutover
./phase0-setup.sh db-auth            # apply, verify, auto-rollback on failure
./phase0-setup.sh db-roles
./phase0-setup.sh research-user
./phase0-setup.sh egress
./phase0-setup.sh agent-cli
./phase0-setup.sh verify             # negative controls 1–6
```

Every subcommand is idempotent. Run `discover` first and read the output.

`db-app-cutover` (moving the services off the Postgres superuser) is
deliberately *not* part of `all`. Run it separately, once the stack has been
observed healthy.

## What the script will not do

1. **Log the agents in.** Authentication is interactive SSO, not API keys, so
   there is no key file. Do this once, as `research`, **before `egress`** — the
   OAuth flow reaches your SSO provider and the vendors' auth domains, which
   the allowlist does not permit:

   ```bash
   sudo -u research -i
   claude          # then /login
   codex login
   exit
   ```

   Then re-run `agent-cli`. Afterwards, `auth-check` is the standing probe:
   SSO tokens refresh against an auth endpoint, and if the allowlist blocks it
   the agents work for days and then stop silently.
2. **Fix `password_encryption=md5`.** It stops. Setting passwords in the wrong
   scheme and then flipping `pg_hba` locks the services out.
3. **Decide about unexpected tables.** Grants are derived from the live table
   list and printed before being applied — read them.
4. **Apply the `HTTPS_PROXY` fallback.** If a CLI cannot reach its API through
   tinyproxy, that is fail-closed, not a hole. Widening egress is a decision.

## Files

| File | Installed to | Purpose |
|---|---|---|
| `phase0-setup.sh` | run in place | Implements plan Tasks 2–9 |
| `qf-research.slice` | `/etc/systemd/system/` | Resource caps for the agent processes |
| `tinyproxy-allowlist.conf` | `/etc/tinyproxy/tinyproxy.conf` | Egress allowlist (domains in `/etc/tinyproxy/allowlist.txt`) |
| `nc-suite.sh` | run in place | Negative controls 1–6; must exit 0 |
| `nc-evidence-phase0.txt` | — | Baseline evidence from the first passing run |

Run order matters: `research-user` → `agent-cli` → **interactive login** →
`egress` → `auth-check` → `verify`. Logging in after the egress lock-down
will fail.

Deliberately not in this repo: `pg_hba.conf` (inside the postgres volume,
backed up as `pg_hba.conf.pre-scram`), `/etc/nftables.conf`, `~/qf-secrets/*.pw`,
and `/home/research/.config/qf/agent-env`.

## Rollback

- SCRAM cutover: `./phase0-setup.sh rollback-db-auth`
- Service identity: `cp .env.pre-app .env && docker compose up -d`

## Egress exceptions

None. If a CLI is found not to honour `HTTPS_PROXY` and a direct nftables
allowance is added for it, record the endpoint, the reason, and the date here.
