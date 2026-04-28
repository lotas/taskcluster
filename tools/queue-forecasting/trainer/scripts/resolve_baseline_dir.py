#!/usr/bin/env python3
"""Print the baseline directory (relative to the trainer root) for a config.

Default is "data/baseline". When the config sets baseline_dir, that value is
echoed verbatim (the trainer expects relative-to-trainer-root paths and will
also accept either "data/baseline_filtered" or "baseline_filtered").

Used by run_training.sh to compute on-disk paths when generating per-day
baseline JSONs through the predictor.
"""
from __future__ import annotations

import argparse
import sys

from src import config as cfg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)

    c = cfg.load_config(args.config)
    sys.stdout.write((c.baseline_dir or "data/baseline") + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
