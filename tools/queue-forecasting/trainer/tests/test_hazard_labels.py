import math

import numpy as np
import pandas as pd
import pytest

from src.hazard_labels import bin_edges_seconds, bin_index_containing, DEFAULT_BIN_EDGES_MINUTES
from src.hazard_labels import determine_fates, FATE_STARTED, FATE_RESOLVED_NO_START, FATE_CENSORED
from src.hazard_labels import build_bin_risk_and_labels


def test_default_bin_edges_minutes_shape():
    # 0, 5, 15, 30, 60, 120, 240, 480, +inf -- 8 finite boundaries + open terminal bin = 8 bins
    assert DEFAULT_BIN_EDGES_MINUTES[0] == 0
    assert DEFAULT_BIN_EDGES_MINUTES[-1] == math.inf
    assert len(DEFAULT_BIN_EDGES_MINUTES) == 9  # 9 edges -> 8 bins


def test_bin_edges_seconds_converts_minutes_to_seconds():
    edges = bin_edges_seconds([0, 5, 15, math.inf])
    assert edges.tolist() == [0.0, 300.0, 900.0, math.inf]


def test_bin_edges_seconds_rejects_nonzero_first_edge():
    with pytest.raises(ValueError, match="first bin edge must be 0"):
        bin_edges_seconds([5, 15, math.inf])


def test_bin_edges_seconds_rejects_non_open_terminal_edge():
    with pytest.raises(ValueError, match="math.inf"):
        bin_edges_seconds([0, 5, 15])


def test_bin_edges_seconds_rejects_non_increasing_edges():
    with pytest.raises(ValueError, match="strictly increasing"):
        bin_edges_seconds([0, 15, 5, math.inf])


def test_bin_index_containing_basic():
    edges = bin_edges_seconds([0, 5, 15, 30, math.inf])  # bins: [0,5) [5,15) [15,30) [30,+inf)
    elapsed = np.array([0.0, 4.99 * 60, 5 * 60, 29 * 60, 30 * 60, 10_000 * 60])
    idx = bin_index_containing(elapsed, edges)
    assert idx.tolist() == [0, 0, 1, 2, 3, 3]


def test_bin_index_containing_rejects_negative_elapsed():
    edges = bin_edges_seconds([0, 5, math.inf])
    with pytest.raises(ValueError, match=">= 0"):
        bin_index_containing(np.array([-1.0]), edges)


def test_bin_index_containing_rejects_non_finite_elapsed():
    edges = bin_edges_seconds([0, 5, math.inf])
    with pytest.raises(ValueError, match="finite"):
        bin_index_containing(np.array([np.inf]), edges)
    with pytest.raises(ValueError, match="finite"):
        bin_index_containing(np.array([np.nan]), edges)


def test_bin_edges_seconds_rejects_duplicate_adjacent_edges():
    with pytest.raises(ValueError, match="strictly increasing"):
        bin_edges_seconds([0, 5, 5, math.inf])


CUTOFF = pd.Timestamp("2026-07-01T00:00:00Z")


def _series(pending_at, resolved_at, wait_duration_s):
    return (
        pd.Series(pd.to_datetime(pending_at, utc=True)),
        pd.Series(pd.to_datetime(resolved_at, utc=True)),
        pd.Series(wait_duration_s, dtype=float),
    )


def test_started_before_cutoff():
    pending, resolved, wait = _series(
        ["2026-06-30T00:00:00Z"], [pd.NaT], [3600.0]  # started 1h after pending, well before cutoff
    )
    fate_kind, elapsed_s = determine_fates(pending, resolved, wait, CUTOFF)
    assert fate_kind.tolist() == [FATE_STARTED]
    assert elapsed_s.tolist() == [3600.0]


def test_started_after_cutoff_is_censored_at_cutoff_not_started():
    """The exact bug flagged in review: a row whose true started_at is after
    the split cutoff (but before the global as_of_date) must NOT be labeled
    as a started event using its real elapsed wait -- it must be
    right-censored at cutoff instead."""
    pending_at = pd.Timestamp("2026-06-30T00:00:00Z")
    # wait_duration_s implies started_at = pending_at + 2 days = 2026-07-02,
    # which is AFTER CUTOFF (2026-07-01). Must be ignored.
    wait_duration_s = 2 * 24 * 3600.0
    pending, resolved, wait = _series([pending_at], [pd.NaT], [wait_duration_s])
    fate_kind, elapsed_s = determine_fates(pending, resolved, wait, CUTOFF)
    assert fate_kind.tolist() == [FATE_CENSORED]
    expected_elapsed = (CUTOFF - pending_at).total_seconds()
    assert elapsed_s.tolist() == [expected_elapsed]


