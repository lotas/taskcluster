# Queue Forecasting Scripts

These scripts are split by responsibility so cron stays small and the training
logic remains reusable.

## Cron-facing scripts

- `backup.sh` creates the Postgres dump, uploads it, prunes old dumps on a GFS
  schedule (7 daily / 4 weekly / 3 monthly, via `KEEP_DAILY` / `KEEP_WEEKLY` /
  `KEEP_MONTHLY`), and owns its own lock (`BACKUP_LOCK_FILE`, namespaced so that
  a crontab-wide `LOCK_FILE=` cannot capture it). Trainer-data is regenerable
  from the DB and is **not** synced by default; pass `--sync-training-data` to
  opt in. Pass `--no-prune` to keep every dump.
- `daily_walk_forward.sh` computes the daily UTC as-of date, starts Postgres,
  calls `walk_forward.sh`, and refreshes `walk_forward_summary.csv`. It no
  longer *owns* a private lock: it **joins a host-wide heavy-training mutex**
  shared with the Phase 2a dispatcher, declaring intent first and then waiting
  (`LOCK_WAIT_S`, default 9000s) rather than skipping the night. `flock` is now
  **required** — the old warn-and-continue branch meant a `PATH` missing one
  binary bypassed the entire mutex. It exits non-zero with a remedy if
  `/var/lib/qf-locks/heavy-training.lock` or `.../intent.d` is missing or not
  writable; `sudo ../host/phase2-setup.sh locks` provisions both.

## Building blocks

- `run_training.sh` trains one config for one optional `--as-of-date`.
- `walk_forward.sh` runs a resume-safe date/config sweep and skips cells whose
  manifest already exists.
- `ensure_baseline_ndjson.sh` maintains the aggregate residual baseline cache.
- `health.sh` is operational diagnostics for the running services.

## Cron

Keep cron to one script invocation plus logging:

```cron
0 0 * * * $HOME/dev/taskcluster/tools/queue-forecasting/scripts/backup.sh >> $HOME/queue-forecasting-backup.log 2>&1
0 1 * * * LOCK_FILE=/var/lib/qf-locks/heavy-training.lock INTENT_DIR=/var/lib/qf-locks/intent.d LOCK_WAIT_S=9000 $HOME/dev/taskcluster/tools/queue-forecasting/scripts/daily_walk_forward.sh >> $HOME/queue-forecasting-walk-forward.log 2>&1
```

Those three assignments go **inline on the entry**, not on standalone crontab
variable lines. A bare `LOCK_FILE=` line in a crontab applies to *every* entry
below it, `backup.sh` included — which would make backups `flock` the
heavy-training mutex and exit 1 whenever a training job held it, killing them
silently and making backups a contender on a mutex they have no business in.
`backup.sh` reads `BACKUP_LOCK_FILE` for exactly that reason, so this is belt
and braces; the habit is still the hazard.

`sudo ../host/phase2-setup.sh cron-lock-path` prints these and then **verifies**
that the crontab's lock path and intent directory `stat` to the same device and
inode as the dispatcher's. `flock` is per inode: two provisioned paths are two
mutexes, and both sides would then run.

`daily_walk_forward.sh` defaults to yesterday in UTC. To backfill manually:

```sh
./scripts/daily_walk_forward.sh --from 2026-04-15 --to 2026-04-30
```

To preview the commands without running Docker:

```sh
./scripts/daily_walk_forward.sh --dry-run
```
