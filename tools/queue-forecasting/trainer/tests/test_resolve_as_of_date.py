"""Tests for the effective as-of-date shell helper."""
from __future__ import annotations

from scripts.prepare_training_cache import main as prepare_main
from src.resolve_as_of_date import main as resolve_main


def test_resolve_as_of_date_uses_config_value(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("""
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

    assert resolve_main(["--config", str(config)]) == 0
    assert capsys.readouterr().out == "2026-04-24\n"


def test_resolve_as_of_date_honors_override(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("""
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

    assert resolve_main([
        "--config", str(config), "--as-of-date", "2026-05-01",
    ]) == 0
    assert capsys.readouterr().out == "2026-05-01\n"


def test_prepare_training_cache_forwards_date_and_refresh(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        "scripts.prepare_training_cache.cfg.load_config",
        lambda path, as_of_date_override=None: captured.update(
            path=path, as_of=as_of_date_override,
        ) or object(),
    )
    monkeypatch.setattr(
        "scripts.prepare_training_cache.data_loader.ensure_main_cache",
        lambda config, refresh_cache=False: captured.update(
            config=config, refresh=refresh_cache,
        ),
    )

    assert prepare_main([
        "--config", "configs/demo.yaml",
        "--as-of-date", "2026-05-01",
        "--refresh-cache",
    ]) == 0
    assert captured["path"] == "configs/demo.yaml"
    assert captured["as_of"] == "2026-05-01"
    assert captured["refresh"] is True
