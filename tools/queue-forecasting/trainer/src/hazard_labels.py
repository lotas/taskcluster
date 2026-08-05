"""Discrete-time hazard label construction for Bet 2 (wait-time hazard model).

Implements the fate/censoring rules from bet2-hazard-survival-design.md's
"Training-set construction" section: every row gets exactly one of three
fates (started / resolved-without-starting / censored) as of its split's
own cutoff, and only events at or before that cutoff are honored -- events
after cutoff are ignored so a row falls through to censored-at-cutoff
instead of leaking its real future outcome into the label.
"""
from __future__ import annotations

import math
from typing import Sequence

import numpy as np
import pandas as pd

# 0-5m, 5-15m, 15-30m, 30-60m, 60-120m, 120-240m, 240-480m, 480m+ (open terminal bin).
DEFAULT_BIN_EDGES_MINUTES: tuple[float, ...] = (0, 5, 15, 30, 60, 120, 240, 480, math.inf)

FATE_STARTED = "started"
FATE_RESOLVED_NO_START = "resolved_no_start"
FATE_CENSORED = "censored"


def bin_edges_seconds(edges_minutes: Sequence[float] = DEFAULT_BIN_EDGES_MINUTES) -> np.ndarray:
    """Convert bin-boundary minutes to seconds.

    First edge must be 0, last edge must be math.inf (the open-ended
    terminal bin -- see the design doc's terminal-bin tail policy for why
    the *model* still gives it a finite shape; this function only handles
    bin-edge bookkeeping).
    """
    edges = list(edges_minutes)
    if edges[0] != 0:
        raise ValueError(f"first bin edge must be 0, got {edges[0]!r}")
    if edges[-1] != math.inf:
        raise ValueError(f"last bin edge must be math.inf (open-ended terminal bin), got {edges[-1]!r}")
    if any(b <= a for a, b in zip(edges, edges[1:])):
        raise ValueError(f"bin edges must be strictly increasing: {edges!r}")
    return np.array([e * 60.0 if math.isfinite(e) else math.inf for e in edges], dtype=np.float64)


def bin_index_containing(elapsed_s: np.ndarray, edges_s: np.ndarray) -> np.ndarray:
    """Index i such that edges_s[i] <= elapsed_s < edges_s[i+1], vectorized.

    Any elapsed_s at or past the last finite edge lands in the open
    terminal bin (index n_bins - 1).
    """
    elapsed_s = np.asarray(elapsed_s, dtype=np.float64)
    if np.any(elapsed_s < 0):
        raise ValueError("elapsed_s must be >= 0")
    if np.any(~np.isfinite(elapsed_s)):
        raise ValueError("elapsed_s must be finite")
    n_bins = len(edges_s) - 1
    idx = np.searchsorted(edges_s, elapsed_s, side="right") - 1
    return np.clip(idx, 0, n_bins - 1)


def determine_fates(
    pending_at: pd.Series,
    resolved_at: pd.Series,
    wait_duration_s: pd.Series,
    cutoff: pd.Timestamp,
) -> tuple[np.ndarray, np.ndarray]:
    """Determine each row's fate as of `cutoff`, honoring only events at or
    before it -- an event that truly happens after cutoff (but is already
    sitting in the database because the query bound is a later, global
    as_of_date) is ignored, and the row falls through to censored-at-cutoff.
    This is the per-split leakage guard from bet2-hazard-survival-design.md.

    Returns (fate_kind, elapsed_s):
      fate_kind: object array of "started" | "resolved_no_start" | "censored"
      elapsed_s: float64 array -- wait_duration_s for "started",
                 (resolved_at - pending_at) for "resolved_no_start",
                 (cutoff - pending_at) for "censored"
    """
    cutoff = pd.Timestamp(cutoff)
    if cutoff.tzinfo is None:
        raise ValueError("cutoff must be timezone-aware")

    pending_at = pd.to_datetime(pd.Series(pending_at), utc=True).reset_index(drop=True)
    resolved_at = pd.to_datetime(pd.Series(resolved_at), utc=True).reset_index(drop=True)
    wait_duration_s = pd.Series(wait_duration_s).astype(float).reset_index(drop=True)

    n = len(pending_at)
    if not (len(resolved_at) == n and len(wait_duration_s) == n):
        raise ValueError("pending_at, resolved_at, wait_duration_s must be the same length")
    if (pending_at >= cutoff).any():
        raise ValueError("cutoff must be strictly after every row's pending_at")
    if (wait_duration_s.dropna() < 0).any():
        raise ValueError("wait_duration_s must be >= 0 where not null")

    censored_elapsed = (cutoff - pending_at).dt.total_seconds().to_numpy()
    fate_kind = np.full(n, FATE_CENSORED, dtype=object)
    elapsed_s = censored_elapsed.copy()

    started_known = wait_duration_s.notna()
    resolved_known = resolved_at.notna()

    # Fate: resolved without ever starting, at or before cutoff. Mutually
    # exclusive with the "started" mask below by construction (requires
    # wait_duration_s null), so assignment order between the two doesn't
    # matter for correctness.
    is_resolved_no_start = (~started_known) & resolved_known & (resolved_at <= cutoff)
    resolved_no_start_np = is_resolved_no_start.to_numpy()
    fate_kind[resolved_no_start_np] = FATE_RESOLVED_NO_START
    elapsed_s[resolved_no_start_np] = (
        (resolved_at - pending_at).dt.total_seconds().to_numpy()[resolved_no_start_np]
    )

    # Fate: started at or before cutoff.
    implied_started_at = pending_at + pd.to_timedelta(wait_duration_s, unit="s")
    is_started = started_known & (implied_started_at <= cutoff)
    started_np = is_started.to_numpy()
    fate_kind[started_np] = FATE_STARTED
    elapsed_s[started_np] = wait_duration_s.to_numpy()[started_np]

    return fate_kind, elapsed_s


