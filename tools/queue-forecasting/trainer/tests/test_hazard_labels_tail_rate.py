import math

import numpy as np
import pandas as pd
import pytest

from src.hazard_labels import fit_exponential_tail_rate

EDGES = [0, 5, math.inf]  # 2 bins: [0,5min) and [5min,+inf); t_last = 300s
CUTOFF = pd.Timestamp("2026-07-01T00:00:00Z")


def _series(pending_at, resolved_at, wait_duration_s):
    return (
        pd.Series(pd.to_datetime(pending_at, utc=True)),
        pd.Series(pd.to_datetime(resolved_at, utc=True)),
        pd.Series(wait_duration_s, dtype=float),
    )


def test_zero_events_returns_zero():
    """No row reaches the terminal bin at all -- degenerate fit."""
    pending, resolved, wait = _series(
        ["2026-06-30T00:00:00Z", "2026-06-30T00:00:00Z"], [pd.NaT, pd.NaT], [60.0, 120.0],
    )
    rate = fit_exponential_tail_rate(pending, resolved, wait, CUTOFF, EDGES)
    assert rate == 0.0


def test_hand_computed_mle_value():
    """Two started-in-terminal-bin events (extra time 100s, 300s) plus one
    resolved-without-start-in-terminal-bin censored row (extra time 500s):
    rate = events / total_person_time = 2 / (100+300+500) = 2/900."""
    pending_at = pd.Timestamp("2026-06-30T00:00:00Z")
    pending = pd.Series([pending_at] * 3)
    resolved = pd.Series([pd.NaT, pd.NaT, pending_at + pd.Timedelta(seconds=300 + 500)])
    wait = pd.Series([300.0 + 100.0, 300.0 + 300.0, np.nan])
    rate = fit_exponential_tail_rate(pending, resolved, wait, CUTOFF, EDGES)
    assert rate == pytest.approx(2.0 / 900.0)


def test_censored_row_outside_terminal_bin_does_not_contribute():
    """A resolved-without-start row whose elapsed time never reaches the
    terminal bin (t_last=300s) must not be counted -- only one genuine
    terminal-bin event should contribute."""
    pending_at = pd.Timestamp("2026-06-30T00:00:00Z")
    pending = pd.Series([pending_at, pending_at])
    resolved = pd.Series([pd.NaT, pending_at + pd.Timedelta(seconds=60)])  # resolves at 60s, well before t_last
    wait = pd.Series([300.0 + 200.0, np.nan])  # row 0: started 200s into the terminal bin
    rate = fit_exponential_tail_rate(pending, resolved, wait, CUTOFF, EDGES)
    assert rate == pytest.approx(1.0 / 200.0)


def test_started_after_cutoff_excluded_from_tail_fit():
    """Leak-guard: a row whose real start happens after cutoff must not
    contribute to the tail-rate fit, even though it's a genuine
    terminal-bin event in the raw data."""
    pending_at = pd.Timestamp("2026-06-30T00:00:00Z")
    cutoff = pending_at + pd.Timedelta(seconds=300 + 50)  # cutoff lands inside the terminal bin
    pending = pd.Series([pending_at])
    resolved = pd.Series([pd.NaT])
    wait = pd.Series([300.0 + 200.0])  # real start is 200s into the terminal bin, i.e. after cutoff
    rate = fit_exponential_tail_rate(pending, resolved, wait, cutoff, EDGES)
    # The row falls through to censored-at-cutoff (elapsed = 350s, still in
    # the terminal bin, contributes no event and is excluded from person-time
    # per the "censored rows excluded from their own containing bin" rule).
    assert rate == 0.0
