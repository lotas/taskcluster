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
