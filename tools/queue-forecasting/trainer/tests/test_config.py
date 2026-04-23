from datetime import datetime, timezone, timedelta
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap

import pytest

from src import config as cfg


def _write(tmp_path, body):
    p = tmp_path / "c.yaml"
    p.write_text(textwrap.dedent(body))
    return p


def test_load_wait_time_config(tmp_path):
    path = _write(tmp_path, """
        target: wait_time
        target_column: wait_duration_s
        lookback_days: 14
        holdout_days: 5
        validation_days: 1
        as_of_date: 2026-04-24
        filters: []
        categorical_features: []
        numeric_features: []
        derived_features: {}
        model_type: lightgbm
        quantiles: [0.5, 0.9]
        model_params: {}
    """)
    c = cfg.load_config(path)
    assert c.target == "wait_time"
    assert c.lookback_days == 14
    assert c.as_of_date == datetime(2026, 4, 24, tzinfo=timezone.utc)


def test_null_as_of_date_resolves_to_today_utc_midnight(tmp_path, monkeypatch):
    path = _write(tmp_path, """
        target: wait_time
        target_column: wait_duration_s
        lookback_days: 14
        holdout_days: 5
        validation_days: 1
        as_of_date: null
        filters: []
        categorical_features: []
        numeric_features: []
        derived_features: {}
        model_type: lightgbm
        quantiles: [0.5, 0.9]
        model_params: {}
    """)

    fixed_now = datetime(2026, 4, 23, 14, 37, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(cfg, "_utcnow", lambda: fixed_now)

    c = cfg.load_config(path)
    assert c.as_of_date == datetime(2026, 4, 23, tzinfo=timezone.utc)


def test_compute_windows(tmp_path):
    path = _write(tmp_path, """
        target: wait_time
        target_column: wait_duration_s
        lookback_days: 14
        holdout_days: 5
        validation_days: 1
        as_of_date: 2026-04-24
        filters: []
        categorical_features: []
        numeric_features: []
        derived_features: {}
        model_type: lightgbm
        quantiles: [0.5]
        model_params: {}
    """)
    c = cfg.load_config(path)
    w = cfg.compute_windows(c)
    assert w.as_of_date   == datetime(2026, 4, 24, tzinfo=timezone.utc)
    assert w.hold_end     == datetime(2026, 4, 24, tzinfo=timezone.utc)
    assert w.hold_start   == datetime(2026, 4, 19, tzinfo=timezone.utc)
    assert w.val_end      == datetime(2026, 4, 19, tzinfo=timezone.utc)
    assert w.val_start    == datetime(2026, 4, 18, tzinfo=timezone.utc)
    assert w.train_end    == datetime(2026, 4, 18, tzinfo=timezone.utc)
    assert w.train_start  == datetime(2026, 4,  4, tzinfo=timezone.utc)


def test_holdout_day_starts(tmp_path):
    path = _write(tmp_path, """
        target: wait_time
        target_column: wait_duration_s
        lookback_days: 14
        holdout_days: 3
        validation_days: 1
        as_of_date: 2026-04-24
        filters: []
        categorical_features: []
        numeric_features: []
        derived_features: {}
        model_type: lightgbm
        quantiles: [0.5]
        model_params: {}
    """)
    c = cfg.load_config(path)
    days = cfg.holdout_day_starts(c)
    assert [d.strftime("%Y-%m-%d") for d in days] == [
        "2026-04-21", "2026-04-22", "2026-04-23",
    ]


def test_non_midnight_as_of_date_raises(tmp_path):
    for bad in ["2026-04-24T12:00:00Z", "2026-04-24T00:00:30Z"]:
        path = _write(tmp_path, f"""
            target: wait_time
            target_column: wait_duration_s
            lookback_days: 14
            holdout_days: 5
            validation_days: 1
            as_of_date: "{bad}"
            filters: []
            categorical_features: []
            numeric_features: []
            derived_features: {{}}
            model_type: lightgbm
            quantiles: [0.5]
            model_params: {{}}
        """)
        with pytest.raises(ValueError, match="UTC midnight"):
            cfg.load_config(path)


def test_midnight_as_of_date_passes(tmp_path):
    path = _write(tmp_path, """
        target: wait_time
        target_column: wait_duration_s
        lookback_days: 14
        holdout_days: 5
        validation_days: 1
        as_of_date: "2026-04-24T00:00:00Z"
        filters: []
        categorical_features: []
        numeric_features: []
        derived_features: {}
        model_type: lightgbm
        quantiles: [0.5]
        model_params: {}
    """)
    c = cfg.load_config(path)
    assert c.as_of_date == datetime(2026, 4, 24, tzinfo=timezone.utc)


def test_resolve_holdout_days_cli(tmp_path):
    path = _write(tmp_path, """
        target: wait_time
        target_column: wait_duration_s
        lookback_days: 14
        holdout_days: 3
        validation_days: 1
        as_of_date: 2026-04-24
        filters: []
        categorical_features: []
        numeric_features: []
        derived_features: {}
        model_type: lightgbm
        quantiles: [0.5]
        model_params: {}
    """)
    result = subprocess.run(
        [sys.executable, "-m", "src.resolve_holdout_days", "--config", str(path)],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["2026-04-21", "2026-04-22", "2026-04-23"]
