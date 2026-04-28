#!/usr/bin/env python3
"""Print one anomalous (excluded) date per line for shell consumption.

Output is empty when:
- the config has no anomaly_filter block,
- anomaly_filter.enabled is false, or
- anomaly_filter.mode is not "baseline" or "both" (e.g. mode=="training",
  the Policy A case, where the predictor.js baseline history is unaffected).

Otherwise, queries queue_forecast_daily_health via data_loader.load_anomalous_dates
and prints each flagged date as YYYY-MM-DD on its own line, sorted ascending.

The --as-of-date flag is accepted (but unused) so this script can be
invoked through the same flag-forwarding path as resolve_holdout_days.
"""
from __future__ import annotations

import argparse
import sys

from src import config as cfg
from src import data_loader


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--as-of-date", default=None,
                        help="Accepted for parity with resolve_holdout_days; unused.")
    args = parser.parse_args(argv)

    c = cfg.load_config(args.config, as_of_date_override=args.as_of_date)

    af = c.anomaly_filter
    if not af or not af.get("enabled"):
        return 0
    mode = af.get("mode", "training")
    if mode not in ("baseline", "both"):
        return 0

    anomalous_dates = data_loader.load_anomalous_dates(c)
    for d in sorted(anomalous_dates):
        sys.stdout.write(d.strftime("%Y-%m-%d") + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
