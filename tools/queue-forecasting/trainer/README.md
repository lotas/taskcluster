# Trainer — production copy (frozen)

This is the **production** trainer. It serves `scripts/daily_walk_forward.sh`
and the `trainer` service in `docker-compose.yml`, and it is the copy the live
predictor's models come from.

**Research does not happen here.** It happens in `lotas/qf-research`, which
holds this directory's history from the same origin and which the research
agent owns outright. See `../auto-research-phase1-design.md`.

## What "frozen" means

Human-curated changes only. Not "unchanged" — new features legitimately add
modules here (bet 1 added `src/queue_context.py`; bet 2 added
`src/hazard_labels.py` and `src/hazard_model.py`). What is excluded is any
automated or agent-driven write.

Promoting a research result is therefore a **curated port**: the code, the
config, and any dependency change, read and applied by a human, followed by a
retrain. It is not a config copy, and there is no branch to merge.

## Retention

This copy is **not** scheduled for deletion. An earlier draft of the Phase 1
design promised Phase 2 would delete it once the dispatcher trained from
`qf-research`; that was withdrawn, because `qf-research` is untrusted by
construction. Four things must be designed and reviewed before deletion is even
proposed: an immutable human-approved revision pinned by object ID, a rewired
production job with a rollback, a hard separation keeping experiment output out
of `data/models/`, and the promotion path itself.

## If you change dependencies here

`pyproject.toml` and `uv.lock` in this directory are the **trusted** manifests.
From Phase 2 the dispatcher builds the training image from these and from a
root-owned Dockerfile in the trusted checkout — never from `qf-research`'s
copies. Refresh them with an explicit reviewed `uv lock`, then `uv sync
--locked`; `--frozen` would skip the check that the lock still agrees with the
manifest.

## Training from a frozen extract (`--from-extract`)

The trainer's default source is Postgres. A probe has no network and no
credential, so it uses the other source: the six Parquet files the trusted
extractor publishes, mounted read-only at `/extract`.

```bash
# From a local extract directory
uv run python -m src.train --config configs/wait_time.yaml \
    --as-of-date 2026-04-24 --from-extract /path/to/extracts/<request_hash>

# Or via the environment, for anything that can set one
QF_EXTRACT_DIR=/extract uv run python -m src.train --config <config>
```

A **probe does not get either form for free**: its entrypoint is one script path
with no arguments and no injected environment, so the research-side wrapper has
to pass `--from-extract /extract` itself. It also has to bridge the baseline
path — the sandbox mounts the promoted baseline at `/baseline`, while a promoted
config resolves it under `trainer/data/baseline_filtered`.

On this path the trainer reads **no** `DATABASE_URL` and writes **no**
`data/cache` files, and the run manifest's `training_lineage.extract` records
the extract's hash and every file digest it read. Set `QF_EXTRACT_VERIFY=0` to
skip re-hashing the files on read.

The window has to fit: the extract's recorded bounds must cover what
`compute_windows` asks for, or the run is refused rather than trained on a
silent subset. Baselines are **not** in the extract — the per-day JSONs and the
residual NDJSON still come from `data/baseline*` (`/baseline` under the
sandbox).

Implementation: `src/extract_source.py`.
