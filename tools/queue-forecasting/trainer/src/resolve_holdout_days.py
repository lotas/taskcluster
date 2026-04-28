"""Print one holdout day per line (YYYY-MM-DD) for shell consumption."""
from __future__ import annotations

import argparse
import sys

from src import config as cfg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--as-of-date", default=None,
                        help="Override as_of_date from config (UTC midnight of given day)")
    args = parser.parse_args(argv)

    c = cfg.load_config(args.config, as_of_date_override=args.as_of_date)
    for d in cfg.holdout_day_starts(c):
        sys.stdout.write(d.strftime("%Y-%m-%d") + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
