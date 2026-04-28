"""Unit tests for the resolve_excluded_dates CLI."""
from __future__ import annotations

import textwrap
from datetime import date

import pytest


def _write(tmp_path, body):
    p = tmp_path / "c.yaml"
    p.write_text(textwrap.dedent(body))
    return p


def _config_with(filter_block: str | None, tmp_path):
    # Each entry is at column 0 (no leading indent) so textwrap.dedent leaves
    # it alone; the optional filter_block is appended below in matching shape.
    base = (
        "target: wait_time\n"
        "target_column: wait_duration_s\n"
        "lookback_days: 14\n"
        "holdout_days: 5\n"
        "validation_days: 1\n"
        "as_of_date: 2026-04-24\n"
        "filters: []\n"
        "categorical_features: []\n"
        "numeric_features: []\n"
        "derived_features: {}\n"
        "model_type: lightgbm\n"
        "quantiles: [0.5]\n"
        "model_params: {}\n"
    )
    body = base + (filter_block + "\n" if filter_block else "")
    return _write(tmp_path, body)


def _run_cli(monkeypatch, capsys, config_path, fake_dates):
    """Patch load_anomalous_dates -> fake_dates and run main(), returning stdout."""
    from scripts import resolve_excluded_dates

    monkeypatch.setattr(
        resolve_excluded_dates.data_loader,
        "load_anomalous_dates",
        lambda c: set(fake_dates),
    )
    rc = resolve_excluded_dates.main(["--config", str(config_path)])
    assert rc == 0
    return capsys.readouterr().out


@pytest.mark.parametrize("mode_block,expect_lines", [
    # Mode=training is Policy A; baseline history must NOT be filtered.
    ("anomaly_filter:\n  enabled: true\n  mode: training",   []),
    # Mode=baseline is Policy B; print the dates.
    ("anomaly_filter:\n  enabled: true\n  mode: baseline",   ["2026-04-22", "2026-04-23"]),
    # Mode=both is Policy C; same dates printed.
    ("anomaly_filter:\n  enabled: true\n  mode: both",       ["2026-04-22", "2026-04-23"]),
    # Disabled or omitted: no output.
    ("anomaly_filter:\n  enabled: false\n  mode: baseline",  []),
    (None,                                                   []),
])
def test_resolve_excluded_dates_per_mode(tmp_path, monkeypatch, capsys, mode_block, expect_lines):
    cfg_path = _config_with(mode_block, tmp_path)
    fake = [date(2026, 4, 23), date(2026, 4, 22)]   # unsorted on purpose
    out = _run_cli(monkeypatch, capsys, cfg_path, fake)
    assert out.splitlines() == expect_lines


def test_resolve_excluded_dates_empty_db(tmp_path, monkeypatch, capsys):
    cfg_path = _config_with(
        "anomaly_filter:\n  enabled: true\n  mode: baseline", tmp_path)
    out = _run_cli(monkeypatch, capsys, cfg_path, [])
    assert out == ""
