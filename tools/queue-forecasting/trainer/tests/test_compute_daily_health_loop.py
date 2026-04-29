import datetime

import scripts.compute_daily_health_loop as loop


def test_tick_excludes_today(monkeypatch):
    """Today is partial — must NOT be in the processed window or volume_anomaly
    would falsely fire on every mid-day tick."""
    captured: list[list[str]] = []
    monkeypatch.setattr(loop, "run_once", lambda argv: captured.append(argv) or 0)
    rc = loop.tick(today=datetime.date(2026, 4, 28), lookback_days=7)
    assert rc == 0
    assert captured == [["--from", "2026-04-21", "--to", "2026-04-28"]]


def test_tick_reads_env_lookback(monkeypatch):
    captured: list[list[str]] = []
    monkeypatch.setattr(loop, "run_once", lambda argv: captured.append(argv) or 0)
    monkeypatch.setenv("HEALTH_LOOKBACK_DAYS", "3")
    loop.tick(today=datetime.date(2026, 4, 28))
    assert captured == [["--from", "2026-04-25", "--to", "2026-04-28"]]


def test_tick_propagates_nonzero_rc(monkeypatch):
    monkeypatch.setattr(loop, "run_once", lambda argv: 7)
    assert loop.tick(today=datetime.date(2026, 4, 28), lookback_days=1) == 7
