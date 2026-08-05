from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _floor_to_utc_midnight(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass
class Windows:
    as_of_date: datetime
    train_start: datetime
    train_end: datetime
    val_start: datetime
    val_end: datetime
    hold_start: datetime
    hold_end: datetime


@dataclass
class Config:
    target: str
    target_column: str
    lookback_days: int
    holdout_days: int
    validation_days: int
    as_of_date: datetime
    filters: list[str]
    categorical_features: list[str]
    numeric_features: list[str]
    derived_features: dict[str, Any]
    model_type: str
    quantiles: list[float]
    model_params: dict[str, Any]
    residual: dict[str, Any] | None = None
    velocity_features: dict[str, Any] | None = None
    throughput_features: dict[str, Any] | None = None
    queue_context_features: dict[str, Any] | None = None
    anomaly_filter: dict[str, Any] | None = None
    baseline_dir: str | None = None
    baseline_features: dict[str, Any] | None = None
    hazard_bins_minutes: list[float] | None = None
    source_path: Path = field(default_factory=Path)


def load_config(
    path: str | Path,
    *,
    as_of_date_override: str | datetime | None = None,
) -> Config:
    p = Path(path)
    with p.open() as fh:
        raw = yaml.safe_load(fh)

    raw_as_of = raw.get("as_of_date")
    if as_of_date_override is not None:
        raw_as_of = as_of_date_override
    as_of_date = _resolve_as_of_date(raw_as_of)

    _validate_model_type_target(raw)

    return Config(
        target=raw["target"],
        target_column=raw["target_column"],
        lookback_days=int(raw["lookback_days"]),
        holdout_days=int(raw["holdout_days"]),
        validation_days=int(raw["validation_days"]),
        as_of_date=as_of_date,
        filters=list(raw.get("filters") or []),
        categorical_features=list(raw.get("categorical_features") or []),
        numeric_features=list(raw.get("numeric_features") or []),
        derived_features=dict(raw.get("derived_features") or {}),
        model_type=raw["model_type"],
        quantiles=list(raw["quantiles"]),
        model_params=dict(raw.get("model_params") or {}),
        residual=raw.get("residual"),
        velocity_features=raw.get("velocity_features"),
        throughput_features=raw.get("throughput_features"),
        queue_context_features=raw.get("queue_context_features"),
        anomaly_filter=raw.get("anomaly_filter"),
        baseline_dir=raw.get("baseline_dir"),
        baseline_features=raw.get("baseline_features"),
        hazard_bins_minutes=(
            [float(x) for x in raw["hazard_bins_minutes"]]
            if raw.get("hazard_bins_minutes") is not None else None
        ),
        source_path=p,
    )


def _validate_model_type_target(raw: dict[str, Any]) -> None:
    """discrete_hazard is wait-only, by design and by implementation.

    Its whole fate/censoring model is wait-specific (pending_at -> started_at,
    with "resolved without ever starting" as the competing risk), and
    train.py's _run_discrete_hazard_training hardcodes the wait baseline
    columns and wait bucket breakdown. A `target: run_duration` hazard config
    would train without complaint and then be scored against wait baselines
    -- wrong numbers that look right. Reject it here, before any query runs,
    rather than at the far end of a training run.
    """
    if raw.get("model_type") != "discrete_hazard":
        return
    target, target_column = raw.get("target"), raw.get("target_column")
    if target != "wait_time" or target_column != "wait_duration_s":
        raise ValueError(
            f"model_type: discrete_hazard supports only target: wait_time / "
            f"target_column: wait_duration_s; got target: {target!r} / "
            f"target_column: {target_column!r}. The hazard model's censoring "
            f"semantics and its evaluation path are both wait-specific "
            f"(see bet2-hazard-survival-design.md)."
        )


def _resolve_as_of_date(value: Any) -> datetime:
    if value is None:
        return _floor_to_utc_midnight(_utcnow())
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    else:
        # YAML also decodes date-only as `datetime.date`
        from datetime import date as _date
        if isinstance(value, _date):
            return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
        if isinstance(value, str):
            s = value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        else:
            raise ValueError(f"Unparseable as_of_date: {value!r}")

    # Enforce whole-UTC-day invariant: partial-day holdouts break per-day
    # baseline aggregation (baseline keys are YYYY-MM-DD, not sub-day).
    dt_utc = dt.astimezone(timezone.utc)
    if (dt_utc.hour, dt_utc.minute, dt_utc.second, dt_utc.microsecond) != (0, 0, 0, 0):
        raise ValueError(
            f"as_of_date must be UTC midnight (e.g. 2026-04-24 or 2026-04-24T00:00:00Z); "
            f"got {value!r} which resolves to {dt_utc.isoformat()}. "
            f"Partial-day holdouts break per-day baseline aggregation."
        )
    return dt_utc


def compute_windows(c: Config) -> Windows:
    from datetime import timedelta
    A = c.as_of_date
    H = c.holdout_days
    V = c.validation_days
    L = c.lookback_days

    hold_end    = A
    hold_start  = A - timedelta(days=H)
    val_end     = hold_start
    val_start   = val_end - timedelta(days=V)
    train_end   = val_start
    train_start = train_end - timedelta(days=L)

    return Windows(
        as_of_date=A,
        train_start=train_start, train_end=train_end,
        val_start=val_start,     val_end=val_end,
        hold_start=hold_start,   hold_end=hold_end,
    )


def holdout_day_starts(c: Config) -> list[datetime]:
    """Return list of per-day start instants inside the holdout window."""
    from datetime import timedelta
    w = compute_windows(c)
    out = []
    d = w.hold_start
    while d < w.hold_end:
        out.append(d)
        d += timedelta(days=1)
    return out
