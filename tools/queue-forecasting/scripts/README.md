# Queue Forecasting Scripts

These scripts are split by responsibility so cron stays small and the training
logic remains reusable.

## Cron-facing scripts

- `backup.sh` creates the Postgres dump, uploads it, prunes old dumps on a GFS
  schedule (7 daily / 4 weekly / 3 monthly, via `KEEP_DAILY` / `KEEP_WEEKLY` /
  `KEEP_MONTHLY`), and owns its own lock. Trainer-data is regenerable from the
  DB and is **not** synced by default; pass `--sync-training-data` to opt in.
  Pass `--no-prune` to keep every dump.
- `daily_walk_forward.sh` computes the daily UTC as-of date, owns the
  walk-forward lock, starts Postgres, calls `walk_forward.sh`, and refreshes
  `walk_forward_summary.csv`.

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
0 1 * * * $HOME/dev/taskcluster/tools/queue-forecasting/scripts/daily_walk_forward.sh >> $HOME/queue-forecasting-walk-forward.log 2>&1
```

`daily_walk_forward.sh` defaults to yesterday in UTC. To backfill manually:

```sh
./scripts/daily_walk_forward.sh --from 2026-04-15 --to 2026-04-30
```

To preview the commands without running Docker:

```sh
./scripts/daily_walk_forward.sh --dry-run
```
