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

## nftables: match the uid positively, never negatively

`meta skuid != <uid> accept` as a leading rule is **wrong**. In nftables,
`meta skuid` on a packet with no owning socket — kernel-generated traffic, ICMP
errors, TCP resets, forwarded packets — does not match at all; the expression
fails rather than evaluating true. Those packets skip the accept, fall through
every later rule, and hit the reject. Observed effect: the collector started
timing out on all outbound requests.

Every rule in `inet qf` therefore matches `meta skuid <uid>` positively, so
anything else matches nothing and reaches the chain's accept policy untouched.

## Rollback

- Egress table only: `sudo nft delete table inet qf`

- SCRAM cutover: `./phase0-setup.sh rollback-db-auth`
- Service identity: `cp .env.pre-app .env && docker compose up -d`

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

## Two invocation traps (both cost real debugging time)

**Never `sudo -i` with a command.** With `-i`, sudo joins its arguments into a
single string and hands that to the target user's login shell, which re-parses
it — quoting and newlines are destroyed. Use `sudo -H -u research bash -lc
"$cmd"`, which passes argv through untouched. Observed failure: `export
NVM_DIR="$HOME/.nvm"; ...` became a bare `export` that dumped the environment,
leaving `$NVM_DIR` empty and every later command broken. This also silently
weakens `nc-suite.sh`, where a command mangled into failure reads as "refused".

## Proxy environment lives in ~/.profile, not ~/.bashrc

Same non-interactive trap as PATH. Verified behaviour:

| invocation | reads `.profile` | reads `.bashrc` |
|---|---|---|
| `bash -lc` (sudo, run_research) | yes | no |
| `bash -c` (cron-like) | no | no |

So `.profile.d-proxy` is sourced from `~/.profile`, and `run_research` sources
it directly as well. **cron reads neither** — the Phase 4 tick must
`. /home/research/.profile.d-proxy` itself or the agents will bypass the proxy
and then be blocked by nftables.

Both upper- and lower-case variables are set: libcurl (curl, git) prefers the
lower-case names, most Node HTTP stacks read the upper-case ones.

## PATH gotcha for anything non-interactive (cron included)

Debian's `~/.bashrc` starts with

```sh
case $- in
    *i*) ;;
      *) return;;
esac
```

so it returns immediately for non-interactive shells — and that is where nvm's
initialisation lives. A `bash -lc` login shell is still non-interactive, so
`node`, `npm`, `claude`, and `codex` are all invisible to it.

Sourcing `nvm.sh` from the tick script is *not* a reliable fix either — it
defines `nvm` as a shell function, and whether that survives depends on the
invocation. `phase0-setup.sh` instead resolves the installed node's bin
directory and prepends it to `PATH`:

```sh
export NVM_DIR="$HOME/.nvm"
_nvmbin="$(ls -d "$NVM_DIR"/versions/node/*/bin 2>/dev/null | sort -V | tail -1)"
[ -n "$_nvmbin" ] && export PATH="$_nvmbin:$PATH"
```

**The Phase 4 cron tick must do the same**, or it fails with `command not
found` and no other clue.

Worth reconsidering at Phase 4: for a cron-driven loop, a system-wide
`/usr/bin/node` (apt/NodeSource) is more robust than a per-user version
manager. The only cost of switching is reinstalling the two CLIs; their
credentials live in `~/.claude` and `~/.codex` and would survive.