def build_bin_risk_and_labels(
    pending_at: pd.Series,
    resolved_at: pd.Series,
    wait_duration_s: pd.Series,
    cutoff: pd.Timestamp,
    edges_minutes: Sequence[float] = DEFAULT_BIN_EDGES_MINUTES,
) -> tuple[np.ndarray, np.ndarray]:
    """Build the (n_rows, n_bins) at-risk and label matrices for discrete-time
    hazard training, per bet2-hazard-survival-design.md's three-fate rule:

      started            -> at risk for bins [0, k]; label 0 for bins < k, 1 for bin k
      resolved_no_start   -> at risk for bins [0, k]; label 0 for all of them (no bin gets 1)
      censored            -> at risk for bins [0, k); label 0 for all of them;
                             bin k (the one it's currently inside) is excluded entirely

    where k is the bin index containing the row's fate-elapsed time.

    label is np.nan wherever at_risk is False -- callers must mask on
    at_risk, not rely on label alone, since 0.0 is a valid label value.
    """
    edges_s = bin_edges_seconds(edges_minutes)
    n_bins = len(edges_s) - 1
    fate_kind, elapsed_s = determine_fates(pending_at, resolved_at, wait_duration_s, cutoff)
    k = bin_index_containing(elapsed_s, edges_s)

    j = np.arange(n_bins)[None, :]  # (1, n_bins)
    k_col = k[:, None]              # (n, 1)

    at_risk_started = j <= k_col
    label_started = np.where(j < k_col, 0.0, np.where(j == k_col, 1.0, np.nan))

    at_risk_resolved_no_start = j <= k_col
    label_resolved_no_start = np.where(j <= k_col, 0.0, np.nan)

    at_risk_censored = j < k_col
    label_censored = np.where(j < k_col, 0.0, np.nan)

    is_started = (fate_kind == FATE_STARTED)[:, None]
    is_resolved_no_start = (fate_kind == FATE_RESOLVED_NO_START)[:, None]

    at_risk = np.where(
        is_started, at_risk_started,
        np.where(is_resolved_no_start, at_risk_resolved_no_start, at_risk_censored),
    )
    label = np.where(
        is_started, label_started,
        np.where(is_resolved_no_start, label_resolved_no_start, label_censored),
    )
    return at_risk.astype(bool), label.astype(np.float64)


def fit_exponential_tail_rate(
    pending_at: pd.Series,
    resolved_at: pd.Series,
    wait_duration_s: pd.Series,
    cutoff: pd.Timestamp,
    edges_minutes: Sequence[float] = DEFAULT_BIN_EDGES_MINUTES,
) -> float:
    """MLE rate for an exponential tail beyond the last finite bin edge,
    fit once (globally, not per-row) from the terminal bin's own at-risk
    rows: rate = total_events / total_person-time -- the standard
    closed-form MLE for an exponential distribution under right censoring.

    This rate is used by DiscreteHazardModel.predict_quantile to give a
    defined, auditable value to any quantile that falls past the last
    finite bin edge (bet2-hazard-survival-design.md's terminal-bin tail
    policy), rather than leaving it undefined or silently extrapolating.

    Returns 0.0 if there are no observed "started" events in the terminal
    bin's risk set (degenerate fit -- callers must treat a 0.0 (or
    otherwise non-positive) rate as "tail quantiles are undefined", not as
    "everyone resolves instantly").
    """
    edges_s = bin_edges_seconds(edges_minutes)
    t_last = edges_s[-2]  # last FINITE edge (edges_s[-1] is math.inf)

    fate_kind, elapsed_s = determine_fates(pending_at, resolved_at, wait_duration_s, cutoff)

    # The terminal bin's at-risk set is exactly the started/resolved_no_start
    # rows whose elapsed time reached the last finite edge -- censored rows
    # currently inside the terminal bin are excluded (see
    # build_bin_risk_and_labels: at_risk_censored never includes a row's own
    # containing bin), so they must not contribute person-time here either.
    in_terminal_risk_set = (elapsed_s >= t_last) & (fate_kind != FATE_CENSORED)
    if not in_terminal_risk_set.any():
        return 0.0

    extra_time = elapsed_s[in_terminal_risk_set] - t_last
    n_events = int((fate_kind[in_terminal_risk_set] == FATE_STARTED).sum())
    total_person_time = float(extra_time.sum())

    if n_events == 0 or total_person_time <= 0:
        return 0.0

    return n_events / total_person_time
