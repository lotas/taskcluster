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


def test_still_pending_row_contributes_its_person_time():
    """A row still pending inside the terminal bin is right-censored, not
    absent: it contributes exposure (no event) to the exponential MLE.
    Dropping its person-time would inflate the rate and shorten every
    deep-tail prediction -- the exact survivorship bias Bet 2 exists to fix.

    Row A starts 100s into the terminal bin; row B is still pending 400s
    into it. rate = 1 event / (100 + 400)s person-time."""
    pending = pd.Series([CUTOFF - pd.Timedelta(seconds=1000), CUTOFF - pd.Timedelta(seconds=700)])
    resolved = pd.Series([pd.NaT, pd.NaT])
    wait = pd.Series([300.0 + 100.0, np.nan])  # row B never started
    rate = fit_exponential_tail_rate(pending, resolved, wait, CUTOFF, EDGES)
    assert rate == pytest.approx(1.0 / 500.0)


def test_still_pending_row_short_of_terminal_bin_contributes_nothing():
    """Censored rows only count once they actually reach the terminal bin --
    a row pending just 200s has no exposure past t_last=300s."""
    pending = pd.Series([CUTOFF - pd.Timedelta(seconds=1000), CUTOFF - pd.Timedelta(seconds=200)])
    resolved = pd.Series([pd.NaT, pd.NaT])
    wait = pd.Series([300.0 + 100.0, np.nan])
    rate = fit_exponential_tail_rate(pending, resolved, wait, CUTOFF, EDGES)
    assert rate == pytest.approx(1.0 / 100.0)


def test_mle_is_unbiased_under_heavy_censoring():
    """The property that matters: with a known exponential tail and most
    terminal-bin rows still pending, the fitted rate must recover the truth.
    Excluding censored person-time inflates this by >3x at this censoring
    level, which would turn a true ~55h deep-tail quantile into ~15h."""
    rng = np.random.default_rng(0)
    n = 20_000
    t_last = 300.0
    true_rate = 1.0 / 1800.0
    # Observation window is short relative to the mean tail wait, so most
    # terminal-bin rows are still pending at cutoff.
    age_s = t_last + rng.uniform(0.0, 1800.0, size=n)
    pending = pd.Series(CUTOFF - pd.to_timedelta(age_s, unit="s"))
    true_wait = t_last + rng.exponential(1.0 / true_rate, size=n)
    started = true_wait <= age_s
    wait = pd.Series(np.where(started, true_wait, np.nan))
    resolved = pd.Series([pd.NaT] * n)

    assert (~started).mean() > 0.5, "test setup must be heavily censored"
    rate = fit_exponential_tail_rate(pending, resolved, wait, CUTOFF, EDGES)
    assert rate == pytest.approx(true_rate, rel=0.05)


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
    # The row falls through to censored-at-cutoff (elapsed = 350s). It still
    # contributes its 50s of terminal-bin person-time, but contributes no
    # event -- so the fit is degenerate and returns 0.0 rather than crediting
    # a start the split was never allowed to see.
    assert rate == 0.0
