"""The quantile path's split must not train on labels that were not yet known at
the split's own cutoff -- the same per-split leakage guard hazard_labels.py has."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from src import config as cfg
from src.train import _split_by_pending_at


def _cfg(target="wait_time", target_column="wait_duration_s"):
    return cfg.Config(
        target=target, target_column=target_column,
        lookback_days=2, holdout_days=1, validation_days=1,
        as_of_date=datetime(2026, 8, 10, tzinfo=timezone.utc),
        filters=[], categorical_features=[], numeric_features=[],
        derived_features={}, model_type="lightgbm", quantiles=[0.5, 0.9],
        model_params={},
    )


def _frame(rows):
    return pd.DataFrame(rows)


def test_wait_rows_whose_start_is_after_train_end_leave_the_train_split():
    # windows: train [08-06, 08-08), val [08-08, 08-09), hold [08-09, 08-10)
    df = _frame([
        # pends in train, starts in train -> kept
        {"task_id": "a", "run_id": 0, "pending_at": "2026-08-07T12:00:00Z",
         "wait_duration_s": 600.0, "resolved_at": "2026-08-07T13:00:00Z"},
        # pends in train 30 min before train_end, waits 8h -> starts in val -> DROPPED
        {"task_id": "b", "run_id": 0, "pending_at": "2026-08-07T23:30:00Z",
         "wait_duration_s": 8 * 3600.0, "resolved_at": "2026-08-08T09:00:00Z"},
        # pends in val, starts in val -> kept in val
        {"task_id": "c", "run_id": 0, "pending_at": "2026-08-08T01:00:00Z",
         "wait_duration_s": 60.0, "resolved_at": "2026-08-08T02:00:00Z"},
        # pends in val, starts in hold -> DROPPED from val
        {"task_id": "d", "run_id": 0, "pending_at": "2026-08-08T23:00:00Z",
         "wait_duration_s": 2 * 3600.0, "resolved_at": "2026-08-09T02:00:00Z"},
        # pends in hold with a long wait -> hold is scored on actuals, KEPT
        {"task_id": "e", "run_id": 0, "pending_at": "2026-08-09T23:00:00Z",
         "wait_duration_s": 5 * 3600.0, "resolved_at": "2026-08-10T05:00:00Z"},
    ])
    train, val, hold = _split_by_pending_at(df, _cfg())
    assert train["task_id"].tolist() == ["a"]
    assert val["task_id"].tolist() == ["c"]
    assert hold["task_id"].tolist() == ["e"]


def test_run_duration_rows_are_censored_on_resolved_at():
    df = _frame([
        {"task_id": "a", "run_id": 0, "pending_at": "2026-08-07T12:00:00Z",
         "run_duration_s": 100.0, "resolved_at": "2026-08-07T13:00:00Z"},
        # resolves after train_end -> dropped from train
        {"task_id": "b", "run_id": 0, "pending_at": "2026-08-07T23:00:00Z",
         "run_duration_s": 100.0, "resolved_at": "2026-08-08T00:30:00Z"},
    ])
    train, _val, _hold = _split_by_pending_at(df, _cfg("run_duration", "run_duration_s"))
    assert train["task_id"].tolist() == ["a"]


def test_run_duration_null_label_with_a_resolved_at_is_not_treated_as_known():
    # resolved_at alone is not "known" for run_duration: a row that resolved
    # WITHOUT ever running (no run_duration_s) has a resolved_at timestamp
    # but no known duration. Its resolved_at falls inside train, so the old
    # (buggy) "resolved_at regardless of label" rule would have kept it.
    df = _frame([
        {"task_id": "a", "run_id": 0, "pending_at": "2026-08-07T12:00:00Z",
         "run_duration_s": None, "resolved_at": "2026-08-07T13:00:00Z"},
    ])
    train, _val, _hold = _split_by_pending_at(df, _cfg("run_duration", "run_duration_s"))
    assert train.empty


def test_a_null_label_is_not_treated_as_known():
    df = _frame([
        {"task_id": "a", "run_id": 0, "pending_at": "2026-08-07T12:00:00Z",
         "wait_duration_s": None, "resolved_at": None},
    ])
    train, _val, _hold = _split_by_pending_at(df, _cfg())
    assert train.empty


def test_wait_rows_are_censored_on_the_production_y_column():
    # Same rows as test_wait_rows_whose_start_is_after_train_end_leave_the_
    # train_split, but the target column is named "y" -- what every real
    # load path (data_loader / extract_source) actually produces -- instead
    # of the config's literal target_column. Same membership must result.
    df = _frame([
        {"task_id": "a", "run_id": 0, "pending_at": "2026-08-07T12:00:00Z",
         "y": 600.0, "resolved_at": "2026-08-07T13:00:00Z"},
        {"task_id": "b", "run_id": 0, "pending_at": "2026-08-07T23:30:00Z",
         "y": 8 * 3600.0, "resolved_at": "2026-08-08T09:00:00Z"},
        {"task_id": "c", "run_id": 0, "pending_at": "2026-08-08T01:00:00Z",
         "y": 60.0, "resolved_at": "2026-08-08T02:00:00Z"},
        {"task_id": "d", "run_id": 0, "pending_at": "2026-08-08T23:00:00Z",
         "y": 2 * 3600.0, "resolved_at": "2026-08-09T02:00:00Z"},
        {"task_id": "e", "run_id": 0, "pending_at": "2026-08-09T23:00:00Z",
         "y": 5 * 3600.0, "resolved_at": "2026-08-10T05:00:00Z"},
    ])
    train, val, hold = _split_by_pending_at(df, _cfg())
    assert train["task_id"].tolist() == ["a"]
    assert val["task_id"].tolist() == ["c"]
    assert hold["task_id"].tolist() == ["e"]


def test_run_duration_rows_are_censored_on_resolved_at_via_y_column():
    # Same rows as test_run_duration_rows_are_censored_on_resolved_at, but
    # named "y" instead of "run_duration_s": renaming the wait/duration
    # column must not matter for this branch since it censors on
    # resolved_at, not on the label column's name or value.
    df = _frame([
        {"task_id": "a", "run_id": 0, "pending_at": "2026-08-07T12:00:00Z",
         "y": 100.0, "resolved_at": "2026-08-07T13:00:00Z"},
        {"task_id": "b", "run_id": 0, "pending_at": "2026-08-07T23:00:00Z",
         "y": 100.0, "resolved_at": "2026-08-08T00:30:00Z"},
    ])
    train, _val, _hold = _split_by_pending_at(df, _cfg("run_duration", "run_duration_s"))
    assert train["task_id"].tolist() == ["a"]
