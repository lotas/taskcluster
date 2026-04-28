from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
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


def test_config_has_no_residual_by_default(tmp_path):
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
    assert c.residual is None


def test_config_parses_residual_block(tmp_path):
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
        residual:
          baseline_file: baseline_predictions.ndjson
          baseline_feature: bl_wait_p50
          transform: log_ratio
    """)
    c = cfg.load_config(path)
    assert c.residual == {
        "baseline_file": "baseline_predictions.ndjson",
        "baseline_feature": "bl_wait_p50",
        "transform": "log_ratio",
    }


def test_config_parses_velocity_features(tmp_path):
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
        velocity_features:
          enabled: true
          trailing_windows_minutes: [60, 240]
          tolerance_minutes: 5
    """)
    c = cfg.load_config(path)
    assert c.velocity_features == {
        "enabled": True,
        "trailing_windows_minutes": [60, 240],
        "tolerance_minutes": 5,
    }


def test_config_velocity_features_default_none(tmp_path):
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
    assert c.velocity_features is None


def test_config_parses_throughput_features(tmp_path):
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
        throughput_features:
          enabled: true
          windows_minutes: [15, 60]
    """)
    c = cfg.load_config(path)
    assert c.throughput_features == {
        "enabled": True,
        "windows_minutes": [15, 60],
    }


def test_config_throughput_features_default_none(tmp_path):
    # Uses an existing test YAML without the block — should be None.
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
    assert c.throughput_features is None


def test_load_config_as_of_date_override(tmp_path):
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
    c = cfg.load_config(path, as_of_date_override="2026-04-20")
    assert c.as_of_date == datetime(2026, 4, 20, tzinfo=timezone.utc)


def test_load_config_override_with_datetime_object(tmp_path):
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
    c = cfg.load_config(path, as_of_date_override=datetime(2026, 4, 19, tzinfo=timezone.utc))
    assert c.as_of_date == datetime(2026, 4, 19, tzinfo=timezone.utc)


def test_config_parses_anomaly_filter(tmp_path):
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
        anomaly_filter:
          enabled: true
          mode: training
    """)
    c = cfg.load_config(path)
    assert c.anomaly_filter == {"enabled": True, "mode": "training"}


def test_config_anomaly_filter_default_none(tmp_path):
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
    assert c.anomaly_filter is None


def test_config_parses_baseline_dir(tmp_path):
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
        baseline_dir: data/baseline_filtered
    """)
    c = cfg.load_config(path)
    assert c.baseline_dir == "data/baseline_filtered"


def test_config_baseline_dir_default_none(tmp_path):
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
    assert c.baseline_dir is None


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
