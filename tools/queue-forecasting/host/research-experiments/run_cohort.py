#!/usr/bin/env python3
"""Train one cohort from the frozen extract and write the prediction set.

THIS FILE BELONGS IN `qf-research`, at `research/experiments/run_cohort.py` --
the only directory a `probe` may name (`spec.py`'s path restriction). It lives
here in the trusted repo because the dispatcher's token is read-only by design,
so an operator has to push it with the AGENT's credential:

    cp host/research-experiments/run_cohort.py \\
       <qf-research worktree>/research/experiments/run_cohort.py
    # commit + push from the research worktree, then:
    sudo -H -u research qf probe --sha <40-hex> \\
        --path research/experiments/run_cohort.py \\
        --extract <request_hash> --baseline <baseline_hash> --wait

WHAT A PROBE GIVES THIS SCRIPT, and what it does not: the entrypoint is this one
path with NO arguments and NO injected environment (`sandbox.py`'s `probe`
branch), so every path below is a fixed mount and nothing is configurable from
the outside. That is the point -- a path passed as an argument is a path
something has to validate twice, and the second validator would be in here.

    /app/trainer          the qf-research worktree root, read-only
    /app/trainer/trainer  the trainer package (so `src.` is under it)
    /app/trainer/trainer/data   WRITABLE (CACHE_DIR is module-relative)
    /extract              the frozen extract, read-only
    /baseline             the promoted baseline, read-only
    /out                  where `predictions.parquet` is collected from

TO CHANGE THE EXPERIMENT, change `CONFIG` -- or the config file it names, or the
feature code under `trainer/src/`. That is the whole research loop.
"""
from __future__ import annotations

import json
import datetime
import os
import pathlib
import sys

CONFIG = "configs/wait_time_residual_throughput_filtered_baseline.yaml"

TRAINER = pathlib.Path("/app/trainer/trainer")
EXTRACT = pathlib.Path("/extract")
BASELINE = pathlib.Path("/baseline")
OUT = pathlib.Path("/out/predictions.parquet")

_MANIFEST: "dict | None" = None


def bridge_baseline() -> None:
    """Point the config's `baseline_dir` at the mount the sandbox provides.

    `_baseline_dir` resolves `baseline_dir: data/baseline_filtered` to
    `<trainer_root>/data/baseline_filtered`, and the sandbox mounts the promoted
    baseline at `/baseline` instead. A symlink is enough and needs no trainer
    change: `trainer/data` is the one writable path in the tree, which is why
    the link goes there and not anywhere else.

    Kept to the ONE directory the config names rather than linking `data`
    wholesale: `data/cache` and `data/models` are the trainer's own writes and
    must stay on the writable mount.
    """
    import yaml

    with (TRAINER / CONFIG).open() as fh:
        raw = yaml.safe_load(fh)
    rel = raw.get("baseline_dir") or "data/baseline"
    if rel.startswith("data/"):
        rel = rel[len("data/"):]
    link = TRAINER / "data" / rel
    if link.is_symlink() or link.exists():
        return
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(BASELINE)
    print(f"[run_cohort] {link} -> {BASELINE}", flush=True)


def manifest() -> dict:
    """The extract's manifest, read once.

    Three callers below ask it three different questions, and an extract that
    changed between two of them would be an extract that changed under a running
    probe -- which cannot happen (a published extract is immutable), so the only
    thing three reads buy is three chances to write the path wrong.
    """
    global _MANIFEST
    if _MANIFEST is None:
        with (EXTRACT / "MANIFEST.json").open() as fh:
            _MANIFEST = json.load(fh)
    return _MANIFEST


def cohort_as_of() -> str:
    """The extract's own `as_of_date`, not the config's.

    The promoted configs carry `as_of_date: null`, which `_resolve_as_of_date`
    turns into TODAY's UTC midnight. Against a fixed extract that is wrong by
    construction -- the extract's window ended whenever it ended, and the
    containment check would refuse the run (correctly, and after the operator
    waited for it). Reading the boundary off the manifest means the cohort
    cannot drift from the data it was handed.
    """
    as_of = (manifest().get("request") or {}).get("as_of_date")
    if not as_of:
        raise SystemExit("[run_cohort] the extract manifest names no as_of_date")
    return as_of


def check_target(config_target: str) -> None:
    """Refuse a config/extract target mismatch before training, not after.

    The contract would catch it at evaluation, but only once the cohort has been
    trained -- and a wait config scored against a run_duration extract is twenty
    minutes nobody gets back.
    """
    request = manifest().get("request") or {}
    if request.get("target") != config_target:
        raise SystemExit(
            f"[run_cohort] the extract's target is {request.get('target')!r}"
            f" and the config's is {config_target!r}")