def test_resolved_without_starting_before_cutoff():
    pending_at = pd.Timestamp("2026-06-30T00:00:00Z")
    resolved_at = pending_at + pd.Timedelta(minutes=10)  # e.g. canceled 10m after pending
    pending, resolved, wait = _series([pending_at], [resolved_at], [np.nan])
    fate_kind, elapsed_s = determine_fates(pending, resolved, wait, CUTOFF)
    assert fate_kind.tolist() == [FATE_RESOLVED_NO_START]
    assert elapsed_s.tolist() == [600.0]


def test_resolved_without_starting_after_cutoff_is_censored_at_cutoff():
    """Same leak-guard as the started case, for the competing-risk fate."""
    pending_at = pd.Timestamp("2026-06-30T00:00:00Z")
    resolved_at = pending_at + pd.Timedelta(days=2)  # after CUTOFF
    pending, resolved, wait = _series([pending_at], [resolved_at], [np.nan])
    fate_kind, elapsed_s = determine_fates(pending, resolved, wait, CUTOFF)
    assert fate_kind.tolist() == [FATE_CENSORED]
    expected_elapsed = (CUTOFF - pending_at).total_seconds()
    assert elapsed_s.tolist() == [expected_elapsed]


def test_genuinely_still_pending_is_censored():
    pending_at = pd.Timestamp("2026-06-30T12:00:00Z")
    pending, resolved, wait = _series([pending_at], [pd.NaT], [np.nan])
    fate_kind, elapsed_s = determine_fates(pending, resolved, wait, CUTOFF)
    assert fate_kind.tolist() == [FATE_CENSORED]
    expected_elapsed = (CUTOFF - pending_at).total_seconds()
    assert elapsed_s.tolist() == [expected_elapsed]


def test_started_and_later_resolved_is_still_fate_started():
    """The normal happy-path row (started, then resolved) must be fate
    'started' -- resolved_at is irrelevant to the wait hazard once the row
    has genuinely started."""
    pending_at = pd.Timestamp("2026-06-30T00:00:00Z")
    resolved_at = pending_at + pd.Timedelta(hours=2)
    pending, resolved, wait = _series([pending_at], [resolved_at], [1800.0])
    fate_kind, elapsed_s = determine_fates(pending, resolved, wait, CUTOFF)
    assert fate_kind.tolist() == [FATE_STARTED]
    assert elapsed_s.tolist() == [1800.0]


def test_mixed_batch_all_three_fates_at_once():
    pending_ats = pd.to_datetime([
        "2026-06-30T00:00:00Z",  # started well before cutoff
        "2026-06-30T00:00:00Z",  # resolved without starting, before cutoff
        "2026-06-30T00:00:00Z",  # still pending
    ], utc=True)
    resolved_ats = pd.to_datetime([pd.NaT, "2026-06-30T00:10:00Z", pd.NaT], utc=True)
    waits = [600.0, np.nan, np.nan]
    fate_kind, elapsed_s = determine_fates(
        pd.Series(pending_ats), pd.Series(resolved_ats), pd.Series(waits), CUTOFF
    )
    assert fate_kind.tolist() == [FATE_STARTED, FATE_RESOLVED_NO_START, FATE_CENSORED]
    assert elapsed_s[0] == 600.0
    assert elapsed_s[1] == 600.0
    assert elapsed_s[2] == (CUTOFF - pending_ats[2]).total_seconds()


def test_cutoff_must_be_timezone_aware():
    pending, resolved, wait = _series(["2026-06-30T00:00:00Z"], [pd.NaT], [np.nan])
    with pytest.raises(ValueError, match="timezone-aware"):
        determine_fates(pending, resolved, wait, pd.Timestamp("2026-07-01T00:00:00"))


def test_cutoff_must_be_after_every_pending_at():
    pending, resolved, wait = _series(["2026-07-02T00:00:00Z"], [pd.NaT], [np.nan])
    with pytest.raises(ValueError, match="strictly after"):
        determine_fates(pending, resolved, wait, CUTOFF)


def test_negative_wait_duration_rejected():
    pending, resolved, wait = _series(["2026-06-30T00:00:00Z"], [pd.NaT], [-5.0])
    with pytest.raises(ValueError, match="wait_duration_s must be >= 0"):
        determine_fates(pending, resolved, wait, CUTOFF)


SMALL_EDGES_MIN = [0, 5, 15, 30, math.inf]  # 4 bins: [0,5) [5,15) [15,30) [30,+inf)


def test_started_fate_matrix():
    """Started at elapsed=600s (10m) -> bin index 1 ([5,15)).
    At risk for bins 0,1; label 0 for bin 0, 1 for bin 1; bins 2,3 excluded."""
    pending, resolved, wait = _series(["2026-06-30T00:00:00Z"], [pd.NaT], [600.0])
    at_risk, label = build_bin_risk_and_labels(pending, resolved, wait, CUTOFF, SMALL_EDGES_MIN)
    assert at_risk.shape == (1, 4)
    assert at_risk[0].tolist() == [True, True, False, False]
    assert label[0, 0] == 0.0
    assert label[0, 1] == 1.0
    assert math.isnan(label[0, 2])
    assert math.isnan(label[0, 3])


