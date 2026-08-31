"""Materialize one cohort's main SQL cache, then exit.

The separate process boundary is intentional: a cold SQL fetch of millions of
rows leaves enough retained heap that combining it with the canonical sort and
baseline join can exceed the trainer cgroup. The real training process starts
after this command exits and takes the compact Parquet cache-hit path.
"""
from __future__ import annotations

import argparse

from src import config as cfg
from src import data_loader


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--as-of-date", default=None)
    parser.add_argument("--refresh-cache", action="store_true")
    args = parser.parse_args(argv)

    c = cfg.load_config(args.config, as_of_date_override=args.as_of_date)
    data_loader.ensure_main_cache(c, refresh_cache=args.refresh_cache)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
