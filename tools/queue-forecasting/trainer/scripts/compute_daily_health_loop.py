"""Long-running wrapper around compute_daily_health.main.

Re-runs the detector on a trailing window every HEALTH_INTERVAL_SECONDS so the
queue_forecast_daily_health table stays fresh. UPSERTs make re-runs free.
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
import time

from scripts.compute_daily_health import main as run_once


def tick(today: _dt.date | None = None, lookback_days: int | None = None) -> int:
    """Run the detector once for a trailing window ending the day before `today`.

    Today is intentionally excluded: n_total is partial mid-day and would trip
    flag_volume_anomaly on every tick until midnight. Yesterday's late-arriving
    task resolutions are picked up on subsequent ticks via the rolling window
    (UPSERT semantics).
    """
    if today is None:
        today = _dt.datetime.now(_dt.timezone.utc).date()
    if lookback_days is None:
        lookback_days = int(os.environ.get("HEALTH_LOOKBACK_DAYS", "7"))
    from_d = today - _dt.timedelta(days=lookback_days)
    to_d = today  # exclusive upper bound — process [today-lookback, today)
    print(f"[health-loop] tick: --from {from_d} --to {to_d}", flush=True)
    return run_once(["--from", str(from_d), "--to", str(to_d)])


def loop_forever() -> int:
    interval = int(os.environ.get("HEALTH_INTERVAL_SECONDS", "3600"))
    lookback = int(os.environ.get("HEALTH_LOOKBACK_DAYS", "7"))
    print(f"[health-loop] starting: interval={interval}s lookback={lookback}d", flush=True)
    while True:
        try:
            rc = tick(lookback_days=lookback)
            if rc != 0:
                print(f"[health-loop] tick failed rc={rc}", file=sys.stderr, flush=True)
        except Exception as exc:  # noqa: BLE001 — long-running loop must not crash on transient errors
            print(f"[health-loop] tick raised: {exc!r}", file=sys.stderr, flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(loop_forever())
