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
import os
import pathlib
import sys

CONFIG = "configs/wait_time_residual_throughput_filtered_baseline.yaml"

TRAINER = pathlib.Path("/app/trainer/trainer")
EXTRACT = pathlib.Path("/extract")
BASELINE = pathlib.Path("/baseline")
OUT = pathlib.Path("/out/predictions.parquet")


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


def cohort_as_of() -> str:
    """The extract's own `as_of_date`, not the config's.

    The promoted configs carry `as_of_date: null`, which `_resolve_as_of_date`
    turns into TODAY's UTC midnight. Against a fixed extract that is wrong by
    construction -- the extract's window ended whenever it ended, and the
    containment check would refuse the run (correctly, and after the operator
    waited for it). Reading the boundary off the manifest means the cohort
    cannot drift from the data it was handed.
    """
    with (EXTRACT / "MANIFEST.json").open() as fh:
        request = json.load(fh).get("request") or {}
    as_of = request.get("as_of_date")
    if not as_of:
        raise SystemExit("[run_cohort] the extract manifest names no as_of_date")
    return as_of


def check_target(config_target: str) -> None:
    """Refuse a config/extract target mismatch before training, not after.

    The contract would catch it at evaluation, but only once the cohort has been
    trained -- and a wait config scored against a run_duration extract is twenty
    minutes nobody gets back.
    """
    with (EXTRACT / "MANIFEST.json").open() as fh:
        request = json.load(fh).get("request") or {}
    if request.get("target") != config_target:
        raise SystemExit(
            f"[run_cohort] the extract's target is {request.get('target')!r}"
            f" and the config's is {config_target!r}")


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
        check_target(yaml.safe_load(fh)["target"])

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