def check_qctx(raw: dict) -> None:
    """Refuse a queue-context config against an extract that cannot serve it.

    `extract_source.qctx_runs` already refuses this, correctly and fail-closed --
    but it refuses when the reference set is LOADED, which is minutes into a run
    that the operator is no longer watching. The information is in the manifest
    at second zero: `task_created` is the column the tasks-side join floor is
    re-applied on, and without it the reference set would be a superset of the
    cohort the SQL path saw.

    Checked from the MANIFEST's column list rather than by opening the parquet:
    the manifest is the extract's description of itself, and it is what
    `ExtractSource` consults for the same decision.
    """
    if not (raw.get("queue_context_features") or {}).get("enabled"):
        return
    columns = ((manifest().get("files") or {}).get("qctx_runs") or {}).get(
        "columns") or []
    if "task_created" not in columns:
        raise SystemExit(
            "[run_cohort] this config enables queue_context_features and the"
            " extract's qctx_runs carries no `task_created`, so the tasks-side"
            " join floor cannot be re-applied and the reference set would be a"
            " SUPERSET of what the database cohort saw.\n"
            "  The column landed in host/extractor/inventory.py on 2026-08-30."
            " `request_hash` does not cover the column list, so an extract"
            " requested before then is still a cache hit for the same window --"
            " re-extract with a BUMPED `generation`.\n"
            f"  qctx_runs columns in this extract: {', '.join(columns) or 'none'}")


def check_window(raw: dict) -> None:
    """Refuse a config whose train window reaches earlier than the extract's.

    `extract_source._covers` already refuses this, correctly -- but it refuses
    when the `runs` table is loaded, which is a container start and a data load
    into a run nobody is watching. The arithmetic is available at second zero,
    because a cohort's train_start is a pure function of the config's window
    keys and the extract's own `as_of_date`:

        train_start = as_of - holdout_days - validation_days - lookback_days

    THIS IS THE CHECK THAT UNBLOCKS AN AGENT rather than just failing it faster.
    A frozen extract's window is a function of the config family it was
    requested for, so a config with a wider window than that family's is not a
    broken config -- `wait_hazard_qctx_d_priority_flow` needs
    `validation_days: 7` for a measured reason, and no amount of editing it to
    fit is a legitimate fix. The remedy is a different extract, which is an
    operator decision, so this says so instead of leaving a traceback to
    interpret.
    """
    request = manifest().get("request") or {}
    have = request.get("train_start")
    as_of = request.get("as_of_date")
    if not (have and as_of):
        return                      # cohort_as_of() already refuses a missing as_of

    days = 0
    for key in ("holdout_days", "validation_days", "lookback_days"):
        value = raw.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            # Not this check's business to validate the config -- the trainer
            # owns that, and guessing a default here would produce a train_start
            # the trainer never computes.
            return
        days += value

    as_of_dt, have_dt = _parse_day(as_of), _parse_day(have)
    if as_of_dt is None or have_dt is None:
        return                      # an unparseable manifest is not this check's call
    need = as_of_dt - datetime.timedelta(days=days)
    if need >= have_dt:
        return

    raise SystemExit(
        f"[run_cohort] this config's cohort needs train_start <="
        f" {need.date()}, and the extract's window starts"
        f" {have_dt.date()}.\n"
        f"  as_of {as_of_dt.date()} minus holdout"
        f" {raw['holdout_days']} + validation {raw['validation_days']} +"
        f" lookback {raw['lookback_days']} = {days} days.\n"
        "  Training on it would silently train on a subset, so this refuses"
        " here rather than after the data load.\n"
        "  DO NOT shrink the config's window to fit. A window key is part of"
        " the experiment (this config's validation_days: 7 is measured), and"
        " editing it to make a run start is changing what is being tested.\n"
        "  The remedy is an extract whose window covers this cohort. Choosing"
        " one is an OPERATOR decision -- it starts a new series, and results"
        " across series are not comparable. Report this and stop:"
        " `qf extracts` prints every published window.")


def _parse_day(value: str):
    """A manifest day boundary as an aware datetime, or None."""
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z",
                                                                   "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def main() -> int:
    for path, what in ((EXTRACT, "extract"), (BASELINE, "baseline"),
                       (OUT.parent, "output")):
        if not path.is_dir():
            print(f"[run_cohort] no {what} mount at {path}", file=sys.stderr)
            return 2
    # A probe has no DATABASE_URL, and this asserts it rather than trusting it:
    # a credential reaching this process would mean the sandbox changed, and the
    # run would silently be reading the live database instead of the extract.
    if os.environ.get("DATABASE_URL"):
        print("[run_cohort] DATABASE_URL is set inside a probe; refusing",
              file=sys.stderr)
        return 2

    os.chdir(TRAINER)
    sys.path.insert(0, str(TRAINER))
    bridge_baseline()

    import yaml
    with (TRAINER / CONFIG).open() as fh:
        raw = yaml.safe_load(fh)
    check_target(raw["target"])
    check_qctx(raw)
    check_window(raw)

    as_of = cohort_as_of()
    print(f"[run_cohort] {CONFIG} as_of={as_of} extract={EXTRACT}", flush=True)

    from src import train

    # `--from-extract` explicitly: the env var is not set for a probe.
    return train.main(["--config", CONFIG,
                       "--as-of-date", as_of,
                       "--from-extract", str(EXTRACT),
                       "--predictions-out", str(OUT)])


if __name__ == "__main__":
    raise SystemExit(main())