def test_resolved_no_start_fate_matrix():
    """Resolved-without-starting at elapsed=600s -> bin index 1.
    At risk for bins 0,1, labeled 0 for BOTH (never starts, even in its own
    bin) -- unlike the started fate, no bin gets label 1."""
    pending_at = pd.Timestamp("2026-06-30T00:00:00Z")
    resolved_at = pending_at + pd.Timedelta(seconds=600)
    pending, resolved, wait = _series([pending_at], [resolved_at], [np.nan])
    at_risk, label = build_bin_risk_and_labels(pending, resolved, wait, CUTOFF, SMALL_EDGES_MIN)
    assert at_risk[0].tolist() == [True, True, False, False]
    assert label[0, 0] == 0.0
    assert label[0, 1] == 0.0
    assert math.isnan(label[0, 2])
    assert math.isnan(label[0, 3])


def test_censored_fate_matrix_excludes_containing_bin():
    """Censored (still pending) at elapsed=600s -> bin index 1.
    At risk ONLY for bin 0 (fully survived below it), label 0. Bin 1 (the
    one it's currently inside) is excluded entirely -- not at-risk, no
    label -- since we don't yet know if it'll start within that bin."""
    pending_at = pd.Timestamp("2026-06-30T00:00:00Z")
    cutoff = pending_at + pd.Timedelta(seconds=600)
    pending, resolved, wait = _series([pending_at], [pd.NaT], [np.nan])
    at_risk, label = build_bin_risk_and_labels(pending, resolved, wait, cutoff, SMALL_EDGES_MIN)
    assert at_risk[0].tolist() == [True, False, False, False]
    assert label[0, 0] == 0.0
    assert math.isnan(label[0, 1])
    assert math.isnan(label[0, 2])
    assert math.isnan(label[0, 3])


def test_censored_fate_at_elapsed_zero_contributes_to_no_bin():
    """A row censored at elapsed=0 (just pended, checked immediately) is
    inside bin 0 with nothing survived yet -- it must not be at-risk for
    ANY bin, not even bin 0."""
    pending_at = pd.Timestamp("2026-06-30T00:00:00Z")
    # determine_fates requires cutoff strictly after pending_at; use a cutoff
    # a hair after it so elapsed is effectively ~0.
    cutoff = pending_at + pd.Timedelta(microseconds=1)
    pending, resolved, wait = _series([pending_at], [pd.NaT], [np.nan])
    at_risk, label = build_bin_risk_and_labels(pending, resolved, wait, cutoff, SMALL_EDGES_MIN)
    assert at_risk[0].tolist() == [False, False, False, False]
    assert np.isnan(label[0]).all()


def test_mixed_batch_matrix_matches_design_doc_narrative():
    """Integration test across all three fates in one call, mirroring the
    design doc's three-fate description end to end."""
    pending_ats = pd.to_datetime([
        "2026-06-30T00:00:00Z",  # starts at 20m -> bin 2 ([15,30))
        "2026-06-30T00:00:00Z",  # resolves without starting at 2m -> bin 0
        "2026-06-30T00:00:00Z",  # still pending at 45m -> bin 3 (terminal, open)
    ], utc=True)
    resolved_ats = pd.to_datetime([pd.NaT, "2026-06-30T00:02:00Z", pd.NaT], utc=True)
    waits = [20 * 60.0, np.nan, np.nan]
    cutoff = pd.Timestamp("2026-06-30T00:45:00Z")

    at_risk, label = build_bin_risk_and_labels(
        pd.Series(pending_ats), pd.Series(resolved_ats), pd.Series(waits), cutoff, SMALL_EDGES_MIN
    )

    # Row 0: started in bin 2 -> at risk 0,1,2; labels 0,0,1; bin 3 excluded.
    assert at_risk[0].tolist() == [True, True, True, False]
    assert label[0].tolist()[:3] == [0.0, 0.0, 1.0]
    assert math.isnan(label[0, 3])

    # Row 1: resolved-without-start in bin 0 -> at risk 0 only; label 0.
    assert at_risk[1].tolist() == [True, False, False, False]
    assert label[1, 0] == 0.0

    # Row 2: censored at 45m, which is past the last finite edge (30m) ->
    # containing bin is the terminal bin (index 3). At risk for bins 0,1,2
    # (fully survived), bin 3 excluded (currently inside it, unresolved).
    assert at_risk[2].tolist() == [True, True, True, False]
    assert label[2].tolist()[:3] == [0.0, 0.0, 0.0]
    assert math.isnan(label[2, 3])
